#include "motor_control.h"
#include "FreeRTOS.h"
#include "task.h"

/* 모터 동작 설정값 */
static const configSTACK_DEPTH_TYPE stackWords = 512U; // 태스크 스택 크기, 워드 단위
static const uint32_t lowDuty = 25U;                    // 기동·정지 구간의 PWM 듀티(%)
static const uint32_t runDuty = 70U;                    // 정상 구동 PWM 듀티(%)
static const uint32_t startHoldMs = 2000U;              // 기동 시 저듀티 유지 시간
static const uint32_t stopHoldMs = 2000U;               // 정상 정지 시 저듀티 유지 시간
static const uint32_t directionTimeoutMs = 200U;       // 마지막 방향 신호의 유효 시간
static const uint32_t permitTimeoutMs = 200U;          // 마지막 운전 허가의 유효 시간

typedef enum {
    MOTOR_OFF,        // 두 PWM 채널이 0%인 상태
    MOTOR_START_LOW,  // 기동 중 저듀티를 유지하는 상태
    MOTOR_RUN,        // 정상 구동 듀티를 유지하는 상태
    MOTOR_STOP_LOW    // 정상 정지 전에 저듀티를 유지하는 상태
} MotorPhase;

/* 입력 처리 태스크와 모터 태스크가 공유하는 변수 */
static MotorDirection requestedDirection; // 마지막으로 받은 유효한 앞·뒤 방향
static uint32_t lastDirectionMs;           // 마지막 유효 방향 신호를 받은 시각
static bool localSafe;                    // MCU 자체의 비상정지·고장 등 운전 조건이 정상인지
static bool permitAllowed;                // 마지막 CAN 운전 허가가 true인지
static uint32_t lastPermitMs;              // 마지막 true 허가를 받은 시각
static bool abortRequested;               // PWM을 0%로 내릴 때까지 기억하는 차단 요청
static bool shaftStopped;                 // 엔코더로 확인한 실제 축 정지 여부

/* 공유 변수를 읽으므로 임계 구역 안에서 호출 */
static bool canDrive(uint32_t now)
{
    return localSafe && permitAllowed && !abortRequested &&
           now - lastPermitMs < permitTimeoutMs;
}

/* CAN 처리 태스크가 검증된 운전 허가를 받은 뒤 호출 */
void MotorControl_OnPermit(bool allowed)
{
    taskENTER_CRITICAL();

    const uint32_t now = HAL_GetTick(); // 이번 허가를 처리하는 시각

    /* 허가 철회 또는 이미 만료된 허가가 뒤늦게 갱신된 경우 */
    if (!allowed || (permitAllowed && now - lastPermitMs >= permitTimeoutMs))
    {
        abortRequested = true;
    }

    permitAllowed = allowed;
    if (allowed)
    {
        lastPermitMs = now;
    }

    taskEXIT_CRITICAL();
}

/* CAN 처리 태스크가 유효한 앞·뒤 신호를 받을 때마다 호출 */
void MotorControl_OnDirection(MotorDirection direction)
{
    taskENTER_CRITICAL();

    const uint32_t now = HAL_GetTick(); // 이번 방향 신호를 처리하는 시각

    if (canDrive(now) &&
        (direction == MOTOR_DIR_FORWARD || direction == MOTOR_DIR_REVERSE))
    {
        requestedDirection = direction;
        lastDirectionMs = now;
    }

    taskEXIT_CRITICAL();
}

/* 보드 운전 조건과 엔코더 정지 판정을 갱신 */
void MotorControl_UpdateLocalState(bool safeToDrive, bool shaftIsStopped)
{
    taskENTER_CRITICAL();

    localSafe = safeToDrive;
    shaftStopped = shaftIsStopped;
    if (!safeToDrive)
    {
        abortRequested = true;
    }

    taskEXIT_CRITICAL();
}

