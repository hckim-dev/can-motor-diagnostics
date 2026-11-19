"""Python 클라이언트로 TCP 조작 목업의 정상·오류 흐름을 검증한다."""

import json
import re
import signal
import socket
import subprocess
import sys
import time
import unittest
import uuid
from pathlib import Path

from check_tcp_mock import FIXTURE, read_message

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "can-do/1.1"


def encode(kind, **data):
    return (json.dumps({"schema": SCHEMA, "type": kind, "data": data}) + "\n").encode()


class ControlMockTest(unittest.TestCase):
    def setUp(self):
        self.server = subprocess.Popen(
            [
                sys.executable,
                "-X",
                "dev",
                "-W",
                "error",
                str(ROOT / "backend" / "tcp_mock.py"),
                "--control",
                "--port",
                "0",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(self.stop_server)
        listening = self.server.stdout.readline()
        self.port = int(re.search(r", (\d+)\)", listening)[1])

    def stop_server(self):
        if self.server.poll() is None:
            self.server.send_signal(signal.SIGTERM)
        try:
            _, stderr = self.server.communicate(timeout=3)
        finally:
            if self.server.poll() is None:
                self.server.kill()
                self.server.communicate()
        self.assertEqual((self.server.returncode, stderr), (0, ""))

    def connect(self):
        deadline = time.monotonic() + 2
        while True:
            conn = socket.create_connection(("127.0.0.1", self.port), timeout=1)
            wire = conn.makefile("rb")
            self.addCleanup(conn.close)
            self.addCleanup(wire.close)
            try:
                hello = read_message(wire)
            except EOFError:
                wire.close()
                conn.close()
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)
                continue
            self.assertEqual((hello["type"], hello["stream_seq"]), ("hello", 0))
            initial = read_message(wire)
            self.assertEqual((initial["type"], initial["stream_seq"]), ("snapshot", 1))
            self.assertEqual(initial["data"]["motor"]["state"], "STOPPED")
            self.assertEqual(initial["data"]["operator"]["link"], "offline")
            return conn, wire, hello["stream_id"]

    def wait_message(self, wire, kind, predicate=lambda data: True):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            message = read_message(wire)
            if message["type"] == kind and predicate(message["data"]):
                return message["data"]
        self.fail(f"{kind} 메시지의 예상 상태를 받지 못했다")

    def request(
        self, conn, wire, stream, opcode="ARM_TEST", argument=0, request_id=None
    ):
        request_id = request_id or str(uuid.uuid4())
        conn.sendall(
            encode(
                "control_request",
                stream_id=stream,
                request_id=request_id,
                opcode=opcode,
                argument=argument,
            )
        )
        return self.wait_message(
            wire,
            "control_result",
            lambda data: data["request_id"] == request_id,
        )

    def test_arm_duplicate_expiry_and_reconnect(self):
        conn, wire, stream = self.connect()
        self.assertEqual(
            self.request(conn, wire, stream)["reason"], "HEARTBEAT_REQUIRED"
        )

        with FIXTURE.with_name("tcp-qt-to-backend.jsonl").open("rb") as fixture:
            heartbeat, request = (read_message(fixture) for _ in range(2))
        with FIXTURE.open("rb") as fixture:
            read_message(fixture)
            read_message(fixture)
            expected_result = read_message(fixture)["data"]
        for message in (heartbeat, request):
            message["data"]["stream_id"] = stream
        request_id = request["data"]["request_id"]

        heartbeat_line = encode(heartbeat["type"], **heartbeat["data"])
        conn.sendall(heartbeat_line[:7])
        self.wait_message(
            wire, "snapshot", lambda data: data["operator"]["link"] == "offline"
        )
        conn.sendall(heartbeat_line[7:] + encode(request["type"], **request["data"]))
        self.assertEqual(self.wait_message(wire, "control_result"), expected_result)

        for _ in range(4):
            conn.sendall(heartbeat_line)
            active = self.wait_message(
                wire, "snapshot", lambda data: data["motor"]["state"] == "TEST_ARMED"
            )
            self.assertTrue(active["operator"]["controls_ready"])

        duplicate = self.request(conn, wire, stream, request_id=request_id)
        self.assertEqual(duplicate["status"], "COMPLETED")
        self.assertEqual(
            self.request(conn, wire, stream, "STOP", request_id=request_id)["reason"],
            "REQUEST_ID_CONFLICT",
        )

        conn.sendall(encode("control_heartbeat", stream_id=str(uuid.uuid4())))
        expired = self.wait_message(
            wire, "snapshot", lambda data: data["operator"]["link"] == "stale"
        )
        self.assertEqual(expired["motor"]["state"], "STOPPED")
        self.assertFalse(expired["operator"]["controls_ready"])
        self.assertGreaterEqual(expired["operator"]["age_ms"], 300)
        self.assertEqual(
            self.request(conn, wire, stream, request_id=request_id), duplicate
        )
        stopped = self.wait_message(
            wire, "snapshot", lambda data: data["operator"]["link"] == "stale"
        )
        self.assertEqual(stopped["motor"]["state"], "STOPPED")

        conn.sendall(heartbeat_line)
        rearmed = self.request(conn, wire, stream)
        self.assertEqual(rearmed["status"], "COMPLETED")
        self.assertEqual(rearmed["cmd_seq"], expected_result["cmd_seq"] + 1)

        wire.close()
        conn.close()
        conn, wire, new_stream = self.connect()
        self.assertNotEqual(new_stream, stream)
        self.assertEqual(self.request(conn, wire, stream)["reason"], "BAD_STREAM")
        self.assertEqual(
            self.request(conn, wire, new_stream)["reason"], "HEARTBEAT_REQUIRED"
        )
        conn.sendall(encode("control_heartbeat", stream_id=new_stream))
        self.assertEqual(
            self.request(conn, wire, new_stream, request_id=request_id)["reason"],
            "REQUEST_FROM_OLD_STREAM",
        )
        after_reconnect = self.request(conn, wire, new_stream)
        self.assertEqual(after_reconnect["status"], "COMPLETED")
        self.assertEqual(after_reconnect["motor_epoch"], expected_result["motor_epoch"])
        self.assertEqual(after_reconnect["cmd_seq"], rearmed["cmd_seq"] + 1)

    def test_argument_validation_and_single_connection(self):
        conn, wire, stream = self.connect()
        with socket.create_connection(("127.0.0.1", self.port), timeout=1) as other:
            self.assertEqual(other.recv(1), b"")
        for opcode, argument, reason in (
            ("ARM_TEST", True, "BAD_ARGUMENT"),
            ("ARM_TEST", 1, "BAD_ARGUMENT"),
            ("UNKNOWN", 0, "BAD_OPCODE"),
            ("START_REAL", 32768, "BAD_ARGUMENT"),
            ("START_REAL", 1, "UNSUPPORTED_OPCODE"),
            ("STOP", 0, "UNSUPPORTED_OPCODE"),
            ("RESET", 0, "UNSUPPORTED_OPCODE"),
        ):
            result = self.request(conn, wire, stream, opcode, argument)
            self.assertEqual(result["status"], "REJECTED")
            self.assertEqual(result["reason"], reason)
            self.assertIsNone(result["motor_epoch"])
            self.assertIsNone(result["cmd_seq"])

    def test_invalid_framing_closes_only_client(self):
        for invalid in (
            b"\n",
            b'{"schema":NaN}\n',
            b"\xff\n",
            b"x" * 16_384 + b"\n",
            b'{"schema":"can-do/1.0","type":"control_heartbeat","data":{}}\n',
        ):
            conn, wire, _ = self.connect()
            conn.sendall(invalid)
            with self.assertRaises(EOFError):
                self.wait_message(wire, "control_result")
            wire.close()
            conn.close()
        self.connect()

    def test_rejected_request_id_cannot_be_corrected_in_place(self):
        conn, wire, stream = self.connect()
        conn.sendall(encode("control_heartbeat", stream_id=stream))
        request_id = str(uuid.uuid4())
        rejected = self.request(
            conn, wire, stream, argument=True, request_id=request_id
        )
        self.assertEqual(rejected["reason"], "BAD_ARGUMENT")
        self.assertEqual(
            self.request(conn, wire, stream, argument=True, request_id=request_id),
            rejected,
        )
        corrected = self.request(conn, wire, stream, argument=0, request_id=request_id)
        self.assertEqual(corrected["reason"], "REQUEST_ID_CONFLICT")
        snapshot = self.wait_message(wire, "snapshot")
        self.assertEqual(snapshot["motor"]["state"], "STOPPED")
        conn.sendall(encode("control_heartbeat", stream_id=stream))
        self.assertEqual(self.request(conn, wire, stream)["status"], "COMPLETED")

    def test_request_limit_preserves_old_results(self):
        conn, wire, stream = self.connect()
        request_id = str(uuid.uuid4())
        first = self.request(conn, wire, stream, "STOP", request_id=request_id)
        self.assertEqual(first["reason"], "UNSUPPORTED_OPCODE")
        for _ in range(254):
            self.assertEqual(
                self.request(conn, wire, stream, "STOP")["reason"], "UNSUPPORTED_OPCODE"
            )
        self.assertEqual(
            self.request(conn, wire, stream, "STOP")["reason"], "REQUEST_LIMIT"
        )
        self.assertEqual(
            self.request(conn, wire, stream, "STOP", request_id=request_id), first
        )

    def test_shutdown_with_active_connection(self):
        _, wire, _ = self.connect()
        self.server.send_signal(signal.SIGTERM)
        with self.assertRaises(EOFError):
            self.wait_message(wire, "control_result")
        self.assertEqual(self.server.wait(timeout=3), 0)


if __name__ == "__main__":
    unittest.main()
