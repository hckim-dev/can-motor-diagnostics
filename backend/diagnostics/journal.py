"""시험 원본과 결과를 저장하고 원본 입력 순서로 재현한다."""

import json
import logging
import os
import shutil
from contextlib import ExitStack
from dataclasses import asdict, fields
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from time import monotonic_ns
from uuid import UUID, uuid4

from ..can_rx.codec import message_motor_boot
from .engine import DiagnosticEngine
from .inputs import Input
from .measurements import MeasurementLog
from .storage import (
    LOG_NAMES,
    LOW_SPACE_BYTES,
    ROTATE_BYTES,
    RecordedLog,
    SegmentedFile,
    validate_segments,
)
from .verdict import Config

FORMAT_VERSION = 1
RECORDING_POLICY_VERSION = 1
PROTOCOL_VERSION = {"can": "2.1", "fpga_uart": "1.0"}
UTC_ZONE = timezone(timedelta(0))
FLUSH_INTERVAL_NS = 1_000_000_000
LOGGER = logging.getLogger(__name__)


def utc_now():
    return datetime.now(UTC_ZONE).isoformat()


def json_line(value):
    return json.dumps(value, separators=(",", ":"), allow_nan=False) + "\n"


def original_row(record, rx_utc):
    return {**record.as_row(), "rx_utc": rx_utc}


def sync(file):
    file.flush()
    os.fsync(file.fileno())


def iter_inputs(file, session, *, recorded=False):
    for index, line in enumerate(file, 1):
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("입력은 JSON 객체여야 한다")
            if recorded and (
                type(row.get("ingest_seq")) is not int
                or not {"backend_session", "record_id", "rx_utc"} <= row.keys()
            ):
                raise ValueError("원본 식별자 또는 관측 시각이 없는 기록")
            yield Input.from_row(row, session, index), row.get("rx_utc")
        except (ValueError, TypeError, KeyError) as exc:
            raise ValueError(f"{file.name}:{index}: {exc}") from exc


def load_manifest(path: Path):
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    if (
        not isinstance(manifest, dict)
        or manifest.get("format_version") != FORMAT_VERSION
    ):
        raise ValueError("지원하지 않는 시험 기록 버전")
    for name in ("backend_session", "evidence_session"):
        if str(UUID(manifest[name])) != manifest[name]:
            raise ValueError("기록 세션 ID 형식 오류")
    if manifest.get("protocol_version") != PROTOCOL_VERSION:
        raise ValueError("지원하지 않는 기록의 통신 규격")
    policy = manifest.get("recording_policy_version", 0)
    if type(policy) is not int or policy not in (0, RECORDING_POLICY_VERSION):
        raise ValueError("지원하지 않는 기록 운영 정책 버전")
    values = manifest.get("config")
    if not isinstance(values, dict) or values.keys() != {
        item.name for item in fields(Config)
    }:
        raise ValueError("원본 기록의 시험 설정이 불완전하다")
    config = Config(**values)
    if manifest["scenario_version"] != config.scenario_version:
        raise ValueError("기록의 시험 프로파일 불일치")
    validate_segments(manifest)
    return manifest, config


def verify_results(directory: Path, results):
    manifest, _ = load_manifest(directory)
    saved = [
        json.loads(line)["context"]
        for line in RecordedLog(directory, "events.jsonl", manifest)
    ]
    if saved != results:
        raise ValueError("리플레이 판정이 원본 결과와 일치하지 않는다")


def verify_source(source, manifest):
    digest = sha256()
    last_ns = 0
    for record, rx_utc in iter_inputs(
        source, manifest["evidence_session"], recorded=True
    ):
        if record.rx_mono_ns < last_ns:
            raise ValueError("원본 기록 시각이 역행한다")
        last_ns = record.rx_mono_ns
        digest.update(json_line(original_row(record, rx_utc)).encode("utf-8"))
    if digest.hexdigest() != manifest["input_sha256"]:
        raise ValueError("원본 입력의 해시 불일치")
    if not isinstance(source, RecordedLog):
        source.seek(0)


