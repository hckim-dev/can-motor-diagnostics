"""같은 부팅에서 수신한 세 측정 프레임을 순번으로 결합한다."""

from dataclasses import dataclass, field
from uuid import uuid4

from .codec import SAMPLE_IDS, CanId, Electric, Mode, SampleTime, Speed

NS_PER_MS = 1_000_000
JOIN_WINDOW = 100 * NS_PER_MS
SAMPLE_LIFETIME = 300 * NS_PER_MS
SENSOR_LIFETIME = 200 * NS_PER_MS
SEQUENCE_MASK = 0xFFFF
SEQUENCE_HALF = 0x8000
SAMPLE_PERIOD = 50 * NS_PER_MS
SEQUENCE_GAP = SAMPLE_PERIOD * SEQUENCE_HALF
TICK_MASK = 0xFFFFFFFF
MAX_PENDING = 16


def forward_delta(new, old, mask=SEQUENCE_MASK):
    return (new - old) & mask


def empty_sample(quality="unavailable"):
    return {
        "seq": None,
        "tick_ms": None,
        "actual_rpm": None,
        "target_rpm": None,
        "pwm_permille": None,
        "current_ma": None,
        "bus_mv": None,
        "sensor_age_ms": None,
        "sample_complete": False,
        "quality": quality,
        "sample_mode": "UNKNOWN",
        "sample_state": "UNKNOWN",
        "synthetic": False,
    }


@dataclass(frozen=True)
class SamplePart:
    message: Speed | Electric | SampleTime
    received_ns: int
    record_id: str


@dataclass
class PendingSample:
    started_ns: int
    parts: dict[CanId, SamplePart] = field(default_factory=dict)
    invalid_ids: dict[CanId, str] = field(default_factory=dict)
    seq64: int | None = None


@dataclass(frozen=True)
class Sample:
    boot: int
    acquisition_id: str
    seq64: int
    timing: SamplePart
    speed: SamplePart | None
    electric: SamplePart | None
    invalid_parts: tuple[tuple[CanId, str], ...]

    @property
    def evidence_ids(self):
        parts = (self.speed, self.electric, self.timing)
        return tuple(part.record_id for part in parts if part is not None) + tuple(
            record_id for _, record_id in self.invalid_parts
        )

    @property
    def complete(self):
        return self.speed is not None and self.electric is not None

    def sensor_age_ns(self, now_ns):
        if self.electric is None or self.electric.message.sensor_age_ms is None:
            return None
        return (
            self.electric.message.sensor_age_ms * NS_PER_MS
            + now_ns
            - self.electric.received_ns
        )

    def snapshot(self, now_ns):
        timing = self.timing.message
        speed = self.speed.message if self.speed else None
        electric = self.electric.message if self.electric else None

        speed_recent = (
            self.speed is not None and now_ns - self.speed.received_ns < SAMPLE_LIFETIME
        )
        electric_recent = (
            self.electric is not None
            and now_ns - self.electric.received_ns < SAMPLE_LIFETIME
        )

        age_ns = self.sensor_age_ns(now_ns)
        sensor_age = age_ns // NS_PER_MS if age_ns is not None else None
        sensor_recent = age_ns is not None and age_ns <= SENSOR_LIFETIME

        if self.invalid_parts:
            quality = "invalid"
        elif not self.complete or None in (
            speed.actual_rpm,
            speed.target_rpm,
            speed.pwm_permille,
            electric.current_ma,
            electric.bus_mv,
        ):
            quality = "unavailable"
        elif not (speed_recent and electric_recent and sensor_recent):
            quality = "stale"
        else:
            quality = "valid"

        return {
            "seq": timing.seq,
            "tick_ms": timing.tick_ms,
            "actual_rpm": speed.actual_rpm if speed_recent else None,
            "target_rpm": speed.target_rpm if speed_recent else None,
            "pwm_permille": speed.pwm_permille if speed_recent else None,
            "current_ma": electric.current_ma if sensor_recent else None,
            "bus_mv": electric.bus_mv if sensor_recent else None,
            "sensor_age_ms": sensor_age,
            "sample_complete": self.complete,
            "quality": quality,
            "sample_mode": electric.mode.name if electric_recent else "UNKNOWN",
            "sample_state": electric.state.name if electric_recent else "UNKNOWN",
            "synthetic": electric_recent and electric.mode == Mode.TEST,
        }


