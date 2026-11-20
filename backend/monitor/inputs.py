"""통합 서비스에 시간순 예제 또는 수신 장치를 연결한다."""

import asyncio
import time

from ..can_rx.state import STATE_LIFETIME
from ..diagnostics.journal import iter_inputs
from ..hardware.socketcan import (
    CAN_ERR_BUSOFF,
    CanDropCounter,
    CanRead,
    can_error_reason,
)
from ..tcp_stream import SNAPSHOT_INTERVAL

NS_PER_SECOND = 1_000_000_000
CAN_RETRY_DELAYS = (1.0, 2.0, 4.0)
CAN_STABLE_NS = 60 * NS_PER_SECOND


class FixtureFeed:
    def __init__(self, file, session):
        self.file = file
        self.session = session
        self.origin_ns = None
        self.started_ns = None

    def now_ns(self):
        if self.origin_ns is None:
            return 0
        return self.origin_ns + time.monotonic_ns() - self.started_ns

    def idle_records(self):
        return ()

    async def records(self):
        previous_ns = 0
        for record, _ in iter_inputs(self.file, self.session):
            if record.rx_mono_ns < previous_ns:
                raise ValueError("예제 입력 시각이 역행합니다")
            previous_ns = record.rx_mono_ns
            if self.origin_ns is None:
                self.origin_ns = record.rx_mono_ns
                self.started_ns = time.monotonic_ns()
            while (remaining := record.rx_mono_ns - self.now_ns()) > 0:
                await asyncio.sleep(min(remaining / NS_PER_SECOND, SNAPSHOT_INTERVAL))
            yield ((record.rx_mono_ns, record.source, record.payload),)


class DeviceFeed:
    now_ns = staticmethod(time.monotonic_ns)

    def __init__(self, devices, readers, *, can_open=None):
        if can_open is not None and "can" in devices:
            raise ValueError("CAN 소켓과 소켓 열기 함수를 함께 지정할 수 없습니다")
        self.devices = devices
        self.readers = readers
        self.can_open = can_open
        self.can_attempts = 0
        self.can_fresh_window = None

    def idle_records(self):
        return ((self.now_ns(), "service", "clock"),)

    def observe_can(self, snapshot):
        if not self.can_attempts:
            return

        now = snapshot.logical_ns
        if snapshot.can.at(now)["motor"]["link"] != "fresh":
            self.can_fresh_window = None
            return

        state_ns = snapshot.can.last_state_ns
        first, last = self.can_fresh_window or (state_ns, state_ns)
        if state_ns - last >= STATE_LIFETIME:
            first = state_ns
        self.can_fresh_window = first, state_ns
        if state_ns - first >= CAN_STABLE_NS:
            self.can_attempts = 0
            self.can_fresh_window = None

    async def records(self):
        loop = asyncio.get_running_loop()
        active = self.devices.copy()
        ready = asyncio.Queue()
        retry = None
        drops = CanDropCounter()

        def notify(source, fd):
            # 장치마다 읽기 알림을 하나만 대기시킨다.
            loop.remove_reader(fd)
            ready.put_nowait(source)

        def watch(source, fd):
            loop.add_reader(fd, notify, source, fd)

        def notice(event, **detail):
            return self.now_ns(), "service", event, detail

        def schedule_retry():
            if self.can_open is None or self.can_attempts >= len(CAN_RETRY_DELAYS):
                return None
            delay = CAN_RETRY_DELAYS[self.can_attempts]
            return loop.call_later(delay, ready.put_nowait, "retry_can")

        def open_can():
            fd = self.can_open()
            try:
                watch("can", fd)
            except BaseException:
                fd.close()
                raise
            active["can"] = fd

        def disconnect(source):
            fd = active.pop(source)
            if source == "can":
                self.can_fresh_window = None
                if self.can_open is not None:
                    fd.close()
                return schedule_retry()
            return None

        try:
            for source, fd in active.items():
                watch(source, fd)
            if self.can_open is not None:
                try:
                    open_can()
                except OSError as exc:
                    retry = schedule_retry()
                    yield (notice("can_disconnect", reason=can_error_reason(exc)),)

            while active or retry is not None:
                source = await ready.get()
                if source == "retry_can":
                    retry = None
                    self.can_attempts += 1
                    try:
                        open_can()
                    except (OSError, ValueError) as exc:
                        retry = schedule_retry()
                        reason = can_error_reason(exc, invalid="INVALID_INTERFACE")
                        yield (
                            notice(
                                "can_retry_failed",
                                attempt=self.can_attempts,
                                reason=reason,
                            ),
                        )
                    else:
                        drops = CanDropCounter()
                        yield (notice("can_reconnect", attempt=self.can_attempts),)
                    continue

                fd = active[source]
                try:
                    payload = self.readers[source](fd)
                except BlockingIOError:
                    payload = None
                except (OSError, EOFError, ValueError) as exc:
                    scheduled = disconnect(source)
                    if source == "can":
                        retry = scheduled
                        yield (notice("can_disconnect", reason=can_error_reason(exc)),)
                    else:
                        yield ((self.now_ns(), "service", "fpga_disconnect"),)
                    continue

                now = self.now_ns()
                batch = []
                if isinstance(payload, CanRead):
                    if delta := drops.observe(payload.dropped_total):
                        batch.append(
                            (now, "service", "can_rx_overflow", {"dropped": delta})
                        )
                    if payload.error is not None:
                        retry = disconnect(source)
                        batch.append(
                            (
                                now,
                                "service",
                                "can_disconnect",
                                {"reason": can_error_reason(payload.error)},
                            )
                        )
                        yield tuple(batch)
                        continue
                    payload = payload.frame

                if payload is not None:
                    batch.append((now, source, payload))
                    if (
                        source == "can"
                        and payload.error
                        and payload.can_id & CAN_ERR_BUSOFF
                    ):
                        retry = disconnect(source)
                        batch.append(
                            (now, "service", "can_disconnect", {"reason": "BUS_OFF"})
                        )
                        yield tuple(batch)
                        continue

                if batch:
                    yield tuple(batch)
                watch(source, fd)
        finally:
            if retry is not None:
                retry.cancel()
            for fd in active.values():
                loop.remove_reader(fd)
            if self.can_open is not None and "can" in active:
                active["can"].close()