class Journal:
    def __init__(
        self,
        directory,
        config,
        *,
        original=None,
        hardware=None,
        metadata=None,
        rotate_bytes=ROTATE_BYTES,
        low_space_bytes=LOW_SPACE_BYTES,
    ):
        if type(rotate_bytes) is not int or rotate_bytes <= 0:
            raise ValueError("기록 회전 크기는 양의 정수여야 한다")
        if type(low_space_bytes) is not int or low_space_bytes < 0:
            raise ValueError("기록 여유 공간 기준은 음이 아닌 정수여야 한다")
        self.directory = directory
        self.rotate_bytes = rotate_bytes
        self.low_space_bytes = low_space_bytes
        self.backend_session = str(uuid4())
        self.evidence_session = (
            original["evidence_session"] if original else self.backend_session
        )
        self.original = original
        self.on_event = None
        self.manifest = {
            "format_version": FORMAT_VERSION,
            "backend_version": f"diagnostics/{FORMAT_VERSION}",
            "protocol_version": PROTOCOL_VERSION,
            "firmware_build": {"motor": None, "fpga": None},
            "source": "replay" if original else "live" if hardware else "mock",
            "backend_session": self.backend_session,
            "evidence_session": self.evidence_session,
            "replay_of": original["backend_session"] if original else None,
            "scenario_version": config.scenario_version,
            "config": asdict(config),
            "started_utc": utc_now(),
        }
        if original:
            for key in ("firmware_build", "run_settings", "recording_policy_version"):
                if key in original:
                    self.manifest[key] = original[key]
        else:
            self.manifest["recording_policy_version"] = RECORDING_POLICY_VERSION
            if metadata:
                self.manifest["firmware_build"] = metadata["firmware_build"]
                self.manifest["run_settings"] = metadata["run_settings"]
        if hardware or (original and "hardware" in original):
            self.manifest["hardware"] = hardware or original["hardware"]
        self._stack = ExitStack()
        self._digest = sha256()
        self._last_flush_ns = None
        self._needs_finish = False
        self._finished = False
        self._space_checked_ns = None
        self.free_bytes = None
        self.space_low = False
        self.last_input_seq = self.durable_input_seq = 0
        self.last_saved_run_id = None

    def __enter__(self):
        self.directory.mkdir(parents=True, exist_ok=False)
        try:
            self.files = {}
            for name in LOG_NAMES:
                file = SegmentedFile(self.directory, name, self.rotate_bytes, sync)
                self._stack.callback(file.close)
                self.files[name] = file
            self.measurements = MeasurementLog(
                self.files["samples.csv"],
                self.evidence_session,
                self.manifest["source"],
                self.write_operation,
            )
            return self
        except BaseException:
            self._stack.close()
            raise

    def __exit__(self, exc_type, exc, traceback):
        active_error = exc
        try:
            if exc_type is None and not self._finished:
                self.sync_files()
        except BaseException as sync_error:
            active_error = sync_error
            raise
        finally:
            try:
                self._stack.close()
            except OSError as close_error:
                if active_error is None:
                    raise
                LOGGER.warning("기록 파일 닫기 실패: %s", close_error)

        if exc_type is None:
            if self._needs_finish:
                raise ValueError("기록 종료 처리가 완료되지 않았습니다")
            self._publish_manifest()

    def _publish_manifest(self):
        self.manifest["input_sha256"] = self._digest.hexdigest()
        segments = {
            name: file.count for name, file in self.files.items() if file.count > 1
        }
        if segments:
            self.manifest["log_segments"] = segments
        temporary = self.directory / ".manifest.tmp"
        published = self.directory / "manifest.json"
        with temporary.open("x", encoding="utf-8") as file:
            file.write(json_line(self.manifest))
            sync(file)
        temporary.rename(published)
        try:
            self._sync_directory()
        except OSError:
            # 디렉터리 반영에 실패한 기록이 리플레이에 노출되지 않게 한다.
            published.unlink()
            raise

    def input(self, record, original_utc, replay_ns):
        row = original_row(record, original_utc if self.original else utc_now())
        self._digest.update(json_line(row).encode("utf-8"))
        if replay_ns is not None:
            row["replay_mono_ns"] = str(replay_ns)
        self.files["inputs.jsonl"].write(json_line(row))
        self.last_input_seq = record.ingest_seq
        return row

    def check_space(self):
        now = monotonic_ns()
        checked = self._space_checked_ns
        if checked is not None and now - checked < FLUSH_INTERVAL_NS:
            return None
        self.free_bytes = shutil.disk_usage(self.directory).free
        self._space_checked_ns = now
        low = self.free_bytes <= self.low_space_bytes
        if low == self.space_low:
            return None
        self.space_low = low
        if low:
            LOGGER.warning("기록 저장 공간 부족: %d바이트 남음", self.free_bytes)
        return "recording_space_low" if low else "recording_space_recovered"

    def report_failure(self, report):
        temporary = self.directory / ".recording-failure.tmp"
        with temporary.open("x", encoding="utf-8") as file:
            file.write(json_line({"backend_session": self.backend_session, **report}))
            sync(file)
        temporary.rename(self.directory / "recording-failure.json")
        self._sync_directory()

    def sync_files(self, *, include_events=True):
        for name, file in self.files.items():
            if include_events or name != "events.jsonl":
                sync(file)
        self._sync_directory()
        if include_events:
            self.durable_input_seq = self.last_input_seq

    def _sync_directory(self):
        fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def verify_input(self):
        if self.original and self._digest.hexdigest() != self.original["input_sha256"]:
            raise ValueError("원본 입력의 해시 불일치")

    def new_engine(self, config):
        hardware = self.manifest.get("hardware", {})
        engine = DiagnosticEngine(
            self.evidence_session,
            config,
            on_sample=self.measurements.completed.append,
            fpga_status_watchdog=hardware.get("fpga_status_watchdog") is True,
            link_low_space_evidence=(
                # 이전 기록은 저장 당시의 판정 근거 목록을 유지한다.
                self.manifest.get("recording_policy_version")
                == RECORDING_POLICY_VERSION
            ),
        )
        self._needs_finish = True
        return engine

    def flush_due(self, record):
        now = record.rx_mono_ns
        clock = record.source == "service" and record.payload == "clock"
        if clock or (
            self._last_flush_ns is not None
            and now - self._last_flush_ns >= FLUSH_INTERVAL_NS
        ):
            self.flush()
            self._last_flush_ns = now
        elif self._last_flush_ns is None:
            self._last_flush_ns = now

    def flush(self):
        for file in self.files.values():
            file.flush()

    def project(self, row, received, sample_gap=0):
        if row["source"] == "can":
            gap = {"sample_seq_gap": sample_gap} if sample_gap else {}
            self.files["raw_can.jsonl"].write(
                json_line(
                    {
                        **row,
                        "interface": self.manifest.get("hardware", {}).get(
                            "can_interface"
                        ),
                        "motor_boot": message_motor_boot(received.message),
                        "decode_error": received.error,
                        **gap,
                    }
                )
            )
        elif row["source"] == "can_tx":
            self.files["raw_can.jsonl"].write(
                json_line(
                    {
                        **row,
                        "interface": self.manifest.get("hardware", {}).get(
                            "can_interface"
                        ),
                        "motor_boot": message_motor_boot(received),
                    }
                )
            )
        elif row["source"] == "fpga":
            candidates = [
                {
                    "raw_hex": item.raw.hex(" "),
                    "session": item.fpga_session,
                    "tx_seq": item.frame.tx_seq if item.frame else None,
                    "input_record_ids": item.record_ids,
                    "issues": [issue.value for issue in item.issues],
                }
                for item in received
            ]
            self.files["raw_fpga.jsonl"].write(
                json_line({**row, "candidates": candidates})
            )

    def result(self, context):
        self.sync_files(include_events=False)
        event = {
            "event_id": str(uuid4()),
            "rx_utc": utc_now(),
            "category": "test",
            "code": context["result"],
            "context": {**context, "evidence_saved": True},
        }
        self.files["events.jsonl"].write_synced(json_line(event), self._sync_directory)
        self.durable_input_seq = self.last_input_seq
        self.last_saved_run_id = context["run_id"]
        if self.on_event is not None:
            self.on_event(event)
        return event["context"]

    def write_operation(self, row):
        self.files["operations.jsonl"].write(json_line(row))

    def finish(self, engine):
        self.measurements.finish(engine)
        results = [self.result(context) for context in engine.finish()]
        self.sync_files()
        self.manifest["can_rx"] = {
            "socket_dropped_frames": engine.can_socket_drops,
            "unobserved_sample_sequences": engine.can.samples.sequence_gaps,
        }
        self._needs_finish = False
        self._finished = True
        return results


def record_input(journal, engine, record, *, original_utc=None, replay_ns=None):
    row = journal.input(record, original_utc, replay_ns)
    previous_gap_count = engine.can.samples.sequence_gaps
    received, completed = engine.ingest(record)
    sample_gap = engine.can.samples.sequence_gaps - previous_gap_count
    journal.project(row, received, sample_gap)
    journal.measurements.project(record, row, received, engine)
    journal.flush_due(record)
    return received, [journal.result(context) for context in completed]


def evaluate(source, journal, config, *, pace=None):
    if journal.original:
        verify_source(source, journal.original)
    engine = journal.new_engine(config)
    results = []
    for record, original_utc in iter_inputs(
        source, journal.evidence_session, recorded=journal.original is not None
    ):
        replay_ns = pace(record.rx_mono_ns) if pace else None
        _, completed = record_input(
            journal, engine, record, original_utc=original_utc, replay_ns=replay_ns
        )
        results.extend(completed)
    journal.verify_input()
    results.extend(journal.finish(engine))
    return results
