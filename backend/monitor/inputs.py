"""통합 서비스에 시간순 예제 또는 수신 장치를 연결한다."""

import asyncio
import time

from ..diagnostics.journal import iter_inputs
from ..hardware.socketcan import CAN_ERR_BUSOFF
from ..tcp_stream import SNAPSHOT_INTERVAL

NS_PER_SECOND = 1_000_000_000


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

    def __init__(self, devices, readers):
        self.devices = devices
        self.readers = readers

    def idle_records(self):
        return ((self.now_ns(), "service", "clock"),)

    async def records(self):
        loop = asyncio.get_running_loop()
        active = self.devices.copy()
        ready = asyncio.Queue()

        def notify(source, fd):
            # 소비자가 읽을 때까지 같은 장치의 알림을 하나만 보관한다.
            loop.remove_reader(fd)
            ready.put_nowait(source)

        def watch(source, fd):
            loop.add_reader(fd, notify, source, fd)

        try:
            for source, fd in active.items():
                watch(source, fd)
            while active:
                source = await ready.get()
                fd = active[source]
                try:
                    payload = self.readers[source](fd)
                except BlockingIOError:
                    payload = None
                except (OSError, EOFError, ValueError):
                    del active[source]
                    yield ((self.now_ns(), "service", f"{source}_disconnect"),)
                    continue

                if payload is not None:
                    now = self.now_ns()
                    entry = (now, source, payload)
                    if (
                        source == "can"
                        and payload.error
                        and payload.can_id & CAN_ERR_BUSOFF
                    ):
                        del active[source]
                        # 종료 요청이 와도 원본과 그 단절을 함께 기록한다.
                        yield entry, (now, "service", "can_disconnect")
                        continue
                    yield (entry,)
                watch(source, fd)
        finally:
            for fd in active.values():
                loop.remove_reader(fd)
