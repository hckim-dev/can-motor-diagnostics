"""조작 연결, CAN 허가, 명령 마감과 결과 대응을 관리한다."""

from collections import deque
from dataclasses import dataclass

from ..can_rx.codec import (
    ENCODER_CALIBRATED,
    OPERATING_CONFIG_VALID,
    STOP_STATES,
    SUPPLY_PRESENT,
    SUPPLY_VALID,
    CommandResult,
    Mode,
    State,
)
from ..can_rx.samples import NS_PER_MS, SEQUENCE_MASK
from ..can_rx.state import (
    FAULT_LIFETIME,
    NC2_CAPABILITY,
    NC2_OPEN,
    PERMIT_LIFETIME,
    STATE_LIFETIME,
    fresh,
)
from .wire import (
    MAX_CMD_SEQ,
    MAX_TARGET_RPM,
    Opcode,
    encode_command,
    encode_operator_hello,
    encode_permit,
    valid_argument,
)

HEARTBEAT_TIMEOUT_NS = 300 * NS_PER_MS
HELLO_PERIOD_NS = 500 * NS_PER_MS
PERMIT_PERIOD_NS = 50 * NS_PER_MS
SEND_TIMEOUT_NS = 50 * NS_PER_MS
RESULT_TIMEOUT_NS = 300 * NS_PER_MS
ACTIVE_STATES = (State.RUNNING, State.TEST_ARMED, State.TEST_ACTIVE)
RESULT_STATUSES = ("ACCEPTED", "COMPLETED", "REJECTED", "FAILED")
RESULT_REASONS = (
    "OK",
    "BAD_EPOCH",
    "BAD_SEQUENCE",
    "BAD_STATE",
    "BAD_MODE",
    "INHIBIT",
    "BAD_ARGUMENT",
    "NOT_CONFIGURED",
    "EXPIRED",
)


@dataclass(frozen=True)
class Outcome:
    request_id: str
    status: str
    reason: str
    motor_epoch: int | None = None
    cmd_seq: int | None = None

    def as_message(self):
        return {
            "request_id": self.request_id,
            "status": self.status,
            "reason": self.reason,
            "motor_epoch": self.motor_epoch,
            "cmd_seq": self.cmd_seq,
        }


@dataclass
class PendingCommand:
    request_id: str
    epoch: int
    seq: int
    opcode: Opcode
    argument: int
    queued_ns: int
    sent_ns: int | None = None
    accepted: bool = False


@dataclass
class InhibitRequest:
    epoch: int
    sent_after_record: int | None = None


