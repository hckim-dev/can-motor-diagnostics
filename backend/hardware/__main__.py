"""실물 장치의 수신 기록과 별도 CAN 송신 제출을 점검한다."""

import argparse
import math
import termios
from pathlib import Path

from ..diagnostics.journal import json_line
from .capture import TIME_UNIT_NS, capture_hardware
from .tx_probe import send_hello_probe


def seconds(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("초는 유한한 양수여야 합니다")
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    capture = commands.add_parser("capture", help="CAN 또는 FPGA UART 수신을 기록")
    capture.add_argument("output", type=Path, help="새 기록 디렉터리")
    capture.add_argument("--can", help="올라온 SocketCAN 인터페이스 이름")
    capture.add_argument("--uart", type=Path, help="FPGA 전용 tty 경로")
    capture.add_argument("--seconds", type=seconds, default=5.0)

    probe = commands.add_parser("can-tx-probe", help="OP_HELLO 한 프레임을 제출")
    probe.add_argument("--can", required=True)
    probe.add_argument("--boot-id-file", type=Path, required=True)
    probe.add_argument(
        "--motor-power-off", action="store_true", help="모터 6V 물리 차단 확인"
    )
    probe.add_argument("--timeout", type=seconds, default=1.0)
    args = parser.parse_args()

    try:
        if args.command == "capture":
            if not (args.can or args.uart):
                parser.error("--can 또는 --uart를 하나 이상 지정해야 합니다")
            result = capture_hardware(
                args.output, args.can, args.uart, int(args.seconds * TIME_UNIT_NS)
            )
            ok = result["ok"]
        else:
            result = send_hello_probe(
                args.can,
                args.boot_id_file,
                args.timeout,
                motor_power_off=args.motor_power_off,
            )
            ok = result["socket_echo"]
    except (OSError, ValueError, OverflowError, termios.error) as exc:
        parser.exit(1, f"장치 확인 실패: {exc}\n")
    print(json_line(result), end="")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
