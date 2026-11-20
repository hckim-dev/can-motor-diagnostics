"""연속 바이트에서 UART 프레임 경계를 복원한다."""

from dataclasses import dataclass

from .codec import FRAME_SIZE, SYNC, Frame, FrameError, InvalidFrame, decode_frame


@dataclass(frozen=True)
class FrameCandidate:
    raw: bytes
    frame: Frame | None = None
    error: FrameError | None = None
    record_ids: tuple[str, ...] = ()


class FrameStream:
    def __init__(self):
        self._buffer = bytearray()
        self._origins: list[str | None] = []

    def reset(self):
        self._buffer.clear()
        self._origins.clear()

    @property
    def has_pending_frame(self) -> bool:
        return bool(self._buffer)

    @property
    def pending_record_ids(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(origin for origin in self._origins if origin is not None)
        )

    def finish(self) -> FrameCandidate | None:
        if not self._buffer:
            return None
        raw = bytes(self._buffer)
        record_ids = self.pending_record_ids
        self.reset()
        return FrameCandidate(raw, error=FrameError.FRAMING, record_ids=record_ids)

    def feed(
        self, data: bytes, *, record_id: str | None = None
    ) -> list[FrameCandidate]:
        previous_size = len(self._buffer)
        self._buffer.extend(data)
        candidates = []
        offset = 0

        while True:
            start = self._buffer.find(SYNC, offset)
            if start < 0:
                # sync의 첫 바이트가 다음 읽기와 나뉠 수 있다.
                tail = len(self._buffer)
                if offset < tail and self._buffer.endswith(SYNC[:1]):
                    tail -= 1
                offset = tail
                break
            offset = start
            end = start + FRAME_SIZE
            if end > len(self._buffer):
                break

            raw = bytes(self._buffer[start:end])
            origins = self._origins[start:end]
            if end > previous_size and record_id is not None:
                origins.append(record_id)
            record_ids = tuple(
                dict.fromkeys(origin for origin in origins if origin is not None)
            )
            try:
                frame = decode_frame(raw)
            except InvalidFrame as exc:
                candidates.append(
                    FrameCandidate(raw, error=exc.reason, record_ids=record_ids)
                )
                # CRC 오류에는 후보 내부의 다음 sync도 검사한다.
                offset = start + 1 if exc.reason == FrameError.CRC else end
            else:
                candidates.append(
                    FrameCandidate(raw, frame=frame, record_ids=record_ids)
                )
                offset = end

        # 다음 프레임에 남은 바이트의 원본만 보존한다.
        new_tail_size = len(self._buffer) - max(offset, previous_size)
        self._origins = self._origins[offset:] + [record_id] * new_tail_size
        del self._buffer[:offset]
        return candidates
