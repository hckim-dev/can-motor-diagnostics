"""완료된 기록을 원래 수신 간격으로 읽기 전용 TCP에 재생한다."""

import asyncio
import json
from contextlib import closing
from hashlib import sha256
from itertools import islice
from uuid import UUID

from ..diagnostics.journal import (
    canonical_input_bytes,
    engine_for_recording,
    iter_inputs,
    load_manifest,
    verify_source,
)
from ..diagnostics.storage import RecordedLog
from ..tcp_stream import SNAPSHOT_INTERVAL, run_tcp_server
from .inputs import NS_PER_SECOND
from .presentation import MonitorPresentation, verdict_event
from .tcp import EVENT_LIMIT, MonitorStream

INPUT_BLOCK_SIZE = 256


class ReplayStream(MonitorStream):
    source = "replay"
    capabilities = ("diagnostics_read_only", "replay")

    def __init__(self, replay_of, initial):
        super().__init__(None, "replay", initial)
        self.replay_of = replay_of
        self.ready = asyncio.Event()
        self.clock_origin = None
        self.next_input_ns = None

    def stream_started(self):
        self.ready.set()

    def snapshot(self, session, now):
        logical_ns = self.latest.logical_ns
        if self.next_input_ns is not None:
            started, origin_ns = self.clock_origin
            replay_ns = origin_ns + int((now - started) * NS_PER_SECOND)
            logical_ns = max(logical_ns, min(replay_ns, self.next_input_ns))
        # EOF 뒤에는 실제 관측이 없는 시간을 추가하지 않는다.
        wall_ns = self.latest.wall_ns + logical_ns - self.latest.logical_ns
        return self._snapshot_at(wall_ns, age_logging=False)

    async def present(self, snapshot, events, recording):
        loop = asyncio.get_running_loop()
        paused_at = None
        while self.client is not None and len(self.events) + len(events) > EVENT_LIMIT:
            if paused_at is None:
                paused_at = loop.time()
            await asyncio.sleep(SNAPSHOT_INTERVAL)
        if paused_at is not None:
            started, origin_ns = self.clock_origin
            self.clock_origin = started + (loop.time() - paused_at), origin_ns
        self.recording = recording
        self.publish(snapshot, events)


def replay_verdicts(contexts, saved):
    for context in contexts:
        line = next(saved, None)
        if line is None:
            raise ValueError("리플레이 판정에 대응하는 원본 결과가 없습니다")
        event = json.loads(line)
        expected = {**context, "evidence_saved": True}
        if (
            not isinstance(event, dict)
            or event.get("category") != "test"
            or event.get("context") != expected
            or event.get("code") != context["result"]
            or not isinstance(event.get("event_id"), str)
            or str(UUID(event["event_id"])) != event["event_id"]
        ):
            raise ValueError("리플레이 판정이 원본 결과와 일치하지 않는다")
        yield verdict_event(event)


def verified_inputs(source, session, block_digests):
    with closing(iter_inputs(source, session, recorded=True)) as records:
        for expected in block_digests:
            block = list(islice(records, INPUT_BLOCK_SIZE))
            digest = sha256()
            for record, rx_utc in block:
                digest.update(canonical_input_bytes(record, rx_utc))
            if digest.digest() != expected:
                raise ValueError("원본 입력의 해시 불일치")
            for record, _ in block:
                yield record
        if next(records, None) is not None:
            raise ValueError("원본 입력의 해시 불일치")


async def play(stream, inputs, results, engine, presentation):
    await stream.ready.wait()
    loop = asyncio.get_running_loop()
    recording = None
    with closing(inputs), closing(iter(results)) as saved:
        for record in inputs:
            if stream.clock_origin is None:
                stream.clock_origin = loop.time(), record.rx_mono_ns
            started, origin_ns = stream.clock_origin
            stream.next_input_ns = record.rx_mono_ns
            scheduled = started + (record.rx_mono_ns - origin_ns) / NS_PER_SECOND
            await asyncio.sleep(max(0, scheduled - loop.time()))
            received, completed = engine.ingest(record)
            events = list(replay_verdicts(completed, saved))
            if event := presentation.observe(engine, record, received):
                events.append(event)
            if record.source == "service":
                if record.payload == "recording_space_low":
                    recording = {"reason": "RECORDING_SPACE_LOW", **record.detail}
                elif record.payload == "recording_space_recovered":
                    recording = None
                elif record.payload == "recording_failed":
                    recording = {"reason": "RECORDING_FAILED"}
            await stream.present(
                presentation.view(engine, engine.now_ns), events, recording
            )

        events = list(replay_verdicts(engine.finish(), saved))
        if next(saved, None) is not None:
            raise ValueError("리플레이로 재현하지 못한 원본 판정이 있습니다")
        stream.next_input_ns = None
        await stream.present(
            presentation.view(engine, engine.now_ns), events, recording
        )


def serve_replay(directory, host, port):
    manifest, config = load_manifest(directory)
    source = RecordedLog(directory, "inputs.jsonl", manifest)
    block_digests = verify_source(source, manifest, block_size=INPUT_BLOCK_SIZE)
    results = RecordedLog(directory, "events.jsonl", manifest)
    engine = engine_for_recording(manifest, config)
    presentation = MonitorPresentation()
    stream = ReplayStream(manifest["backend_session"], presentation.view(engine, 0))

    async def background():
        inputs = verified_inputs(source, engine.backend_session, block_digests)
        await play(stream, inputs, results, engine, presentation)

    asyncio.run(run_tcp_server(stream, host, port, background))
