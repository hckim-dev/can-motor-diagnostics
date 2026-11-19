"""CAN 2.1에서 Jetson이 보내는 세 프레임을 인코딩한다."""

from enum import IntEnum

from ..can_rx.codec import (
    COMMAND_LAYOUT,
    VERSION,
    WORD_BYTES,
    WORD_SHORT_BYTES,
    CanId,
    Frame,
    validate_epoch,
)
from ..can_rx.state import CONTROLS_READY, STOP_REQUESTED

MAX_CMD_SEQ = 255
MAX_BOOT_ID = 0xFFFFFFFF
MAX_TARGET_RPM = 32_767


class Opcode(IntEnum):
    START_REAL = 1
    STOP = 2
    RESET = 3
    ARM_TEST = 4


def valid_argument(opcode, argument):
    return type(argument) is int and (
        1 <= argument <= MAX_TARGET_RPM
        if opcode == Opcode.START_REAL
        else argument == 0
    )


def encode_operator_hello(boot_id):
    if not 1 <= boot_id <= MAX_BOOT_ID:
        raise ValueError("조작 노드 부팅 ID 범위 오류")
    return Frame(CanId.OP_HELLO, WORD_BYTES.pack(boot_id, *VERSION, 0, 0))


def encode_permit(epoch, alive_seq, *, ready=False, inhibit=False):
    validate_epoch(epoch)
    if ready and inhibit:
        raise ValueError("운전 허가와 출력 억제를 동시에 보낼 수 없다")
    flags = CONTROLS_READY if ready else STOP_REQUESTED if inhibit else 0
    return Frame(CanId.OP_PERMIT, WORD_SHORT_BYTES.pack(epoch, alive_seq, flags, 0))


def encode_command(epoch, cmd_seq, opcode, argument):
    validate_epoch(epoch)
    opcode = Opcode(opcode)
    if not 1 <= cmd_seq <= MAX_CMD_SEQ or not valid_argument(opcode, argument):
        raise ValueError("명령 순번 또는 인자 범위 오류")
    return Frame(CanId.COMMAND, COMMAND_LAYOUT.pack(epoch, cmd_seq, opcode, argument))
