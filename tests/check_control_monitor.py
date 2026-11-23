"""한 TCP 세션에서 CAN 조작과 UART 시험 기록을 검증한다."""

import asyncio
import errno
import json
import tempfile
import threading
import time
import unittest
import uuid
from contextlib import ExitStack, nullcontext, suppress
from pathlib import Path
from unittest.mock import patch

from check_diagnostics import scenario_rows, uart
from check_fpga_recovery import open_pty, raw
from check_live_can_control import DatagramCan
from check_monitor import (
    connected,
    is_test_event,
    observe,
    read_frame,
    rows_in,
    running,
    snapshot_where,
)

from backend.can_control.mock_motor import MockMotor
from backend.can_rx.codec import CanId, State, decode_frame
from backend.can_rx.state import STOP_REQUESTED
from backend.diagnostics.inputs import Input
from backend.diagnostics.journal import Journal, evaluate, load_manifest, verify_results
from backend.diagnostics.verdict import Config
from backend.hardware.socketcan import unpack_frame
from backend.hardware.uart import open_uart, read_uart
from backend.monitor.control import ControlFeed
from backend.monitor.inputs import DeviceFeed
from backend.monitor.service import Monitor
from backend.monitor.tcp import MonitorStream


async def send(writer, stream, kind, **fields):
    message = {
        "schema": "can-do/1.1",
        "type": kind,
        "data": {"stream_id": stream, **fields},
    }
    writer.write((json.dumps(message) + "\n").encode())
    await writer.drain()


async def request(reader, writer, stream, opcode):
    request_id = str(uuid.uuid4())
    await send(
        writer,
        stream,
        "control_request",
        opcode=opcode,
        request_id=request_id,
        argument=0,
    )
    event = await observe(
        reader,
        lambda msg: (
            msg["type"] == "control_result" and msg["data"]["request_id"] == request_id
        ),
    )
    return request_id, event["data"]


async def feed_motor(device, motor):
    while device.peer.fileno() >= 0:
        now = time.monotonic_ns()
        device.inject(*motor.tick(now))
        for frame in device.transmitted():
            device.inject(*motor.receive(frame, now))
        await asyncio.sleep(0.005)


async def heartbeat(writer, stream):
    while True:
        await send(writer, stream, "control_heartbeat")
        await asyncio.sleep(0.05)


