"""TCP 조작 요청을 메모리 안의 모의 CAN 버스로 연결한다."""

import argparse
import asyncio
import time
from pathlib import Path

from .can_control.boot_id import next_boot_id
from .can_control.controller import POLL_INTERVAL
from .can_control.mock_motor import MemoryCan, MockMotor
from .can_control.tcp import TcpCanControl
from .can_rx.codec import Mode
from .tcp_stream import PORT, run_tcp_server

DEFAULT_BOOT_ID_PATH = Path(".local/can-mock-boot-id")


class TcpCanMock(TcpCanControl):
    def __init__(self, boot_id, mode=Mode.TEST):
        self.bus = MemoryCan(MockMotor(mode))
        super().__init__(boot_id, self.bus.try_send)

    async def run_bus(self):
        while True:
            self.bus.step(self.controller, time.monotonic_ns())
            if self.client is None:
                self.control.drain()
            await asyncio.sleep(POLL_INTERVAL)


async def serve(host, port, boot_id, mode):
    mock = TcpCanMock(boot_id, mode)
    await run_tcp_server(mock, host, port, mock.run_bus)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--boot-id-file", type=Path, default=DEFAULT_BOOT_ID_PATH)
    parser.add_argument("--motor-mode", choices=("REAL", "TEST"), default="TEST")
    args = parser.parse_args()
    try:
        boot_id = next_boot_id(args.boot_id_file)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    asyncio.run(serve(args.host, args.port, boot_id, Mode[args.motor_mode]))


if __name__ == "__main__":
    main()
