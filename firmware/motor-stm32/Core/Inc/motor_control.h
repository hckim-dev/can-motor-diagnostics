#ifndef MOTOR_CONTROL_H
#define MOTOR_CONTROL_H

#include <stdbool.h>
#include "main.h"

typedef enum {
    MOTOR_DIR_NONE = 0,  // 처리할 방향 요청이 없음
    MOTOR_DIR_FORWARD,   // 전진 요청
    MOTOR_DIR_REVERSE    // 후진 요청
} MotorDirection;

void MotorControl_Init(TIM_HandleTypeDef *timer);
void MotorControl_OnPermit(bool allowed);
void MotorControl_OnDirection(MotorDirection direction);
void MotorControl_UpdateLocalState(bool safeToDrive, bool shaftIsStopped);

#endif