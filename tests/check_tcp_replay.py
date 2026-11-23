"""저장 기록의 TCP 재생과 읽기 전용 연결을 검증한다."""

import asyncio
import json
import signal
import sys
import tempfile
import unittest
import uuid
from ast import literal_eval
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from check_diagnostics import EPOCH, scenario_rows, service, status
from check_monitor import (
    ROOT,
    connected,
    is_test_event,
    observe,
    read_frame,
    rows_in,
    save_fixture,
)

from backend.can_rx.codec import State
from backend.diagnostics.engine import DiagnosticEngine
from backend.diagnostics.inputs import Input
from backend.diagnostics.journal import Journal, evaluate, load_manifest, verify_source
from backend.diagnostics.storage import RecordedLog
from backend.diagnostics.verdict import Config
from backend.monitor.presentation import MonitorPresentation
from backend.monitor.replay import ReplayStream, play, verified_inputs
from backend.monitor.tcp import EVENT_LIMIT


def make_recording(base):
    fixture = base / "fixture"
    output = base / "run"
    rows = scenario_rows()
    rows += [
        {**service(2600, "can_rx_overflow"), "detail": {"dropped": 3}},
        {
            **service(2700, "can_retry_failed"),
            "detail": {"attempt": 1, "reason": "IO_ERROR"},
        },
        {**service(2800, "can_reconnect"), "detail": {"attempt": 2}},
        *status(2850, State.STOPPED, epoch=EPOCH + 2),
    ]
    save_fixture(fixture, rows)
    config = Config(gpio_verified=True)
    with (
        fixture.open(encoding="utf-8") as source,
        Journal(output, config, rotate_bytes=1024) as journal,
    ):
        evaluate(source, journal, config)
    return output


async def launch(output, *extra):
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-X",
        "dev",
        "-W",
        "error",
        "-m",
        "backend.monitor",
        "--replay",
        str(output),
        "--port",
        "0",
        *extra,
        cwd=ROOT,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


