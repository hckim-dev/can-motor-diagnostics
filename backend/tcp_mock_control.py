"""조작 목업의 heartbeat, 시험 준비 상태, 요청 결과를 관리한다."""

import json
import uuid
from dataclasses import dataclass
from itertools import count

HEARTBEAT_TIMEOUT = 0.3
REQUEST_LIMIT = 255
COMMAND_ARGUMENTS = {
    "ARM_TEST": range(1),
    "START_REAL": range(1, 32_768),
    "STOP": range(1),
    "RESET": range(1),
}

SNAPSHOT = {
    "motor": {
        "epoch": 65537,
        "state": "STOPPED",
        "mode": "TEST",
        "fault_mask": 0,
        "link": "fresh",
        "age_ms": 0,
        "sample": {
            "seq": None,
            "tick_ms": None,
            "actual_rpm": None,
            "target_rpm": None,
            "pwm_permille": None,
            "current_ma": None,
            "bus_mv": None,
            "sensor_age_ms": None,
            "sample_complete": False,
            "quality": "unavailable",
            "sample_mode": "UNKNOWN",
            "sample_state": "UNKNOWN",
            "synthetic": False,
        },
    },
    "operator": {
        "link": "offline",
        "age_ms": None,
        "estop_active": False,
        "controls_ready": None,
    },
    "test": {"run_id": None, "scenario_id": None, "status": "IDLE", "reason": None},
    "services": {"can": "OK", "fpga": "OFFLINE", "log": "OK", "ai": "DISABLED"},
}


@dataclass(frozen=True)
class RequestRecord:
    stream_id: str
    payload: str
    result: dict | None


def request_payload(data):
    # 거부된 요청도 비교할 수 있도록 값의 형식과 필드 누락을 구분한다.
    return json.dumps(
        {name: data[name] for name in ("opcode", "argument") if name in data},
        sort_keys=True,
        allow_nan=False,
    )


class MockControl:
    def __init__(self):
        self.requests: dict[uuid.UUID, RequestRecord] = {}
        self.command_sequences = count(1)

    def open_session(self, stream_id):
        return ControlSession(stream_id, self)


class ControlSession:
    def __init__(self, stream_id, control):
        self.stream_id = stream_id
        self.control = control
        self.last_heartbeat = None
        self.heartbeat_deadline = None
        self.armed = False

    def expire(self, now):
        if self.heartbeat_deadline is not None and now >= self.heartbeat_deadline:
            self.heartbeat_deadline = None
            self.armed = False

    def snapshot(self, now):
        ready = self.heartbeat_deadline is not None
        age_ms = (
            None
            if self.last_heartbeat is None
            else int((now - self.last_heartbeat) * 1000)
        )
        link = "offline" if age_ms is None else ("fresh" if ready else "stale")
        state = "TEST_ARMED" if self.armed else "STOPPED"

        return {
            **SNAPSHOT,
            "motor": {**SNAPSHOT["motor"], "state": state},
            "operator": {
                **SNAPSHOT["operator"],
                "link": link,
                "age_ms": age_ms,
                "controls_ready": ready if age_ms is not None else None,
            },
        }

    def handle(self, kind, data, now):
        self.expire(now)
        if kind == "control_heartbeat":
            if data.get("stream_id") == self.stream_id:
                self.last_heartbeat = now
                self.heartbeat_deadline = now + HEARTBEAT_TIMEOUT
            return None
        return self.handle_request(data)

    def handle_request(self, data):
        request_id = data.get("request_id")
        if not isinstance(request_id, str):
            raise ValueError("요청 식별자는 UUID 문자열이어야 한다")
        key = uuid.UUID(request_id)

        result = {
            "request_id": request_id,
            "status": "REJECTED",
            "reason": "BAD_STREAM",
            "motor_epoch": None,
            "cmd_seq": None,
        }
        if data.get("stream_id") != self.stream_id:
            return result

        opcode, argument = data.get("opcode"), data.get("argument")
        payload = request_payload(data)

        requests = self.control.requests
        cached = requests.get(key)
        if cached is not None:
            if cached.stream_id != self.stream_id:
                return {**result, "reason": "REQUEST_FROM_OLD_STREAM"}
            if cached.payload != payload:
                return {**result, "reason": "REQUEST_ID_CONFLICT"}
            return {**cached.result, "request_id": request_id}
        if len(requests) >= REQUEST_LIMIT:
            return {**result, "reason": "REQUEST_LIMIT"}

        allowed_arguments = (
            COMMAND_ARGUMENTS.get(opcode) if isinstance(opcode, str) else None
        )
        if allowed_arguments is None:
            reason = "BAD_OPCODE"
        elif type(argument) is not int or argument not in allowed_arguments:
            reason = "BAD_ARGUMENT"
        elif opcode != "ARM_TEST":
            reason = "UNSUPPORTED_OPCODE"
        elif self.heartbeat_deadline is None:
            reason = "HEARTBEAT_REQUIRED"
        elif self.armed:
            reason = "BAD_STATE"
        else:
            self.armed = True
            reason = "OK"
            result.update(
                status="COMPLETED",
                motor_epoch=SNAPSHOT["motor"]["epoch"],
                cmd_seq=next(self.control.command_sequences),
            )

        result["reason"] = reason
        requests[key] = RequestRecord(self.stream_id, payload, result)
        return result
