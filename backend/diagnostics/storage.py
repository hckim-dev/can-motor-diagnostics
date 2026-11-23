"""기록 파일을 줄 경계에서 회전하고 매니페스트 순서로 읽는다."""

import logging
from pathlib import Path

LOG_NAMES = (
    "inputs.jsonl",
    "raw_can.jsonl",
    "raw_fpga.jsonl",
    "events.jsonl",
    "samples.csv",
    "operations.jsonl",
)
ROTATE_BYTES = 100 * 1024**2
LOW_SPACE_BYTES = 1024**3
LOGGER = logging.getLogger(__name__)


class RecordingFailure(OSError):
    """기록이 중단되어 수집과 조작을 다시 시작해야 한다."""


def segment_name(name, index):
    return name if index == 1 else f"{name}.{index:06d}"


class SegmentedFile:
    def __init__(self, directory, name, limit, sync):
        self.directory = directory
        self.name = name
        self.limit = limit
        self.sync = sync
        self.count = 1
        self.size = 0
        self.file = self._open(1)

    def _open(self, index):
        return (self.directory / segment_name(self.name, index)).open(
            "x", encoding="utf-8", newline=""
        )

    def write(self, text):
        size = len(text.encode("utf-8"))
        if self.size and self.size + size > self.limit:
            self.sync(self.file)
            self.file.close()
            self.file = self._open(self.count + 1)
            self.count += 1
            self.size = 0
        written = self.file.write(text)
        self.size += size
        return written

    def write_synced(self, text, sync_directory):
        segment, offset = self.count, self.size
        try:
            self.write(text)
            self.sync(self.file)
            if self.count != segment:
                sync_directory()
        except OSError:
            if not self.closed:
                # 동기화가 실패한 행만 되돌려 이전 판정은 보존한다.
                offset = offset if self.count == segment else 0
                try:
                    self.file.truncate(offset)
                    self.file.seek(offset)
                    self.size = offset
                    self.sync(self.file)
                except OSError as exc:
                    LOGGER.warning("미확정 기록을 되돌리지 못했습니다: %s", exc)
            raise

    @property
    def closed(self):
        return self.file.closed

    def flush(self):
        self.file.flush()

    def fileno(self):
        return self.file.fileno()

    def close(self):
        self.file.close()


class RecordedLog:
    def __init__(self, directory, name, manifest):
        self.directory = Path(directory)
        self.name = name
        self.count = manifest.get("log_segments", {}).get(name, 1)

    def __iter__(self):
        for index in range(1, self.count + 1):
            with (self.directory / segment_name(self.name, index)).open(
                encoding="utf-8"
            ) as file:
                yield from file


def validate_segments(manifest):
    counts = manifest.get("log_segments", {})
    if not isinstance(counts, dict) or any(
        name not in LOG_NAMES or type(count) is not int or count < 2
        for name, count in counts.items()
    ):
        raise ValueError("기록 파일 분할 정보 형식 오류")
