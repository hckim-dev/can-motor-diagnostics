"""조작 노드의 부팅 ID를 프로세스 재시작 사이에 보존한다."""

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile

from .wire import MAX_BOOT_ID


def next_boot_id(path: Path):
    with boot_id_session(path, wait=True) as boot_id:
        return boot_id


@contextmanager
def boot_id_session(path: Path, *, wait=False):
    """부팅 ID를 발급하고 조작 세션이 끝날 때까지 파일 잠금을 유지한다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with lock_path.open("a") as lock:
        flags = fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB
        try:
            fcntl.flock(lock, flags)
        except BlockingIOError as exc:
            raise BlockingIOError(
                exc.errno, "조작 노드가 이미 실행 중입니다", path
            ) from exc

        try:
            saved = path.read_text(encoding="ascii").strip()
        except FileNotFoundError:
            boot_id = 1
        else:
            if not saved.isdecimal() or not 1 <= int(saved) < MAX_BOOT_ID:
                raise ValueError("보존된 조작 노드 부팅 ID가 잘못됐거나 소진됐다")
            boot_id = int(saved) + 1

        # 새 값이 디스크에 반영된 뒤에만 송신에 사용한다.
        with NamedTemporaryFile(
            mode="w", encoding="ascii", dir=path.parent, delete=False
        ) as output:
            temporary = Path(output.name)
            try:
                output.write(f"{boot_id}\n")
                output.flush()
                os.fsync(output.fileno())
                output.close()
                temporary.replace(path)
                directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                temporary.unlink(missing_ok=True)
        yield boot_id
