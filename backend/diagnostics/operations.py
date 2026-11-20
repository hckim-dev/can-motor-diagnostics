"""수락된 모터 상태로 REAL 운전의 관측 구간을 추적한다."""

from dataclasses import dataclass
from uuid import UUID, uuid5

from ..can_rx.codec import Mode, State
from ..can_rx.state import HELLO_LIFETIME, STATE_LIFETIME, Observation


@dataclass(frozen=True)
class Operation:
    operation_id: str
    started: Observation
    rx_utc: str
    previous_stop: Observation | None


class OperationTracker:
    def __init__(self, session, write):
        self.namespace = UUID(session)
        self.write = write
        self.operation: Operation | None = None
        self.state: Observation | None = None
        self.hello: Observation | None = None

    def close(self, reason, now_ns, *, row=None, end_observed=False):
        if self.operation is None:
            return
        operation = self.operation
        start = operation.started
        self.write(
            {
                "operation_id": operation.operation_id,
                "motor_boot": start.message.epoch >> 16,
                "start_rx_mono_ns": str(start.rx_mono_ns),
                "start_rx_utc": operation.rx_utc,
                "start_record_id": start.record_id,
                "start_observed": operation.previous_stop is not None,
                "previous_stop_record_id": (
                    operation.previous_stop.record_id
                    if operation.previous_stop
                    else None
                ),
                "end_rx_mono_ns": str(now_ns),
                "end_rx_utc": row["rx_utc"] if row else None,
                "end_record_id": row["record_id"] if row else None,
                "end_observed": end_observed,
                "end_reason": reason,
            }
        )
        self.operation = None

    def observe(self, record, row, received, can):
        now = record.rx_mono_ns
        previous = self.state
        if previous is not None:
            state_deadline = previous.rx_mono_ns + STATE_LIFETIME
            hello_deadline = self.hello.rx_mono_ns + HELLO_LIFETIME
            deadline = min(state_deadline, hello_deadline)
            if now >= deadline:
                reason = (
                    "STATE_TIMEOUT" if deadline == state_deadline else "HELLO_TIMEOUT"
                )
                self.close(reason, deadline)
                previous = self.state = None

        old_hello, self.hello = self.hello, can.hello
        if record.source == "service" and record.payload in (
            "can_disconnect",
            "can_rx_overflow",
            "recording_failed",
        ):
            self.close(record.payload.upper(), now, row=row)
            self.state = None
            return

        # 새 부팅을 알리는 프레임은 HELLO가 오기 전에도 이전 관측을 무효화한다.
        required_boot = max(can.highest_epoch, can.required_epoch) >> 16
        if previous is not None and required_boot > previous.message.epoch >> 16:
            self.close("MOTOR_BOOT_CHANGED", now, row=row)
            previous = self.state = None
        elif (
            old_hello is not None
            and self.hello is not None
            and old_hello.message.version != self.hello.message.version
        ):
            self.close("CAN_PROTOCOL_CHANGED", now, row=row)
            previous = self.state = None

        if received is None:
            return
        if received is can.fault and received.message.mask:
            self.close("FAULT", now, row=row, end_observed=True)
            self.state = None
        if received is not can.state:
            return

        self.state = received
        state = received.message
        if state.state == State.FAULT:
            self.close("FAULT", now, row=row, end_observed=True)
        elif state.mode != Mode.REAL:
            self.close("MODE_CHANGED", now, row=row, end_observed=True)
        elif state.state == State.STOPPED:
            self.close("STOPPED", now, row=row, end_observed=True)
        elif state.state == State.RUNNING:
            fault = can.fault
            if self.operation is None and not (fault and fault.message.mask):
                previous_stop = (
                    previous
                    if previous is not None and previous.message.state == State.STOPPED
                    else None
                )
                self.operation = Operation(
                    str(uuid5(self.namespace, f"operation:{received.record_id}")),
                    received,
                    row["rx_utc"],
                    previous_stop,
                )
        else:
            self.close("RUNNING_ENDED", now, row=row, end_observed=True)
