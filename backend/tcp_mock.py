"""모터 상태 전송과 시험 준비 요청을 검증하는 TCP 목업."""

import argparse
import asyncio
import time
from functools import partial
from pathlib import Path

if __package__:
    from .can_rx.fixture import feed_frames, load_frames
    from .can_rx.state import CanReceiver
    from .tcp_mock_control import SNAPSHOT, MockControl
    from .tcp_stream import PORT, TcpStream, run_tcp_server
else:
    from can_rx.fixture import feed_frames, load_frames
    from can_rx.state import CanReceiver
    from tcp_mock_control import SNAPSHOT, MockControl
    from tcp_stream import PORT, TcpStream, run_tcp_server


class TcpMock(TcpStream):
    def __init__(self, control=False, can_input=False):
        if control and can_input:
            raise ValueError("조작 목업과 CAN 수신 목업을 동시에 사용할 수 없다")

        super().__init__()
        self.control = MockControl() if control else None
        self.can_receiver = CanReceiver(self.backend_session) if can_input else None

    def snapshot(self, session, now):
        if self.can_receiver is not None:
            return {
                "test": SNAPSHOT["test"],
                "services": {**SNAPSHOT["services"], "log": "DISABLED"},
                **self.can_receiver.snapshot(time.monotonic_ns()),
            }

        return session.snapshot(now) if session else SNAPSHOT


async def serve(host, port, control=False, can_frames=None):
    mock = TcpMock(control, can_input=can_frames is not None)
    background = (
        partial(feed_frames, can_frames, mock.can_receiver)
        if can_frames is not None
        else None
    )
    await run_tcp_server(mock, host, port, background)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=PORT)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--control", action="store_true", help="조작 목업을 활성화한다.")
    mode.add_argument(
        "--can-fixture", type=Path, help="시간순 CAN 입력 예제를 사용한다."
    )
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
