"""실물 없이 CAN 수신 재연결과 드롭 계측을 검증한다."""

import asyncio
import csv
import errno
import socket
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from check_diagnostics import run_engine, scenario_rows
from check_measurement_recording import hello, sample, state, timing
from check_monitor import connected, rows_in, running, snapshot_where

from backend.can_rx.codec import CanId, State
from backend.diagnostics.journal import Journal, evaluate, load_manifest
from backend.diagnostics.verdict import Config
from backend.hardware.capture import capture
from backend.hardware.socketcan import (
    CAN_ERR_BUSOFF,
    CAN_ERR_FLAG,
    CAN_FRAME,
    DROP_COUNT,
    SO_RXQ_OVFL,
    CanDropCounter,
    CanRead,
    can_error_reason,
    open_can,
    read_can_observation,
    read_external_frame,
)
from backend.monitor.inputs import DEVICE_STABLE_NS, DeviceFeed
from backend.monitor.service import Monitor


def send_rows(sender, rows):
    for row in rows:
        sender.send(CAN_FRAME.pack(row["can_id"], 8, 0, bytes.fromhex(row["hex"])))


class SocketDropTest(unittest.TestCase):
    def test_interface_lookup_without_errno_reports_missing_interface(self):
        with (
            patch(
                "backend.hardware.socketcan.socket.if_nametoindex",
                side_effect=OSError("no interface with this name"),
            ),
            self.assertRaises(OSError) as raised,
        ):
            open_can("can-test")
        self.assertEqual(can_error_reason(raised.exception), "INTERFACE_MISSING")

    def test_open_enables_drop_ancillary_before_binding_when_requested(self):
        sock = Mock()
        with (
            patch("backend.hardware.socketcan.socket.if_nametoindex"),
            patch(
                "backend.hardware.socketcan.Path.read_text", side_effect=["280", "1"]
            ),
            patch("backend.hardware.socketcan.socket.socket", return_value=sock),
        ):
            self.assertIs(open_can("can-test", count_overflow=True), sock)
        self.assertEqual(sock.method_calls[1].args, (socket.SOL_SOCKET, SO_RXQ_OVFL, 1))
        self.assertEqual(sock.method_calls[2].args, (("can-test",),))

    def test_counter_counts_wrap_and_preserves_observed_drops_on_local_echo(self):
        sock = Mock()
        control = [(socket.SOL_SOCKET, SO_RXQ_OVFL, DROP_COUNT.pack(7))]
        sock.recvmsg.return_value = (
            CAN_FRAME.pack(0x200, 8, 0, bytes(8)),
            control,
            socket.MSG_DONTROUTE,
            (),
        )
        self.assertEqual(read_can_observation(sock), CanRead(None, 7))
        sock.recvmsg.assert_called_once_with(CAN_FRAME.size, socket.CMSG_SPACE(4))

        counter = CanDropCounter()
        self.assertEqual(counter.observe((1 << 32) - 2), (1 << 32) - 2)
        self.assertEqual(counter.observe(None), 0)
        self.assertEqual(counter.observe(1), 3)
        self.assertEqual(counter.observe(1), 0)

    def test_bad_ancillary_data_is_reported(self):
        sock = Mock()
        frame = CAN_FRAME.pack(0x200, 8, 0, bytes(8))
        sock.recvmsg.side_effect = [
            (frame, [], socket.MSG_CTRUNC, ()),
            (frame, [(socket.SOL_SOCKET, SO_RXQ_OVFL, b"x")], 0, ()),
        ]
        with self.assertRaisesRegex(ValueError, "부가 정보"):
            read_can_observation(sock)
        with self.assertRaisesRegex(ValueError, "카운터 길이"):
            read_can_observation(sock)

    def test_capture_records_overflow_before_frame_and_splits_acquisition(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            sender, receiver = map(
                stack.enter_context, socket.socketpair(type=socket.SOCK_DGRAM)
            )
            rows = [hello(0), state(0, State.RUNNING), *sample(0, 65535), timing(0, 1)]
            rows += [state(0, State.STOPPED), hello(0), state(0, State.RUNNING)]
            send_rows(sender, rows)
            totals = iter([None] * 6 + [3, 3, 3])

            def read(sock):
                return CanRead(read_external_frame(sock), next(totals))

            output = Path(directory) / "run"
            config = Config(functional_only=True)
            with Journal(output, config) as journal:
                result = capture(
                    {"can": receiver}, {"can": read}, journal, config, 100_000_000
                )

            self.assertFalse(result["ok"])
            self.assertEqual(result["counts"]["socket_dropped_frames"], 3)
            self.assertEqual(result["counts"]["unobserved_sample_sequences"], 1)
            self.assertEqual(result["counts"]["can_motor_states"], 2)
            inputs = rows_in(output / "inputs.jsonl")
            loss, next_frame = inputs[6:8]
            self.assertEqual(loss["event"], "can_rx_overflow")
            self.assertEqual(loss["detail"], {"dropped": 3})
            self.assertEqual(loss["rx_mono_ns"], next_frame["rx_mono_ns"])
            first, second = rows_in(output / "operations.jsonl")
            self.assertEqual(first["end_reason"], "CAN_RX_OVERFLOW")
            self.assertFalse(second["start_observed"])

    def test_capture_keeps_drop_count_when_the_attached_frame_is_truncated(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            sender, receiver = map(
                stack.enter_context, socket.socketpair(type=socket.SOCK_DGRAM)
            )
            sender.send(b"ready")
            control = [(socket.SOL_SOCKET, SO_RXQ_OVFL, DROP_COUNT.pack(4))]
            reading = Mock()
            reading.recvmsg.return_value = (b"short", control, socket.MSG_TRUNC, ())

            def read(sock):
                sock.recv(32)
                return read_can_observation(reading)

            output = Path(directory) / "run"
            config = Config(functional_only=True)
            with Journal(output, config) as journal:
                result = capture(
                    {"can": receiver}, {"can": read}, journal, config, 100_000_000
                )

            self.assertFalse(result["ok"])
            self.assertEqual(result["counts"]["socket_dropped_frames"], 4)
            loss, disconnect, _ = rows_in(output / "inputs.jsonl")
            self.assertEqual(loss["event"], "can_rx_overflow")
            self.assertEqual(disconnect["detail"], {"reason": "INVALID_SOCKET_FRAME"})
            self.assertEqual(loss["rx_mono_ns"], disconnect["rx_mono_ns"])
            self.assertEqual(rows_in(output / "raw_can.jsonl"), [])

            replayed = Path(directory) / "replay"
            manifest, config = load_manifest(output)
            with (
                (output / "inputs.jsonl").open(encoding="utf-8") as source,
                Journal(replayed, config, original=manifest) as journal,
            ):
                evaluate(source, journal, config)
            self.assertEqual(load_manifest(replayed)[0]["can_rx"], manifest["can_rx"])

    def test_queue_loss_prevents_a_pending_pass_at_the_deadline(self):
        for at_ms in (2400, 2500, 2501):
            with self.subTest(at_ms=at_ms):
                rows = scenario_rows()[:-1]
                rows.append(
                    {
                        "source": "service",
                        "event": "can_rx_overflow",
                        "rx_mono_ns": str(at_ms * 1_000_000),
                        "detail": {"dropped": 1},
                    }
                )
                (result,) = run_engine(rows)
                self.assertEqual(
                    (result["result"], result["reason"]),
                    ("INCONCLUSIVE", "CAN_RX_OVERFLOW"),
                )


class ReconnectTest(unittest.IsolatedAsyncioTestCase):
    async def test_monitor_keeps_overflow_and_resets_counter_on_a_new_socket(self):
        with (
            ExitStack() as stack,
            patch("backend.monitor.inputs.CAN_RETRY_DELAYS", (0.01,)),
        ):
            receivers = []
            for total in (4, 1):
                sender, receiver = map(
                    stack.enter_context, socket.socketpair(type=socket.SOCK_DGRAM)
                )
                sender.send(bytes([total]))
                receivers.append(receiver)

            def read(sock):
                (total,) = sock.recv(1)
                return CanRead(None, total, ValueError("잘린 프레임"))

            feed = DeviceFeed({}, {"can": read}, can_open=Mock(side_effect=receivers))
            first, reconnect, second = [batch async for batch in feed.records()]

            self.assertEqual(reconnect[0][2:], ("can_reconnect", {"attempt": 1}))
            for (loss, disconnect), total in ((first, 4), (second, 1)):
                self.assertEqual(loss[2:], ("can_rx_overflow", {"dropped": total}))
                self.assertEqual(
                    disconnect[2:],
                    ("can_disconnect", {"reason": "INVALID_SOCKET_FRAME"}),
                )
                self.assertEqual(loss[0], disconnect[0])

    async def test_bus_off_reopens_can_and_waits_for_a_fresh_session(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            ExitStack() as stack,
            patch("backend.monitor.inputs.CAN_RETRY_DELAYS", (0.01, 0.02, 0.04)),
        ):
            sockets = [
                tuple(
                    map(stack.enter_context, socket.socketpair(type=socket.SOCK_DGRAM))
                )
                for _ in range(2)
            ]
            (before, old), (after, new) = sockets
            old.setblocking(False)
            new.setblocking(False)
            open_can_socket = Mock(
                side_effect=[old, OSError(errno.ENODEV, "분리됨"), new]
            )
            output = Path(directory) / "run"
            replay = Path(directory) / "replay"
            config = Config(functional_only=True)
            with Journal(output, config, hardware={"can_interface": "test"}) as journal:
                feed = DeviceFeed(
                    {}, {"can": read_can_observation}, can_open=open_can_socket
                )
                monitor = Monitor(journal, config, feed)
                async with running(monitor) as port, connected(port) as (reader, _):
                    send_rows(
                        before,
                        [
                            hello(0),
                            state(0, State.STOPPED),
                            state(1, State.RUNNING),
                            *sample(10, 1),
                            *sample(20, 4),
                        ],
                    )
                    first = await snapshot_where(
                        reader, lambda snap: snap["motor"]["sample"]["seq"] == 4
                    )
                    self.assertEqual(first["motor"]["state"], "RUNNING")
                    before.send(
                        CAN_FRAME.pack(CAN_ERR_FLAG | CAN_ERR_BUSOFF, 8, 0, bytes(8))
                    )
                    send_rows(after, [state(0, State.RUNNING)])
                    offline = await snapshot_where(
                        reader, lambda snap: snap["services"]["can"] == "OFFLINE"
                    )
                    self.assertEqual(offline["motor"]["state"], "UNKNOWN")
                    self.assertEqual(old.fileno(), -1)
                    send_rows(after, [hello(1), state(1, State.RUNNING)])
                    await snapshot_where(
                        reader, lambda snap: snap["services"]["can"] == "OK"
                    )
                    for _ in range(2):
                        await asyncio.sleep(0.16)
                        send_rows(after, [state(1, State.RUNNING)])
                    send_rows(after, sample(30, 1))
                    await snapshot_where(
                        reader, lambda snap: snap["motor"]["sample"]["seq"] == 1
                    )
                    send_rows(after, [state(40, State.STOPPED)])
                    await snapshot_where(
                        reader, lambda snap: snap["motor"]["state"] == "STOPPED"
                    )

            self.assertEqual(new.fileno(), -1)
            self.assertEqual(open_can_socket.call_count, 3)
            inputs = rows_in(output / "inputs.jsonl")
            events = [
                (row["event"], row["detail"]) for row in inputs if "detail" in row
            ]
            self.assertEqual(
                events,
                [
                    ("can_disconnect", {"reason": "BUS_OFF"}),
                    ("can_retry_failed", {"attempt": 1, "reason": "INTERFACE_MISSING"}),
                    ("can_reconnect", {"attempt": 2}),
                ],
            )
            raw = rows_in(output / "raw_can.jsonl")
            self.assertEqual(
                [row["sample_seq_gap"] for row in raw if "sample_seq_gap" in row], [2]
            )
            operations = rows_in(output / "operations.jsonl")
            self.assertEqual(
                [op["end_reason"] for op in operations], ["CAN_DISCONNECT", "STOPPED"]
            )
            self.assertEqual([op["start_observed"] for op in operations], [True, False])
            reconnect = next(
                i for i, row in enumerate(inputs) if row.get("event") == "can_reconnect"
            )
            resumed = [row for row in inputs[reconnect + 1 :] if row["source"] == "can"]
            state_before_hello, resumed_hello, accepted_state = resumed[:3]
            self.assertEqual(
                [
                    row["can_id"]
                    for row in (state_before_hello, resumed_hello, accepted_state)
                ],
                [CanId.MOTOR_STATE, CanId.MOTOR_HELLO, CanId.MOTOR_STATE],
            )
            self.assertEqual(
                operations[1]["start_record_id"], accepted_state["record_id"]
            )
            with (output / "samples.csv").open(newline="", encoding="utf-8") as file:
                samples = list(csv.DictReader(file))
            self.assertEqual(
                [row["operation_id"] for row in samples],
                [operations[0]["operation_id"]] * 2 + [operations[1]["operation_id"]],
            )
            self.assertNotEqual(
                samples[0]["acquisition_id"], samples[-1]["acquisition_id"]
            )
            manifest, config = load_manifest(output)
            self.assertEqual(
                manifest["can_rx"],
                {
                    "socket_dropped_frames": 0,
                    "unobserved_sample_sequences": 2,
                },
            )
            with (
                (output / "inputs.jsonl").open(encoding="utf-8") as source,
                Journal(replay, config, original=manifest) as journal,
            ):
                self.assertEqual(evaluate(source, journal, config), [])
            self.assertEqual(load_manifest(replay)[0]["can_rx"], manifest["can_rx"])
            self.assertEqual(rows_in(replay / "operations.jsonl"), operations)

    async def test_startup_retries_are_bounded_and_cancel_on_close(self):
        opener = Mock(side_effect=OSError(errno.ENETDOWN, "인터페이스 다운"))
        with patch("backend.monitor.inputs.CAN_RETRY_DELAYS", (0.01, 0.02, 0.03)):
            feed = DeviceFeed({}, {}, can_open=opener)
            records = [entry async for batch in feed.records() for entry in batch]
        self.assertEqual(opener.call_count, 4)
        self.assertEqual(
            [entry[2] for entry in records],
            [
                "can_disconnect",
                "can_retry_failed",
                "can_retry_failed",
                "can_retry_failed",
            ],
        )
        self.assertEqual([entry[3]["attempt"] for entry in records[1:]], [1, 2, 3])

        opener.reset_mock()
        with patch("backend.monitor.inputs.CAN_RETRY_DELAYS", (0.02,)):
            generator = DeviceFeed({}, {}, can_open=opener).records()
            self.assertEqual((await anext(generator))[0][2], "can_disconnect")
            await generator.aclose()
            await asyncio.sleep(0.05)
        opener.assert_called_once_with()

    async def test_repeated_bus_off_cannot_extend_retry_budget(self):
        with (
            ExitStack() as stack,
            patch("backend.monitor.inputs.CAN_RETRY_DELAYS", (0.01, 0.02, 0.04)),
        ):
            sockets = []
            for _ in range(4):
                sender, receiver = map(
                    stack.enter_context, socket.socketpair(type=socket.SOCK_DGRAM)
                )
                sender.send(
                    CAN_FRAME.pack(CAN_ERR_FLAG | CAN_ERR_BUSOFF, 8, 0, bytes(8))
                )
                receiver.setblocking(False)
                sockets.append(receiver)
            opener = Mock(side_effect=sockets)
            feed = DeviceFeed({}, {"can": read_can_observation}, can_open=opener)

            async def collect():
                return [
                    entry[2]
                    async for batch in feed.records()
                    for entry in batch
                    if entry[1] == "service"
                ]

            events = await asyncio.wait_for(collect(), 2)
            self.assertEqual(
                events, ["can_disconnect"] + ["can_reconnect", "can_disconnect"] * 3
            )
            self.assertEqual(opener.call_count, 4)
            self.assertTrue(all(sock.fileno() == -1 for sock in sockets))

    async def test_fpga_input_continues_while_can_retry_is_pending(self):
        for error, reason in (
            (OSError(errno.ENODEV, "분리됨"), "INTERFACE_MISSING"),
            (ValueError("CAN 인터페이스가 아닙니다"), "INVALID_INTERFACE"),
        ):
            with (
                self.subTest(reason=reason),
                ExitStack() as stack,
                patch("backend.monitor.inputs.CAN_RETRY_DELAYS", (10.0,)),
            ):
                opener = Mock(side_effect=error)
                sender, receiver = map(
                    stack.enter_context, socket.socketpair(type=socket.SOCK_DGRAM)
                )
                receiver.setblocking(False)
                feed = DeviceFeed(
                    {"fpga": receiver},
                    {"fpga": lambda sock: sock.recv(32)},
                    can_open=opener,
                )
                records = feed.records()
                try:
                    (event,) = await anext(records)
                    self.assertEqual(event[2:], ("can_disconnect", {"reason": reason}))
                    sender.send(b"uart-data")
                    ((_, source, raw),) = await asyncio.wait_for(anext(records), 1)
                    self.assertEqual((source, raw), ("fpga", b"uart-data"))
                    opener.assert_called_once_with()
                finally:
                    await records.aclose()

    def test_retry_budget_resets_after_continuous_fresh_state(self):
        feed = DeviceFeed({}, {}, can_open=Mock())
        retry = feed.retry_states["can"]
        retry.attempts = 2
        can = Mock()

        def status(now, link):
            can.last_state_ns = now
            can.at.return_value = {"motor": {"link": link}}
            feed.observe_devices(SimpleNamespace(logical_ns=now, can=can))

        status(0, "fresh")
        status(DEVICE_STABLE_NS, "fresh")
        self.assertEqual(retry.attempts, 2)
        status(DEVICE_STABLE_NS + 1, "stale")
        started = DEVICE_STABLE_NS + 2
        step = 100_000_000
        for now in range(started, started + DEVICE_STABLE_NS, step):
            status(now, "fresh")
        self.assertEqual(retry.attempts, 2)
        status(started + DEVICE_STABLE_NS, "fresh")
        self.assertEqual(retry.attempts, 0)
