"""통합 서비스에 시간순 예제 또는 수신 장치를 연결한다."""

import asyncio
import logging
import termios
import time
from contextlib import ExitStack
from dataclasses import dataclass

from ..can_rx.state import STATE_LIFETIME
from ..diagnostics.journal import iter_inputs
from ..fpga_uart.session import STATUS_LIFETIME_NS
from ..hardware.socketcan import (
    CAN_ERR_BUSOFF,
    CanDropCounter,
    CanRead,
    can_error_reason,
)
from ..hardware.uart import uart_error_reason
from ..tcp_stream import SNAPSHOT_INTERVAL

NS_PER_SECOND = 1_000_000_000
CAN_RETRY_DELAYS = (1.0, 2.0, 4.0)
FPGA_RETRY_DELAYS = CAN_RETRY_DELAYS
DEVICE_STABLE_NS = 60 * NS_PER_SECOND
LOGGER = logging.getLogger(__name__)


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
            yield ((record.rx_mono_ns, record.source, record.payload, record.detail),)


@dataclass
class RetryState:
    attempts: int = 0
    fresh_window: tuple[int, int] | None = None

    def observe(self, observed_ns, lifetime):
        if not self.attempts:
            return
        if observed_ns is None:
            self.fresh_window = None
            return

        first, last = self.fresh_window or (observed_ns, observed_ns)
        if observed_ns - last >= lifetime:
            first = observed_ns
        self.fresh_window = first, observed_ns
        if observed_ns - first >= DEVICE_STABLE_NS:
            self.attempts = 0
            self.fresh_window = None


class DeviceFeed:
    now_ns = staticmethod(time.monotonic_ns)

    def __init__(self, devices, readers, *, can_open=None, fpga_open=None):
        self.openers = {
            source: opener
            for source, opener in (("can", can_open), ("fpga", fpga_open))
            if opener is not None
        }
        if self.openers.keys() & devices.keys():
            raise ValueError("같은 장치의 포트와 열기 함수를 함께 지정할 수 없습니다")
        self.devices = devices
        self.readers = readers
        self.retry_states = {source: RetryState() for source in self.openers}

    def idle_records(self):
        return ((self.now_ns(), "service", "clock"),)

    def observe_devices(self, snapshot):
        now = snapshot.logical_ns
        if (retry := self.retry_states.get("can")) and retry.attempts:
            state = snapshot.can.last_state_ns
            if snapshot.can.at(now)["motor"]["link"] != "fresh":
                state = None
            retry.observe(state, STATE_LIFETIME)

        if (retry := self.retry_states.get("fpga")) and retry.attempts:
            last = snapshot.fpga_last_ns
            fresh = (
                snapshot.fpga_connected
                and not snapshot.fpga_degraded
                and last is not None
                and now - last < STATUS_LIFETIME_NS
            )
            retry.observe(last if fresh else None, STATUS_LIFETIME_NS)

    async def records(self):
        loop = asyncio.get_running_loop()
        active = self.devices.copy()
        ready = asyncio.Queue()
        timers = {}
        owners = {}
        drops = CanDropCounter()
        delays = {"can": CAN_RETRY_DELAYS, "fpga": FPGA_RETRY_DELAYS}

        def notify(source, fd):
            # 장치마다 읽기 알림을 하나만 대기시킨다.
            loop.remove_reader(fd)
            ready.put_nowait((source, False))

        def watch(source, fd):
            loop.add_reader(fd, notify, source, fd)

        def notice(source, event, **detail):
            return self.now_ns(), "service", f"{source}_{event}", detail

        def schedule(source):
            retry = self.retry_states.get(source)
            if retry is not None and retry.attempts < len(delays[source]):
                timers[source] = loop.call_later(
                    delays[source][retry.attempts], ready.put_nowait, (source, True)
                )

        def open_device(source):
            with ExitStack() as stack:
                fd = stack.enter_context(self.openers[source]())
                watch(source, fd)
                active[source] = fd
                owners[source] = stack.pop_all()

        def close_owner(owner):
            try:
                owner.close()
            except termios.error as exc:
                LOGGER.warning("FPGA UART 포트 설정 복구 실패: %s", exc)

        def disconnect(source):
            active.pop(source)
            if source in owners:
                self.retry_states[source].fresh_window = None
                close_owner(owners.pop(source))
                schedule(source)

        def open_reason(source, error):
            if source == "can":
                return can_error_reason(error, invalid="INVALID_INTERFACE")
            return uart_error_reason(error)

        try:
            for source, fd in active.items():
                watch(source, fd)
            for source in self.openers:
                try:
                    open_device(source)
                except (OSError, ValueError, termios.error) as exc:
                    schedule(source)
                    yield (
                        notice(source, "disconnect", reason=open_reason(source, exc)),
                    )

            while active or timers:
                source, retrying = await ready.get()
                if retrying:
                    del timers[source]
                    retry = self.retry_states[source]
                    retry.attempts += 1
                    try:
                        open_device(source)
                    except (OSError, ValueError, termios.error) as exc:
                        schedule(source)
                        yield (
                            notice(
                                source,
                                "retry_failed",
                                attempt=retry.attempts,
                                reason=open_reason(source, exc),
                            ),
                        )
                    else:
                        if source == "can":
                            drops = CanDropCounter()
                        yield (notice(source, "reconnect", attempt=retry.attempts),)
                    continue

                fd = active[source]
                try:
                    payload = self.readers[source](fd)
                except BlockingIOError:
                    payload = None
                except (OSError, EOFError, ValueError) as exc:
                    disconnect(source)
                    reason = (
                        can_error_reason(exc)
                        if source == "can"
                        else uart_error_reason(exc)
                    )
                    yield (notice(source, "disconnect", reason=reason),)
                    continue

                now = self.now_ns()
                batch = []
                if isinstance(payload, CanRead):
                    if delta := drops.observe(payload.dropped_total):
                        batch.append(
                            (now, "service", "can_rx_overflow", {"dropped": delta})
                        )
                    if payload.error is not None:
                        disconnect(source)
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
                        disconnect(source)
                        batch.append(
                            (now, "service", "can_disconnect", {"reason": "BUS_OFF"})
                        )
                        yield tuple(batch)
                        continue

                if batch:
                    yield tuple(batch)
                watch(source, fd)
        finally:
            for timer in timers.values():
                timer.cancel()
            with ExitStack() as cleanup:
                for owner in owners.values():
                    cleanup.callback(close_owner, owner)
                for fd in active.values():
                    loop.remove_reader(fd)
