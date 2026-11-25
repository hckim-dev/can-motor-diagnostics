#include "motor_control.h"
#include "FreeRTOS.h"
#include "task.h"

static const configSTACK_DEPTH_TYPE stackWords = 512U;
static const uint32_t lowDuty = 25U;
static const uint32_t runDuty = 70U;
static const uint32_t startHoldMs = 2000U;
static const uint32_t stopHoldMs = 2000U;
static const uint32_t directionTimeoutMs = 200U;
static const uint32_t permitTimeoutMs = 200U;

typedef enum {
    MOTOR_OFF,
    MOTOR_START_LOW,
    MOTOR_RUN,
    MOTOR_STOP_LOW
} MotorPhase;

static MotorDirection requestedDirection;
static uint32_t lastDirectionMs;
static bool localSafe;
static bool permitAllowed;
static uint32_t lastPermitMs;
static bool abortRequested;
static bool shaftStopped;

static bool canDrive(uint32_t now)
{
    return localSafe && permitAllowed && !abortRequested &&
           now - lastPermitMs < permitTimeoutMs;
}

void MotorControl_OnPermit(bool allowed)
{
    taskENTER_CRITICAL();

    const uint32_t now = HAL_GetTick();

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

void MotorControl_OnDirection(MotorDirection direction)
{
    taskENTER_CRITICAL();

    const uint32_t now = HAL_GetTick();

    if (canDrive(now) &&
        (direction == MOTOR_DIR_FORWARD || direction == MOTOR_DIR_REVERSE))
    {
        requestedDirection = direction;
        lastDirectionMs = now;
    }

    taskEXIT_CRITICAL();
}

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
    TIM_HandleTypeDef *timer = argument;

    const uint32_t counts = __HAL_TIM_GET_AUTORELOAD(timer) + 1U;
    const uint32_t lowCompare = counts * lowDuty / 100U;
    const uint32_t runCompare = counts * runDuty / 100U;

    MotorPhase phase = MOTOR_OFF;
    MotorDirection activeDirection = MOTOR_DIR_NONE;
    uint32_t phaseStartedMs = 0U;

    setPwm(timer, MOTOR_DIR_NONE, 0U);
    if (HAL_TIM_PWM_Start(timer, TIM_CHANNEL_1) != HAL_OK ||
        HAL_TIM_PWM_Start(timer, TIM_CHANNEL_2) != HAL_OK)
    {
        Error_Handler();
    }

    for (;;)
    {
        taskENTER_CRITICAL();

        const uint32_t now = HAL_GetTick();

        if (now - lastDirectionMs >= directionTimeoutMs)
        {
            requestedDirection = MOTOR_DIR_NONE;
        }

        const bool hasDirection = requestedDirection != MOTOR_DIR_NONE;
        const bool directionChanged = phase != MOTOR_OFF && hasDirection &&
                                      requestedDirection != activeDirection;
        const bool stopFinished = phase == MOTOR_STOP_LOW &&
                                  now - phaseStartedMs >= stopHoldMs;

        if (!canDrive(now) || directionChanged || stopFinished)
        {
            setPwm(timer, MOTOR_DIR_NONE, 0U);
            if (phase != MOTOR_OFF)
            {
                shaftStopped = false;
            }

            phase = MOTOR_OFF;
            activeDirection = MOTOR_DIR_NONE;
            requestedDirection = MOTOR_DIR_NONE;
            abortRequested = false;
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
        vTaskDelay(pdMS_TO_TICKS(5U));
    }
}

void main_MotorControl(TIM_HandleTypeDef *timer)
{
    if (xTaskCreate(motorTask, "motor", stackWords, timer, 1U, NULL) != pdPASS)
    {
        Error_Handler();
    }
}
