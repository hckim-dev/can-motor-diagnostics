"""연속 바이트에서 UART 프레임 경계를 복원한다."""

from dataclasses import dataclass

from .codec import FRAME_SIZE, SYNC, Frame, FrameError, InvalidFrame, decode_frame


@dataclass(frozen=True)
class FrameCandidate:
    raw: bytes
    frame: Frame | None = None
    error: FrameError | None = None


class FrameStream:
    def __init__(self):
        self._buffer = bytearray()

    def reset(self):
        self._buffer.clear()

    def finish(self) -> FrameCandidate | None:
        if not self._buffer:
            return None
        raw = bytes(self._buffer)
        self.reset()
        return FrameCandidate(raw, error=FrameError.FRAMING)

    def feed(self, data: bytes) -> list[FrameCandidate]:
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
            try:
                frame = decode_frame(raw)
            except InvalidFrame as exc:
                candidates.append(FrameCandidate(raw, error=exc.reason))
                # CRC 오류에는 후보 내부의 다음 sync도 검사한다.
                offset = start + 1 if exc.reason == FrameError.CRC else end
            else:
                candidates.append(FrameCandidate(raw, frame=frame))
                offset = end

        del self._buffer[:offset]
        return candidates
