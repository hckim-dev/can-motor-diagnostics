#ifndef MOTOR_CONTROL_H
#define MOTOR_CONTROL_H

#include <stdbool.h> // bool inlcude
#include "main.h"

typedef enum {
	MOTOR_DIR_NONE = 0,
	MOTOR_DIR_FORWARD,
	MOTOR_DIR_REVERSE
}MotorCommand;

void MotorControl_OnPermint(bool allowed);
void MotorControl_OnDirection(MotorDirection direction);
void MotorControl_UpdateLocalState(bool safeToDrive, bool IsStopped);
void main_MotorControl(TIM_HandleTypeDef *timer);

#endif
