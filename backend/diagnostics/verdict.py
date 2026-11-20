"""고정 파형 시험의 관측 근거를 결과로 변환한다."""

from dataclasses import dataclass, field

from ..can_rx.codec import Mode, State
from ..can_rx.state import FAULT_LIFETIME, STATE_LIFETIME, Observation, fresh
from ..fpga_uart.session import RunStatus

FEEDBACK_MISSING = 1 << 2
SCENARIO_VERSION = "fpga-basic/1.0"
BASELINE_US = 1_000_000
OBSERVATION_US = {1: 2_000_000, 2: 1_000_000, 3: 1_000_000}
GPIO_LIMIT_US = 300_000
POST_END_NS = 500_000_000


@dataclass(frozen=True)
class Config:
    gpio_verified: bool = False
    functional_only: bool = False
    scenario_version: str = SCENARIO_VERSION
    gpio_limit_us: int = GPIO_LIMIT_US

    def __post_init__(self):
        if self.scenario_version != SCENARIO_VERSION:
            raise ValueError("지원하지 않는 시험 프로파일")
        if type(self.gpio_limit_us) is not int or self.gpio_limit_us <= 0:
            raise ValueError("GPIO 응답 한계는 양의 정수여야 한다")
        if (
            type(self.gpio_verified) is not bool
            or type(self.functional_only) is not bool
        ):
            raise ValueError("시험 설정 플래그는 불리언이어야 한다")


@dataclass
class Trial:
    run_id: str
    scenario_id: int
    start_ns: int
    baseline_tick: int | None
    motor_boot: int | None
    evidence: dict[str, None] = field(default_factory=dict)
    issues: dict[str, None] = field(default_factory=dict)
    active_seen: bool = False
    inject_tick: int | None = None
    inject_epoch: int | None = None
    end_tick: int | None = None
    end_ns: int | None = None
    terminal: RunStatus | None = None
    latency_us: int | None = None
    diag_seen: bool = False
    early_fault: bool = False
    normal_state_deviation: bool = False
    expected_fault_epoch: int | None = None
    interrupted_by: str | None = None
    control_after_end: bool = False
    state: Observation | None = None
    fault: Observation | None = None
    observed_faults: int = 0
    state_at_end: Observation | None = None
    fault_at_end: Observation | None = None
    gap_state: Observation | None = None
    gap_fault: Observation | None = None
    gap_fault_cleared: bool = False
    baseline_accepted: bool = False

    def add_evidence(self, *ids):
        self.evidence.update(dict.fromkeys(ids))

    def add_issue(self, reason):
        self.issues.setdefault(reason, None)


def duration_us(start, end):
    return (end - start) & 0xFFFFFFFF


def corroborated(state, fault):
    return (
        state is not None
        and fault is not None
        and state.message.epoch == fault.message.epoch
        and state.message.mode == fault.message.mode == Mode.TEST
    )


def fresh_pair(state, fault, end_ns):
    if not corroborated(state, fault):
        return False
    observed_at = max(end_ns, state.rx_mono_ns, fault.rx_mono_ns)
    return fresh(state, observed_at, STATE_LIFETIME) and fresh(
        fault, observed_at, FAULT_LIFETIME
    )


