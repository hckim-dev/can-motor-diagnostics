"""수신·판정·기록을 직렬 처리하고 TCP에 읽기 전용 상태를 게시한다."""

import asyncio
import time

from ..diagnostics.inputs import Input
from ..diagnostics.journal import record_input
from ..tcp_stream import SNAPSHOT_INTERVAL
from .tcp import MonitorStream, MonitorView


class Monitor:
    def __init__(self, journal, config, feed):
        self.journal = journal
        self.engine = journal.new_engine(config)
        self.feed = feed
        self.fpga_last_ns = None
        self.fpga_degraded = False
        self.events = []
        self.journal.on_event = self._remember_event
        self.finished = False
        self.stream = MonitorStream(
            journal.backend_session, journal.manifest["source"], self._view()
        )

    def _remember_event(self, event):
        context = event["context"]
        # 전체 증거 목록은 디스크에 보존하고 TCP에는 event_id와 판정 요약을 보낸다.
        self.events.append(
            {
                "event_id": event["event_id"],
                "category": event["category"],
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
        )

    def _view(self):
        wall_ns = time.monotonic_ns()
        result = self.engine.snapshot()
        uart = self.engine.uart
        return MonitorView(
            can=self.engine.can.capture_view(),
            logical_ns=max(self.engine.now_ns, self.feed.now_ns()),
            wall_ns=wall_ns,
            test={
                "run_id": result.get("run_id"),
                "scenario_id": result.get("scenario_id"),
                "status": result["result"],
                "reason": result.get("reason"),
            },
            fpga_connected=uart.connected,
            fpga_degraded=self.fpga_degraded or uart.quarantined,
            fpga_last_ns=self.fpga_last_ns,
            healthy_recording=self.engine.healthy_recording,
        )

    def _consume(self, batch):
        for now, source, payload in batch:
            record = Input(
                self.journal.evidence_session,
                self.engine.ingest_seq + 1,
                now,
                source,
                payload,
            )
            received, _ = record_input(self.journal, self.engine, record)
            if source == "fpga":
                for observation in received:
                    if observation.frame is not None and not observation.duplicate:
                        self.fpga_last_ns = now
                    if not observation.duplicate:
                        self.fpga_degraded = bool(observation.issues)
            elif source == "service" and payload == "fpga_reconnect":
                self.fpga_last_ns = None
                self.fpga_degraded = False
        return self._publication()

    def _publication(self):
        snapshot = self._view()
        events, self.events = self.events, []
        return snapshot, events

    def _finish(self):
        self.journal.finish(self.engine)
        return self._publication()

    async def _publish_from(self, work, *args):
        task = asyncio.create_task(asyncio.to_thread(work, *args))
        try:
            snapshot, events = await asyncio.shield(task)
        except asyncio.CancelledError:
            # 쓰기 작업이 끝난 뒤에만 호출자가 파일을 닫을 수 있다.
            await task
            raise
        self.stream.publish(snapshot, events)

    async def run(self, stop):
        records = self.feed.records()
        read = None if stop.is_set() else asyncio.create_task(anext(records))
        stopping = asyncio.create_task(stop.wait())
        try:
            while not stop.is_set() or (read is not None and read.done()):
                waiting = (read, stopping) if read is not None else (stopping,)
                await asyncio.wait(
                    waiting,
                    timeout=SNAPSHOT_INTERVAL,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                # 종료와 동시에 수신이 끝났으면 읽은 기록까지 마감한다.
                if read is not None and read.done():
                    completed, read = read, None
                    try:
                        batch = completed.result()
                    except StopAsyncIteration:
                        await self._publish_from(self._finish)
                        self.finished = True
                    else:
                        await self._publish_from(self._consume, batch)
                        if not stop.is_set():
                            read = asyncio.create_task(anext(records))
                elif stop.is_set():
                    break
                else:
                    batch = () if self.finished else self.feed.idle_records()
                    await self._publish_from(self._consume, batch)
        finally:
            tasks = (read, stopping) if read is not None else (stopping,)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await records.aclose()

        if not self.finished:
            await self._publish_from(self._finish)
            self.finished = True
