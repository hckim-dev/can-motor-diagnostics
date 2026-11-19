"""시간 순서가 붙은 JSONL 예제를 모의 CAN 입력으로 공급한다."""

import asyncio
import json
import time

from .codec import Frame
from .samples import NS_PER_MS


def load_frames(path):
    entries = []
    previous_ms = 0

    with path.open(encoding="utf-8") as source:
        for line_no, line in enumerate(source, 1):
            try:
                item = json.loads(line)
                at_ms = item["at_ms"]
                if type(at_ms) is not int or at_ms < previous_ms:
                    raise ValueError("at_ms는 음이 아닌 정수이며 시간 순서여야 한다")

                can_id = item["can_id"]
                data = bytes.fromhex(item["hex"])
                flags = {
                    name: item.get(name, False) for name in ("extended", "rtr", "error")
                }
                if any(type(value) is not bool for value in flags.values()):
                    raise ValueError("CAN 플래그는 불리언이어야 한다")
                if type(item["dlc"]) is not int:
                    raise ValueError("dlc는 정수여야 한다")

                entries.append(
                    (at_ms, Frame(int(can_id, 16), data, item["dlc"], **flags))
                )
            except (ValueError, KeyError, TypeError) as exc:
                raise ValueError(f"{path}:{line_no}: {exc}") from exc
            previous_ms = at_ms

    return entries


async def feed_frames(entries, receiver):
    started_ns = time.monotonic_ns()
    for at_ms, frame in entries:
        target_ns = started_ns + at_ms * NS_PER_MS
        delay_ns = target_ns - time.monotonic_ns()
        if delay_ns > 0:
            await asyncio.sleep(delay_ns / (1000 * NS_PER_MS))
        receiver.ingest(frame, time.monotonic_ns())
