"""운영자가 확인한 펌웨어 빌드와 시험 설정을 기록한다."""

import argparse


def positive(value):
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("양의 정수를 입력해야 합니다") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("양의 정수를 입력해야 합니다")
    return number


def add_metadata_arguments(parser):
    parser.add_argument("--motor-build", help="실제 사용한 모터 펌웨어 빌드 ID")
    parser.add_argument("--fpga-build", help="실제 사용한 FPGA 빌드 ID")
    parser.add_argument(
        "--encoder-counts-per-rev", type=positive, help="출력축 1회전당 x4 카운트"
    )
    parser.add_argument("--rpm-max", type=positive, help="펌웨어 설정의 최대 RPM")
    parser.add_argument(
        "--feedback-timeout-ms", type=positive, help="펌웨어의 피드백 검출 시간(ms)"
    )


def recording_metadata(args):
    if args.rpm_max is not None and args.rpm_max > 32767:
        raise ValueError("기록용 RPM 상한은 32767 이하여야 합니다")
    return {
        "firmware_build": {"motor": args.motor_build, "fpga": args.fpga_build},
        "run_settings": {
            "encoder_counts_per_rev": args.encoder_counts_per_rev,
            "rpm_max": args.rpm_max,
            "feedback_timeout_ms": args.feedback_timeout_ms,
        },
    }
