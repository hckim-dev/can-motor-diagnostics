"""공통 예제로 TCP 목업의 응답과 재접속을 검증한다."""

import argparse
import json
import socket
import time
import uuid
from pathlib import Path

MAX_LINE_BYTES = 16_384
FIXTURE = Path(__file__).with_name("fixtures") / "tcp-backend-to-qt.jsonl"


def reject_constant(value):
    raise ValueError(f"Invalid JSON constant: {value}")


def read_message(stream):
    line = stream.readline(MAX_LINE_BYTES + 1)
    if not line:
        raise EOFError("Connection closed before the next TCP message")
    if len(line) > MAX_LINE_BYTES or not line.endswith(b"\n"):
        raise ValueError(
            "Expected a complete LF-terminated TCP message within 16384 bytes"
        )
    message = json.loads(line.decode("utf-8"), parse_constant=reject_constant)
    if not isinstance(message, dict):
        raise ValueError("Expected a JSON object")
    return message


def require(condition, message):
    if not condition:
        raise ValueError(message)


def check_stream(
    host, port, hello_data, snapshot_data, snapshot_count, *, reconnect=False
):
    with (
        socket.create_connection((host, port), timeout=2) as sock,
        sock.makefile("rb") as stream,
    ):
        try:
            hello = read_message(stream)
        except EOFError:
            # 서버가 이전 연결의 종료를 아직 처리 중이면 잠시 뒤 다시 접속한다.
            if reconnect:
                return None
            raise
        stream_id = uuid.UUID(hello["stream_id"])
        backend_session = uuid.UUID(hello["data"]["backend_session"])
        expected_hello = {**hello_data, "backend_session": str(backend_session)}
        previous_ms = -1
        first_snapshot_ms = None
        start = time.monotonic()
        for seq in range(snapshot_count + 1):
            message = hello if seq == 0 else read_message(stream)
            expected_type = "hello" if seq == 0 else "snapshot"
            expected_data = expected_hello if seq == 0 else snapshot_data
            require(
                message["schema"] == "can-do/1.1" and message["source"] == "mock",
                "Unexpected schema or source",
            )
            require(
                message["type"] == expected_type
                and type(message["stream_seq"]) is int
                and message["stream_seq"] == seq
                and message["stream_id"] == str(stream_id),
                f"Unexpected type, sequence or stream ID at message {seq}",
            )
            require(
                json.dumps(message["data"], sort_keys=True)
                == json.dumps(expected_data, sort_keys=True),
                f"Unexpected {expected_type} data",
            )
            mono_ms = message["mono_ms"]
            require(
                type(mono_ms) is int and mono_ms >= 0 and mono_ms >= previous_ms,
                "Invalid monotonic timestamp",
            )
            if seq > 1:
                interval_ms = mono_ms - previous_ms
                require(
                    50 <= interval_ms <= 150,
                    f"Unexpected snapshot interval at message {seq}: {interval_ms}ms",
                )
            if seq == 1:
                first_snapshot_ms = mono_ms
                start = time.monotonic()
            previous_ms = mono_ms

    if snapshot_count > 1:
        expected_seconds = (snapshot_count - 1) / 10
        sent_seconds = (mono_ms - first_snapshot_ms) / 1000
        received_seconds = time.monotonic() - start
        for label, elapsed in (("send", sent_seconds), ("receive", received_seconds)):
            require(
                abs(elapsed - expected_seconds) <= 0.25,
                f"Unexpected {label} cadence: {elapsed:.3f}s for {snapshot_count - 1} intervals",
            )
        print(
            f"{snapshot_count} snapshots: sent in {sent_seconds:.3f}s, received in {received_seconds:.3f}s"
        )
    return stream_id, backend_session


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args()
    try:
        with FIXTURE.open("rb") as fixture:
            hello_data = read_message(fixture)["data"]
            snapshot_data = read_message(fixture)["data"]
        stream_id, session = check_stream(
            args.host, args.port, hello_data, snapshot_data, 11
        )
        deadline = time.monotonic() + 1
        while True:
            result = check_stream(
                args.host, args.port, hello_data, snapshot_data, 1, reconnect=True
            )
            if result is not None:
                new_stream_id, same_session = result
                break
            require(time.monotonic() < deadline, "Reconnect closed before hello")
            time.sleep(0.02)
        require(
            new_stream_id != stream_id and same_session == session,
            "Reconnect must start a new stream within the same backend session",
        )
    except (EOFError, OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(1, f"FAIL: {exc}\n")
    print("PASS: hello -> TEST / STOPPED snapshot, 10Hz cadence, reconnect")


if __name__ == "__main__":
    main()
