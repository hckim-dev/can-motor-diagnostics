"""C10의 측정 샘플과 REAL 운전 회차를 원본 증거에 연결한다."""

import csv
from dataclasses import dataclass

from ..can_rx.codec import CanId, Mode, State
from ..can_rx.samples import SENSOR_LIFETIME
from ..can_rx.state import STATE_LIFETIME, fresh
from .operations import OperationTracker

SPEED_FIELDS = ("actual_rpm", "target_rpm", "pwm_permille")
ELECTRIC_FIELDS = ("current_ma", "bus_mv", "sensor_age_ms")
FIELDS = (
    "backend_session",
    "acquisition_id",
    "sample_seq64",
    "source",
    "operation_id",
    "run_id",
    "motor_boot",
    "seq",
    "tick_ms",
    "rx_mono_ns",
    "rx_utc",
    "mode",
    "state",
    *SPEED_FIELDS,
    *ELECTRIC_FIELDS,
    *(f"{name}_validity" for name in (*SPEED_FIELDS, *ELECTRIC_FIELDS)),
    "sample_complete",
    "quality",
    "evidence_ids",
)
TEST_STATES = {State.TEST_ARMED, State.TEST_ACTIVE, State.FAULT}


@dataclass(frozen=True)
class SampleContext:
    rx_utc: str
    operation_id: str | None = None
    run_id: str | None = None


class MeasurementLog:
    def __init__(self, sample_file, session, source, write_operation):
        self.session = session
        self.source = source
        self.operations = OperationTracker(session, write_operation)
        self.writer = csv.DictWriter(sample_file, FIELDS, lineterminator="\n")
        self.writer.writeheader()
        self.completed = []
        self.contexts = {}

    def project(self, record, row, received, engine):
        can = engine.can
        self.operations.observe(record, row, received, can)
        if (
            record.source == "can"
            and received.message is not None
            and received.frame.can_id in (CanId.SAMPLE_TIME, CanId.ELECTRIC)
        ):
            self.contexts[record.record_id] = self._context(row["rx_utc"], engine)

        self._write_completed()
        # 결합 창 안에 남아 있는 프레임의 회차만 기억한다.
        pending_ids = {
            part.record_id
            for pending in can.samples.pending.values()
            for kind, part in pending.parts.items()
            if kind in (CanId.SAMPLE_TIME, CanId.ELECTRIC)
        }
        self.contexts = {
            key: context for key, context in self.contexts.items() if key in pending_ids
        }

    def _context(self, utc, engine):
        can, now = engine.can, engine.now_ns
        if not can.motor_known(now) or not fresh(can.state, now, STATE_LIFETIME):
            return SampleContext(utc)

        state = can.state.message
        operation = self.operations.operation
        trial = engine.trial
        if (
            operation is not None
            and state.mode == Mode.REAL
            and state.state == State.RUNNING
        ):
            return SampleContext(utc, operation_id=operation.operation_id)
        if (
            trial is not None
            and trial.baseline_accepted
            and trial.end_ns is None
            and not trial.interrupted_by
            and state.mode == Mode.TEST
            and state.state in TEST_STATES
            and state.epoch >> 16 == trial.motor_boot
        ):
            return SampleContext(utc, run_id=trial.run_id)
        return SampleContext(utc)

    def _write_completed(self):
        for sample in self.completed:
            timing = sample.timing
            context = self.contexts[timing.record_id]
            electric_context = (
                self.contexts[sample.electric.record_id] if sample.electric else None
            )
            electric = sample.electric.message if sample.electric else None
            mode = electric.mode if electric else Mode.UNKNOWN
            state = electric.state if electric else None
            # 두 프레임이 같은 회차에서 수신된 경우만 귀속한다.
            operation_id = run_id = None
            if electric_context:
                if (
                    mode == Mode.REAL
                    and state == State.RUNNING
                    and context.operation_id == electric_context.operation_id
                ):
                    operation_id = context.operation_id
                if (
                    mode == Mode.TEST
                    and state in TEST_STATES
                    and context.run_id == electric_context.run_id
                ):
                    run_id = context.run_id

            parts = (sample.speed, sample.electric, timing)
            observed_ns = max(part.received_ns for part in parts if part is not None)
            age_ns = sample.sensor_age_ns(observed_ns)
            sensors_stale = age_ns is not None and age_ns > SENSOR_LIFETIME
            invalid = {kind for kind, _ in sample.invalid_parts}
            values = {}
            validity = []
            for kind, part, fields in (
                (CanId.SPEED, sample.speed, SPEED_FIELDS),
                (CanId.ELECTRIC, sample.electric, ELECTRIC_FIELDS),
            ):
                for field in fields:
                    value = getattr(part.message, field) if part else None
                    quality = self._validity(
                        value,
                        invalid=kind in invalid,
                        stale=sensors_stale and field in ("current_ma", "bus_mv"),
                    )
                    values[field] = value if quality == "valid" else None
                    values[f"{field}_validity"] = quality
                    validity.append(quality)

            self.writer.writerow(
                {
                    "backend_session": self.session,
                    "acquisition_id": sample.acquisition_id,
                    "sample_seq64": sample.seq64,
                    "source": self.source,
                    "operation_id": operation_id,
                    "run_id": run_id,
                    "motor_boot": sample.boot,
                    "seq": timing.message.seq,
                    "tick_ms": timing.message.tick_ms,
                    "rx_mono_ns": timing.received_ns,
                    "rx_utc": context.rx_utc,
                    "mode": mode.name,
                    "state": state.name if state is not None else "UNKNOWN",
                    "sample_complete": sample.complete,
                    "quality": next(
                        level
                        for level in ("invalid", "unavailable", "stale", "valid")
                        if level in validity
                    ),
                    "evidence_ids": ";".join(sample.evidence_ids),
                    **values,
                }
            )
        self.completed.clear()

    @staticmethod
    def _validity(value, *, invalid, stale):
        if invalid:
            return "invalid"
        if value is None:
            return "unavailable"
        if stale:
            return "stale"
        return "valid"

    def finish(self, engine):
        engine.can.samples.flush()
        self._write_completed()
        self.operations.close("RECORD_ENDED", engine.now_ns)
