"""전원을 차단한 장비의 CAN 조작을 비차단 소켓으로 점검한다."""

import asyncio
import errno
import logging
import time
from functools import partial

from ..can_control.boot_id import boot_id_session
from ..can_control.controller import POLL_INTERVAL
from ..can_control.tcp import TcpCanControl
from ..tcp_stream import run_tcp_server
from .socketcan import (
    CAN_ERR_BUSOFF,
    CanDropCounter,
    can_error_reason,
    open_can,
    pack_frame,
    read_can_observation,
)

LOGGER = logging.getLogger(__name__)
RETRY_NS = 1_000_000_000
MAX_RX_PER_POLL = 64
SEND_BUSY = {errno.EAGAIN, errno.EWOULDBLOCK, errno.ENOBUFS}


class SocketCanControl:
    def __init__(self, opener, *, now_ns=time.monotonic_ns, record=None):
        self.opener = opener
        self.now_ns = now_ns
        self.socket = None
        self.retry_at_ns = 0
        self.drops = CanDropCounter()
        self.send_error = None
        self.record = record
        self.open_attempt = 0

    def note(self, now, source, payload, **detail):
        if self.record is not None:
            self.record((now, source, payload, detail or None))

    def submitted(self, frame, now, request_id):
        detail = {"request_id": request_id} if request_id is not None else {}
        self.note(now, "can_tx", frame, **detail)

    def receive(self, controller, frame):
        now = self.now_ns()
        if self.record is None:
            controller.receive(frame, now)
            return

        detail = {}

        def match(request_id, opcode):
            detail.update(request_id=request_id, opcode=opcode.name)

        controller.receive(frame, now, on_result=match)
        self.note(now, "can", frame, **detail)

    def try_send(self, frame):
        if self.socket is None or self.send_error is not None:
            return False

        packet = pack_frame(frame)
        try:
            if self.socket.send(packet) != len(packet):
                raise OSError("CAN 프레임 제출 길이 오류")
        except OSError as exc:
            if not isinstance(exc, BlockingIOError) and exc.errno not in SEND_BUSY:
                self.send_error = exc
            return False
        return True

    def close_socket(self):
        if self.socket is not None:
            sock, self.socket = self.socket, None
            sock.close()

    def lose_link(self, controller, error, *, reason=None, submit_inhibit=False):
        now = self.now_ns()
        controller.disconnect_can(now)
        controller.disconnect_client(now)
        try:
            if submit_inhibit:
                controller.send_permit(now, receive_queue_drained=False)
        finally:
            self.close_socket()
        self.send_error = None
        self.retry_at_ns = now + RETRY_NS
        self.open_attempt = 0
        self.note(
            now,
            "service",
            "can_disconnect",
            reason=reason or can_error_reason(error),
        )
        LOGGER.warning("CAN 조작 연결 중단: %s", error)

    def step(self, controller):
        now = self.now_ns()
        if self.socket is None:
            controller.advance_time(now)
            if now < self.retry_at_ns:
                return
            attempt = self.open_attempt + 1 if self.retry_at_ns else 0
            try:
                self.socket = self.opener()
            except (OSError, ValueError) as exc:
                reason = can_error_reason(exc, invalid="INVALID_INTERFACE")
                if attempt:
                    self.open_attempt = attempt
                    self.retry_at_ns = now + RETRY_NS
                    self.note(
                        now,
                        "service",
                        "can_retry_failed",
                        attempt=attempt,
                        reason=reason,
                    )
                    LOGGER.warning("CAN 조작 재연결 실패: %s", exc)
                else:
                    self.lose_link(controller, exc, reason=reason)
                return
            self.drops = CanDropCounter()
            now = self.now_ns()
            controller.disconnect_client(now)
            controller.reconnect_can(now)
            if attempt:
                self.note(now, "service", "can_reconnect", attempt=attempt)
            self.retry_at_ns = 0
            self.open_attempt = 0

        # 송신 전에 대기 중인 STATE를 먼저 읽는다.
        for _ in range(MAX_RX_PER_POLL):
            try:
                received = read_can_observation(self.socket, own_operator=True)
            except BlockingIOError:
                controller.poll(self.now_ns())
                break
            except ValueError as exc:
                self.lose_link(controller, exc, submit_inhibit=True)
                return
            except OSError as exc:
                self.lose_link(controller, exc)
                return

            frame = received.frame
            if drop := self.drops.observe(received.dropped_total):
                self.note(self.now_ns(), "service", "can_rx_overflow", dropped=drop)
                self.lose_link(controller, "수신 큐 드롭", submit_inhibit=True)
                return
            if received.error is not None:
                self.lose_link(controller, received.error, submit_inhibit=True)
                return
            if frame is not None:
                if frame.error and frame.can_id & CAN_ERR_BUSOFF:
                    self.note(self.now_ns(), "can", frame)
                    self.lose_link(controller, "bus-off", reason="BUS_OFF")
                    return
                self.receive(controller, frame)
        else:
            now = self.now_ns()
            controller.advance_time(now)
            controller.send_permit(now, receive_queue_drained=False)

        if self.send_error is not None:
            self.lose_link(controller, self.send_error)

    async def run(self, stream):
        controller = stream.control.controller
        try:
            while True:
                self.step(controller)
                if stream.client is None:
                    stream.control.drain()
                await asyncio.sleep(POLL_INTERVAL)
        finally:
            try:
                if self.socket is not None:
                    now = self.now_ns()
                    try:
                        controller.disconnect_client(now)
                        controller.start_inhibit("CANCELLED_BY_SHUTDOWN", renew=True)
                        controller.send_permit(now)
                    finally:
                        controller.disconnect_can(now)
            finally:
                self.close_socket()


async def serve_live_control(interface, boot_file, port, *, motor_power_off=False):
    if not motor_power_off:
        raise ValueError("모터 6V 물리 차단 확인이 필요합니다")

    bus = SocketCanControl(
        partial(open_can, interface, count_overflow=True, receive_own=True)
    )
    with boot_id_session(boot_file) as boot_id:
        stream = TcpCanControl(boot_id, bus.try_send, power_off_test=True)
        stream.controller.disconnect_can(bus.now_ns())
        await run_tcp_server(stream, "127.0.0.1", port, partial(bus.run, stream))
