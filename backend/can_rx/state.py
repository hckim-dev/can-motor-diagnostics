"""CAN 수신 근거의 세션과 필드별 유효 기간을 관리한다."""

from collections import Counter
from dataclasses import dataclass
from uuid import uuid4

from .codec import (
    SAMPLE_IDS,
    VERSION,
    CanId,
    Fault,
    Frame,
    Hello,
    InvalidFrame,
    MotorState,
    OperatorHello,
    Permit,
    SampleTime,
    decode_frame,
)
from .samples import (
    NS_PER_MS,
    SEQUENCE_HALF,
    SampleAssembler,
    empty_sample,
    forward_delta,
)

HELLO_LIFETIME = 1500 * NS_PER_MS
STATE_LIFETIME = 300 * NS_PER_MS
FAULT_LIFETIME = 1000 * NS_PER_MS
FAULT_SEQ_MASK = 0xFF
FAULT_SEQ_HALF = (FAULT_SEQ_MASK + 1) // 2
PERMIT_LIFETIME = 200 * NS_PER_MS
BOOT_QUARANTINE = 300 * NS_PER_MS
NC2_CAPABILITY = 1 << 4
NC2_OPEN = 1 << 7
CONTROLS_READY = 1 << 0
STOP_REQUESTED = 1 << 2


@dataclass(frozen=True)
class Observation:
    record_id: str
    ingest_seq: int
    rx_mono_ns: int
    frame: Frame
    message: object = None
    error: str | None = None


def fresh(received, now_ns, lifetime):
    return received is not None and now_ns - received.rx_mono_ns < lifetime


