#ifndef CAN_COM_H
#define CAN_COM_H

#include <stm32f1xx_hal.h>

// 호출할 함수 선언과 디버거 결과 이름 정의
typedef enum {
	CAN_TEST_NOT_RUN = 0,
	CAN_TEST_PASS,
	CAN_TEST_FILTER_ERROR,
	CAN_TEST_START_ERROR,
	CAN_TEST_TX_ERROR,
	CAN_TEST_TIMEOUT,
	CAN_TEST_RX_ERROR,
	CAN_TEST_MISMATCH
} CanLoopbackResult;

CanLoopbackResult CAN_Com_RunLoopback(CAN_HandleTypeDef *can);

#endif