class ControlMonitorTest(unittest.IsolatedAsyncioTestCase):
    async def test_storage_failure_during_collection_cleanup_reports_both_errors(self):
        async def idle_can(_stream):
            await asyncio.Event().wait()

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "record"
            config = Config()
            with (
                self.assertLogs("backend.monitor.service", "ERROR"),
                self.assertRaisesRegex(OSError, "UART 수집 실패.*종료 원본 저장 실패"),
                Journal(output, config) as journal,
            ):
                feed = ControlFeed(None, DeviceFeed({}, {}), boot_id=1)
                monitor = Monitor(journal, config, feed)

                async def faulty_uart():
                    now = time.monotonic_ns()
                    feed._record((now, "fpga", b"\x00"))
                    feed._record((now, "fpga", b"\x01"))
                    raise OSError("UART 수집 실패")

                with (
                    patch.object(feed.bus, "run", side_effect=idle_can),
                    patch.object(feed, "_receive_uart", side_effect=faulty_uart),
                    patch.object(
                        journal,
                        "input",
                        side_effect=OSError(errno.ENOSPC, "디스크 공간 부족"),
                    ),
                ):
                    await asyncio.wait_for(monitor.run(asyncio.Event()), 3)

            (report,) = rows_in(output / "recording-failure.json")
            self.assertIn("UART 수집 실패", report["error"])
            self.assertIn("디스크 공간 부족", report["error"])
            self.assertEqual(report["dropped_records"], 1)
            self.assertEqual(report["unconfirmed_records"], 2)
            self.assertEqual(
                monitor.stream.snapshot(None, None)["services"]["log"], "DEGRADED"
            )
            self.assertFalse((output / "manifest.json").exists())

    async def test_write_failure_submits_inhibit_and_disables_can_control(self):
        can = DatagramCan()
        self.addCleanup(can.close)
        can.inject(*MockMotor().state_frames())
        config = Config()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "record"
            with (
                self.assertLogs("backend.monitor.service", "ERROR"),
                self.assertRaisesRegex(OSError, "디스크 공간 부족"),
                Journal(output, config) as journal,
            ):
                feed = ControlFeed(lambda: can, DeviceFeed({}, {}), boot_id=1)
                monitor = Monitor(journal, config, feed)
                with (
                    patch.object(
                        journal,
                        "input",
                        side_effect=OSError(errno.ENOSPC, "디스크 공간 부족"),
                    ),
                    patch.object(can, "send", wraps=can.send) as sent,
                ):
                    try:
                        await asyncio.wait_for(monitor.run(asyncio.Event()), 3)
                    finally:
                        packet = sent.call_args.args[0]
                        frame = unpack_frame(packet)
                        self.assertEqual(frame.can_id, CanId.OP_PERMIT)
                        self.assertEqual(decode_frame(frame).flags, STOP_REQUESTED)
                        self.assertFalse(feed.stream.control.controller.can_connected)
                        self.assertEqual(can.socket.fileno(), -1)

            self.assertFalse((output / "manifest.json").exists())

    async def test_record_queue_overflow_counts_rejected_uart_batch(self):
        class UartBatch(DeviceFeed):
            async def records(self):
                now = self.now_ns()
                yield tuple((now, "fpga", bytes([value])) for value in range(3))

        async def idle_can(_stream):
            await asyncio.Event().wait()

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "record"
            config = Config()
            with (
                self.assertLogs("backend.monitor.service", "ERROR"),
                self.assertRaisesRegex(OSError, "기록 큐"),
                Journal(output, config) as journal,
            ):
                feed = ControlFeed(None, UartBatch({}, {}), boot_id=1)
                feed.queue = asyncio.Queue(maxsize=1)
                monitor = Monitor(journal, config, feed)
                with patch.object(feed.bus, "run", side_effect=idle_can):
                    await asyncio.wait_for(monitor.run(asyncio.Event()), 3)

            (saved,) = rows_in(output / "inputs.jsonl")
            self.assertEqual(saved["hex"], "00")
            (report,) = rows_in(output / "recording-failure.json")
            self.assertEqual(report["dropped_records"], 2)
            self.assertEqual(report["unconfirmed_records"], 3)
            self.assertFalse((output / "manifest.json").exists())

    async def test_final_input_from_failed_worker_cannot_publish_verdict(self):
        rows = scenario_rows(1, latency=None, fault=0)
        config = Config(gpio_verified=True)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "record"
            with self.assertRaisesRegex(OSError, "모의 UART 수집 실패"):
                with Journal(output, config) as journal:
                    feed = ControlFeed(None, DeviceFeed({}, {}), boot_id=1)
                    monitor = Monitor(journal, config, feed)
                    inputs = tuple(
                        Input.from_row(row, journal.evidence_session, index)
                        for index, row in enumerate(rows, 1)
                    )
                    waiting = asyncio.Event()
                    get = feed.queue.get

                    def enqueue(record):
                        feed._record(
                            (
                                record.rx_mono_ns,
                                record.source,
                                record.payload,
                                record.detail,
                            )
                        )

                    async def read_queue():
                        if monitor.engine.ingest_seq == len(inputs) - 1:
                            waiting.set()
                        return await get()

                    async def faulty_uart():
                        for record in inputs[:-1]:
                            enqueue(record)
                        await waiting.wait()
                        enqueue(inputs[-1])
                        raise OSError("모의 UART 수집 실패")

                    async def idle_can(_stream):
                        await asyncio.Event().wait()

                    with (
                        patch.object(feed.bus, "run", side_effect=idle_can),
                        patch.object(feed, "_receive_uart", side_effect=faulty_uart),
                        patch.object(feed, "idle_records", return_value=()),
                        patch.object(feed.queue, "get", side_effect=read_queue),
                    ):
                        await asyncio.wait_for(monitor.run(asyncio.Event()), 3)

            saved = rows_in(output / "inputs.jsonl")
            self.assertEqual(
                [row["record_id"] for row in saved],
                [record.record_id for record in inputs],
            )
            self.assertEqual(rows_in(output / "events.jsonl"), [])
            self.assertFalse(monitor.stream.events)
            self.assertFalse((output / "manifest.json").exists())

    async def test_worker_failure_preserves_already_received_inputs(self):
        for stop_requested in (False, True):
            with self.subTest(stop_requested=stop_requested):
                await self.verify_worker_failure_preserves_inputs(stop_requested)

    async def test_uart_failure_reports_inhibit_record_dropped_at_queue_limit(self):
        await self.verify_worker_failure_preserves_inputs(False, full_queue=True)

    async def verify_worker_failure_preserves_inputs(
        self, stop_requested, *, full_queue=False
    ):
        writing = asyncio.Event()
        fail_uart = asyncio.Event()
        stop = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        can = DatagramCan()
        self.addCleanup(can.close)
        can.inject(*MockMotor().state_frames())
        uart_data = raw(uart(0, 0, 1, 0, run=0, scenario=0))

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "record"
            config = Config()
            expected_error = "모의 UART 수집 실패"
            logs = nullcontext()
            if full_queue:
                expected_error += ".*기록 큐"
                logs = self.assertLogs("backend.monitor.service", "ERROR")
            with logs, self.assertRaisesRegex(OSError, expected_error):
                with Journal(output, config) as journal:
                    feed = ControlFeed(lambda: can, DeviceFeed({}, {}), boot_id=1)
                    if full_queue:
                        feed.queue = asyncio.Queue(maxsize=32)
                    monitor = Monitor(journal, config, feed)
                    consume = monitor._consume

                    def blocked(batch):
                        loop.call_soon_threadsafe(writing.set)
                        if not release.wait(3):
                            raise TimeoutError("시험 기록 차단을 해제하지 못했습니다")
                        return consume(batch)

                    async def faulty_uart():
                        await fail_uart.wait()
                        if full_queue:
                            while feed.queue.qsize() < feed.queue.maxsize - 1:
                                feed._record((time.monotonic_ns(), "service", "clock"))
                        feed._record((time.monotonic_ns(), "fpga", uart_data))
                        raise OSError("모의 UART 수집 실패")

                    with (
                        patch.object(feed, "_receive_uart", side_effect=faulty_uart),
                        patch.object(monitor, "_consume", side_effect=blocked),
                    ):
                        worker = asyncio.create_task(monitor.run(stop))
                        try:
                            await asyncio.wait_for(writing.wait(), 2)
                            fail_uart.set()
                            for _ in range(100):
                                if can.socket.fileno() < 0:
                                    break
                                await asyncio.sleep(0.005)
                            self.assertEqual(can.socket.fileno(), -1)
                            if stop_requested:
                                stop.set()
                        finally:
                            release.set()
                            await asyncio.wait_for(worker, 5)

            rows = rows_in(output / "inputs.jsonl")
            (uart_row,) = (row for row in rows if row["source"] == "fpga")
            self.assertEqual(bytes.fromhex(uart_row["hex"]), uart_data)
            self.assertEqual(
                [row["ingest_seq"] for row in rows], list(range(1, len(rows) + 1))
            )
            if full_queue:
                self.assertEqual(rows[-1], uart_row)
                (report,) = rows_in(output / "recording-failure.json")
                self.assertEqual(report["dropped_records"], 1)
                self.assertEqual(report["unconfirmed_records"], len(rows) + 1)
                self.assertEqual(monitor.stream.recording["reason"], "RECORDING_FAILED")
            else:
                self.assertEqual(
                    (rows[-1]["source"], rows[-1]["can_id"]),
                    ("can_tx", CanId.OP_PERMIT),
                )
            self.assertFalse((output / "manifest.json").exists())
            self.assertEqual(rows_in(output / "events.jsonl"), [])

    async def test_uart_worker_failure_stops_can_while_record_consumer_is_paused(self):
        for full_queue in (False, True):
            with self.subTest(full_queue=full_queue):
                await self.verify_uart_failure_stops_can(full_queue)

    async def verify_uart_failure_stops_can(self, full_queue):
        fail = asyncio.Event()
        can = DatagramCan()
        self.addCleanup(can.close)
        can.inject(*MockMotor().state_frames())

        async def failing_uart():
            await fail.wait()
            raise OSError("모의 UART 수집 실패")

        feed = ControlFeed(lambda: can, DeviceFeed({}, {}), boot_id=1)
        feed.queue = asyncio.Queue(maxsize=32)
        feed.attach_control(MonitorStream(str(uuid.uuid4()), "mock", None))
        with (
            patch.object(feed, "_receive_uart", side_effect=failing_uart),
            patch.object(can, "send", wraps=can.send) as sent,
        ):
            records = feed.records()
            await asyncio.wait_for(anext(records), 2)
            sent.reset_mock()
            try:
                if full_queue:
                    while not feed.queue.full():
                        feed.queue.put_nowait((time.monotonic_ns(), "service", "clock"))
                fail.set()
                for _ in range(100):
                    if can.socket.fileno() < 0:
                        break
                    await asyncio.sleep(0.005)
                self.assertEqual(can.socket.fileno(), -1)
                self.assertFalse(feed.stream.control.controller.can_connected)
                sent.assert_called_once()
                frame = unpack_frame(sent.call_args.args[0])
                self.assertEqual(frame.can_id, CanId.OP_PERMIT)
                self.assertEqual(decode_frame(frame).flags, STOP_REQUESTED)
            finally:
                with self.assertRaisesRegex(OSError, "모의 UART 수집 실패"):
                    await records.aclose()

    async def test_shutdown_submits_inhibit_while_recording_is_blocked(self):
        loop = asyncio.get_running_loop()
        writing = asyncio.Event()
        release = threading.Event()
        stop = asyncio.Event()
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            output = Path(directory) / "record"
            can = DatagramCan()
            stack.callback(can.close)
            can.inject(*MockMotor().state_frames())
            config = Config()
            with Journal(output, config) as journal:
                feed = ControlFeed(lambda: can, DeviceFeed({}, {}), boot_id=1)
                monitor = Monitor(journal, config, feed)
                consume = monitor._consume

                def blocked(batch):
                    loop.call_soon_threadsafe(writing.set)
                    if not release.wait(3):
                        raise TimeoutError("시험 기록 차단을 해제하지 못했습니다")
                    return consume(batch)

                with (
                    patch.object(monitor, "_consume", side_effect=blocked),
                    patch.object(can, "send", wraps=can.send) as sent,
                ):
                    worker = asyncio.create_task(monitor.run(stop))
                    try:
                        await asyncio.wait_for(writing.wait(), 2)
                        sent.reset_mock()
                        stop.set()
                        for _ in range(200):
                            if can.socket.fileno() < 0:
                                break
                            await asyncio.sleep(0.005)
                        self.assertEqual(can.socket.fileno(), -1)
                        sent.assert_called_once()
                        frame = unpack_frame(sent.call_args.args[0])
                        self.assertEqual(frame.can_id, CanId.OP_PERMIT)
                        self.assertEqual(decode_frame(frame).flags, STOP_REQUESTED)
                    finally:
                        stop.set()
                        release.set()
                        await asyncio.wait_for(worker, 5)

            self.assertTrue((output / "manifest.json").is_file())
            last = rows_in(output / "raw_can.jsonl")[-1]
            self.assertEqual(
                (last["source"], last["can_id"]), ("can_tx", CanId.OP_PERMIT)
            )

    async def test_control_and_diagnostics_share_one_stream_and_replay(self):
        for action, expected in (
            (None, ("PASS", "NORMAL_CONFIRMED")),
            ("STOP", ("ABORTED", "STOP")),
            ("RESET", ("ABORTED", "RESET")),
        ):
            with self.subTest(action=action):
                await self.run_trial(action, expected)

    async def run_trial(self, action, expected):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            output = Path(directory) / "record"
            can = DatagramCan()
            stack.callback(can.close)
            motor = MockMotor()
            writer, _, uart_path = open_pty(stack)
            uart_feed = DeviceFeed(
                {},
                {"fpga": read_uart},
                fpga_open=lambda: open_uart(uart_path, discard_pending=True),
            )
            config = Config(gpio_verified=True)
            hardware = {"fpga_status_watchdog": True, "can_interface": "mock"}
            with Journal(output, config, hardware=hardware) as journal:
                feed = ControlFeed(lambda: can, uart_feed, boot_id=1)
                monitor = Monitor(journal, config, feed)
                worker = asyncio.create_task(feed_motor(can, motor))
                try:
                    async with running(monitor) as port, connected(port) as (rx, tx):
                        hello = await read_frame(rx)
                        stream = hello["stream_id"]
                        self.assertIn("can_control", hello["data"]["capabilities"])
                        await snapshot_where(
                            rx,
                            lambda snap: (
                                snap["services"]["can"] == "OK"
                                and feed.stream.control.controller.inhibit is None
                            ),
                        )
                        beats = asyncio.create_task(heartbeat(tx, stream))
                        try:
                            await send(tx, stream, "control_heartbeat")
                            arm_id, arm = await request(rx, tx, stream, "ARM_TEST")
                            self.assertEqual(arm["status"], "COMPLETED")
                            await snapshot_where(
                                rx, lambda snap: snap["motor"]["state"] == "TEST_ARMED"
                            )

                            writer.write(
                                raw(uart(0, 0, 1, 0, run=0, scenario=0))
                                + raw(uart(0, 1, 2, 1, scenario=1))
                            )
                            started = await snapshot_where(
                                rx, lambda snap: snap["test"]["status"] == "RUNNING"
                            )
                            action_id = None
                            if action == "STOP":
                                action_id, result = await request(
                                    rx, tx, stream, action
                                )
                                self.assertIn(
                                    result["status"], ("COMPLETED", "REJECTED")
                                )
                                writer.write(raw(uart(10, 2, 6, 3, scenario=1)))
                            elif action == "RESET":
                                motor.state = State.FAULT
                                motor.next_state_ns = 0
                                await snapshot_where(
                                    rx, lambda snap: snap["motor"]["state"] == "FAULT"
                                )
                                action_id, result = await request(
                                    rx, tx, stream, action
                                )
                                self.assertEqual(result["status"], "COMPLETED")
                            else:
                                motor.state = State.TEST_ACTIVE
                                motor.next_state_ns = 0
                                await snapshot_where(
                                    rx,
                                    lambda snap: (
                                        snap["motor"]["state"] == "TEST_ACTIVE"
                                    ),
                                )
                                writer.write(
                                    raw(uart(1000, 2, 3, 2, scenario=1))
                                    + raw(uart(3000, 3, 5, 3, scenario=1))
                                )

                            event = await observe(rx, is_test_event)
                            context = event["data"]["context"]
                            self.assertEqual(event["stream_id"], stream)
                            self.assertEqual(
                                context["run_id"], started["test"]["run_id"]
                            )
                            self.assertEqual(
                                (context["result"], context["reason"]), expected
                            )
                        finally:
                            beats.cancel()
                            with suppress(asyncio.CancelledError):
                                await beats
                finally:
                    worker.cancel()
                    with suppress(asyncio.CancelledError):
                        await worker

            (saved,) = rows_in(output / "events.jsonl")
            evidence = saved["context"]["evidence_ids"]
            raw_can = rows_in(output / "raw_can.jsonl")
            for request_id in filter(None, (arm_id, action_id)):
                matched = [
                    row
                    for row in raw_can
                    if row.get("detail", {}).get("request_id") == request_id
                ]
                self.assertTrue(
                    any(
                        row["source"] == "can_tx" and row["record_id"] in evidence
                        for row in matched
                    )
                )
                self.assertTrue(
                    any(
                        row["can_id"] == CanId.COMMAND_RESULT
                        and row["record_id"] in evidence
                        for row in matched
                    )
                )
            original, config = load_manifest(output)
            with (
                (output / "inputs.jsonl").open(encoding="utf-8") as source,
                Journal(
                    output.with_name("replay"), config, original=original
                ) as journal,
            ):
                verify_results(output, evaluate(source, journal, config))