class SampleAssembler:
    def __init__(self, *, on_sample=None, new_acquisition_id=uuid4):
        self.on_sample = on_sample
        self.new_acquisition_id = new_acquisition_id
        self.pending: dict[int, PendingSample] = {}
        self.sequence_gaps = 0
        self.reset()

    def reset(self):
        if self.on_sample is not None:
            self.flush()
        self.pending.clear()
        self.acquisition_id = str(self.new_acquisition_id())
        self.last_time: SamplePart | None = None
        self.seq64: int | None = None
        self.latest: Sample | None = None

    def advance(self, now_ns):
        for seq, pending in tuple(self.pending.items()):
            if now_ns - pending.started_ns >= JOIN_WINDOW:
                self.finish(seq)
        if self.last_time and now_ns - self.last_time.received_ns >= SEQUENCE_GAP:
            self.reset()

    def flush(self):
        for seq in tuple(self.pending):
            self.finish(seq)

    def discard_unanchored(self, seq):
        pending = self.pending.get(seq)
        if pending is not None and CanId.SAMPLE_TIME not in pending.parts:
            del self.pending[seq]

    def add(self, kind, seq, rx_ns, record_id, message=None):
        pending = self.pending.get(seq)
        if pending is None and self.last_time:
            delta = forward_delta(seq, self.last_time.message.seq)
            if delta == SEQUENCE_HALF and isinstance(message, SampleTime):
                self.reset()
            elif not 1 <= delta < SEQUENCE_HALF:
                return

        if pending is None:
            if len(self.pending) >= MAX_PENDING:
                return
            pending = self.pending[seq] = PendingSample(rx_ns)
        if kind in pending.parts:
            return
        if isinstance(message, SampleTime) and not self.anchor(
            pending, message, rx_ns, record_id
        ):
            return

        if message is None:
            pending.invalid_ids.setdefault(kind, record_id)
        else:
            pending.invalid_ids.pop(kind, None)
            pending.parts[kind] = SamplePart(message, rx_ns, record_id)

        if len(pending.parts) == len(SAMPLE_IDS):
            self.finish(seq)

    def anchor(self, pending, message, rx_ns, record_id):
        delta = 0
        if self.last_time:
            last = self.last_time.message
            delta = forward_delta(message.seq, last.seq)
            tick_delta = forward_delta(message.tick_ms, last.tick_ms, TICK_MASK)
            if not 1 <= delta < SEQUENCE_HALF or tick_delta > TICK_MASK // 2:
                return False
            self.sequence_gaps += delta - 1

        self.seq64 = message.seq if self.seq64 is None else self.seq64 + delta
        pending.seq64 = self.seq64
        self.last_time = SamplePart(message, rx_ns, record_id)
        return True

    def finish(self, seq):
        pending = self.pending.pop(seq)
        timing = pending.parts.get(CanId.SAMPLE_TIME)
        # 시간 프레임이 없으면 측정값을 현재 부팅에 귀속할 수 없다.
        if timing is None:
            return

        sample = Sample(
            timing.message.boot,
            self.acquisition_id,
            pending.seq64,
            timing,
            pending.parts.get(CanId.SPEED),
            pending.parts.get(CanId.ELECTRIC),
            tuple(pending.invalid_ids.items()),
        )
        if self.latest is None or sample.seq64 > self.latest.seq64:
            self.latest = sample
        if self.on_sample is not None:
            self.on_sample(sample)
