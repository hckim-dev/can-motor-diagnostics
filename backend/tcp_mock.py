"""모터 상태 전송과 시험 준비 요청을 검증하는 TCP 목업."""

import argparse
import asyncio
import json
import signal
import time
import uuid
from pathlib import Path

if __package__:
    from .can_rx.codec import VERSION
    from .can_rx.fixture import feed_frames, load_frames
    from .can_rx.state import CanReceiver
    from .tcp_mock_control import SNAPSHOT, MockControl
else:
    from can_rx.codec import VERSION
    from can_rx.fixture import feed_frames, load_frames
    from can_rx.state import CanReceiver
    from tcp_mock_control import SNAPSHOT, MockControl

SCHEMA = "can-do/1.1"
CAN_PROTOCOL = ".".join(map(str, VERSION))
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
    def __init__(self, control=False, can_input=False):
        if control and can_input:
            raise ValueError("조작 목업과 CAN 수신 목업을 동시에 사용할 수 없다")

        self.backend_session = str(uuid.uuid4())
        self.started_ns = time.monotonic_ns()
        self.control = MockControl() if control else None
        self.can_receiver = CanReceiver(self.backend_session) if can_input else None
        self.client = None
        self.stopping = False

    def close(self):
        self.stopping = True
        if self.client is not None:
            self.client.transport.abort()

    def snapshot(self, session, now):
        if self.can_receiver is not None:
            return {
                "test": SNAPSHOT["test"],
                "services": {**SNAPSHOT["services"], "log": "DISABLED"},
                **self.can_receiver.snapshot(time.monotonic_ns()),
            }

        return session.snapshot(now) if session else SNAPSHOT

    async def handle_client(self, reader, writer):
        if self.stopping or self.client is not None:
            writer.transport.abort()
            return

        self.client = writer
        stream_id = str(uuid.uuid4())
        seq = 0

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
                raise ValueError("TCP 메시지 길이 제한 초과")

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
                    "protocol": CAN_PROTOCOL,
                    "replay_of": None,
                    "capabilities": [],
                },
            )

            session = self.control.open_session(stream_id) if self.control else None
            await self.stream_snapshots(reader, send, session)
        except (TimeoutError, ConnectionError):
            pass
        finally:
            writer.transport.abort()
            self.client = None

    async def stream_snapshots(self, reader, send, session):
        read = reader.readline if session else lambda: reader.read(1)
        loop = asyncio.get_running_loop()
        await send("snapshot", self.snapshot(session, loop.time()))
        next_snapshot = loop.time() + SNAPSHOT_INTERVAL
        read_task = asyncio.create_task(read())

        try:
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
                    await send("snapshot", self.snapshot(session, now))
                    next_snapshot = now + SNAPSHOT_INTERVAL
        finally:
            read_task.cancel()
            await asyncio.gather(read_task, return_exceptions=True)


async def wait_for_stop(stop, can_frames, can_receiver):
    if can_frames is None:
        await stop.wait()
        return

    feeder = asyncio.create_task(feed_frames(can_frames, can_receiver))
    shutdown = asyncio.create_task(stop.wait())

    try:
        done, _ = await asyncio.wait(
            (feeder, shutdown), return_when=asyncio.FIRST_COMPLETED
        )
        if feeder in done:
            feeder.result()
        await shutdown
    finally:
        for task in (feeder, shutdown):
            task.cancel()
        await asyncio.gather(feeder, shutdown, return_exceptions=True)


async def serve(host, port, control=False, can_frames=None):
    mock = TcpMock(control, can_input=can_frames is not None)
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
            await wait_for_stop(stop, can_frames, mock.can_receiver)
        finally:
            mock.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=PORT)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--control", action="store_true", help="조작 목업을 활성화한다."
    )
    mode.add_argument("--can-fixture", type=Path, help="시간순 CAN 입력 예제를 사용한다.")
    args = parser.parse_args()

    try:
        can_frames = (
            load_frames(args.can_fixture) if args.can_fixture is not None else None
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    asyncio.run(serve(args.host, args.port, args.control, can_frames))


if __name__ == "__main__":
    main()
