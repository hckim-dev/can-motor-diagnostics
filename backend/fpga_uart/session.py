"""UART 연결과 시험 회차의 관측 근거를 추적한다."""

from dataclasses import dataclass, replace
from enum import Enum
from uuid import uuid4

from .codec import OVERFLOW_INVALID, TIMING_VALID, EventType, Frame, FrameError, Phase
from .stream import FrameCandidate, FrameStream

TX_MASK = 0xFFFF
TICK_MASK = 0xFFFFFFFF
RUN_LIMIT_NS = 60_000_000_000
RUN_LIMIT_US = RUN_LIMIT_NS // 1_000


class RunStatus(str, Enum):
    OPEN = "OPEN"
    END_RECEIVED = "END_RECEIVED"
    ABORT_RECEIVED = "ABORT_RECEIVED"
    INTERRUPTED = "INTERRUPTED"


class Issue(str, Enum):
    TX_GAP = "TX_GAP"
    RUN_GAP = "RUN_GAP"
    COUNTER_DISCONTINUITY = "COUNTER_DISCONTINUITY"
    SESSION_QUARANTINED = "SESSION_QUARANTINED"
    DISCONNECTED = "DISCONNECTED"
    MISSING_BASELINE = "MISSING_BASELINE"
    MISSING_TERMINAL = "MISSING_TERMINAL"
    MISSING_DIAG_EDGE = "MISSING_DIAG_EDGE"
    EVENT_ORDER = "EVENT_ORDER"
    FPGA_INVALID = "FPGA_INVALID"
    RUN_TIMEOUT = "RUN_TIMEOUT"


@dataclass(frozen=True)
class Run:
    fpga_session: str
    run_seq: int
    scenario: int
    first_rx_ns: int
    first_tick_us: int
    status: RunStatus = RunStatus.OPEN
    injected: bool = False
    timed_edge_seen: bool = False
    issues: tuple[FrameError | Issue, ...] = ()

    @property
    def run_id(self) -> str:
        return f"{self.fpga_session}:{self.run_seq}"


@dataclass(frozen=True)
class Observation:
    fpga_session: str
    rx_mono_ns: int
    raw: bytes
    frame: Frame | None
    issues: tuple[FrameError | Issue, ...] = ()
    run: Run | None = None
    interrupted_run: Run | None = None
    duplicate: bool = False
    record_ids: tuple[str, ...] = ()


