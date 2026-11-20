"""TCP 요청과 CAN 제어기의 결과를 현재 스트림에 연결한다."""

import time
import uuid

from ..can_rx.state import CanReceiver
from ..tcp_mock_control import REQUEST_LIMIT, SNAPSHOT, RequestRecord, request_payload
from ..tcp_stream import TcpStream
from .controller import CanController, Outcome
from .wire import Opcode


class CanControl:
    def __init__(self, controller):
        self.controller = controller
        self.requests: dict[uuid.UUID, RequestRecord] = {}
        self.limit_reached = False

    def open_session(self, stream_id):
        return CanSession(stream_id, self)

    def drain(self, stream_id=None):
        messages = []
        while self.controller.outcomes:
            outcome = self.controller.outcomes.popleft()
            key = uuid.UUID(outcome.request_id)
            previous = self.requests[key]
            result = outcome.as_message()
            self.requests[key] = RequestRecord(
                previous.stream_id, previous.payload, result
            )
            if previous.stream_id == stream_id:
                messages.append(result)
        return messages


class CanSession:
    def __init__(self, stream_id, control):
        self.stream_id = stream_id
        self.control = control

    @property
    def heartbeat_deadline(self):
        return None

    def expire(self, now):
        self.control.controller.advance_time(time.monotonic_ns())

    def handle(self, kind, data, now):
        received_ns = time.monotonic_ns()
        controller = self.control.controller
        if kind == "control_heartbeat":
            if (
                data.get("stream_id") == self.stream_id
                and not self.control.limit_reached
            ):
                controller.heartbeat(received_ns)
            return None

        request_id = data.get("request_id")
        if not isinstance(request_id, str):
            raise ValueError("요청 식별자는 UUID 문자열이어야 한다")
        key = uuid.UUID(request_id)
        if data.get("stream_id") != self.stream_id:
            return Outcome(request_id, "REJECTED", "BAD_STREAM").as_message()

        payload = request_payload(data)
        previous = self.control.requests.get(key)
        if previous is not None:
            if previous.stream_id != self.stream_id:
                return Outcome(
                    request_id, "REJECTED", "REQUEST_FROM_OLD_STREAM"
                ).as_message()
            if previous.payload != payload:
                return Outcome(
                    request_id, "REJECTED", "REQUEST_ID_CONFLICT"
                ).as_message()
            # 송신 대기 중인 결과가 있으면 캐시의 이전 결과를 반복하지 않는다.
            if previous.result is None or any(
                uuid.UUID(outcome.request_id) == key for outcome in controller.outcomes
            ):
                return None
            return {**previous.result, "request_id": request_id}

        opcode_name = data.get("opcode")
        opcode = (
            Opcode.__members__.get(opcode_name)
            if isinstance(opcode_name, str)
            else None
        )
        if len(self.control.requests) >= REQUEST_LIMIT:
            # 기록 한도가 차면 새 요청을 차단하고 기존 허가도 철회한다.
            if not self.control.limit_reached:
                self.control.limit_reached = True
                controller.disconnect_client(received_ns, reason="REQUEST_LIMIT")
            return Outcome(request_id, "REJECTED", "REQUEST_LIMIT").as_message()

        outcome = (
            controller.request(request_id, opcode, data.get("argument"), received_ns)
            if opcode is not None
            else Outcome(request_id, "REJECTED", "BAD_OPCODE")
        )
        result = outcome.as_message() if outcome is not None else None
        self.control.requests[key] = RequestRecord(self.stream_id, payload, result)
        return result


class TcpCanControl(TcpStream):
    def __init__(self, boot_id, try_send, *, power_off_test=False):
        super().__init__()
        self.source = "live" if power_off_test else "mock"
        self.can_receiver = CanReceiver(self.backend_session)
        self.controller = CanController(
            self.can_receiver, try_send, boot_id, power_off_test=power_off_test
        )
        self.control = CanControl(self.controller)

    def snapshot(self, session, now):
        view = self.can_receiver.snapshot(time.monotonic_ns())
        services = {**SNAPSHOT["services"], "log": "DISABLED"}
        if self.source == "live":
            services["can"] = {
                "fresh": "OK",
                "stale": "DEGRADED",
                "offline": "OFFLINE",
            }[view["motor"]["link"]]
        return {**view, "test": SNAPSHOT["test"], "services": services}

    def pending_results(self, session):
        return self.control.drain(session.stream_id)

    def client_disconnected(self, session):
        self.controller.disconnect_client(time.monotonic_ns())
        self.control.drain()
