"""TCP에서 최신 상태와 저장된 진단 이벤트를 읽기 전용으로 제공한다."""

import time
import uuid
from collections import deque
from dataclasses import dataclass, field

from ..can_rx.state import STATE_LIFETIME, CanView
from ..fpga_uart.session import STATUS_LIFETIME_NS
from ..tcp_stream import TcpStream

EVENT_LIMIT = 32


def snapshot_links(snapshot):
    return {name: snapshot[name]["link"] for name in ("motor", "operator")}


@dataclass(frozen=True)
class MonitorView:
    can: CanView
    logical_ns: int
    wall_ns: int
    test: dict
    fpga_connected: bool
    fpga_degraded: bool
    fpga_last_ns: int | None
    healthy_recording: bool
    service_details: dict = field(default_factory=dict)

    def at(self, wall_ns, *, age_logging=True):
        elapsed_ns = wall_ns - self.wall_ns
        now_ns = self.logical_ns + elapsed_ns
        view = self.can.at(now_ns)
        motor_link = view["motor"]["link"]

        fpga = "OFFLINE"
        if self.fpga_connected:
            if self.fpga_degraded:
                fpga = "DEGRADED"
            elif self.fpga_last_ns is not None:
                age_ns = now_ns - self.fpga_last_ns
                fpga = "OK" if age_ns < STATUS_LIFETIME_NS else "DEGRADED"

        return {
            **view,
            "test": self.test,
            "services": {
                "can": {"fresh": "OK", "stale": "DEGRADED", "offline": "OFFLINE"}[
                    motor_link
                ],
                "fpga": fpga,
                "log": (
                    "OK"
                    if self.healthy_recording
                    and (not age_logging or elapsed_ns < STATE_LIFETIME)
                    else "DEGRADED"
                ),
                "ai": "DISABLED",
            },
        }


class ReadOnlySession:
    heartbeat_deadline = None

    def __init__(self, stream_id):
        self.stream_id = stream_id

    def expire(self, now):
        pass

    def handle(self, kind, data, now):
        if kind == "control_heartbeat":
            return None
        request_id = data.get("request_id")
        if not isinstance(request_id, str):
            raise ValueError("요청 식별자는 UUID 문자열이어야 한다")
        uuid.UUID(request_id)
        reason = (
            "READ_ONLY" if data.get("stream_id") == self.stream_id else "BAD_STREAM"
        )
        return {
            "request_id": request_id,
            "status": "REJECTED",
            "reason": reason,
            "motor_epoch": None,
            "cmd_seq": None,
        }


class MonitorStream(TcpStream):
    capabilities = ("diagnostics_read_only",)

    def __init__(self, session, source, initial):
        super().__init__(session)
        self.source = source
        self.latest = initial
        self.events = deque()
        self.recording = None
        self.reported_services = {}
        self.reported_links = None

    def open_session(self, stream_id):
        if self.control is not None:
            return self.control.open_session(stream_id)
        return ReadOnlySession(stream_id)

    def pending_results(self, session):
        return self.control.drain(session.stream_id) if self.control is not None else ()

    def client_disconnected(self, session):
        if self.control is not None:
            self.control.controller.disconnect_client(time.monotonic_ns())
            self.control.drain()

    def snapshot(self, session, now):
        return self._snapshot_at(time.monotonic_ns())

    def _snapshot_at(self, wall_ns, *, age_logging=True):
        snapshot = self.latest.at(wall_ns, age_logging=age_logging)
        if self.recording is not None:
            snapshot["recording"] = self.recording
        return snapshot

    def publish(self, snapshot, events):
        self.latest = snapshot
        if self.client is None:
            return
        if len(self.events) + len(events) > EVENT_LIMIT:
            self.client.transport.abort()
            return
        self.events.extend(events)

    def notifications(self):
        for _ in range(len(self.events)):
            yield "event", self.events.popleft()

    def snapshot_notifications(self, snapshot):
        messages = []
        links = snapshot_links(snapshot)
        if self.reported_links is not None:
            for name, link in links.items():
                previous = self.reported_links[name]
                if link != previous:
                    messages.append(
                        (
                            "event",
                            {
                                "event_id": str(uuid.uuid4()),
                                "category": "link",
                                "code": "LINK_CHANGED",
                                "severity": "info" if link == "fresh" else "warning",
                                "context": {
                                    "link": name,
                                    "previous": previous,
                                    "current": link,
                                },
                            },
                        )
                    )
        self.reported_links = links

        service_details = self.latest.service_details
        recording = snapshot.get("recording")
        for name, status in snapshot["services"].items():
            details = service_details.get(name, {})
            reason = details.get("reason")
            dropped = details.get("dropped", 0)
            if name == "log" and recording is not None:
                reason = recording["reason"]
                dropped = recording.get("dropped_records", 0)
            elif name == "log" and status == "DEGRADED":
                reason = (
                    "RECORDING_DELAY"
                    if self.latest.healthy_recording
                    else "RECORDING_FAILED"
                )
            service = {
                "name": name,
                "status": status,
                "reason": None if status == "OK" else reason,
                "retries": details.get("retries", 0),
                "dropped": dropped,
            }
            if service != self.reported_services.get(name):
                self.reported_services[name] = service
                messages.append(("service", service))
        return messages

    def stream_closed(self):
        self.events.clear()
        self.reported_services.clear()
        self.reported_links = None