/* 전진은 CH1, 후진은 CH2에 비교값을 적용하고 나머지 채널은 0%로 설정 */
static void setPwm(TIM_HandleTypeDef *timer, MotorDirection direction,
                   uint32_t compare)
{
    __HAL_TIM_SET_COMPARE(timer, TIM_CHANNEL_1,
                         direction == MOTOR_DIR_FORWARD ? compare : 0U);
    __HAL_TIM_SET_COMPARE(timer, TIM_CHANNEL_2,
                         direction == MOTOR_DIR_REVERSE ? compare : 0U);
}

static void motorTask(void *argument)
{
    TIM_HandleTypeDef *timer = argument; // main.c에서 전달한 TIM3 핸들

    const uint32_t counts = __HAL_TIM_GET_AUTORELOAD(timer) + 1U; // PWM 한 주기의 카운트 수
    const uint32_t lowCompare = counts * lowDuty / 100U;          // 저듀티에 해당하는 비교값
    const uint32_t runCompare = counts * runDuty / 100U;          // 정상 구동 듀티에 해당하는 비교값

    MotorPhase phase = MOTOR_OFF;                     // 현재 구동 단계
    MotorDirection activeDirection = MOTOR_DIR_NONE; // 현재 PWM을 출력하는 방향
    uint32_t phaseStartedMs = 0U;                      // 현재 저듀티 단계에 들어온 시각

    setPwm(timer, MOTOR_DIR_NONE, 0U);
    if (HAL_TIM_PWM_Start(timer, TIM_CHANNEL_1) != HAL_OK ||
        HAL_TIM_PWM_Start(timer, TIM_CHANNEL_2) != HAL_OK)
    {
        Error_Handler();
    }

    for (;;)
    {
        taskENTER_CRITICAL();

        const uint32_t now = HAL_GetTick(); // 이번 태스크 반복의 기준 시각

        if (now - lastDirectionMs >= directionTimeoutMs)
        {
            requestedDirection = MOTOR_DIR_NONE;
        }

        const bool hasDirection = requestedDirection != MOTOR_DIR_NONE; // 유효한 방향이 남아 있는지
        const bool directionChanged = phase != MOTOR_OFF && hasDirection &&
                                      requestedDirection != activeDirection; // 구동 중 반대 방향 요청 여부
        const bool stopFinished = phase == MOTOR_STOP_LOW &&
                                  now - phaseStartedMs >= stopHoldMs; // 정지 전 저듀티 시간이 끝났는지

        if (!canDrive(now) || directionChanged || stopFinished)
        {
            setPwm(timer, MOTOR_DIR_NONE, 0U);
            if (phase != MOTOR_OFF)
            {
                shaftStopped = false; // 다음 기동 전 엔코더로 정지를 다시 확인
            }
            phase = MOTOR_OFF;
            activeDirection = MOTOR_DIR_NONE;
            requestedDirection = MOTOR_DIR_NONE;
            abortRequested = false; // PWM을 0%로 내린 뒤 차단 요청을 해제
        }
        else if (phase == MOTOR_OFF && hasDirection && shaftStopped)
        {
            activeDirection = requestedDirection;
            phase = MOTOR_START_LOW;
            phaseStartedMs = now;
            shaftStopped = false;
            setPwm(timer, activeDirection, lowCompare);
        }
        else if ((phase == MOTOR_START_LOW || phase == MOTOR_RUN) &&
                 !hasDirection)
        {
            phase = MOTOR_STOP_LOW;
            phaseStartedMs = now;
            setPwm(timer, activeDirection, lowCompare);
        }
        else if (phase == MOTOR_START_LOW &&
                 now - phaseStartedMs >= startHoldMs)
        {
            phase = MOTOR_RUN;
            setPwm(timer, activeDirection, runCompare);
        }

        taskEXIT_CRITICAL();
        vTaskDelay(pdMS_TO_TICKS(5U)); // 입력과 상태를 5ms 간격으로 다시 확인
    }
}

void MotorControl_Init(TIM_HandleTypeDef *timer)
{
    if (xTaskCreate(motorTask, "motor", stackWords, timer, 1U, NULL) != pdPASS)
    {
        Error_Handler();
    }
}