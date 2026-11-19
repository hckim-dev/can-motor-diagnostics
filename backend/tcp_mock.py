"""Read-only CAN DO TCP mock. Run with python3 backend/tcp_mock.py."""

import argparse
import asyncio
import json
import signal
import time
import uuid
from asyncio import TimeoutError as AsyncTimeoutError

SCHEMA = "can-do/1.1"
PORT = 5000
MAX_LINE_BYTES = 16_384
SNAPSHOT_INTERVAL = 0.1
WRITE_TIMEOUT = 2.0

SNAPSHOT = {
    "motor": {
        "epoch": 65537,
        "state": "STOPPED",
        "mode": "TEST",
        "fault_mask": 0,
        "link": "fresh",
        "age_ms": 0,
        "sample": {
            "seq": None,
            "tick_ms": None,
            "actual_rpm": None,
            "target_rpm": None,
            "pwm_permille": None,
            "current_ma": None,
            "bus_mv": None,
            "sensor_age_ms": None,
            "sample_complete": False,
            "quality": "unavailable",
            "sample_mode": "UNKNOWN",
            "sample_state": "UNKNOWN",
            "synthetic": False,
        },
    },
    "operator": {
        "link": "offline",
        "age_ms": None,
        "estop_active": False,
        "controls_ready": None,
    },
    "test": {"run_id": None, "scenario_id": None, "status": "IDLE", "reason": None},
    "services": {"can": "OK", "fpga": "OFFLINE", "log": "OK", "ai": "DISABLED"},
}


class TcpMock:
    def __init__(self):
        self.backend_session = str(uuid.uuid4())
        self.started_ns = time.monotonic_ns()
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
            await asyncio.wait_for(writer.drain(), WRITE_TIMEOUT)
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
            while True:
                deadline = asyncio.get_running_loop().time() + SNAPSHOT_INTERVAL
                await send("snapshot", SNAPSHOT)
                # EOF and any client bytes both end the read-only connection.
                remaining = deadline - asyncio.get_running_loop().time()
                try:
                    await asyncio.wait_for(reader.read(1), max(remaining, 0.001))
                except AsyncTimeoutError:
                    continue
                break
        except (AsyncTimeoutError, ConnectionError):
            pass
        finally:
            writer.transport.abort()
            self.client = None


async def serve(host, port):
    mock = TcpMock()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    async with await asyncio.start_server(mock.handle_client, host, port) as server:
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
    args = parser.parse_args()
    asyncio.run(serve(args.host, args.port))


if __name__ == "__main__":
    main()