class CanReceiver:
    def __init__(self, backend_session=None):
        self.backend_session = backend_session or str(uuid4())
        self.ingest_seq = 0
        self.errors = Counter()
        self.samples = SampleAssembler()
        self.highest_epoch = 0
        self.required_epoch = 0
        self.highest_operator_boot = 0
        self.epoch_at_operator_restart = 0
        self.quarantine_until = 0
        self.hello = None
        self.operator_hello = None
        self.state = None
        self.fault = None
        self.last_fault: Fault | None = None
        self.permit = None
        self.permit_seq = None
        self.last_state_ns = None
        self.last_permit_ns = None
        self.connected = True
        self.last_ns = None

    def disconnect(self, now_ns):
        self.advance(now_ns)
        self.connected = False
        self.hello = self.operator_hello = None
        self.state = self.fault = self.permit = None
        # 연결이 끊겨도 같은 세션의 순번 비교 기준은 유지한다.
        self.last_state_ns = self.last_permit_ns = None
        self.samples.reset()
        self.quarantine_until = now_ns + BOOT_QUARANTINE

    def advance(self, now_ns):
        if self.last_ns is not None and now_ns < self.last_ns:
            raise ValueError("CAN 관측 시각은 단조 증가해야 한다")
        self.last_ns = now_ns
        self.samples.advance(now_ns)

    def ingest(self, frame, now_ns):
        self.advance(now_ns)
        self.connected = True
        self.ingest_seq += 1
        record_id = f"{self.backend_session}:{self.ingest_seq}"

        try:
            message = decode_frame(frame)
        except InvalidFrame as exc:
            self.errors[frame.can_id] += 1
            if exc.sample_seq is not None and self.samples_allowed(now_ns):
                self.samples.add(CanId(frame.can_id), exc.sample_seq, now_ns, record_id)
            return Observation(
                record_id, self.ingest_seq, now_ns, frame, error=str(exc)
            )

        received = Observation(record_id, self.ingest_seq, now_ns, frame, message)
        self.accept(received)
        return received

    def motor_known(self, now_ns):
        return (
            self.connected
            and fresh(self.hello, now_ns, HELLO_LIFETIME)
            and self.hello.message.version == VERSION
            and self.hello.message.epoch >= self.required_epoch
        )

    def samples_allowed(self, now_ns):
        return self.motor_known(now_ns) and now_ns >= self.quarantine_until

    def operator_known(self, now_ns):
        return (
            fresh(self.operator_hello, now_ns, HELLO_LIFETIME)
            and self.operator_hello.message.version == VERSION
        )

    def accept(self, received):
        message, now_ns = received.message, received.rx_mono_ns
        if isinstance(message, Hello):
            self.accept_hello(received)
        elif isinstance(message, OperatorHello):
            self.accept_operator_hello(received)
        elif isinstance(message, (MotorState, Fault)):
            self.accept_motor_status(received)
        elif isinstance(message, Permit):
            self.accept_permit(received)
        elif received.frame.can_id in SAMPLE_IDS:
            if isinstance(message, SampleTime) and self.highest_epoch:
                boot = self.highest_epoch >> 16
                if message.boot != boot:
                    # 이전 부팅의 시간 프레임에 딸린 미확정 측정값도 버린다.
                    self.samples.discard_unanchored(message.seq)
                    if message.boot > boot:
                        self.required_epoch = max(
                            self.required_epoch, (message.boot << 16) | 1
                        )
                    return
            if self.samples_allowed(now_ns):
                self.samples.add(
                    CanId(received.frame.can_id),
                    message.seq,
                    now_ns,
                    received.record_id,
                    message,
                )

    def accept_hello(self, received):
        message, now_ns = received.message, received.rx_mono_ns
        if message.epoch < max(self.highest_epoch, self.required_epoch):
            return

        epoch_changed = (
            message.version == VERSION and message.epoch != self.highest_epoch
        )
        old_boot = self.highest_epoch >> 16
        continuity_lost = old_boot and (
            not fresh(self.hello, now_ns, HELLO_LIFETIME)
            or self.hello.message.version != VERSION
            or message.version != VERSION
        )

        # 같은 부팅의 epoch 변경은 제어 상태만 초기화한다.
        if epoch_changed or continuity_lost:
            self.state = self.fault = self.permit = None
        if epoch_changed:
            self.highest_epoch = message.epoch
            self.last_fault = None
            self.permit_seq = None
            self.last_state_ns = self.last_permit_ns = None
        if old_boot and (message.boot != old_boot or continuity_lost):
            self.samples.reset()
            self.quarantine_until = now_ns + BOOT_QUARANTINE

        self.hello = received

    def accept_motor_status(self, received):
        message, now_ns = received.message, received.rx_mono_ns
        if self.highest_epoch and message.epoch > self.highest_epoch:
            self.required_epoch = max(self.required_epoch, message.epoch)
            return
        if not self.motor_known(now_ns) or message.epoch != self.highest_epoch:
            return

        if isinstance(message, MotorState):
            self.state = received
            self.last_state_ns = now_ns
            return

        if self.last_fault is not None:
            last = self.last_fault
            delta = forward_delta(message.event_seq, last.event_seq, FAULT_SEQ_MASK)
            if delta >= FAULT_SEQ_HALF or (delta == 0 and message.mask != last.mask):
                return

        self.last_fault = message
        self.fault = received

    def accept_operator_hello(self, received):
        message = received.message
        if message.boot_id < self.highest_operator_boot:
            return
        if message.version == VERSION and message.boot_id != self.highest_operator_boot:
            if self.highest_operator_boot:
                # 조작 노드 재부팅을 관측하면 모터가 새 epoch를 알릴 때까지 기다린다.
                self.epoch_at_operator_restart = self.highest_epoch
            self.permit = None
            self.permit_seq = None
            self.last_permit_ns = None
            self.highest_operator_boot = message.boot_id
        if message.version != VERSION or not self.operator_known(received.rx_mono_ns):
            # HELLO가 복구돼도 만료 전 허가를 다시 사용하지 않는다.
            self.permit = None
        self.operator_hello = received

    def accept_permit(self, received):
        message, now_ns = received.message, received.rx_mono_ns
        if (
            not self.motor_known(now_ns)
            or message.epoch != self.highest_epoch
            or message.epoch <= self.epoch_at_operator_restart
            or not self.operator_known(now_ns)
        ):
            return

        if self.permit_seq is not None:
            delta = forward_delta(message.alive_seq, self.permit_seq)
            if not 1 <= delta < SEQUENCE_HALF:
                return

        self.permit = received
        self.permit_seq = message.alive_seq
        self.last_permit_ns = now_ns

    def snapshot(self, now_ns):
        self.advance(now_ns)
        known = self.motor_known(now_ns)
        state = (
            self.state.message
            if known and fresh(self.state, now_ns, STATE_LIFETIME)
            else None
        )
        fault = (
            self.fault.message
            if known and fresh(self.fault, now_ns, FAULT_LIFETIME)
            else None
        )
        permit = (
            self.permit.message
            if known
            and fresh(self.permit, now_ns, PERMIT_LIFETIME)
            and self.operator_known(now_ns)
            else None
        )

        estop = (
            bool(state.flags & NC2_OPEN)
            if state is not None and self.hello.message.capabilities & NC2_CAPABILITY
            else None
        )
        sample = self.samples.latest if known else None

        return {
            "motor": {
                "epoch": self.highest_epoch if known else None,
                "state": state.state.name if state else "UNKNOWN",
                "mode": state.mode.name if state else "UNKNOWN",
                "fault_mask": fault.mask if fault else None,
                "link": self.link(state, self.last_state_ns),
                "age_ms": self.age(self.last_state_ns, now_ns),
                "sample": sample.snapshot(now_ns) if sample else empty_sample(),
            },
            "operator": {
                "link": self.link(permit, self.last_permit_ns),
                "age_ms": self.age(self.last_permit_ns, now_ns),
                "estop_active": estop,
                "controls_ready": (
                    bool(
                        permit.flags & CONTROLS_READY
                        and not permit.flags & STOP_REQUESTED
                    )
                    if permit
                    else None
                ),
            },
        }

    def link(self, current, last_ns):
        if not self.connected or last_ns is None:
            return "offline"
        return "fresh" if current is not None else "stale"

    @staticmethod
    def age(last_ns, now_ns):
        return (now_ns - last_ns) // NS_PER_MS if last_ns is not None else None
