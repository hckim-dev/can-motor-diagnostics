"""모터 상태 전송과 시험 준비 요청을 검증하는 TCP 목업."""

import argparse
import asyncio
import json
import signal
import time
import uuid

if __package__:
    from .tcp_mock_control import SNAPSHOT, MockControl
else:
    from tcp_mock_control import SNAPSHOT, MockControl

SCHEMA = "can-do/1.1"
PORT = 5000
MAX_LINE_BYTES = 16_384
SNAPSHOT_INTERVAL = 0.1
WRITE_TIMEOUT = 2.0


def reject_constant(value):
    raise ValueError(f"허용되지 않는 JSON 값: {value}")


def decode_control_message(line):
    if len(line) > MAX_LINE_BYTES or not line.endswith(b"\n"):
        raise ValueError("TCP 메시지 길이 또는 줄 끝 오류")
    message = json.loads(line.decode("utf-8"), parse_constant=reject_constant)
    if not isinstance(message, dict) or message.get("schema") != SCHEMA:
        raise ValueError("TCP 스키마 오류")
    kind = message.get("type")
    data = message.get("data")
    if kind not in ("control_heartbeat", "control_request") or not isinstance(data, dict):
        raise ValueError("제어 메시지 형식 오류")
    return kind, data


class TcpMock:
    def __init__(self, control=False):
        self.backend_session = str(uuid.uuid4())
        self.started_ns = time.monotonic_ns()
        self.control = MockControl() if control else None
        self.client = None
        self.stopping = False

    def close(self):
        self.stopping = True
        if self.client is not None:
            self.client.transport.abort()

    async def handle_client(self, reader, writer):
        if self.stopping or self.client is not None:
            writer.transport.abort()
            return
        self.client = writer
        stream_id = str(uuid.uuid4())
        seq = 0
        read_task = None

        async def send(kind, data):
            nonlocal seq
            message = {
                "schema": SCHEMA,
                "type": kind,
                "stream_id": stream_id,
                "stream_seq": seq,
                "source": "mock",
                "mono_ms": (time.monotonic_ns() - self.started_ns) // 1_000_000,
                "data": data,
            }
            line = (
                json.dumps(message, separators=(",", ":"), allow_nan=False) + "\n"
            ).encode("utf-8")
            if len(line) > MAX_LINE_BYTES:
                raise ValueError("TCP message exceeds the line limit")
            writer.write(line)
            drain = asyncio.create_task(writer.drain())
            try:
                done, _ = await asyncio.wait((drain,), timeout=WRITE_TIMEOUT)
                if not done:
                    raise TimeoutError("TCP 송신 대기 시간 초과")
                drain.result()
            except BaseException:
                drain.cancel()
                await asyncio.gather(drain, return_exceptions=True)
                raise
            seq += 1

        try:
            await send(
                "hello",
                {
                    "backend_session": self.backend_session,
                    "protocol": "2.1",
                    "replay_of": None,
                    "capabilities": [],
                },
            )
            session = self.control.open_session(stream_id) if self.control else None
            read = reader.readline if session else lambda: reader.read(1)
            await send("snapshot", SNAPSHOT)
            loop = asyncio.get_running_loop()
            next_snapshot = loop.time() + SNAPSHOT_INTERVAL
            read_task = asyncio.create_task(read())
            while True:
                deadline = next_snapshot
                if session and session.heartbeat_deadline is not None:
                    deadline = min(deadline, session.heartbeat_deadline)
                done, _ = await asyncio.wait(
                    (read_task,), timeout=max(0, deadline - loop.time())
                )
                now = loop.time()
                if session:
                    session.expire(now)
                if read_task in done:
                    try:
                        line = read_task.result()
                        if not line or session is None:
                            break
                        kind, data = decode_control_message(line)
                        result = session.handle(kind, data, now)
                    except (ValueError, RecursionError):
                        break
                    if result is not None:
                        await send("control_result", result)
                    read_task = asyncio.create_task(read())
                now = loop.time()
                if now >= next_snapshot:
                    if session:
                        session.expire(now)
                    await send(
                        "snapshot", session.snapshot(now) if session else SNAPSHOT
                    )
                    next_snapshot = now + SNAPSHOT_INTERVAL
        except (TimeoutError, ConnectionError):
            pass
        finally:
            writer.transport.abort()
            self.client = None
            if read_task is not None:
                read_task.cancel()
                await asyncio.gather(read_task, return_exceptions=True)


async def serve(host, port, control=False):
    mock = TcpMock(control)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    async with await asyncio.start_server(
        mock.handle_client,
        host,
        port,
        limit=MAX_LINE_BYTES,
    ) as server:
        addresses = ", ".join(str(sock.getsockname()) for sock in server.sockets)
        print(f"CAN DO TCP mock listening on {addresses}", flush=True)
        try:
            await stop.wait()
        finally:
            mock.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument(
        "--control", action="store_true", help="조작 목업을 활성화한다."
    )
    args = parser.parse_args()
    asyncio.run(serve(args.host, args.port, args.control))


if __name__ == "__main__":
    main()