def decide(trial, config):
    if trial.issues:
        return "INCONCLUSIVE", next(iter(trial.issues))
    if trial.interrupted_by:
        if trial.interrupted_by != "RESET" and (
            trial.early_fault or trial.observed_faults
        ):
            return "INCONCLUSIVE", "AMBIGUOUS_ABORT"
        return "ABORTED", trial.interrupted_by
    if config.functional_only or not config.gpio_verified:
        return "INCONCLUSIVE", "TIMING_UNAVAILABLE"
    if not trial.active_seen:
        return "INCONCLUSIVE", "TEST_ACTIVE_UNOBSERVED"
    if trial.early_fault:
        return "FAIL", "BASELINE_FAULT"
    if trial.terminal != RunStatus.END_RECEIVED:
        return "INCONCLUSIVE", "ABORT_CAUSE_UNKNOWN"

    if (
        trial.baseline_tick is None
        or trial.inject_tick is None
        or duration_us(trial.baseline_tick, trial.inject_tick) < BASELINE_US
    ):
        return "INCONCLUSIVE", "BASELINE_INCOMPLETE"
    if (
        duration_us(trial.inject_tick, trial.end_tick)
        < OBSERVATION_US[trial.scenario_id]
    ):
        return "INCONCLUSIVE", "OBSERVATION_INCOMPLETE"

    if trial.scenario_id == 1:
        if not fresh_pair(trial.state_at_end, trial.fault_at_end, trial.end_ns):
            return "INCONCLUSIVE", "CAN_EVIDENCE_MISSING"
        if trial.observed_faults or trial.diag_seen or trial.normal_state_deviation:
            return "FAIL", "NORMAL_DEVIATION"
        return "PASS", "NORMAL_CONFIRMED"

    if trial.scenario_id == 3 and not trial.gap_fault_cleared:
        if not fresh_pair(trial.gap_state, trial.gap_fault, trial.end_ns):
            return "INCONCLUSIVE", "GAP_RECOVERY_EVIDENCE_MISSING"
    if not trial.gap_fault_cleared and not fresh_pair(
        trial.state, trial.fault, trial.end_ns
    ):
        return "INCONCLUSIVE", "CAN_EVIDENCE_MISSING"
    if trial.observed_faults & ~FEEDBACK_MISSING:
        return "FAIL", "UNEXPECTED_FAULT"
    if trial.gap_fault_cleared:
        return "FAIL", "FAULT_CLEARED_AFTER_GAP"
    if trial.latency_us is None:
        return "FAIL", "NO_DIAG_RESPONSE"
    if trial.scenario_id == 2 and (
        trial.fault.message.mask != FEEDBACK_MISSING
        or trial.state.message.state != State.FAULT
    ):
        if trial.control_after_end:
            return "INCONCLUSIVE", "CONTROL_CHANGED_BEFORE_FAULT_CONFIRMATION"
        return "FAIL", "EXPECTED_FAULT_MISSING"
    if trial.scenario_id == 2 and (
        trial.inject_epoch is None or trial.state.message.epoch <= trial.inject_epoch
    ):
        return "INCONCLUSIVE", "FAULT_EPOCH_UNCONFIRMED"
    if trial.latency_us > config.gpio_limit_us:
        return "FAIL", "GPIO_LATENCY_EXCEEDED"
    if trial.scenario_id == 3:
        if trial.expected_fault_epoch is None:
            return "INCONCLUSIVE", "GAP_INITIAL_FAULT_UNOBSERVED"
        if trial.gap_state.message.epoch != trial.expected_fault_epoch:
            return "INCONCLUSIVE", "GAP_RECOVERY_EPOCH_CHANGED"
    return "PASS", "EXPECTED_FAULT_CONFIRMED"


def result_context(trial, config, end_ns):
    status, reason = decide(trial, config)
    return {
        "run_id": trial.run_id,
        "scenario_id": trial.scenario_id,
        "scenario_version": config.scenario_version,
        "motor_boot": trial.motor_boot,
        "mode": "TEST" if trial.motor_boot is not None else None,
        "expected_fault_mask": 0 if trial.scenario_id == 1 else FEEDBACK_MISSING,
        "observed_fault_mask": trial.observed_faults
        if trial.fault is not None
        else None,
        "timing_source": "fpga_gpio"
        if config.gpio_verified and not config.functional_only
        else "host_observed",
        "latency_us": None if trial.scenario_id == 1 else trial.latency_us,
        "limit_us": None if trial.scenario_id == 1 else config.gpio_limit_us,
        "result": status,
        "reason": reason,
        "issues": list(trial.issues),
        "evidence_ids": list(trial.evidence),
        "start_rx_mono_ms": trial.start_ns // 1_000_000,
        "end_rx_mono_ms": end_ns // 1_000_000,
    }
