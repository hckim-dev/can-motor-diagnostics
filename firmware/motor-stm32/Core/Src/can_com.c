#include "can_com.h"
#include <string.h>

CAN_HandleTypeDef hcan1;

CanLoopbackResult CAN_Com_RunLoopback(CAN_HandleTypeDef *can)
{
	CAN_FilterTypeDef filter = {0};
	filter.FilterBank = 0U;
	filter.FilterMode = CAN_FILTERMODE_IDMASK;
	filter.FilterScale = CAN_FILTERSCALE_32BIT;
	filter.FilterFIFOAssignment = CAN_FILTER_FIFO0;
	filter.FilterActivation = ENABLE;

	    if (HAL_CAN_ConfigFilter(can, &filter) != HAL_OK)
	    {
	        return CAN_TEST_FILTER_ERROR;
	    }

	    if (HAL_CAN_Start(can) != HAL_OK)
	    {
	        return CAN_TEST_START_ERROR;
	    }

	    CAN_TxHeaderTypeDef tx = {0};
	    tx.StdId = 0x123U;
	    tx.IDE = CAN_ID_STD;
	    tx.RTR = CAN_RTR_DATA;
	    tx.DLC = 8U;

	    uint8_t txData[8] = {0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08};
	    uint32_t mailbox;

	    if (HAL_CAN_AddTxMessage(can, &tx, txData, &mailbox) != HAL_OK)
	    {
	        return CAN_TEST_TX_ERROR;
	    }

	    uint32_t startedAt = HAL_GetTick();
	    while (HAL_CAN_GetRxFifoFillLevel(can, CAN_RX_FIFO0) == 0U)
	    {
	        if ((HAL_GetTick() - startedAt) >= 100U)
	        {
	            return CAN_TEST_TIMEOUT;
	        }
	    }

	    CAN_RxHeaderTypeDef rx = {0};
	    uint8_t rxData[8] = {0};

	    if (HAL_CAN_GetRxMessage(can, CAN_RX_FIFO0, &rx, rxData) != HAL_OK)
	    {
	        return CAN_TEST_RX_ERROR;
	    }

	    if (rx.IDE != tx.IDE || rx.RTR != tx.RTR ||
	        rx.StdId != tx.StdId || rx.DLC != tx.DLC ||
	        memcmp(rxData, txData, sizeof(txData)) != 0)
	    {
	        return CAN_TEST_MISMATCH;
	    }

	    return CAN_TEST_PASS;
	}
