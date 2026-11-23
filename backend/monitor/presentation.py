"""판정·장치 관측에서 TCP 상태와 이벤트를 구성한다."""

import time
from uuid import UUID, uuid5

from ..fpga_uart.session import TX_MASK, Issue
from .tcp import MonitorView

LINK_INPUTS = {
    "can_disconnect",
    "can_reconnect",
    "can_retry_failed",
    "can_rx_overflow",
    "fpga_disconnect",
    "fpga_reconnect",
    "fpga_retry_failed",
}


def verdict_event(event):
    context = event["context"]
    return {
        "event_id": event["event_id"],
        "category": "test",
        "code": event["code"],
        "severity": {"FAIL": "error", "INCONCLUSIVE": "warning"}.get(
            event["code"], "info"
        ),
        "context": {
            key: context[key]
            for key in (
                "run_id",
                "scenario_id",
                "result",
                "reason",
                "timing_source",
                "latency_us",
                "limit_us",
                "evidence_saved",
            )
        },
    }


def input_event(record, category, code, severity, **context):
    event_id = uuid5(UUID(record.backend_session), f"{record.ingest_seq}:{code}")
    return {
        "event_id": str(event_id),
        "category": category,
        "code": code,
        "severity": severity,
        "context": {"record_id": record.record_id, **context},
    }


class MonitorPresentation:
    def __init__(self):
        self.fpga_last_ns = None
        self.fpga_degraded = False
        self.last_fault = None
        self.last_fpga_sequence = None
        self.service_details = {}

    def observe(self, engine, record, received):
        if record.source == "fpga":
            for observation in received:
                if observation.duplicate:
                    continue
                if observation.frame is not None:
                    self.fpga_last_ns = record.rx_mono_ns
                    self._observe_fpga_sequence(observation)
                self.fpga_degraded = bool(observation.issues)
        elif (
            record.source == "can"
            and received is not None
            and received is engine.can.fault
        ):
            fault = received.message
            boot = fault.epoch >> 16
            old = self.last_fault
            self.last_fault = (boot, fault.mask)
            if old != self.last_fault and (fault.mask or (old and old[0] == boot)):
                return input_event(
                    record,
                    "fault",
                    "MOTOR_FAULT_CHANGED",
                    "error" if fault.mask else "info",
                    motor_epoch=fault.epoch,
                    fault_mask=fault.mask,
                    previous_mask=old[1] if old and old[0] == boot else None,
                )
        elif record.source == "service" and record.payload in LINK_INPUTS:
            return self._observe_link(record)
        return None

    def _observe_fpga_sequence(self, observation):
        previous = self.last_fpga_sequence
        sequence = observation.frame.tx_seq
        self.last_fpga_sequence = sequence
        if previous is not None and Issue.TX_GAP in observation.issues:
            details = self.service_details.get("fpga", {})
            lost = (sequence - previous) & TX_MASK
            self.service_details["fpga"] = {
                **details,
                "dropped": details.get("dropped", 0) + lost - 1,
            }

    def _observe_link(self, record):
        name, change = record.payload.split("_", 1)
        if record.payload == "fpga_reconnect":
            self.fpga_last_ns = None
            self.fpga_degraded = False
            self.last_fpga_sequence = None
        detail = dict(record.detail or {})
        if "reason" in detail:
            detail["reason"] = detail["reason"][:128]
        previous = self.service_details.get(name, {})
        self.service_details[name] = {
            "reason": detail.get("reason"),
            "retries": detail.get("attempt", previous.get("retries", 0)),
            "dropped": previous.get("dropped", 0) + detail.get("dropped", 0),
        }
        return input_event(
            record,
            "link",
            record.payload.upper(),
            "info" if change == "reconnect" else "warning",
            service=name,
            **detail,
        )

    def view(self, engine, logical_ns):
        result = engine.snapshot()
        return MonitorView(
            can=engine.can.capture_view(),
            logical_ns=max(engine.now_ns, logical_ns),
            wall_ns=time.monotonic_ns(),
            test={
                "run_id": result.get("run_id"),
                "scenario_id": result.get("scenario_id"),
                "status": result["result"],
                "reason": result.get("reason"),
            },
            fpga_connected=engine.uart.connected,
            fpga_degraded=self.fpga_degraded or engine.uart.quarantined,
            fpga_last_ns=(
                engine.fpga_status_ns
                if engine.fpga_status_watchdog
                else self.fpga_last_ns
            ),
            healthy_recording=(
                engine.healthy_recording and engine.low_space_record_id is None
            ),
            service_details=self.service_details.copy(),
        )
