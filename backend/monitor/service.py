"""수신·판정·기록을 직렬 처리하고 TCP에 상태를 게시한다."""

import asyncio
import logging
import time

from ..diagnostics.inputs import Input
from ..diagnostics.journal import record_input
from ..diagnostics.storage import RecordingFailure
from ..tcp_stream import SNAPSHOT_INTERVAL
from .tcp import MonitorStream, MonitorView

LOGGER = logging.getLogger(__name__)


class Monitor:
    def __init__(self, journal, config, feed):
        self.journal = journal
        self.engine = journal.new_engine(config)
        self.feed = feed
        self.observe_devices = getattr(feed, "observe_devices", None)
        self.fpga_last_ns = None
        self.fpga_degraded = False
        self.events = []
        self.journal.on_event = self._remember_event
        self.finished = False
        self.batch_end_seq = 0
        self.unattempted_records = 0
        self.abandoned_records = 0
        self.stream = MonitorStream(
            journal.backend_session, journal.manifest["source"], self._view()
        )
        if attach := getattr(feed, "attach_control", None):
            attach(self.stream)

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
            fpga_last_ns=(
                self.engine.fpga_status_ns
                if self.engine.fpga_status_watchdog
                else self.fpga_last_ns
            ),
            healthy_recording=(
                self.engine.healthy_recording
                and self.engine.low_space_record_id is None
            ),
        )

    def _consume(self, batch):
        self._consume_records(batch)
        return self._publication()

    def _batch_inputs(self, batch):
        for seq, (now, source, payload, *detail) in enumerate(
            batch, self.engine.ingest_seq + 1
        ):
            yield Input(
                self.journal.evidence_session,
                seq,
                now,
                source,
                payload,
                *detail,
            )

    def _consume_records(self, batch):
        self.batch_end_seq = self.engine.ingest_seq + len(batch)
        self.unattempted_records = len(batch)
        if not self.finished and (event := self.journal.check_space()) is not None:
            now = batch[0][0] if batch else self.engine.now_ns
            self.batch_end_seq += 1
            notice = (now, "service", event, {"free_bytes": self.journal.free_bytes})
            (record,) = self._batch_inputs((notice,))
            record_input(self.journal, self.engine, record)
        for record in self._batch_inputs(batch):
            self.unattempted_records -= 1
            received, _ = record_input(self.journal, self.engine, record)
            if record.source == "fpga":
                for observation in received:
                    if observation.frame is not None and not observation.duplicate:
                        self.fpga_last_ns = record.rx_mono_ns
                    if not observation.duplicate:
                        self.fpga_degraded = bool(observation.issues)
            elif record.source == "service" and record.payload == "fpga_reconnect":
                self.fpga_last_ns = None
                self.fpga_degraded = False

    def _save_remaining_inputs(self, batch):
        # 수집 장애로 중단된 기록에는 판정을 추가하지 않는다.
        self.batch_end_seq = self.engine.ingest_seq + len(batch)
        self.unattempted_records = len(batch)
        for record in self._batch_inputs(batch):
            self.unattempted_records -= 1
            self.journal.input(record, None, None)
        self.journal.flush()

    def _publication(self):
        snapshot = self._view()
        events, self.events = self.events, []
        return snapshot, events

    def _finish(self, batch=()):
        self._consume_records(batch)
        self.journal.finish(self.engine)
        return self._publication()

    def _recording_failure(self, error):
        self.engine.fail_recording(self.journal.last_saved_run_id)
        self.events.clear()
        result = self.engine.latest_result
        if result is not None and result.get("evidence_saved") is not False:
            result = None
        outside_batch = self.abandoned_records + getattr(
            self.feed, "dropped_records", 0
        )
        dropped = self.unattempted_records + outside_batch
        report = {
            "reason": "RECORDING_FAILED",
            "error": str(error),
            "dropped_records": dropped,
            "unconfirmed_records": (
                max(self.batch_end_seq, self.journal.last_input_seq)
                - self.journal.durable_input_seq
                + outside_batch
            ),
            "result": result,
        }
        LOGGER.error(
            "기록 중단: %s (미기록 %d건, 저장 미확인 %d건)",
            error,
            dropped,
            report["unconfirmed_records"],
        )
        return self._view(), report

    async def _write(self, work, *args):
        task = asyncio.create_task(asyncio.to_thread(work, *args))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # 쓰기 작업이 끝난 뒤에만 호출자가 파일을 닫을 수 있다.
            await task
            raise

    async def _publish_from(self, work, *args):
        try:
            snapshot, events = await self._write(work, *args)
        except OSError as exc:
            raise RecordingFailure(str(exc)) from exc
        if self.observe_devices is not None:
            self.observe_devices(snapshot)
        self.stream.recording = (
            {"reason": "RECORDING_SPACE_LOW", "free_bytes": self.journal.free_bytes}
            if self.journal.space_low
            else None
        )
        self.stream.publish(snapshot, events)

    async def run(self, stop):
        try:
            await self._run(stop)
        except RecordingFailure as exc:
            if final_records := getattr(self.feed, "final_records", None):
                self.abandoned_records += len(final_records())
            snapshot, report = self._recording_failure(exc)
            self.stream.recording = {
                key: report[key]
                for key in ("reason", "dropped_records", "unconfirmed_records")
            }
            self.stream.publish(snapshot, ())
            try:
                await self._write(self.journal.report_failure, report)
            except OSError:
                # 원래 기록 장치가 가득 차면 TCP와 시스템 로그에 상태를 남긴다.
                LOGGER.warning("기록 실패 보고서를 저장하지 못했습니다", exc_info=True)
            raise

    async def _run(self, stop):
        records = self.feed.records()
        read = None if stop.is_set() else asyncio.create_task(anext(records))
        stopping = asyncio.create_task(self._watch_stop(stop))
        active_error = None
        failed_batch = ()
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
                        if worker_error := getattr(self.feed, "worker_error", None):
                            # 큐에서 꺼낸 입력도 판정 전에 수집 실패를 확인한다.
                            failed_batch = batch
                            raise worker_error
                        await self._publish_from(self._consume, batch)
                        if not stop.is_set():
                            read = asyncio.create_task(anext(records))
                elif stop.is_set():
                    break
                else:
                    batch = () if self.finished else self.feed.idle_records()
                    await self._publish_from(self._consume, batch)
        except BaseException as exc:
            active_error = exc
            raise
        finally:
            tasks = (read, stopping) if read is not None else (stopping,)
            for task in tasks:
                task.cancel()
            outcome = await asyncio.gather(*tasks, return_exceptions=True)
            if (
                isinstance(active_error, RecordingFailure)
                and read is not None
                and not isinstance(outcome[0], BaseException)
            ):
                self.abandoned_records += len(outcome[0])
            await self._close_inputs(records, active_error, failed_batch)

        if not self.finished:
            final_records = getattr(self.feed, "final_records", None)
            if final_records is None:
                await self._publish_from(self._finish)
            else:
                await self._publish_from(self._finish, final_records())
            self.finished = True

    async def _close_inputs(self, records, active_error, failed_batch):
        close_error = None
        try:
            await records.aclose()
        except Exception as exc:
            close_error = exc

        worker_error = getattr(self.feed, "worker_error", None)
        error = active_error or close_error or worker_error
        if error is not None and error is worker_error:
            try:
                batch = (*failed_batch, *self.feed.final_records())
                await self._write(self._save_remaining_inputs, batch)
            except OSError as exc:
                raise RecordingFailure(
                    f"{error}; 종료 원본 저장 실패: {exc}"
                ) from error
            if getattr(self.feed, "dropped_records", 0) and not isinstance(
                error, RecordingFailure
            ):
                raise RecordingFailure(f"{error}; 기록 큐 한도 초과") from error
        if close_error is not None and close_error is not error:
            LOGGER.warning("수집 입력 종료 실패: %s", close_error)
        if active_error is None and error is not None:
            raise error

    async def _watch_stop(self, stop):
        await stop.wait()
        if stop_inputs := getattr(self.feed, "stop_inputs", None):
            stop_inputs()
