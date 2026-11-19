"""CAN 2.1 수신 프레임의 바이트 배치와 값 범위를 검증한다."""

from dataclasses import dataclass
from enum import IntEnum
from struct import Struct

VERSION = (2, 1)
DLC = 8
SIGNED_MISSING = -32768
UNSIGNED_MISSING = 65535
OP_LINK_VALID = 1 << 0
SUPPLY_PRESENT = 1 << 1
SUPPLY_VALID = 1 << 2
DIAG_FAULT_OUTPUT = 1 << 3
TEST_READY_OUTPUT = 1 << 4
ENCODER_CALIBRATED = 1 << 5
OPERATING_CONFIG_VALID = 1 << 6


class CanId(IntEnum):
    OP_PERMIT = 0x080
    MOTOR_FAULT = 0x090
    COMMAND = 0x100
    COMMAND_RESULT = 0x110
    MOTOR_STATE = 0x200
    SPEED = 0x210
    ELECTRIC = 0x211
    SAMPLE_TIME = 0x212
    MOTOR_HELLO = 0x700
    OP_HELLO = 0x701


SAMPLE_IDS = (CanId.SPEED, CanId.ELECTRIC, CanId.SAMPLE_TIME)
HELLO_RESERVED_MASKS = {VERSION: 0xE0, (2, 0): 0xF0}


class Mode(IntEnum):
    UNKNOWN = 0
    REAL = 1
    TEST = 2
    TRANSITION = 3


class State(IntEnum):
    BOOT = 0
    STOPPED = 1
    RUNNING = 2
    TEST_ARMED = 3
    TEST_ACTIVE = 4
    FAULT = 5


STOP_STATES = (State.STOPPED, State.FAULT)


@dataclass(frozen=True)
class Frame:
    can_id: int
    data: bytes
    dlc: int = DLC
    extended: bool = False
    rtr: bool = False
    error: bool = False


@dataclass(frozen=True)
class Hello:
    epoch: int
    version: tuple[int, int]
    capabilities: int

    @property
    def boot(self):
        return self.epoch >> 16


@dataclass(frozen=True)
class OperatorHello:
    boot_id: int
    version: tuple[int, int]


@dataclass(frozen=True)
class MotorState:
    epoch: int
    state: State
    mode: Mode
    flags: int
    last_cmd_seq: int


@dataclass(frozen=True)
class Fault:
    epoch: int
    mask: int
    event_seq: int
    mode: Mode


@dataclass(frozen=True)
class Permit:
    epoch: int
    alive_seq: int
    flags: int


@dataclass(frozen=True)
class Command:
    epoch: int
    cmd_seq: int
    opcode: int
    argument: int


@dataclass(frozen=True)
class CommandResult:
    request_epoch: int
    cmd_seq: int
    result: int
    detail: int
    resulting_state: State


@dataclass(frozen=True)
class Speed:
    seq: int
    actual_rpm: int | None
    target_rpm: int | None
    pwm_permille: int | None


@dataclass(frozen=True)
class Electric:
    seq: int
    current_ma: int | None
    bus_mv: int | None
    mode: Mode
    state: State
    sensor_age_ms: int | None


@dataclass(frozen=True)
class SampleTime:
    seq: int
    tick_ms: int
    boot: int


class InvalidFrame(ValueError):
    def __init__(self, reason, sample_seq=None):
        super().__init__(reason)
        self.sample_seq = sample_seq


WORD_BYTES = Struct("<IBBBB")
WORD_SHORT_BYTES = Struct("<IHBB")
COMMAND_LAYOUT = Struct("<IBBH")
SPEED_LAYOUT = Struct("<HhHH")
ELECTRIC_LAYOUT = Struct("<HhHBB")
TIME_LAYOUT = Struct("<HIH")


def optional(value, missing):
    return None if value == missing else value


def require(condition, reason):
    if not condition:
        raise InvalidFrame(reason)


def validate_epoch(epoch):
    require(epoch >> 16 != 0 and epoch & 0xFFFF != 0, "유효하지 않은 epoch")


