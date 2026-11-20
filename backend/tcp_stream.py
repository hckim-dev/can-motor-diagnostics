"""CAN DO TCP 연결, 프레이밍, 송신 주기와 연결 종료를 관리한다."""

import asyncio
import json
import logging
import signal
import time
import uuid
from contextlib import asynccontextmanager, contextmanager

if __package__:
    from .can_rx.codec import VERSION
else:
    from can_rx.codec import VERSION

SCHEMA = "can-do/1.1"
CAN_PROTOCOL = ".".join(map(str, VERSION))
PORT = 5000
MAX_LINE_BYTES = 16_384
SNAPSHOT_INTERVAL = 0.1
WRITE_TIMEOUT = 2.0
CONTROL_TYPES = ("control_heartbeat", "control_request")
LOGGER = logging.getLogger(__name__)


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
    if not isinstance(kind, str) or not kind or not isinstance(data, dict):
        raise ValueError("제어 메시지 형식 오류")

    return kind, data


class TcpStream:
    source = "mock"
    capabilities = ()

    def __init__(self, backend_session=None):
        self.backend_session = backend_session or str(uuid.uuid4())
        self.started_ns = time.monotonic_ns()
        self.control = None
        self.client = None
        self.stopping = False

    def close(self):
        self.stopping = True
        if self.client is not None:
            self.client.transport.abort()

    def notifications(self):
        return ()

    def stream_closed(self):
        pass

    def pending_results(self, session):
        return ()

    def client_disconnected(self, session):
        pass

    def open_session(self, stream_id):
        return self.control.open_session(stream_id) if self.control else None

    async def handle_client(self, reader, writer):
        if self.stopping or self.client is not None:
            writer.transport.abort()
            return

        self.client = writer
        stream_id = str(uuid.uuid4())
        seq = 0
        session = None

        async def send(kind, data):
            nonlocal seq
            message = {
                "schema": SCHEMA,
                "type": kind,
                "stream_id": stream_id,
                "stream_seq": seq,
                "source": self.source,
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
                    "capabilities": self.capabilities,
                },
            )

            session = self.open_session(stream_id)
            await self.stream_snapshots(reader, send, session)
        except (TimeoutError, ConnectionError):
            pass
        finally:
            writer.transport.abort()
            self.client = None
            if session is not None:
                self.client_disconnected(session)
            self.stream_closed()

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
                        result = None
                        if kind in CONTROL_TYPES:
                            result = session.handle(kind, data, now)
                        else:
                            LOGGER.debug("지원하지 않는 TCP 메시지 타입: %.80r", kind)
                    except (ValueError, RecursionError):
                        break
                    if result is not None:
                        await send("control_result", result)
                    read_task = asyncio.create_task(read())

                now = loop.time()
                if now >= next_snapshot:
                    if session:
                        session.expire(now)
                        for result in self.pending_results(session):
                            await send("control_result", result)
                    for kind, data in self.notifications():
                        await send(kind, data)
                    await send("snapshot", self.snapshot(session, now))
                    next_snapshot = now + SNAPSHOT_INTERVAL
        finally:
            read_task.cancel()
            await asyncio.gather(read_task, return_exceptions=True)


async def wait_for_stop(stop, background):
    if background is None:
        await stop.wait()
        return

    worker = asyncio.create_task(background())
    shutdown = asyncio.create_task(stop.wait())

    try:
        done, _ = await asyncio.wait(
            (worker, shutdown), return_when=asyncio.FIRST_COMPLETED
        )
        if worker in done:
            worker.result()
        await shutdown
    finally:
        for task in (worker, shutdown):
            task.cancel()
        await asyncio.gather(worker, shutdown, return_exceptions=True)


@contextmanager
def shutdown_signal():
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    signals = (signal.SIGINT, signal.SIGTERM)
    for sig in signals:
        loop.add_signal_handler(sig, stop.set)
    try:
        yield stop
    finally:
        for sig in signals:
            loop.remove_signal_handler(sig)


@asynccontextmanager
async def listen(stream, host, port):
    async with await asyncio.start_server(
        stream.handle_client,
        host,
        port,
        limit=MAX_LINE_BYTES,
    ) as server:
        addresses = ", ".join(str(sock.getsockname()) for sock in server.sockets)
        print(f"CAN DO TCP listening on {addresses}", flush=True)
        try:
            yield
        finally:
            stream.close()


async def run_tcp_server(stream, host, port, background=None):
    with shutdown_signal() as stop:
        async with listen(stream, host, port):
            await wait_for_stop(stop, background)