class UartReceiver:
    def __init__(self, session_id_factory=None):
        self.stream = FrameStream()
        self.last_rx_ns: int | None = None
        self.connected = True
        self._session_id_factory = session_id_factory or uuid4
        self._new_session()

    def _new_session(self):
        self.fpga_session = str(self._session_id_factory())
        self.last_frame: Frame | None = None
        self.run: Run | None = None
        self.quarantined = False
        self.fpga_invalid_latched = False

    @property
    def has_pending_frame(self) -> bool:
        return self.stream.has_pending_frame

    @property
    def pending_record_ids(self) -> tuple[str, ...]:
        return self.stream.pending_record_ids

    def _mark(self, issues):
        if self.run is not None and self.run.status == RunStatus.OPEN:
            merged = tuple(dict.fromkeys((*self.run.issues, *issues)))
            if merged != self.run.issues:
                self.run = replace(self.run, issues=merged)

    def _interrupt(self, issue) -> Run | None:
        if self.run is None or self.run.status != RunStatus.OPEN:
            return None
        self._mark((issue,))
        self.run = replace(self.run, status=RunStatus.INTERRUPTED)
        return self.run

    def advance(self, now_ns: int) -> Run | None:
        if self.last_rx_ns is not None and now_ns < self.last_rx_ns:
            raise ValueError("UART 관측 시각은 단조 증가해야 한다")
        self.last_rx_ns = now_ns
        if self.run is not None and now_ns - self.run.first_rx_ns >= RUN_LIMIT_NS:
            return self._interrupt(Issue.RUN_TIMEOUT)
        return None

    def disconnect(self, now_ns: int) -> Run | None:
        expired = self.advance(now_ns)
        self.connected = False
        self.stream.reset()
        return expired or self._interrupt(Issue.DISCONNECTED)

    def reconnect(self, now_ns: int) -> Run | None:
        interrupted = self.disconnect(now_ns)
        self._new_session()
        self.connected = True
        return interrupted

    def feed(
        self, data: bytes, now_ns: int, *, record_id: str | None = None
    ) -> list[Observation]:
        expired = self.advance(now_ns)
        if not self.connected:
            return []
        observations = []
        if expired is not None:
            observations.append(
                Observation(
                    self.fpga_session,
                    now_ns,
                    b"",
                    None,
                    (Issue.RUN_TIMEOUT,),
                    interrupted_run=expired,
                )
            )
        observations.extend(
            self._observe(candidate, now_ns)
            for candidate in self.stream.feed(data, record_id=record_id)
        )
        return observations

    def finish(self, now_ns: int) -> list[Observation]:
        observations = self.feed(b"", now_ns)
        if self.connected and (candidate := self.stream.finish()) is not None:
            observations.append(self._observe(candidate, now_ns))
        return observations

    def _observe(self, candidate: FrameCandidate, now_ns: int) -> Observation:
        observation = Observation(
            self.fpga_session,
            now_ns,
            candidate.raw,
            candidate.frame,
            record_ids=candidate.record_ids,
        )
        frame_issues = (candidate.error,) if candidate.error is not None else ()
        if self.quarantined:
            return replace(
                observation, issues=(*frame_issues, Issue.SESSION_QUARANTINED)
            )
        if frame_issues:
            self._mark(frame_issues)
            return replace(observation, issues=frame_issues, run=self.run)

        frame = candidate.frame
        assert frame is not None
        issues = []
        previous = self.last_frame
        if previous is not None:
            delta = (frame.tx_seq - previous.tx_seq) & TX_MASK
            if delta == 0 and frame == previous:
                return replace(observation, run=self.run, duplicate=True)
            tick_delta = (frame.tick_us - previous.tick_us) & TICK_MASK
            if (
                not 0 < delta <= TX_MASK // 2
                or tick_delta > TICK_MASK // 2
                or (frame.run_seq < previous.run_seq)
            ):
                interrupted = self._interrupt(Issue.COUNTER_DISCONTINUITY)
                self._new_session()
                self.quarantined = True
                return replace(
                    observation,
                    fpga_session=self.fpga_session,
                    issues=(Issue.COUNTER_DISCONTINUITY,),
                    interrupted_run=interrupted,
                )
            if delta > 1:
                issues.append(Issue.TX_GAP)
            if frame.run_seq > previous.run_seq + 1:
                issues.append(Issue.RUN_GAP)

        self.last_frame = frame
        frame_invalid = bool(frame.flags & OVERFLOW_INVALID)
        if frame.kind == EventType.STATUS and frame_invalid:
            self.fpga_invalid_latched = True
        if frame_invalid or self.fpga_invalid_latched:
            issues.append(Issue.FPGA_INVALID)
        self._mark(issues)
        interrupted = self._apply_run_frame(frame, now_ns, issues)
        run = self.run if self.run and self.run.run_seq == frame.run_seq else None
        return replace(
            observation, issues=tuple(issues), run=run, interrupted_run=interrupted
        )

    def _apply_run_frame(self, frame, now_ns, issues):
        interrupted = None
        if self.run is not None and self.run.run_seq != frame.run_seq:
            interrupted = self._interrupt(Issue.MISSING_TERMINAL)
            self.run = None

        idle_status = frame.kind == EventType.STATUS and frame.phase not in (
            Phase.BASELINE,
            Phase.OBSERVE,
        )
        if idle_status and self.run is None:
            return interrupted
        if not idle_status and (not frame.run_seq or not frame.scenario):
            issues.append(Issue.EVENT_ORDER)
            self._mark(issues)
            return interrupted

        if self.run is None:
            if frame.kind != EventType.BASELINE_START:
                issues.append(Issue.MISSING_BASELINE)
            self.run = Run(
                self.fpga_session, frame.run_seq, frame.scenario, now_ns, frame.tick_us
            )
        elif self.run.status != RunStatus.OPEN:
            if frame.kind != EventType.STATUS:
                issues.append(Issue.EVENT_ORDER)
            return interrupted
        elif frame.kind == EventType.BASELINE_START or (
            not idle_status and frame.scenario != self.run.scenario
        ):
            issues.append(Issue.EVENT_ORDER)
            self._mark(issues)
            return interrupted

        self._mark(issues)
        elapsed_us = (frame.tick_us - self.run.first_tick_us) & TICK_MASK
        if elapsed_us >= RUN_LIMIT_US:
            issues.append(Issue.RUN_TIMEOUT)
            return self._interrupt(Issue.RUN_TIMEOUT)
        self._apply_event(frame, issues)
        return interrupted

    def _apply_event(self, frame, issues):
        if frame.kind == EventType.BASELINE_START and frame.phase != Phase.BASELINE:
            issues.append(Issue.EVENT_ORDER)
        elif frame.kind == EventType.INJECT:
            if self.run.injected or frame.phase != Phase.OBSERVE:
                issues.append(Issue.EVENT_ORDER)
            else:
                self.run = replace(self.run, injected=True)
        elif frame.kind == EventType.DIAG_EDGE and frame.flags & TIMING_VALID:
            if (
                not self.run.injected
                or frame.phase != Phase.OBSERVE
                or self.run.timed_edge_seen
            ):
                issues.append(Issue.EVENT_ORDER)
            else:
                self.run = replace(self.run, timed_edge_seen=True)
        elif frame.kind == EventType.END:
            if not self.run.injected or frame.phase != Phase.COMPLETED:
                issues.append(Issue.EVENT_ORDER)
            if frame.flags & TIMING_VALID and not self.run.timed_edge_seen:
                issues.append(Issue.MISSING_DIAG_EDGE)

        self._mark(issues)
        if frame.kind == EventType.END:
            self.run = replace(self.run, status=RunStatus.END_RECEIVED)
        elif frame.kind == EventType.ABORT:
            self.run = replace(self.run, status=RunStatus.ABORT_RECEIVED)
