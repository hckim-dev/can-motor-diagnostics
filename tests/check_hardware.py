"""실물 장치 없이 Jetson 수신 경로의 ABI와 장애 처리를 검증한다."""

import json
import os
import pty
import socket
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from binascii import crc_hqx
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from backend.can_control.wire import encode_operator_hello
from backend.can_rx.codec import CanId, State
from backend.diagnostics.journal import Journal, evaluate, load_manifest
from backend.diagnostics.verdict import Config
from backend.hardware.capture import capture, receive_inputs
from backend.hardware.socketcan import (
    CAN_EFF_FLAG,
    CAN_ERR_BUSOFF,
    CAN_ERR_FLAG,
    CAN_FRAME,
    CAN_RTR_FLAG,
    read_external_frame,
    unpack_frame,
)
from backend.hardware.tx_probe import send_hello_probe
from backend.hardware.uart import read_uart

FIXTURE = Path(__file__).with_name("fixtures") / "fpga-uart-1.0.hex"


def packet(can_id, data, raw_dlc=0):
    return CAN_FRAME.pack(can_id, len(data), raw_dlc, data)


class SocketCanTest(unittest.TestCase):
    def test_classical_padding_flags_and_original_dlc(self):
        motor = unpack_frame(packet(CanId.MOTOR_STATE, b"12345678"))
        self.assertEqual(
            (motor.can_id, motor.dlc, motor.data), (CanId.MOTOR_STATE, 8, b"12345678")
        )
        remote = unpack_frame(
            CAN_FRAME.pack(CAN_EFF_FLAG | CAN_RTR_FLAG | 0x1ABCDE, 7, 0, b"12345678")
        )
        self.assertEqual(
            (remote.can_id, remote.extended, remote.rtr, remote.dlc, remote.data),
            (0x1ABCDE, True, True, 7, b""),
        )
        self.assertEqual(unpack_frame(packet(0x200, b"12345678", 9)).dlc, 9)
        with self.assertRaises(ValueError):
            unpack_frame(b"short")

    def test_local_echo_is_excluded_from_external_evidence(self):
        fake = MagicMock()
        raw = packet(0x200, b"12345678")
        fake.recvmsg.side_effect = [
            (raw, [], socket.MSG_DONTROUTE, ("can0",)),
            (raw, [], 0, ("can0",)),
        ]
        self.assertIsNone(read_external_frame(fake))
        self.assertEqual(read_external_frame(fake).data, b"12345678")

    def test_submit_only_after_power_confirmation_and_match_own_echo(self):
        fake = MagicMock()
        fake.__enter__.return_value = fake
        frame = encode_operator_hello(10)
        raw = packet(frame.can_id, frame.data)
        fake.send.return_value = len(raw)
        fake.recvmsg.side_effect = [
            (raw, [], socket.MSG_DONTROUTE, ("can0",)),
            (raw, [], socket.MSG_CONFIRM, ("can0",)),
        ]
        with (
            patch("backend.hardware.tx_probe.open_can", return_value=fake) as open_can,
            patch("backend.hardware.tx_probe.next_boot_id", return_value=10) as boot,
        ):
            with self.assertRaisesRegex(ValueError, "모터 6V 물리 차단"):
                send_hello_probe("can0", Path("unused"), 0.5)
            open_can.assert_not_called()
            boot.assert_not_called()
            result = send_hello_probe("can0", Path("unused"), 0.5, motor_power_off=True)
        fake.send.assert_called_once_with(raw)
        self.assertEqual(fake.recvmsg.call_count, 2)
        self.assertTrue(result["socket_echo"])

    def test_unusable_probe_timeout_does_not_allocate_boot_id_or_submit(self):
        with (
            patch("backend.hardware.tx_probe.open_can") as open_can,
            patch("backend.hardware.tx_probe.next_boot_id") as next_boot_id,
        ):
            for timeout in (0.0, float("nan"), float("inf")):
                with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                    send_hello_probe(
                        "can0", Path("unused"), timeout, motor_power_off=True
                    )
            open_can.assert_not_called()

            sock = open_can.return_value.__enter__.return_value
            sock.settimeout.side_effect = OverflowError("소켓 시간 범위 초과")
            with self.assertRaisesRegex(OverflowError, "소켓 시간 범위 초과"):
                send_hello_probe("can0", Path("unused"), 1e100, motor_power_off=True)

            next_boot_id.assert_not_called()
            sock.send.assert_not_called()