class TcpReplayTest(unittest.IsolatedAsyncioTestCase):
    async def test_replay_snapshot_ages_while_previous_event_is_sent(self):
        session = str(uuid.uuid4())
        engine = DiagnosticEngine(session, Config())
        for seq, row in enumerate(status(0, State.STOPPED), 1):
            engine.ingest(Input.from_row(row, session, seq))
        presentation = MonitorPresentation()
        stream = ReplayStream(session, presentation.view(engine, 0))
        stream.clock_origin = asyncio.get_running_loop().time(), 0
        stream.next_input_ns = 1_000_000_000
        stream.events.append({"code": "QUEUED"})
        reader = asyncio.StreamReader()
        links = []

        async def send(kind, data):
            if kind == "event" and data["code"] == "QUEUED":
                await asyncio.sleep(0.35)
            if kind == "snapshot":
                links.append(data["motor"]["link"])
                if len(links) == 2:
                    reader.feed_eof()

        await asyncio.wait_for(stream.stream_snapshots(reader, send, None), 2)
        self.assertEqual(links, ["fresh", "stale"])

    async def test_replay_keeps_original_intervals_after_queue_pause(self):
        session = str(uuid.uuid4())
        engine = DiagnosticEngine(session, Config())
        presentation = MonitorPresentation()
        stream = ReplayStream(session, presentation.view(engine, 0))
        stream.ready.set()
        stream.client = object()
        stream.events.extend({} for _ in range(EVENT_LIMIT))
        now = 100.0
        published = []
        records = [
            Input.from_row(service(at, "can_disconnect"), session, seq)
            for seq, at in enumerate((0, 200, 350), 1)
        ]

        async def sleep(delay):
            nonlocal now
            if delay > 0 and len(stream.events) == EVENT_LIMIT:
                now += 1
                stream.events.clear()
            else:
                now += delay

        def publish(snapshot, events):
            published.append((snapshot.logical_ns, round(now - 100, 3)))

        clock = SimpleNamespace(time=lambda: now, sleep=sleep)
        clock.get_running_loop = lambda: clock
        with (
            patch("backend.monitor.replay.asyncio", clock),
            patch.object(stream, "publish", side_effect=publish),
        ):
            await play(
                stream,
                (record for record in records),
                StringIO(),
                engine,
                presentation,
            )

        self.assertEqual(
            published[:3], [(0, 1.0), (200_000_000, 1.2), (350_000_000, 1.35)]
        )

    async def test_replay_checks_entire_block_before_exposing_it(self):
        with tempfile.TemporaryDirectory() as directory:
            output = await asyncio.to_thread(make_recording, Path(directory))
            manifest, _ = load_manifest(output)
            source = RecordedLog(output, "inputs.jsonl", manifest)
            digests = verify_source(source, manifest, block_size=2)
            lines = list(source)
            changed = json.loads(lines[3])
            changed["rx_utc"] = "변경된 기록"
            lines[3] = json.dumps(changed) + "\n"

            with (
                StringIO("".join(lines)) as file,
                patch("backend.monitor.replay.INPUT_BLOCK_SIZE", 2),
            ):
                replay = verified_inputs(file, manifest["evidence_session"], digests)
                self.assertEqual(next(replay).ingest_seq, 1)
                self.assertEqual(next(replay).ingest_seq, 2)
                with self.assertRaisesRegex(ValueError, "원본 입력의 해시 불일치"):
                    next(replay)

    async def test_replay_rejects_input_changed_after_startup_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            output = await asyncio.to_thread(make_recording, Path(directory))
            process = await launch(output)
            try:
                announcement = await asyncio.wait_for(process.stdout.readline(), 5)
                _, port = literal_eval(
                    announcement.decode().split("listening on ", 1)[1]
                )
                path = output / "inputs.jsonl"
                rows = rows_in(path)
                rows[0]["rx_utc"] = "변경된 기록"
                save_fixture(path, rows)

                async with connected(port) as (reader, _):
                    raw = await asyncio.wait_for(reader.read(), 5)
                messages = [json.loads(line) for line in raw.splitlines()]
                self.assertEqual(
                    [msg["type"] for msg in messages[:2]], ["hello", "snapshot"]
                )
                self.assertFalse(any(is_test_event(msg) for msg in messages))
                _, errors = await asyncio.wait_for(process.communicate(), 5)
                self.assertEqual(process.returncode, 1)
                self.assertIn("원본 입력의 해시 불일치", errors.decode())
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.communicate()

    async def test_rotated_recording_replays_read_only_without_modifying_source(self):
        with tempfile.TemporaryDirectory() as directory:
            output = await asyncio.to_thread(make_recording, Path(directory))
            manifest, _ = load_manifest(output)
            self.assertGreater(manifest["log_segments"]["inputs.jsonl"], 1)
            (saved,) = rows_in(output / "events.jsonl")
            before = {path.name: path.read_bytes() for path in output.iterdir()}
            process = await launch(output)
            try:
                announcement = await asyncio.wait_for(process.stdout.readline(), 5)
                _, port = literal_eval(
                    announcement.decode().split("listening on ", 1)[1]
                )
                async with connected(port) as (reader, writer):
                    hello = await read_frame(reader)
                    self.assertEqual(hello["source"], "replay")
                    self.assertEqual(
                        hello["data"]["replay_of"], manifest["backend_session"]
                    )
                    self.assertNotEqual(
                        hello["data"]["backend_session"], manifest["backend_session"]
                    )
                    self.assertEqual(
                        (await read_frame(reader))["data"]["test"]["status"], "IDLE"
                    )
                    request = {
                        "schema": "can-do/1.1",
                        "type": "control_request",
                        "data": {
                            "stream_id": hello["stream_id"],
                            "request_id": str(uuid.uuid4()),
                            "opcode": "START_REAL",
                            "argument": 42,
                        },
                    }
                    writer.write((json.dumps(request) + "\n").encode())
                    await writer.drain()
                    reply = await observe(
                        reader, lambda msg: msg["type"] == "control_result"
                    )
                    self.assertEqual(reply["data"]["reason"], "READ_ONLY")
                    event = await observe(reader, is_test_event)
                    self.assertEqual(event["data"]["event_id"], saved["event_id"])
                    self.assertEqual(event["data"]["context"]["result"], "PASS")
                    dropped = await observe(
                        reader,
                        lambda msg: (
                            msg["type"] == "service"
                            and msg["data"]["name"] == "can"
                            and msg["data"]["dropped"] == 3
                        ),
                    )
                    self.assertEqual(dropped["data"]["status"], "OFFLINE")
                    cleared = await observe(
                        reader,
                        lambda msg: (
                            msg["type"] == "event"
                            and msg["data"]["code"] == "MOTOR_FAULT_CHANGED"
                        ),
                    )
                    self.assertEqual(cleared["data"]["context"]["previous_mask"], 4)
                    self.assertEqual(cleared["data"]["context"]["fault_mask"], 0)
                    recovered = await observe(
                        reader,
                        lambda msg: (
                            msg["type"] == "service"
                            and msg["data"]["name"] == "can"
                            and msg["data"]["status"] == "OK"
                        ),
                    )
                    self.assertEqual(recovered["data"]["retries"], 2)

                await asyncio.sleep(0.05)
                async with connected(port) as (reader, _):
                    reconnect = await read_frame(reader)
                    self.assertEqual(reconnect["data"], hello["data"])
                    self.assertNotEqual(reconnect["stream_id"], hello["stream_id"])
                    final = (await read_frame(reader))["data"]
                    self.assertEqual(final["test"]["status"], "PASS")
                    self.assertEqual(final["motor"]["state"], "STOPPED")
                    await asyncio.sleep(0.35)
                    next_snapshot = await observe(
                        reader, lambda msg: msg["type"] == "snapshot"
                    )
                    self.assertEqual(next_snapshot["data"]["motor"], final["motor"])
            finally:
                if process.returncode is None:
                    process.send_signal(signal.SIGTERM)
                _, errors = await asyncio.wait_for(process.communicate(), 5)
            self.assertEqual(process.returncode, 0, errors.decode())
            self.assertEqual(
                {path.name: path.read_bytes() for path in output.iterdir()}, before
            )

    async def test_replay_rejects_corrupt_input_and_device_options(self):
        with tempfile.TemporaryDirectory() as directory:
            output = await asyncio.to_thread(make_recording, Path(directory))
            process = await launch(output, "--can", "can0")
            stdout, _ = await asyncio.wait_for(process.communicate(), 5)
            self.assertEqual(process.returncode, 2)
            self.assertFalse(stdout)

            (output / "inputs.jsonl.000002").unlink()
            process = await launch(output)
            stdout, errors = await asyncio.wait_for(process.communicate(), 5)
            self.assertEqual(process.returncode, 1)
            self.assertIn(b"inputs.jsonl.000002", errors)
            self.assertFalse(stdout)

    async def test_replay_does_not_publish_a_mismatched_verdict(self):
        with tempfile.TemporaryDirectory() as directory:
            output = await asyncio.to_thread(make_recording, Path(directory))
            path = output / "events.jsonl"
            (event,) = rows_in(path)
            event["context"]["reason"] = "CHANGED"
            path.write_text(json.dumps(event) + "\n", encoding="utf-8")
            process = await launch(output)
            try:
                announcement = await asyncio.wait_for(process.stdout.readline(), 5)
                _, port = literal_eval(
                    announcement.decode().split("listening on ", 1)[1]
                )
                async with connected(port) as (reader, _):
                    raw = await asyncio.wait_for(reader.read(), 5)
                messages = [json.loads(line) for line in raw.splitlines()]
                self.assertEqual(
                    [msg["type"] for msg in messages[:2]], ["hello", "snapshot"]
                )
                self.assertFalse(any(is_test_event(msg) for msg in messages))
                _, errors = await asyncio.wait_for(process.communicate(), 5)
                self.assertEqual(process.returncode, 1)
                self.assertIn("원본 결과와 일치하지 않는다", errors.decode())
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.communicate()