class CanController:
    def __init__(self, receiver, try_send, boot_id, rpm_max=MAX_TARGET_RPM):
        if not 1 <= rpm_max <= MAX_TARGET_RPM:
            raise ValueError("목업 최대 RPM 범위 오류")
        self.receiver = receiver
        self.try_send = try_send
        self.operator_hello = encode_operator_hello(boot_id)
        self.rpm_max = rpm_max
        self.next_hello_ns = 0
        self.operator_announced = False
        self.can_connected = True
        self.next_permit_ns = 0
        self.epoch = 0
        self.cmd_seq = 0
        self.alive_seq = 0
        self.heartbeat_until_ns = None
        self.permit_sent_ns = None
        self.pending: PendingCommand | None = None
        self.inhibit: InhibitRequest | None = None
        self.outcomes: deque[Outcome] = deque()

    def heartbeat(self, now_ns):
        self.advance_time(now_ns)
        self.heartbeat_until_ns = now_ns + HEARTBEAT_TIMEOUT_NS

    def disconnect_client(self, now_ns, *, reason="CANCELLED_BY_DISCONNECT"):
        self.advance_time(now_ns)
        if self.heartbeat_until_ns is not None or self.pending is not None:
            self.start_inhibit(reason)
        self.heartbeat_until_ns = None

    def disconnect_can(self, now_ns):
        self.advance_time(now_ns)
        if not self.can_connected:
            return

        self.receiver.disconnect(now_ns)
        self.can_connected = False
        self.operator_announced = False
        # 단절 이전 억제 송신으로 재연결 후의 정지를 확인할 수 없다.
        self.start_inhibit("CANCELLED_BY_CAN_LOSS", renew=True)

    def reconnect_can(self, now_ns):
        self.advance_time(now_ns)
        if not self.can_connected:
            self.can_connected = True
            self.next_hello_ns = now_ns

    def current_state(self, now_ns):
        state = self.receiver.state
        if self.receiver.motor_known(now_ns) and fresh(state, now_ns, STATE_LIFETIME):
            return state.message
        return None

    def _apply_motor_observation(self, now_ns):
        epoch = self.receiver.highest_epoch
        if epoch != self.epoch and self.receiver.motor_known(now_ns):
            self.epoch = epoch
            self.cmd_seq = self.alive_seq = 0
            self.permit_sent_ns = None
            self.next_permit_ns = now_ns
            # 송신한 STOP 결과는 이전 epoch의 키로 도착할 수 있다.
            pending = self.pending
            if pending and (pending.opcode != Opcode.STOP or pending.sent_ns is None):
                self.cancel_pending("CANCELLED_BY_SESSION")
        self._check_permit(now_ns)

    def advance_time(self, now_ns):
        """새 입력을 반영하기 전에 기존 상태의 마감을 처리한다."""
        self.receiver.advance(now_ns)

        if self.heartbeat_until_ns is not None and now_ns >= self.heartbeat_until_ns:
            self.heartbeat_until_ns = None
            self.start_inhibit("HEARTBEAT_EXPIRED")

        pending = self.pending
        if pending is not None:
            if (
                pending.sent_ns is None
                and now_ns - pending.queued_ns >= SEND_TIMEOUT_NS
            ):
                self.complete("FAILED", "SEND_TIMEOUT")
            elif (
                pending.sent_ns is not None
                and now_ns - pending.sent_ns >= RESULT_TIMEOUT_NS
            ):
                self.complete("UNKNOWN_OUTCOME", "RESULT_TIMEOUT")

        self._check_permit(now_ns)

    def _check_permit(self, now_ns):
        state = self.current_state(now_ns)
        if self.permit_sent_ns is not None and (
            not self.can_grant(state) or now_ns - self.permit_sent_ns >= PERMIT_LIFETIME
        ):
            self.start_inhibit("CANCELLED_BY_CAN_LOSS")
        if (
            state is not None
            and state.state in ACTIVE_STATES
            and self.permit_sent_ns is None
            and self.inhibit is None
        ):
            self.start_inhibit("ACTIVE_WITHOUT_PERMIT")
        self.check_inhibit_confirmation(state)

    def can_grant(self, state):
        return (
            self.heartbeat_until_ns is not None
            and state is not None
            and state.mode in (Mode.REAL, Mode.TEST)
            and not state.flags & NC2_OPEN
        )

    def start_inhibit(self, reason, *, renew=False):
        if self.inhibit is None or renew:
            self.inhibit = InhibitRequest(self.epoch)
        self.next_permit_ns = 0
        self.permit_sent_ns = None
        if self.pending is not None and (renew or self.pending.opcode != Opcode.STOP):
            self.cancel_pending(reason)

    def check_inhibit_confirmation(self, state):
        inhibit = self.inhibit
        if (
            inhibit is not None
            and inhibit.sent_after_record is not None
            and state is not None
            and state.epoch > inhibit.epoch
            and state.state in STOP_STATES
            and self.receiver.state.ingest_seq > inhibit.sent_after_record
        ):
            self.inhibit = None
            self.next_permit_ns = 0

    def send_permit(self, now_ns):
        if not self.epoch or now_ns < self.next_permit_ns:
            return

        if self.inhibit is not None:
            if (
                self.inhibit.sent_after_record is not None
                and self.epoch > self.inhibit.epoch
            ):
                state = self.current_state(now_ns)
                if state is None or state.state not in ACTIVE_STATES:
                    return
            frame = encode_permit(self.epoch, self.alive_seq, inhibit=True)
        elif self.operator_announced and self.can_grant(self.current_state(now_ns)):
            frame = encode_permit(self.epoch, self.alive_seq, ready=True)
        else:
            return

        sent = self.try_send(frame)
        # 실패한 억제 프레임은 다음 버스 주기에 다시 시도한다.
        if sent or self.inhibit is None:
            self.next_permit_ns = now_ns + PERMIT_PERIOD_NS
        if not sent:
            return
        self.alive_seq = (self.alive_seq + 1) & SEQUENCE_MASK
        if self.inhibit is not None:
            if (
                self.inhibit.sent_after_record is None
                or self.epoch > self.inhibit.epoch
            ):
                self.inhibit.epoch = self.epoch
                self.inhibit.sent_after_record = self.receiver.ingest_seq
        else:
            self.permit_sent_ns = now_ns

    def poll(self, now_ns):
        self.advance_time(now_ns)
        if not self.can_connected:
            return

        if now_ns >= self.next_hello_ns:
            sent = self.try_send(self.operator_hello)
            self.operator_announced |= sent
            self.next_hello_ns = now_ns + (
                HELLO_PERIOD_NS if sent else PERMIT_PERIOD_NS
            )
        self.send_permit(now_ns)

        pending = self.pending
        if pending is None or pending.sent_ns is not None:
            return
        if pending.opcode != Opcode.STOP:
            if self.permit_sent_ns is None:
                return
            reason = self.command_precondition(pending.opcode, pending.argument, now_ns)
            if reason is not None:
                self.complete("FAILED", reason)
                return
        if pending.opcode == Opcode.STOP and (
            self.inhibit is None or self.inhibit.sent_after_record is None
        ):
            return
        frame = encode_command(
            pending.epoch, pending.seq, pending.opcode, pending.argument
        )
        if self.try_send(frame):
            pending.sent_ns = now_ns

    def request(self, request_id, opcode, argument, now_ns):
        """명령을 대기 상태로 등록하고 실제 송신은 poll에서 처리한다."""
        self.advance_time(now_ns)
        if not valid_argument(opcode, argument):
            return Outcome(request_id, "REJECTED", "BAD_ARGUMENT")
        if opcode == Opcode.STOP:
            self.start_inhibit("CANCELLED_BY_STOP", renew=True)
            if not self.receiver.motor_known(now_ns) or self.cmd_seq >= MAX_CMD_SEQ:
                return Outcome(request_id, "UNKNOWN_OUTCOME", "STOP_INHIBIT_ONLY")
        else:
            reason = self.rejection_reason(opcode, argument, now_ns)
            if reason is not None:
                return Outcome(request_id, "REJECTED", reason)

        self.cmd_seq += 1
        self.pending = PendingCommand(
            request_id, self.epoch, self.cmd_seq, opcode, argument, now_ns
        )
        return None

    def rejection_reason(self, opcode, argument, now_ns):
        if not self.can_connected:
            return "CAN_OFFLINE"
        if self.heartbeat_until_ns is None:
            return "HEARTBEAT_REQUIRED"
        if self.inhibit is not None:
            return "INHIBIT"
        if self.pending is not None:
            return "BUSY"
        if self.cmd_seq >= MAX_CMD_SEQ:
            return "SEQUENCE_EXHAUSTED"

        return self.command_precondition(opcode, argument, now_ns)

    def command_precondition(self, opcode, argument, now_ns):
        state = self.current_state(now_ns)
        if state is None:
            return "MOTOR_STATE_UNAVAILABLE"
        if not self.can_grant(state):
            return "INHIBIT"
        if opcode == Opcode.RESET:
            return None if state.state == State.FAULT else "BAD_STATE"
        if state.state != State.STOPPED or not fresh(
            self.receiver.fault, now_ns, FAULT_LIFETIME
        ):
            return "BAD_STATE"
        if self.receiver.fault.message.mask:
            return "INHIBIT"
        if opcode == Opcode.ARM_TEST:
            if state.mode != Mode.TEST:
                return "BAD_MODE"
            return (
                None
                if state.flags & SUPPLY_VALID and not state.flags & SUPPLY_PRESENT
                else "INHIBIT"
            )

        if state.mode != Mode.REAL:
            return "BAD_MODE"
        if not self.receiver.hello.message.capabilities & NC2_CAPABILITY:
            return "NC2_UNAVAILABLE"
        required = (
            SUPPLY_PRESENT | SUPPLY_VALID | ENCODER_CALIBRATED | OPERATING_CONFIG_VALID
        )
        if state.flags & required != required:
            return "NOT_CONFIGURED"
        return None if argument <= self.rpm_max else "BAD_ARGUMENT"

    def receive(self, frame, now_ns):
        self.advance_time(now_ns)
        if not self.can_connected:
            return None

        received = self.receiver.ingest(frame, now_ns)
        self._apply_motor_observation(now_ns)
        message = received.message
        pending = self.pending
        if (
            not isinstance(message, CommandResult)
            or pending is None
            or pending.sent_ns is None
            or (message.request_epoch, message.cmd_seq) != (pending.epoch, pending.seq)
        ):
            return received

        status = RESULT_STATUSES[message.result]
        reason = RESULT_REASONS[message.detail]
        if status == "ACCEPTED":
            if not pending.accepted:
                pending.accepted = True
                self.outcomes.append(
                    Outcome(
                        pending.request_id, status, reason, pending.epoch, pending.seq
                    )
                )
        else:
            self.complete(status, reason)
        return received

    def cancel_pending(self, reason):
        status = "UNKNOWN_OUTCOME" if self.pending.sent_ns is not None else "FAILED"
        self.complete(status, reason)

    def complete(self, status, reason):
        pending = self.pending
        was_sent = pending.sent_ns is not None
        self.outcomes.append(
            Outcome(
                pending.request_id,
                status,
                reason,
                pending.epoch if was_sent else None,
                pending.seq if was_sent else None,
            )
        )
        self.pending = None