class CaptureTest(unittest.TestCase):
    def test_ready_batch_does_not_read_next_device_after_deadline(self):
        selector = MagicMock()
        selector.__enter__.return_value = selector
        selector.select.return_value = [
            (SimpleNamespace(data="fpga", fileobj=1), 1),
            (SimpleNamespace(data="can", fileobj=2), 1),
        ]
        now = 0
        late_reader = MagicMock(side_effect=OSError("수집 종료 이후 단절"))
        with (
            patch(
                "backend.hardware.capture.selectors.DefaultSelector",
                return_value=selector,
            ),
            patch(
                "backend.hardware.capture.time.monotonic_ns", side_effect=lambda: now
            ),
        ):
            records = receive_inputs(
                {"fpga": 1, "can": 2},
                {"fpga": lambda _: b"data", "can": late_reader},
                10,
            )
            self.assertEqual(next(records), (0, "fpga", b"data", None))
            now = 11
            self.assertEqual(list(records), [(11, "service", "clock", None)])
        late_reader.assert_not_called()

    def test_uart_session_discontinuity_fails_capture_after_a_valid_frame(self):
        original = bytes.fromhex(FIXTURE.read_text(encoding="ascii"))
        regressed_body = original[2:4] + b"\x02\x00" + original[6:-2]
        regressed = (
            original[:2]
            + regressed_body
            + crc_hqx(regressed_body, 0xFFFF).to_bytes(2, "little")
        )

        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            sender, receiver = map(
                stack.enter_context, socket.socketpair(type=socket.SOCK_DGRAM)
            )
            sender.send(original + regressed + original)
            output = Path(directory) / "uart"
            config = Config(functional_only=True)
            with Journal(output, config, hardware={"fpga_uart": "test"}) as journal:
                result = capture(
                    {"fpga": receiver.fileno()},
                    {"fpga": read_uart},
                    journal,
                    config,
                    50_000_000,
                )

            self.assertFalse(result["ok"])
            self.assertEqual(result["counts"]["fpga_frames"], 1)
            self.assertEqual(result["counts"]["fpga_session_discontinuities"], 1)
            row = json.loads((output / "raw_fpga.jsonl").read_text(encoding="utf-8"))
            observed = [item["issues"] for item in row["candidates"]]
            self.assertEqual(
                observed[-2:], [["COUNTER_DISCONTINUITY"], ["SESSION_QUARANTINED"]]
            )

    def test_can_receive_and_bus_off_are_recorded(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            sender, receiver = map(
                stack.enter_context, socket.socketpair(type=socket.SOCK_DGRAM)
            )
            sender.send(
                packet(CanId.MOTOR_HELLO, bytes.fromhex("01 00 01 00 02 01 1F 00"))
            )
            sender.send(
                packet(CanId.MOTOR_STATE, bytes((1, 0, 1, 0, State.STOPPED, 2, 4, 0)))
            )
            sender.send(packet(CAN_ERR_FLAG | CAN_ERR_BUSOFF, bytes(8)))
            output = Path(directory) / "can"
            config = Config(functional_only=True)
            with Journal(output, config, hardware={"can_interface": "test"}) as journal:
                result = capture(
                    {"can": receiver},
                    {"can": read_external_frame},
                    journal,
                    config,
                    250_000_000,
                )
            self.assertEqual(result["counts"]["can_motor_states"], 1)
            self.assertFalse(result["ok"])
            self.assertEqual(result["errors"], ["CAN bus-off"])
            rows = [
                json.loads(line)
                for line in (output / "inputs.jsonl").read_text().splitlines()
            ]
            self.assertEqual(
                [row.get("event") for row in rows[-2:]], ["can_disconnect", "clock"]
            )
            self.assertEqual(rows[-3]["rx_mono_ns"], rows[-2]["rx_mono_ns"])
            manifest, original_config = load_manifest(output)
            self.assertEqual(manifest["source"], "live")
            with (
                (output / "inputs.jsonl").open() as source,
                Journal(
                    Path(directory) / "replay", original_config, original=manifest
                ) as journal,
            ):
                self.assertEqual(evaluate(source, journal, original_config), [])

    def test_uart_cli_handles_split_frame_and_restores_port(self):
        master, slave = pty.openpty()
        try:
            path = os.ttyname(slave)
            previous = termios.tcgetattr(slave)
            frame = bytes.fromhex(FIXTURE.read_text(encoding="ascii"))
            with tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "uart"
                command = [
                    sys.executable,
                    "-X",
                    "dev",
                    "-W",
                    "error",
                    "-m",
                    "backend.hardware",
                    "capture",
                    str(output),
                    "--uart",
                    path,
                    "--seconds",
                    "0.4",
                ]
                with subprocess.Popen(
                    command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
                ) as process:
                    try:
                        deadline = time.monotonic() + 2
                        while (
                            not output.is_dir()
                            and process.poll() is None
                            and time.monotonic() < deadline
                        ):
                            time.sleep(0.005)
                        self.assertTrue(output.is_dir())
                        attrs = termios.tcgetattr(slave)
                        self.assertEqual(attrs[4:6], [termios.B115200, termios.B115200])
                        os.write(master, frame[:10])
                        time.sleep(0.02)
                        os.write(master, frame[10:])
                        stdout, stderr = process.communicate(timeout=3)
                    finally:
                        if process.poll() is None:
                            process.kill()
                            process.communicate()
                self.assertEqual((process.returncode, stderr), (0, ""))
                self.assertEqual(json.loads(stdout)["counts"]["fpga_frames"], 1)
                self.assertEqual(termios.tcgetattr(slave), previous)
                projections = (output / "raw_fpga.jsonl").read_text().splitlines()
                saved = [
                    item
                    for row in projections
                    for item in json.loads(row)["candidates"]
                ]
                self.assertEqual(saved[0]["tx_seq"], 3)
                self.assertEqual(len(saved[0]["input_record_ids"]), 2)
        finally:
            os.close(slave)
            os.close(master)


if __name__ == "__main__":
    unittest.main()
