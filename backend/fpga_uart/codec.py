"""FPGA UART 고정 프레임의 배치와 필드를 검증한다."""

from binascii import crc_hqx
from dataclasses import dataclass
from enum import Enum, IntEnum
from struct import Struct

SYNC = b"\xa5\x5a"
VERSION = 1
BODY = Struct("<BBHHBBIIH")
CRC_SIZE = 2
FRAME_SIZE = len(SYNC) + BODY.size + CRC_SIZE
CRC_INITIAL = 0xFFFF
ALLOWED_FLAGS = 0x0F
TIMING_VALID = 1 << 2
OVERFLOW_INVALID = 1 << 3


class EventType(IntEnum):
    STATUS = 1
    BASELINE_START = 2
    INJECT = 3
    DIAG_EDGE = 4
    END = 5
    ABORT = 6


class Phase(IntEnum):
    IDLE = 0
    BASELINE = 1
    OBSERVE = 2
    COMPLETED = 3


class FrameError(str, Enum):
    FRAMING = "FRAMING"
    CRC = "CRC"
    VERSION = "VERSION"
    FIELDS = "FIELDS"


class InvalidFrame(ValueError):
    def __init__(self, reason: FrameError):
        super().__init__(reason.value)
        self.reason = reason


@dataclass(frozen=True)
class Frame:
    kind: EventType
    tx_seq: int
    run_seq: int
    scenario: int
    phase: Phase
    tick_us: int
    value: int
    flags: int


def crc16(data: bytes) -> int:
    return crc_hqx(data, CRC_INITIAL)


def decode_frame(raw: bytes) -> Frame:
    if len(raw) != FRAME_SIZE or not raw.startswith(SYNC):
        raise InvalidFrame(FrameError.FRAMING)
    body = raw[len(SYNC) : -CRC_SIZE]
    if crc16(body) != int.from_bytes(raw[-CRC_SIZE:], "little"):
        raise InvalidFrame(FrameError.CRC)

    version, kind, tx_seq, run_seq, scenario, phase, tick, value, flags = BODY.unpack(
        body
    )
    if version != VERSION:
        raise InvalidFrame(FrameError.VERSION)
    try:
        event = EventType(kind)
        stage = Phase(phase)
    except ValueError as exc:
        raise InvalidFrame(FrameError.FIELDS) from exc

    if scenario > 3 or flags & ~ALLOWED_FLAGS:
        raise InvalidFrame(FrameError.FIELDS)
    if event != EventType.STATUS and (not run_seq or not scenario):
        raise InvalidFrame(FrameError.FIELDS)
    if event != EventType.DIAG_EDGE and value:
        raise InvalidFrame(FrameError.FIELDS)
    return Frame(event, tx_seq, run_seq, scenario, stage, tick, value, flags)
