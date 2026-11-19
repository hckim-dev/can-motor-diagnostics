"""바이너리 캡처나 hex 예제에서 FPGA UART 수신을 확인한다."""

import argparse
import json
import time
from dataclasses import asdict
from io import BytesIO
from pathlib import Path

from .session import UartReceiver

READ_SIZE = 4096


def inspect_stream(source):
    receiver = UartReceiver()
    while data := source.read(READ_SIZE):
        print_observations(receiver.feed(data, time.monotonic_ns()))
    print_observations(receiver.finish(time.monotonic_ns()))


def print_observations(observations):
    for observation in observations:
        row = asdict(observation)
        row["raw"] = observation.raw.hex(" ")
        if observation.frame is not None:
            row["frame"]["kind"] = observation.frame.kind.name
            row["frame"]["phase"] = observation.frame.phase.name
        for field in ("run", "interrupted_run"):
            run = getattr(observation, field)
            if run is not None:
                row[field]["run_id"] = run.run_id
        print(json.dumps(row, separators=(",", ":")))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="UART 캡처 또는 hex 예제 경로")
    parser.add_argument("--hex", action="store_true", help="입력을 hex 텍스트로 읽는다")
    args = parser.parse_args()

    try:
        source = (
            BytesIO(bytes.fromhex(args.path.read_text(encoding="ascii")))
            if args.hex
            else args.path.open("rb")
        )
        with source:
            inspect_stream(source)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
