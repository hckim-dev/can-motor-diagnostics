"""CAN·FPGA UART를 기록하고 TCP에서 상태와 판정을 관측한다."""

import argparse
import asyncio
import termios
from contextlib import ExitStack
from functools import partial
from pathlib import Path

from ..diagnostics.journal import Journal
from ..diagnostics.verdict import Config
from ..hardware.socketcan import open_can, read_can_observation
from ..hardware.uart import open_uart, read_uart
from ..tcp_stream import PORT, listen, shutdown_signal
from .inputs import DeviceFeed, FixtureFeed
from .service import Monitor


async def serve(journal, config, feed, host, port):
    monitor = Monitor(journal, config, feed)
    with shutdown_signal() as stop:
        async with listen(monitor.stream, host, port):
            await monitor.run(stop)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="새 기록 디렉터리")
    parser.add_argument("--fixture", type=Path, help="시간순 모의 진단 입력 JSONL")
    parser.add_argument("--can", help="SocketCAN 수신 인터페이스")
    parser.add_argument("--uart", type=Path, help="FPGA 전용 tty 경로")
    parser.add_argument(
        "--gpio-verified", action="store_true", help="모의 GPIO 검증 전제"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()
    if bool(args.fixture) == bool(args.can or args.uart):
        parser.error(
            "--fixture 또는 --can/--uart 중 한 가지 입력 방식을 지정해야 합니다"
        )
    if args.gpio_verified and not args.fixture:
        parser.error("--gpio-verified는 모의 입력에서만 사용할 수 있습니다")

    config = Config(gpio_verified=args.gpio_verified, functional_only=not args.fixture)
    try:
        with ExitStack() as stack:
            hardware = None
            if args.fixture:
                file = stack.enter_context(args.fixture.open(encoding="utf-8"))
            else:
                hardware = {
                    "can_interface": args.can,
                    "fpga_uart": str(args.uart) if args.uart else None,
                    "fpga_status_watchdog": bool(args.uart),
                }
            journal = stack.enter_context(
                Journal(args.output, config, hardware=hardware)
            )
            feed = (
                FixtureFeed(file, journal.evidence_session)
                if args.fixture
                else DeviceFeed(
                    {},
                    {"can": read_can_observation, "fpga": read_uart},
                    can_open=(
                        partial(open_can, args.can, count_overflow=True)
                        if args.can
                        else None
                    ),
                    fpga_open=(
                        partial(open_uart, args.uart, discard_pending=True)
                        if args.uart
                        else None
                    ),
                )
            )
            asyncio.run(serve(journal, config, feed, args.host, args.port))
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        OverflowError,
        termios.error,
    ) as exc:
        parser.exit(1, f"통합 서비스 오류: {exc}\n")


if __name__ == "__main__":
    main()
