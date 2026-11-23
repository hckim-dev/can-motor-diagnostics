"""Python TCP 클라이언트로 통합 수신·판정·기록을 검증한다."""

import asyncio
import csv
import errno
import json
import os
import pty
import signal
import socket
import sys
import tempfile
import threading
import unittest
import uuid
from ast import literal_eval
from contextlib import ExitStack, asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from check_diagnostics import scenario_rows, service
from check_measurement_recording import EPOCH, MS, hello, sample, state

from backend.can_control.wire import encode_operator_hello, encode_permit
from backend.can_rx.codec import Frame, Mode, State
from backend.diagnostics.inputs import Input
from backend.diagnostics.journal import Journal, evaluate, load_manifest
from backend.diagnostics.storage import RecordedLog
from backend.diagnostics.verdict import Config
from backend.hardware.socketcan import (
    CAN_ERR_BUSOFF,
    CAN_ERR_FLAG,
    CAN_FRAME,
    read_external_frame,
)
from backend.hardware.uart import open_uart, read_uart
from backend.monitor.inputs import DeviceFeed, FixtureFeed
from backend.monitor.service import Monitor
from backend.monitor.tcp import MonitorStream
from backend.tcp_stream import MAX_LINE_BYTES

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "diagnostic-no-edge.jsonl"


