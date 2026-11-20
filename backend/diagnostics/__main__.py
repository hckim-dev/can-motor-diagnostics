"""모의 시험 입력을 판정·기록하고 저장한 판정을 리플레이한다."""

import argparse
import time
from pathlib import Path

from .journal import Journal, evaluate, json_line, load_manifest, verify_results
from .verdict import Config


class ReplayClock:
    def __init__(self, fast):
        self.fast = fast
        self.origin_ns = None
        self.started_ns = None

    def __call__(self, original_ns):
        if self.origin_ns is None:
            self.origin_ns = original_ns
            self.started_ns = time.monotonic_ns()
        elapsed = original_ns - self.origin_ns
        if not self.fast:
            remaining = self.started_ns + elapsed - time.monotonic_ns()
            if remaining > 0:
                time.sleep(remaining / 1_000_000_000)
        return time.monotonic_ns()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    record = commands.add_parser("record")
    record.add_argument("source", type=Path, help="모의 입력 JSONL 파일")
    record.add_argument("output", type=Path, help="새 기록 디렉터리")
    record.add_argument(
        "--gpio-verified", action="store_true", help="예제의 GPIO 검증 전제 적용"
    )
    record.add_argument(
        "--functional-only", action="store_true", help="기능 관측만 기록"
    )

    replay = commands.add_parser("replay")
    replay.add_argument("source", type=Path, help="원본 기록 디렉터리")
    replay.add_argument("output", type=Path, help="새 리플레이 기록 디렉터리")
    replay.add_argument(
        "--fast", action="store_true", help="실제 대기 없이 원본 시각으로 재생"
    )
    args = parser.parse_args()

    try:
        original = None
        pace = None
        source = args.source
        if args.command == "replay":
            original, config = load_manifest(source)
            source = source / "inputs.jsonl"
            pace = ReplayClock(args.fast)
        else:
            config = Config(
                gpio_verified=args.gpio_verified, functional_only=args.functional_only
            )
        with (
            source.open(encoding="utf-8") as file,
            Journal(args.output, config, original=original) as journal,
        ):
            results = evaluate(file, journal, config, pace=pace)
            if original is not None:
                verify_results(args.source, results)
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
        parser.exit(1, f"시험 기록 처리 실패: {exc}\n")
    for result in results:
        print(json_line(result), end="")


if __name__ == "__main__":
    main()