def decode_frame(frame: Frame):
    if frame.extended or frame.rtr or frame.error or not 0 <= frame.can_id <= 0x7FF:
        raise InvalidFrame("지원하지 않는 CAN 프레임 형식")

    try:
        kind = CanId(frame.can_id)
    except ValueError:
        return None

    require(frame.dlc == len(frame.data) == DLC, "DLC 또는 payload 길이 오류")
    data = frame.data
    if kind in SAMPLE_IDS:
        try:
            return decode_sample(kind, data)
        except ValueError as exc:
            raise InvalidFrame(str(exc), int.from_bytes(data[:2], "little")) from exc

    try:
        return decode_control(kind, data)
    except ValueError as exc:
        raise InvalidFrame(str(exc)) from exc


def decode_sample(kind, data):
    if kind == CanId.SPEED:
        seq, rpm, target, pwm = SPEED_LAYOUT.unpack(data)
        require(pwm == UNSIGNED_MISSING or pwm <= 1000, "PWM 범위 오류")
        return Speed(
            seq,
            optional(rpm, SIGNED_MISSING),
            optional(target, UNSIGNED_MISSING),
            optional(pwm, UNSIGNED_MISSING),
        )

    if kind == CanId.SAMPLE_TIME:
        seq, tick, boot = TIME_LAYOUT.unpack(data)
        require(boot != 0, "부팅 식별자 0")
        return SampleTime(seq, tick, boot)

    seq, current, voltage, flags, age = ELECTRIC_LAYOUT.unpack(data)
    require(flags & 0x80 == 0, "ELECTRIC 예약 비트")
    current = optional(current, SIGNED_MISSING)
    voltage = optional(voltage, UNSIGNED_MISSING)
    require(
        bool(flags & 1) == (current is not None), "전류 유효 비트와 결측 표식 불일치"
    )
    require(
        bool(flags & 2) == (voltage is not None), "전압 유효 비트와 결측 표식 불일치"
    )
    require(age != 255 or flags & 3 == 0, "유효한 센서값의 측정 시각 누락")

    return Electric(
        seq,
        current,
        voltage,
        Mode((flags >> 2) & 3),
        State((flags >> 4) & 7),
        age * 10 if age != 255 else None,
    )


def decode_control(kind, data):
    if kind in (CanId.MOTOR_HELLO, CanId.OP_HELLO):
        identifier, major, minor, flags, reserved = WORD_BYTES.unpack(data)
        require(reserved == 0, "HELLO 예약 바이트")
        if kind == CanId.OP_HELLO:
            require(identifier != 0 and flags == 0, "OP_HELLO 식별자 또는 예약 비트")
            return OperatorHello(identifier, (major, minor))
        validate_epoch(identifier)
        reserved_mask = HELLO_RESERVED_MASKS.get((major, minor), 0)
        require(flags & reserved_mask == 0, "MOTOR_HELLO 예약 비트")
        return Hello(identifier, (major, minor), flags)

    if kind == CanId.MOTOR_FAULT:
        epoch, fault_mask, event_seq, mode = WORD_SHORT_BYTES.unpack(data)
        validate_epoch(epoch)
        require(fault_mask & ~0x1FF == 0, "FAULT 예약 비트")
        return Fault(epoch, fault_mask, event_seq, Mode(mode))

    if kind == CanId.OP_PERMIT:
        epoch, alive_seq, flags, reserved = WORD_SHORT_BYTES.unpack(data)
        validate_epoch(epoch)
        require(reserved == 0 and flags & ~0x05 == 0, "PERMIT 예약 비트")
        return Permit(epoch, alive_seq, flags)

    if kind == CanId.COMMAND:
        epoch, seq, opcode, argument = COMMAND_LAYOUT.unpack(data)
        validate_epoch(epoch)
        require(seq != 0 and 1 <= opcode <= 4, "COMMAND 순번 또는 opcode")
        require(
            1 <= argument <= 32767 if opcode == 1 else argument == 0,
            "COMMAND 인자 범위",
        )
        return Command(epoch, seq, opcode, argument)

    if kind == CanId.COMMAND_RESULT:
        epoch, cmd_seq, result, detail, resulting_state = WORD_BYTES.unpack(data)
        validate_epoch(epoch)
        require(
            cmd_seq != 0 and result <= 3 and detail <= 8,
            "COMMAND_RESULT 열거값 또는 순번",
        )
        return CommandResult(epoch, cmd_seq, result, detail, State(resulting_state))

    epoch, state, mode, flags, last_cmd_seq = WORD_BYTES.unpack(data)
    validate_epoch(epoch)
    return MotorState(epoch, State(state), Mode(mode), flags, last_cmd_seq)
