"""조작 CAN과 UART 관측을 하나의 기록 순서로 전달한다."""

import asyncio
import time
from contextlib import aclosing

from ..can_control.controller import CanController
from ..can_control.tcp import CanControl
from ..can_rx.state import CanReceiver
from ..diagnostics.storage import RecordingFailure
from ..hardware.live_control import MAX_RX_PER_POLL, SocketCanControl

RECORD_LIMIT = 4096


class ControlFeed:
    now_ns = staticmethod(time.monotonic_ns)

    def __init__(self, can_open, uart_feed, boot_id):
        self.queue = asyncio.Queue(maxsize=RECORD_LIMIT)
        self.bus = SocketCanControl(can_open, record=self._record)
        self.uart_feed = uart_feed
        self.boot_id = boot_id
        self.stream = None
        self.workers = ()
        self.worker_error = None
        self.dropped_records = 0

    def attach_control(self, stream):
        self.stream = stream
        controller = CanController(
            CanReceiver(stream.backend_session),
            self.bus.try_send,
            self.boot_id,
            power_off_test=True,
            on_submission=self.bus.submitted,
        )
        controller.disconnect_can(self.now_ns())
        stream.control = CanControl(controller)
        stream.capabilities = ("diagnostics", "can_control")

    def _record(self, record):
        try:
            self.queue.put_nowait(record)
        except asyncio.QueueFull as exc:
            self.dropped_records += 1
            raise RecordingFailure("통합 제어 기록 큐의 한도를 초과했습니다") from exc

    async def _receive_uart(self):
        async with aclosing(self.uart_feed.records()) as records:
            async for batch in records:
                for index, record in enumerate(batch):
                    try:
                        self._record(record)
                    except RecordingFailure:
                        self.dropped_records += len(batch) - index - 1
                        raise

    def stop_inputs(self):
        workers, self.workers = self.workers, ()
        for worker in workers:
            worker.cancel()

    def observe_devices(self, snapshot):
        self.uart_feed.observe_devices(snapshot)

    def idle_records(self):
        return ((self.now_ns(), "service", "clock"),) if self.queue.empty() else ()

    def _drain(self, limit):
        batch = []
        for _ in range(min(limit, self.queue.qsize())):
            if (record := self.queue.get_nowait()) is not None:
                batch.append(record)
        return batch

    def final_records(self):
        return self._drain(RECORD_LIMIT)

    def _worker_done(self, worker):
        if (
            self.worker_error is None
            and not worker.cancelled()
            and (error := worker.exception()) is not None
        ):
            self.worker_error = error
            # 기록 소비가 멈춰 있어도 수집 오류를 CAN 종료에 즉시 반영한다.
            self.stop_inputs()
        if not self.queue.full():
            self.queue.put_nowait(None)

    async def records(self):
        workers = self.workers = (
            asyncio.create_task(self.bus.run(self.stream)),
            asyncio.create_task(self._receive_uart()),
        )
        for worker in workers:
            worker.add_done_callback(self._worker_done)

        try:
            while self.worker_error is None:
                first = await self.queue.get()
                if first is not None:
                    yield (first, *self._drain(MAX_RX_PER_POLL - 1))
        finally:
            self.stop_inputs()
            await asyncio.gather(*workers, return_exceptions=True)
            if self.worker_error is not None:
                raise self.worker_error