def rows_in(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def save_fixture(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


async def read_frame(reader, timeout=5):
    line = await asyncio.wait_for(reader.readline(), timeout)
    if not line.endswith(b"\n") or len(line) > MAX_LINE_BYTES:
        raise AssertionError("완전한 TCP 메시지를 받지 못했습니다")
    return json.loads(line)


async def observe(reader, predicate):
    async def wait():
        while True:
            message = await read_frame(reader)
            if predicate(message):
                return message

    return await asyncio.wait_for(wait(), 5)


async def snapshot_where(reader, predicate):
    message = await observe(
        reader, lambda msg: msg["type"] == "snapshot" and predicate(msg["data"])
    )
    return message["data"]


def is_test_event(message):
    return message["type"] == "event" and message["data"]["category"] == "test"


@asynccontextmanager
async def connected(port):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        yield reader, writer
    finally:
        writer.close()
        await writer.wait_closed()


@asynccontextmanager
async def running(monitor):
    stop = asyncio.Event()
    listener = await asyncio.start_server(
        monitor.stream.handle_client, "127.0.0.1", 0, limit=MAX_LINE_BYTES
    )
    async with listener:
        worker = asyncio.create_task(monitor.run(stop))
        try:
            yield listener.sockets[0].getsockname()[1]
        finally:
            try:
                stop.set()
                await asyncio.wait_for(worker, 5)
            finally:
                monitor.stream.close()


class SnapshotFreshnessTest(unittest.TestCase):
    def test_notification_drain_leaves_new_events_for_next_snapshot(self):
        stream = MonitorStream(str(uuid.uuid4()), "mock", None)
        original = {"code": "FIRST"}
        later = {"code": "LATER"}
        stream.events.append(original)
        pending = stream.notifications()
        self.assertEqual(next(pending), ("event", original))
        stream.events.append(later)
        self.assertEqual(list(pending), [])
        self.assertEqual(list(stream.notifications()), [("event", later)])

    def test_tcp_ages_each_can_field_while_worker_has_no_new_publication(self):
        now_ns = 190 * MS
        config = Config()
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("time.monotonic_ns", side_effect=lambda: now_ns),
            Journal(Path(directory) / "run", config) as journal,
        ):
            feed = DeviceFeed({}, {})
            feed.now_ns = lambda: now_ns
            monitor = Monitor(journal, config, feed)
            rows = [hello(0), state(100, State.RUNNING), *sample(100, 1)]
            batch = []
            for i, row in enumerate(rows, 1):
                record = Input.from_row(row, journal.evidence_session, i)
                batch.append((record.rx_mono_ns, record.source, record.payload))
            batch.insert(1, (0, "can", encode_operator_hello(1)))
            batch.insert(2, (0, "can", encode_permit(EPOCH, 0, ready=True)))
            monitor.stream.publish(*monitor._consume(batch))

            initial = monitor.stream.snapshot(None, 0)
            self.assertTrue(initial["operator"]["controls_ready"])
            self.assertEqual(initial["motor"]["sample"]["bus_mv"], 6000)

            now_ns = 250 * MS
            snapshot = monitor.stream.snapshot(None, 0)
            self.assertIsNone(snapshot["operator"]["controls_ready"])
            self.assertEqual(snapshot["motor"]["state"], "RUNNING")

            now_ns = 300 * MS
            snapshot = monitor.stream.snapshot(None, 0)
            self.assertIsNone(snapshot["motor"]["sample"]["bus_mv"])
            self.assertEqual(snapshot["motor"]["sample"]["actual_rpm"], 59)

            now_ns = 400 * MS
            snapshot = monitor.stream.snapshot(None, 0)
            self.assertEqual(snapshot["motor"]["state"], "UNKNOWN")
            self.assertEqual(snapshot["motor"]["age_ms"], 300)
            monitor._finish()


class MonitorTest(unittest.IsolatedAsyncioTestCase):
    async def test_low_space_blocks_new_verdict_even_after_recovery_and_replay(self):
        for link_evidence in (False, True):
            with self.subTest(link_evidence=link_evidence):
                await self.verify_low_space_replay(link_evidence)

    async def verify_low_space_replay(self, link_evidence):
        rows = scenario_rows()
        config = Config(gpio_verified=True)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "run"
            with Journal(output, config, low_space_bytes=1000) as journal:
                if not link_evidence:
                    journal.manifest.pop("recording_policy_version")
                monitor = Monitor(journal, config, DeviceFeed({}, {}))
                batch = []
                for index, row in enumerate(rows, 1):
                    record = Input.from_row(row, journal.evidence_session, index)
                    batch.append(
                        (
                            record.rx_mono_ns,
                            record.source,
                            record.payload,
                            record.detail,
                        )
                    )

                with (
                    self.assertLogs("backend.diagnostics.journal", "WARNING"),
                    patch(
                        "backend.diagnostics.journal.shutil.disk_usage",
                        side_effect=[
                            SimpleNamespace(free=1000),
                            SimpleNamespace(free=1001),
                        ],
                    ),
                    patch(
                        "backend.diagnostics.journal.monotonic_ns",
                        side_effect=[0, 1_000_000_000],
                    ),
                ):
                    await monitor._publish_from(monitor._consume, batch[:5])
                    self.assertEqual(
                        monitor.stream.recording["reason"], "RECORDING_SPACE_LOW"
                    )
                    snapshot = monitor.stream.snapshot(None, None)
                    notices = {
                        data["name"]: data
                        for kind, data in monitor.stream.snapshot_notifications(
                            snapshot
                        )
                        if kind == "service"
                    }
                    self.assertEqual(notices["log"]["status"], "DEGRADED")
                    self.assertEqual(notices["log"]["reason"], "RECORDING_SPACE_LOW")
                    await monitor._publish_from(monitor._finish, batch[5:])
                    self.assertIsNone(monitor.stream.recording)

            (event,) = rows_in(output / "events.jsonl")
            self.assertEqual(event["code"], "INCONCLUSIVE")
            self.assertEqual(event["context"]["reason"], "RECORDING_SPACE_LOW")
            (warning,) = [
                row
                for row in rows_in(output / "inputs.jsonl")
                if row.get("event") == "recording_space_low"
            ]
            self.assertEqual(
                warning["record_id"] in event["context"]["evidence_ids"], link_evidence
            )
            manifest, replay_config = load_manifest(output)
            source = RecordedLog(output, "inputs.jsonl", manifest)
            with Journal(
                output.with_name("replay"), replay_config, original=manifest
            ) as journal:
                self.assertEqual(
                    evaluate(source, journal, replay_config), [event["context"]]
                )

    async def test_fixture_cli_connects_tcp_to_record_and_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            fixture, output, replay = (
                base / name for name in ("fixture", "run", "replay")
            )
            rows = scenario_rows()
            telemetry = sample(50, 1, motor_state=State.TEST_ARMED, mode=Mode.TEST)
            rows = [*rows[:5], *telemetry, *rows[5:]]
            disconnect_detail = {"reason": "IO_ERROR"}
            rows += [
                service(2900, "can_disconnect"),
                {**service(3000, "fpga_disconnect"), "detail": disconnect_detail},
            ]
            save_fixture(fixture, rows)
            command = [
                sys.executable,
                "-X",
                "dev",
                "-W",
                "error",
                "-m",
                "backend.monitor",
                str(output),
                "--fixture",
                str(fixture),
                "--gpio-verified",
                "--port",
                "0",
            ]
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=ROOT,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stopping = False
            try:
                announcement = await asyncio.wait_for(process.stdout.readline(), 5)
                self.assertIn(b"listening on ", announcement)
                _, port = literal_eval(
                    announcement.decode().split("listening on ", 1)[1]
                )
                async with connected(port) as (reader, writer):
                    hello_msg = await read_frame(reader)
                    self.assertEqual(
                        (
                            hello_msg["schema"],
                            hello_msg["type"],
                            hello_msg["stream_seq"],
                        ),
                        ("can-do/1.1", "hello", 0),
                    )
                    self.assertEqual(hello_msg["source"], "mock")
                    self.assertEqual((await read_frame(reader))["type"], "snapshot")
                    services = [await read_frame(reader) for _ in range(4)]
                    self.assertTrue(all(msg["type"] == "service" for msg in services))
                    self.assertEqual(
                        {msg["data"]["name"] for msg in services},
                        {"can", "fpga", "log", "ai"},
                    )
                    writer.write(
                        (
                            json.dumps(
                                {
                                    "schema": "can-do/1.1",
                                    "type": "control_request",
                                    "data": {
                                        "stream_id": hello_msg["stream_id"],
                                        "request_id": str(uuid.uuid4()),
                                        "opcode": "ARM_TEST",
                                        "argument": 0,
                                    },
                                }
                            )
                            + "\n"
                        ).encode()
                    )
                    await writer.drain()
                    result = await observe(
                        reader, lambda msg: msg["type"] == "control_result"
                    )
                    self.assertEqual(result["data"]["reason"], "READ_ONLY")
                    running_view = await snapshot_where(
                        reader,
                        lambda snap: (
                            snap["test"]["status"] == "RUNNING"
                            and snap["motor"]["sample"]["actual_rpm"] == 59
                        ),
                    )
                    run_id = running_view["test"]["run_id"]
                    fault = await observe(
                        reader,
                        lambda msg: (
                            msg["type"] == "event"
                            and msg["data"]["code"] == "MOTOR_FAULT_CHANGED"
                        ),
                    )
                    self.assertEqual(fault["data"]["category"], "fault")
                    self.assertEqual(fault["data"]["context"]["fault_mask"], 4)
                    event = await observe(reader, is_test_event)
                    self.assertEqual(event["data"]["code"], "PASS")
                    self.assertEqual(event["data"]["context"]["run_id"], run_id)
                    self.assertTrue(event["data"]["context"]["evidence_saved"])
                    stale = await snapshot_where(
                        reader, lambda snap: snap["motor"]["link"] == "stale"
                    )
                    self.assertEqual(stale["motor"]["state"], "UNKNOWN")
                    link = await observe(
                        reader,
                        lambda msg: (
                            msg["type"] == "event"
                            and msg["data"]["code"] == "CAN_DISCONNECT"
                        ),
                    )
                    self.assertEqual(link["data"]["category"], "link")
                    offline = await snapshot_where(
                        reader,
                        lambda snap: (
                            snap["services"]["can"] == "OFFLINE"
                            and snap["services"]["fpga"] == "OFFLINE"
                        ),
                    )
                    self.assertEqual(offline["test"]["status"], "PASS")

                await asyncio.sleep(0.05)
                async with connected(port) as (reader, _):
                    reconnect = await read_frame(reader)
                    self.assertNotEqual(reconnect["stream_id"], hello_msg["stream_id"])
                    self.assertEqual(
                        reconnect["data"]["backend_session"],
                        hello_msg["data"]["backend_session"],
                    )
                    self.assertEqual(
                        (await read_frame(reader))["data"]["test"]["run_id"], run_id
                    )
                    await snapshot_where(
                        reader, lambda snap: snap["test"]["run_id"] == run_id
                    )
                    process.send_signal(signal.SIGTERM)
                    stopping = True
                    await asyncio.wait_for(reader.read(), 5)
                    self.assertTrue(reader.at_eof())
            finally:
                if process.returncode is None and not stopping:
                    process.send_signal(signal.SIGTERM)
                _, errors = await asyncio.wait_for(process.communicate(), 5)
            self.assertEqual(process.returncode, 0, errors.decode())
            (saved,) = rows_in(output / "events.jsonl")
            self.assertEqual(saved["event_id"], event["data"]["event_id"])
            (disconnect,) = (
                row
                for row in rows_in(output / "inputs.jsonl")
                if row.get("event") == "fpga_disconnect"
            )
            self.assertEqual(disconnect.get("detail"), disconnect_detail)
            self.assertEqual(
                rows_in(output / "manifest.json")[0]["backend_session"],
                hello_msg["data"]["backend_session"],
            )
            with (output / "samples.csv").open(newline="", encoding="utf-8") as file:
                (row,) = csv.DictReader(file)
            self.assertEqual(row["run_id"], run_id)

            replay_process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-X",
                "dev",
                "-W",
                "error",
                "-m",
                "backend.diagnostics",
                "replay",
                str(output),
                str(replay),
                "--fast",
                cwd=ROOT,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(replay_process.communicate(), 5)
            self.assertEqual(replay_process.returncode, 0, stderr.decode())
            self.assertEqual(json.loads(stdout), saved["context"])

    async def test_device_feed_reports_bus_off_and_uart_disconnect(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            sender, receiver = map(
                stack.enter_context, socket.socketpair(type=socket.SOCK_DGRAM)
            )
            receiver.setblocking(False)
            master, slave = pty.openpty()
            fpga = stack.enter_context(os.fdopen(master, "wb", buffering=0))
            stack.callback(os.close, slave)
            tty = stack.enter_context(open_uart(os.ttyname(slave)))
            output = Path(directory) / "run"
            config = Config(functional_only=True)
            journal = stack.enter_context(
                Journal(output, config, hardware={"can_interface": "test"})
            )
            feed = DeviceFeed(
                {"can": receiver, "fpga": tty},
                {"can": read_external_frame, "fpga": read_uart},
            )
            monitor = Monitor(journal, config, feed)
            rows = rows_in(FIXTURE)
            async with running(monitor) as port, connected(port) as (reader, _):
                self.assertEqual((await read_frame(reader))["source"], "live")
                for row in rows[:3]:
                    sender.send(
                        CAN_FRAME.pack(row["can_id"], 8, 0, bytes.fromhex(row["hex"]))
                    )
                await snapshot_where(
                    reader, lambda snap: snap["motor"]["state"] == "TEST_ARMED"
                )
                data = b"".join(bytes.fromhex(row["hex"]) for row in rows[3:5])
                fpga.write(data[:8])
                fpga.write(data[8:])
                await snapshot_where(
                    reader, lambda snap: snap["test"]["status"] == "RUNNING"
                )
                sender.send(
                    CAN_FRAME.pack(CAN_ERR_FLAG | CAN_ERR_BUSOFF, 8, 0, bytes(8))
                )
                event = await observe(reader, is_test_event)
                self.assertEqual(event["data"]["context"]["reason"], "CAN_DISCONNECT")
                await snapshot_where(
                    reader, lambda snap: snap["services"]["can"] == "OFFLINE"
                )
                fpga.close()
                await snapshot_where(
                    reader, lambda snap: snap["services"]["fpga"] == "OFFLINE"
                )

            self.assertTrue(monitor.finished)
            stack.close()
            disconnects = [
                row["event"]
                for row in rows_in(output / "inputs.jsonl")
                if row.get("event") in ("can_disconnect", "fpga_disconnect")
            ]
            self.assertEqual(disconnects, ["can_disconnect", "fpga_disconnect"])
            self.assertTrue((output / "manifest.json").is_file())

    async def test_delayed_persistence_does_not_hold_tcp_or_publish_unsaved_result(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            fixture, output = base / "fixture", base / "run"
            save_fixture(fixture, [*scenario_rows()[:7], service(275, "reset")])
            config = Config(gpio_verified=True)
            writing = threading.Event()
            release = threading.Event()
            with (
                fixture.open(encoding="utf-8") as source,
                Journal(output, config) as journal,
            ):
                monitor = Monitor(
                    journal, config, FixtureFeed(source, journal.evidence_session)
                )
                persist = journal.result

                def delay(context):
                    writing.set()
                    if not release.wait(6):
                        raise TimeoutError("시험에서 저장 지연을 해제하지 못했습니다")
                    return persist(context)

                with patch.object(journal, "result", side_effect=delay):
                    async with running(monitor) as port, connected(port) as (reader, _):
                        try:
                            await snapshot_where(
                                reader, lambda snap: snap["test"]["status"] == "RUNNING"
                            )
                            self.assertTrue(await asyncio.to_thread(writing.wait, 2))
                            while True:
                                message = await read_frame(reader, timeout=2)
                                self.assertFalse(is_test_event(message))
                                if message["type"] != "snapshot":
                                    continue
                                if message["data"]["services"]["log"] == "DEGRADED":
                                    self.assertEqual(
                                        message["data"]["motor"]["state"], "UNKNOWN"
                                    )
                                    self.assertEqual(
                                        message["data"]["test"]["status"], "RUNNING"
                                    )
                                    break
                        finally:
                            release.set()
                        event = await observe(reader, is_test_event)
                        self.assertEqual(event["data"]["code"], "ABORTED")
                        self.assertTrue(event["data"]["context"]["evidence_saved"])

    async def test_shutdown_finishes_open_trial_without_using_future_input(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            fixture, output = base / "fixture", base / "run"
            save_fixture(fixture, [*scenario_rows()[:5], service(5000, "clock")])
            config = Config(gpio_verified=True)
            with (
                fixture.open(encoding="utf-8") as source,
                Journal(output, config) as journal,
            ):
                monitor = Monitor(
                    journal, config, FixtureFeed(source, journal.evidence_session)
                )
                async with running(monitor) as port, connected(port) as (reader, _):
                    trial = await snapshot_where(
                        reader, lambda snap: snap["test"]["status"] == "RUNNING"
                    )
                self.assertFalse((output / "manifest.json").exists())

            self.assertTrue((output / "manifest.json").is_file())
            (event,) = rows_in(output / "events.jsonl")
            self.assertEqual(event["context"]["run_id"], trial["test"]["run_id"])
            self.assertEqual(event["context"]["reason"], "RECORD_ENDED_EARLY")
            self.assertEqual(len(rows_in(output / "inputs.jsonl")), 5)

    async def test_shutdown_records_bus_off_batch_already_read_by_device(self):
        stop = asyncio.Event()
        frame = Frame(CAN_ERR_BUSOFF, bytes(8), error=True)

        class StoppingFeed(DeviceFeed):
            async def records(self):
                now = self.now_ns()
                batch = ((now, "can", frame), (now, "service", "can_disconnect"))
                stop.set()
                yield batch

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "run"
            config = Config(functional_only=True)
            with Journal(output, config) as journal:
                monitor = Monitor(journal, config, StoppingFeed({}, {}))
                await monitor.run(stop)

            can, disconnect = rows_in(output / "inputs.jsonl")
            self.assertEqual(can["can_id"], CAN_ERR_BUSOFF)
            self.assertEqual(disconnect["event"], "can_disconnect")
            self.assertEqual(disconnect["rx_mono_ns"], can["rx_mono_ns"])
            self.assertTrue((output / "manifest.json").is_file())

    async def test_shutdown_records_input_completed_during_idle_flush(self):
        stop = asyncio.Event()
        allow_read = asyncio.Event()
        flushing = threading.Event()
        release_flush = threading.Event()

        class WaitingFeed(DeviceFeed):
            async def records(self):
                await allow_read.wait()
                stop.set()
                yield ((self.now_ns(), "service", "can_disconnect"),)

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "run"
            config = Config(functional_only=True)
            with Journal(output, config) as journal:
                monitor = Monitor(journal, config, WaitingFeed({}, {}))
                flush = journal.flush

                def delay():
                    flushing.set()
                    if not release_flush.wait(5):
                        raise TimeoutError("시험에서 저장 지연을 해제하지 못했습니다")
                    flush()

                with patch.object(journal, "flush", side_effect=delay):
                    worker = asyncio.create_task(monitor.run(stop))
                    try:
                        self.assertTrue(await asyncio.to_thread(flushing.wait, 2))
                        allow_read.set()
                        await asyncio.wait_for(stop.wait(), 2)
                    finally:
                        stop.set()
                        release_flush.set()
                        await asyncio.wait_for(worker, 5)

            self.assertEqual(
                [row["event"] for row in rows_in(output / "inputs.jsonl")],
                ["clock", "can_disconnect"],
            )

    async def test_failed_persistence_leaves_no_complete_record_or_tcp_event(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            fixture, output = base / "fixture", base / "run"
            save_fixture(fixture, [*scenario_rows()[:7], service(275, "reset")])
            config = Config(gpio_verified=True)
            writing = threading.Event()

            def fail(context):
                writing.set()
                raise OSError(errno.ENOSPC, "기록 공간 부족")

            with (
                self.assertRaisesRegex(OSError, "기록 공간 부족"),
                fixture.open(encoding="utf-8") as source,
                Journal(output, config) as journal,
            ):
                monitor = Monitor(
                    journal, config, FixtureFeed(source, journal.evidence_session)
                )
                with patch.object(journal, "result", side_effect=fail):
                    async with running(monitor) as port, connected(port) as (reader, _):
                        await snapshot_where(
                            reader, lambda snap: snap["test"]["status"] == "RUNNING"
                        )
                        self.assertTrue(await asyncio.to_thread(writing.wait, 2))
                        snapshot = await snapshot_where(
                            reader,
                            lambda snap: (
                                snap.get("recording", {}).get("reason")
                                == "RECORDING_FAILED"
                            ),
                        )
                        self.assertEqual(snapshot["services"]["log"], "DEGRADED")
                        self.assertEqual(snapshot["test"]["status"], "INCONCLUSIVE")
                        self.assertEqual(snapshot["test"]["reason"], "RECORDING_FAILED")
                        self.assertGreater(
                            snapshot["recording"]["unconfirmed_records"], 0
                        )
            self.assertFalse((output / "manifest.json").exists())
            self.assertEqual(rows_in(output / "events.jsonl"), [])
            self.assertEqual(list(monitor.stream.events), [])
            (failure,) = rows_in(output / "recording-failure.json")
            self.assertEqual(failure["result"]["result"], "INCONCLUSIVE")
            self.assertFalse(failure["result"]["evidence_saved"])
