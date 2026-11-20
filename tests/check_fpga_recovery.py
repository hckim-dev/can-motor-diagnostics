"""가상 시리얼 포트에서 UART 복구와 시험 증거의 세션 경계를 검증한다."""

import asyncio
import errno
import os
import pty
import select
import socket
import tempfile
import termios
import threading
import unittest
from binascii import crc_hqx
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

from check_can_recovery import send_rows
from check_diagnostics import scenario_rows, service, status, uart
from check_monitor import (
    connected,
    observe,
    rows_in,
    running,
    save_fixture,
    snapshot_where,
)

from backend.can_rx.codec import State
from backend.diagnostics.journal import Journal, evaluate, load_manifest, verify_results
from backend.diagnostics.verdict import Config
from backend.hardware.capture import capture, capture_hardware
from backend.hardware.socketcan import read_external_frame
from backend.hardware.uart import open_uart, read_uart
from backend.monitor.inputs import DeviceFeed
from backend.monitor.service import Monitor


def open_pty(stack):
    master, slave = pty.openpty()
    stack.callback(os.close, slave)
    writer = stack.enter_context(os.fdopen(master, "wb", buffering=0))
    return writer, slave, os.ttyname(slave)


def raw(row):
    return bytes.fromhex(row["hex"])


class UartCaptureTest(unittest.TestCase):
    def test_capture_ignores_frames_queued_before_open(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            writer, _, path = open_pty(stack)
            fd = stack.enter_context(open_uart(path))
            writer.write(raw(uart(0, 0, 1, 0, run=0, scenario=0)))
            self.assertEqual(select.select([fd], [], [], 1)[0], [fd])
            output = Path(directory) / "run"

            result = capture_hardware(output, None, path, 50_000_000)

            self.assertFalse(result["ok"])
            self.assertEqual(result["counts"].get("fpga_frames", 0), 0)
            self.assertIn("유효한 FPGA UART 프레임을 받지 못했습니다", result["errors"])
            self.assertTrue((output / "manifest.json").is_file())

    def test_open_discards_buffered_input_and_restores_termios(self):
        with ExitStack() as stack:
            writer, slave, path = open_pty(stack)
            previous = termios.tcgetattr(slave)
            writer.write(b"old\n")
            with open_uart(path, discard_pending=True) as fd:
                with self.assertRaises(BlockingIOError):
                    read_uart(fd)
                writer.write(b"new")
                self.assertEqual(read_uart(fd), b"new")
            self.assertEqual(termios.tcgetattr(slave), previous)

    def test_capture_reports_crc_error_and_sequence_gap_even_with_valid_frames(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            writer, _, path = open_pty(stack)
            fd = stack.enter_context(open_uart(path))
            first = raw(uart(0, 0, 1, 0, run=0, scenario=0))
            bad = bytearray(raw(uart(100, 1, 1, 0, run=0, scenario=0)))
            bad[-1] ^= 1
            good = raw(uart(200, 2, 1, 0, run=0, scenario=0))
            writer.write(first + bad + good)
            output = Path(directory) / "run"

            config = Config(functional_only=True)
            with Journal(output, config) as journal:
                result = capture(
                    {"fpga": fd}, {"fpga": read_uart}, journal, config, 100_000_000
                )

            self.assertFalse(result["ok"])
            self.assertEqual(result["counts"]["fpga_frames"], 2)
            self.assertEqual(result["counts"]["fpga_crc_errors"], 1)
            self.assertEqual(result["counts"]["fpga_tx_gaps"], 1)
            candidates = [
                item
                for row in rows_in(output / "raw_fpga.jsonl")
                for item in row["candidates"]
            ]
            self.assertEqual(
                [item["issues"] for item in candidates], [[], ["CRC"], ["TX_GAP"]]
            )

    def test_capture_rejects_incompatible_frames_before_first_valid_frame(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            writer, _, path = open_pty(stack)
            fd = stack.enter_context(open_uart(path))
            status_frame = raw(uart(0, 0, 1, 0, run=0, scenario=0))
            version_body = b"\x02" + status_frame[3:-2]
            fields_body = status_frame[2:3] + b"\xff" + status_frame[4:-2]
            for body in (version_body, fields_body):
                checksum = crc_hqx(body, 0xFFFF).to_bytes(2, "little")
                writer.write(status_frame[:2] + body + checksum)
            writer.write(status_frame)
            output = Path(directory) / "run"

            config = Config(functional_only=True)
            with Journal(output, config) as journal:
                result = capture(
                    {"fpga": fd}, {"fpga": read_uart}, journal, config, 100_000_000
                )

            self.assertFalse(result["ok"])
            self.assertEqual(result["counts"]["fpga_frames"], 1)
            self.assertEqual(result["counts"]["fpga_version_errors"], 1)
            self.assertEqual(result["counts"]["fpga_field_errors"], 1)
            candidates = [
                item
                for row in rows_in(output / "raw_fpga.jsonl")
                for item in row["candidates"]
            ]
            self.assertEqual(
                [item["issues"] for item in candidates], [["VERSION"], ["FIELDS"], []]
            )

    def test_capture_reports_latched_invalid_status_without_sequence_gaps(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            writer, _, path = open_pty(stack)
            fd = stack.enter_context(open_uart(path))
            writer.write(raw(uart(0, 0, 1, 0, run=0, scenario=0, flags=0x08)))
            writer.write(raw(uart(100, 1, 1, 0, run=0, scenario=0, flags=0)))
            output = Path(directory) / "run"

            config = Config(functional_only=True)
            with Journal(output, config) as journal:
                result = capture(
                    {"fpga": fd}, {"fpga": read_uart}, journal, config, 100_000_000
                )

            self.assertFalse(result["ok"])
            self.assertEqual(result["counts"]["fpga_frames"], 2)
            self.assertEqual(result["counts"]["fpga_invalid_observations"], 2)
            self.assertEqual(result["counts"]["fpga_tx_gaps"], 0)
            candidates = [
                item
                for row in rows_in(output / "raw_fpga.jsonl")
                for item in row["candidates"]
            ]
            self.assertEqual(
                [item["issues"] for item in candidates],
                [["FPGA_INVALID"], ["FPGA_INVALID"]],
            )

    def test_status_watchdog_uses_valid_status_and_replays_the_same_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            entries = scenario_rows()[:5]
            entries += status(200, State.TEST_ACTIVE)
            entries += [uart(200, 2, 1, 1)]
            entries += status(400, State.TEST_ACTIVE)
            entries += [uart(450, 3, 3, 2)]
            entries += status(600, State.TEST_ACTIVE)
            entries += [service(699), service(700)]
            fixture = base / "fixture.jsonl"
            save_fixture(fixture, entries)
            config = Config(functional_only=True)
            expected = [("INCONCLUSIVE", "FPGA_STATUS_TIMEOUT", 700)]
            for guarded in (True, False):
                target = base / f"run-{guarded}"
                hardware = {"fpga_uart": "test"}
                if guarded:
                    hardware["fpga_status_watchdog"] = True
                with (
                    fixture.open(encoding="utf-8") as source,
                    Journal(target, config, hardware=hardware) as journal,
                ):
                    results = evaluate(source, journal, config)
                found = [
                    (row["result"], row["reason"], row["end_rx_mono_ms"])
                    for row in results
                ]
                if guarded:
                    self.assertEqual(found, expected)
                else:
                    self.assertEqual(
                        found, [("INCONCLUSIVE", "RECORD_ENDED_EARLY", 700)]
                    )

                original, config = load_manifest(target)
                with (
                    (target / "inputs.jsonl").open(encoding="utf-8") as source,
                    Journal(
                        base / f"replay-{guarded}", config, original=original
                    ) as journal,
                ):
                    verify_results(target, evaluate(source, journal, config))


class UartReconnectTest(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_publishes_record_when_uart_restore_fails(self):
        stop = asyncio.Event()
        consuming = threading.Event()
        release = threading.Event()
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            peer, port = map(stack.enter_context, socket.socketpair())
            output = Path(directory) / "run"
            config = Config(functional_only=True)

            @contextmanager
            def open_fpga():
                try:
                    yield port.fileno()
                finally:
                    port.close()
                    raise termios.error(errno.EINVAL, "모의 종료 설정 복구 오류")

            with Journal(output, config) as journal:
                feed = DeviceFeed({}, {"fpga": read_uart}, fpga_open=open_fpga)
                monitor = Monitor(journal, config, feed)
                consume = monitor._consume

                def pause(batch):
                    if batch[0][1] == "fpga":
                        consuming.set()
                        if not release.wait(5):
                            raise TimeoutError("시험 수신 처리를 해제하지 못했습니다")
                    return consume(batch)

                peer.sendall(raw(uart(0, 0, 1, 0, run=0, scenario=0)))
                with (
                    self.assertLogs("backend.monitor.inputs", level="WARNING") as log,
                    patch.object(monitor, "_consume", side_effect=pause),
                ):
                    worker = asyncio.create_task(monitor.run(stop))
                    try:
                        self.assertTrue(await asyncio.to_thread(consuming.wait, 2))
                    finally:
                        stop.set()
                        release.set()
                        await asyncio.wait_for(worker, 5)
                self.assertIn("모의 종료 설정 복구 오류", log.output[0])

            self.assertEqual(port.fileno(), -1)
            self.assertTrue((output / "manifest.json").is_file())
            (record,) = rows_in(output / "raw_fpga.jsonl")
            self.assertEqual(record["candidates"][0]["tx_seq"], 0)

    async def test_uart_restore_failure_does_not_stop_can_reception(self):
        with (
            ExitStack() as stack,
            patch("backend.monitor.inputs.FPGA_RETRY_DELAYS", (10.0,)),
        ):
            sender, receiver = map(
                stack.enter_context, socket.socketpair(type=socket.SOCK_DGRAM)
            )
            old_peer, old_uart = map(stack.enter_context, socket.socketpair())
            old_peer.close()

            @contextmanager
            def open_fpga():
                try:
                    yield old_uart.fileno()
                finally:
                    old_uart.close()
                    raise termios.error(errno.EINVAL, "모의 설정 복구 오류")

            feed = DeviceFeed(
                {"can": receiver},
                {"can": read_external_frame, "fpga": read_uart},
                fpga_open=open_fpga,
            )
            records = feed.records()
            try:
                with self.assertLogs("backend.monitor.inputs", level="WARNING") as log:
                    (disconnect,) = await asyncio.wait_for(anext(records), 1)
                self.assertEqual(disconnect[2:], ("fpga_disconnect", {"reason": "EOF"}))
                self.assertIn("모의 설정 복구 오류", log.output[0])
                self.assertEqual(old_uart.fileno(), -1)

                send_rows(sender, status(0, State.TEST_ARMED)[:1])
                (frame,) = await asyncio.wait_for(anext(records), 1)
                self.assertEqual(frame[1], "can")
            finally:
                await records.aclose()

    async def test_port_reappears_under_same_link_without_reusing_partial_input(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            ExitStack() as stack,
            patch("backend.monitor.inputs.FPGA_RETRY_DELAYS", (0.05, 0.20, 0.30)),
        ):
            base = Path(directory)
            sender, receiver = map(
                stack.enter_context, socket.socketpair(type=socket.SOCK_DGRAM)
            )
            receiver.setblocking(False)
            old, _, old_path = open_pty(stack)
            new, new_slave, new_path = open_pty(stack)
            new_previous = termios.tcgetattr(new_slave)
            port_link = base / "fpga"
            port_link.symlink_to(old_path)
            partial_read = asyncio.Event()
            missing_port = asyncio.Event()
            second_port = asyncio.Event()
            old_frame = raw(uart(30, 2, 3, 2))
            opens = []

            def open_fpga():
                if not port_link.exists():
                    missing_port.set()
                    raise FileNotFoundError(errno.ENOENT, "모의 UART 미연결")
                opens.append(port_link.resolve().name)
                return open_uart(port_link, discard_pending=True)

            def read_fpga(fd):
                data = read_uart(fd)
                if data == old_frame[:8]:
                    partial_read.set()
                if len(opens) == 2:
                    second_port.set()
                return data

            output = base / "run"
            config = Config(functional_only=True)
            hardware = {"fpga_uart": str(port_link), "fpga_status_watchdog": True}
            with Journal(output, config, hardware=hardware) as journal:
                feed = DeviceFeed(
                    {"can": receiver},
                    {"can": read_external_frame, "fpga": read_fpga},
                    fpga_open=open_fpga,
                )
                monitor = Monitor(journal, config, feed)
                async with running(monitor) as port, connected(port) as (client, _):
                    greeting = await observe(client, lambda msg: msg["type"] == "hello")
                    stream = greeting["stream_id"]
                    send_rows(sender, status(0, State.TEST_ARMED))
                    await snapshot_where(
                        client, lambda snap: snap["services"]["can"] == "OK"
                    )
                    first_status = raw(uart(0, 0, 1, 0, run=0, scenario=0))
                    baseline = raw(uart(0, 1, 2, 1))
                    old.write(first_status + baseline)
                    first = await snapshot_where(
                        client, lambda snap: snap["test"]["status"] == "RUNNING"
                    )
                    old_run = first["test"]["run_id"]
                    old.write(old_frame[:8])
                    await asyncio.wait_for(partial_read.wait(), 2)
                    port_link.unlink()
                    old.close()
                    await asyncio.wait_for(missing_port.wait(), 2)
                    event = await observe(client, lambda msg: msg["type"] == "event")
                    self.assertEqual(event["stream_id"], stream)
                    self.assertEqual(
                        event["data"]["context"]["reason"], "FPGA_DISCONNECT"
                    )
                    send_rows(sender, status(0, State.TEST_ARMED))
                    await snapshot_where(
                        client,
                        lambda snap: (
                            snap["services"]["fpga"] == "OFFLINE"
                            and snap["services"]["can"] == "OK"
                        ),
                    )
                    port_link.symlink_to(new_path)
                    for _ in range(100):
                        if len(opens) == 2:
                            break
                        await asyncio.sleep(0.01)
                    self.assertEqual(len(opens), 2)
                    send_rows(sender, status(0, State.TEST_ARMED))
                    new.write(old_frame[8:] + first_status + baseline)
                    await asyncio.wait_for(second_port.wait(), 2)
                    resumed = await snapshot_where(
                        client,
                        lambda snap: (
                            snap["test"]["status"] == "RUNNING"
                            and snap["test"]["run_id"] != old_run
                        ),
                    )
                    self.assertEqual(resumed["services"]["fpga"], "OK")
                    new_run = resumed["test"]["run_id"]
                    broken = bytearray(raw(uart(20, 2, 1, 1)))
                    broken[-1] ^= 1
                    new.write(broken + raw(uart(30, 3, 1, 1)))
                    await snapshot_where(
                        client, lambda snap: snap["services"]["fpga"] == "DEGRADED"
                    )

            self.assertEqual(termios.tcgetattr(new_slave), new_previous)
            self.assertNotEqual(old_run.split(":")[0], new_run.split(":")[0])
            self.assertEqual((old_run.split(":")[1], new_run.split(":")[1]), ("1", "1"))
            inputs = rows_in(output / "inputs.jsonl")
            self.assertEqual(
                [
                    row["event"]
                    for row in inputs
                    if row.get("event", "").startswith("fpga_")
                ],
                ["fpga_disconnect", "fpga_retry_failed", "fpga_reconnect"],
            )
            truncated = next(
                row for row in inputs if row.get("hex") == old_frame[:8].hex(" ")
            )
            first, second = rows_in(output / "events.jsonl")
            self.assertIn(truncated["record_id"], first["context"]["evidence_ids"])
            self.assertEqual(second["context"]["run_id"], new_run)
            self.assertIn("UART_CRC", second["context"]["issues"])
            self.assertIn("UART_TX_GAP", second["context"]["issues"])
            candidates = [
                item
                for row in rows_in(output / "raw_fpga.jsonl")
                for item in row["candidates"]
            ]
            self.assertEqual(
                [item["tx_seq"] for item in candidates], [0, 1, 0, 1, None, 3]
            )

            original, config = load_manifest(output)
            with (
                (output / "inputs.jsonl").open(encoding="utf-8") as source,
                Journal(base / "replay", config, original=original) as journal,
            ):
                verify_results(output, evaluate(source, journal, config))

    async def test_uart_retry_limit_leaves_other_input_open(self):
        with (
            ExitStack() as stack,
            patch("backend.monitor.inputs.FPGA_RETRY_DELAYS", (0.01, 0.02, 0.04)),
        ):
            sender, receiver = map(
                stack.enter_context, socket.socketpair(type=socket.SOCK_DGRAM)
            )

            def missing():
                raise termios.error(errno.ENOTTY, "모의 장치 설정 실패")

            feed = DeviceFeed(
                {"can": receiver}, {"can": read_external_frame}, fpga_open=missing
            )
            records = feed.records()
            try:
                (event,) = await asyncio.wait_for(anext(records), 1)
                self.assertEqual(
                    event[2:], ("fpga_disconnect", {"reason": "INVALID_PORT"})
                )
                for expected in range(1, 4):
                    (event,) = await asyncio.wait_for(anext(records), 1)
                    self.assertEqual(event[2], "fpga_retry_failed")
                    self.assertEqual(event[3]["attempt"], expected)
                sender.send(b"not a frame")
                (event,) = await asyncio.wait_for(anext(records), 1)
                self.assertEqual(
                    event[2:], ("can_disconnect", {"reason": "INVALID_SOCKET_FRAME"})
                )
            finally:
                await records.aclose()

            self.assertGreaterEqual(receiver.fileno(), 0)

    async def test_close_cancels_pending_uart_retry(self):
        calls = 0

        def missing():
            nonlocal calls
            calls += 1
            raise FileNotFoundError(errno.ENOENT, "모의 UART 미연결")

        with patch("backend.monitor.inputs.FPGA_RETRY_DELAYS", (0.02,)):
            records = DeviceFeed({}, {}, fpga_open=missing).records()
            await asyncio.wait_for(anext(records), 1)
            await records.aclose()
            await asyncio.sleep(0.05)
        self.assertEqual(calls, 1)
