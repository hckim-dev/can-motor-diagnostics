"""같은 입력 순서에서 CAN과 UART 증거를 시험 회차에 연결한다."""

from dataclasses import dataclass, replace
from uuid import UUID, uuid5

from ..can_control.wire import Opcode
from ..can_rx.codec import (
    SUPPLY_PRESENT,
    SUPPLY_VALID,
    CanId,
    Command,
    CommandResult,
    Mode,
    Permit,
    State,
    decode_frame,
)
from ..can_rx.samples import SampleAssembler
from ..can_rx.state import (
    FAULT_LIFETIME,
    NC2_OPEN,
    STATE_LIFETIME,
    STOP_REQUESTED,
    CanReceiver,
    fresh,
)
from ..fpga_uart.codec import DIAG_HIGH, READY_HIGH, TIMING_VALID, EventType, Phase
from ..fpga_uart.session import STATUS_LIFETIME_NS, Issue, RunStatus, UartReceiver
from .inputs import Input
from .verdict import (
    FEEDBACK_MISSING,
    POST_END_NS,
    Trial,
    corroborated,
    fresh_pair,
    result_context,
)

CONTROL_FRAMES = {CanId.MOTOR_HELLO, CanId.MOTOR_STATE, CanId.MOTOR_FAULT}


@dataclass(frozen=True)
class ArmEvidence:
    epoch: int
    cmd_seq: int
    record_ids: tuple[str, ...]
    failed: bool = False


class DiagnosticEngine:
    def __init__(
        self,
        backend_session: str,
        config,
        *,
        on_sample=None,
        fpga_status_watchdog=False,
        link_low_space_evidence=True,
    ):
        self.backend_session = backend_session
        self.config = config
        self._namespace = UUID(backend_session)
        self._can_generation = 0
        self.can = CanReceiver(
            backend_session,
            samples=SampleAssembler(
                on_sample=on_sample, new_acquisition_id=self._next_acquisition
            ),
        )
        self._uart_generation = 0
        self.uart = UartReceiver(self._next_uart_session)
        self.fpga_status_watchdog = fpga_status_watchdog
        self.fpga_status_ns = None
        self.idle_evidence: tuple[str, ...] = ()
        self.healthy_recording = True
        self.link_low_space_evidence = link_low_space_evidence
        self.low_space_record_id = None
        self.trial: Trial | None = None
        self.now_ns = 0
        self.ingest_seq = 0
        self.results = []
        self.latest_result = None
        self.can_socket_drops = 0
        self.sent_command = None
        self.arm_evidence = None

    def _next_acquisition(self):
        self._can_generation += 1
        return uuid5(self._namespace, f"can:{self._can_generation}")

    def _next_uart_session(self):
        self._uart_generation += 1
        return uuid5(self._namespace, f"fpga:{self._uart_generation}")

    def _check_can_interval(self, now_ns):
        trial = self.trial
        if trial is None or trial.end_ns is not None or trial.motor_boot is None:
            return
        if not self.can.motor_known(now_ns) or not (
            fresh(trial.state, now_ns, STATE_LIFETIME)
            and fresh(trial.fault, now_ns, FAULT_LIFETIME)
        ):
            trial.add_issue("CAN_STALE")

    def _check_fpga_interval(self, now_ns):
        trial = self.trial
        if not self.fpga_status_watchdog or trial is None or trial.end_ns is not None:
            return
        last = trial.start_ns if self.fpga_status_ns is None else self.fpga_status_ns
        deadline = max(trial.start_ns, last + STATUS_LIFETIME_NS)
        if now_ns >= deadline:
            trial.add_evidence(*self.uart.pending_record_ids)
            trial.add_issue("FPGA_STATUS_TIMEOUT")
            self._close(deadline)

    def ingest(self, record: Input):
        if (
            record.backend_session != self.backend_session
            or record.ingest_seq != self.ingest_seq + 1
            or record.rx_mono_ns < self.now_ns
        ):
            raise ValueError("판정 입력의 세션·순번·시각이 연속이어야 한다")
        self.ingest_seq = record.ingest_seq
        self.now_ns = now = record.rx_mono_ns
        self.results = []

        # 드롭은 이전 수신 이후 어느 시점에 발생했는지 알 수 없다.
        overflow = record.source == "service" and record.payload == "can_rx_overflow"
        if not overflow and self._deadline() is not None and now > self._deadline():
            self._close(self._deadline())
        self._check_fpga_interval(now)
        if self.trial and self.trial.end_ns is None:
            if expired := self.uart.advance(now):
                self._interrupt(expired, record.record_id)
        self._check_can_interval(now)

        if record.source != "can":
            self.can.advance(now)

        observations = None
        if record.source == "can":
            observations = self._receive_can(record)
        elif record.source == "can_tx":
            observations = self._receive_submission(record)
        elif record.source == "fpga":
            observations = self.uart.feed(
                record.payload, now, record_id=record.record_id
            )
            for observation in observations:
                self._receive_uart(observation, record.record_id)
        else:
            self._receive_service(record)
        return observations, self.results

    def _deadline(self):
        if self.trial is not None and self.trial.end_ns is not None:
            return self.trial.end_ns + POST_END_NS
        return None

    def _receive_can(self, record):
        received = self.can.ingest(
            record.payload, self.now_ns, ingest_seq=record.ingest_seq
        )
        if isinstance(received.message, CommandResult) and record.detail:
            self._receive_command_result(record, received.message)
        trial = self.trial
        if (
            trial is None
            or trial.control_after_end
            or received.frame.can_id not in CONTROL_FRAMES
        ):
            return received
        if received.error:
            trial.add_evidence(record.record_id)
            trial.add_issue("CAN_INVALID")
            return received
        if not self.can.motor_known(self.now_ns):
            trial.add_evidence(record.record_id)
            trial.add_issue("CAN_SESSION_UNCONFIRMED")
            return received
        if self.can.highest_epoch >> 16 != trial.motor_boot:
            trial.add_evidence(record.record_id)
            trial.add_issue("MOTOR_BOOT_CHANGED")
            return received
        if received is self.can.hello:
            trial.add_evidence(record.record_id)
        elif received is self.can.state or received is self.can.fault:
            self._apply_can_evidence(trial, received)
        return received

    def _receive_submission(self, record):
        message = decode_frame(record.payload)
        if record.detail is None:
            return message

        trial = self.trial
        stop = isinstance(message, Permit) and message.flags & STOP_REQUESTED
        if isinstance(message, Command):
            self.sent_command = record, message
            self.arm_evidence = (
                ArmEvidence(message.epoch, message.cmd_seq, (record.record_id,))
                if message.opcode == Opcode.ARM_TEST
                else None
            )
            stop = message.opcode == Opcode.STOP
        elif stop:
            self.arm_evidence = None

        reset = isinstance(message, Command) and message.opcode == Opcode.RESET
        if (stop or reset) and (
            trial is not None and message.epoch >> 16 == trial.motor_boot
        ):
            trial.add_evidence(record.record_id)
            if trial.end_ns is not None:
                trial.control_after_end = True
            elif stop:
                self._mark_control_change(trial, "STOP")
            else:
                if trial.pending_reset_id is not None:
                    trial.add_issue("RESET_OUTCOME_UNKNOWN")
                trial.pending_reset_id = record.record_id
        return message

    def _receive_command_result(self, result, message):
        if self.sent_command is None:
            return
        submitted, command = self.sent_command
        if (
            result.detail["request_id"] != submitted.detail["request_id"]
            or result.detail["opcode"] != Opcode(command.opcode).name
            or (message.request_epoch, message.cmd_seq)
            != (command.epoch, command.cmd_seq)
        ):
            return

        completed = message.result == 1
        failed = message.result not in (0, 1)
        if command.opcode == Opcode.ARM_TEST and self.arm_evidence is not None:
            self.arm_evidence = replace(
                self.arm_evidence,
                record_ids=(*self.arm_evidence.record_ids, result.record_id),
                failed=failed,
            )

        trial = self.trial
        if trial is not None and submitted.record_id in trial.evidence:
            trial.add_evidence(result.record_id)
            if failed and trial.arm_record_id == submitted.record_id:
                trial.add_issue("ARM_TEST_RESULT_CONFLICT")
            matches_pending_reset = trial.pending_reset_id == submitted.record_id
            if matches_pending_reset and message.result != 0:
                trial.pending_reset_id = None
            if matches_pending_reset and completed:
                if trial.terminal == RunStatus.END_RECEIVED:
                    trial.add_issue("RESET_TIMING_UNCONFIRMED")
                self._mark_control_change(trial, "RESET")
                if not trial.control_after_end:
                    trial.interrupted_by = "RESET"
                    self._close(self.now_ns)

        if message.result != 0:
            self.sent_command = None

    def _apply_can_evidence(self, trial, received):
        trial.add_evidence(received.record_id)
        message = received.message
        if message.mode != Mode.TEST:
            self._mark_control_change(trial, "MODE_CHANGED")
            return
        if received is self.can.state:
            if message.flags & NC2_OPEN:
                self._mark_control_change(trial, "ESTOP")
                if trial.control_after_end:
                    return
            trial.state = received
            if not self._supply_off(message.flags):
                trial.add_issue("TEST_SUPPLY_UNCONFIRMED")
            if trial.end_ns is None:
                trial.active_seen |= message.state == State.TEST_ACTIVE
                if trial.inject_tick is not None and message.state != State.TEST_ACTIVE:
                    trial.normal_state_deviation = True
            else:
                # 같은 시각이면 END와 CAN 프레임의 입력 순서로 구분한다.
                trial.gap_state = received
        else:
            trial.fault = received
            trial.observed_faults |= message.mask
            if trial.inject_tick is None:
                trial.early_fault |= bool(message.mask)
            if trial.end_ns is not None:
                trial.gap_fault = received
        if (
            trial.scenario_id == 3
            and trial.end_ns is not None
            and fresh_pair(trial.gap_state, trial.gap_fault, trial.end_ns)
        ):
            # 이후에 고장이 다시 래치돼도 복귀 직후의 해제는 남긴다.
            trial.gap_fault_cleared |= (
                trial.gap_state.message.state != State.FAULT
                or not trial.gap_fault.message.mask & FEEDBACK_MISSING
            )
        if (
            trial.inject_tick is not None
            and trial.inject_epoch is not None
            and trial.end_ns is None
            and trial.expected_fault_epoch is None
            and corroborated(trial.state, trial.fault)
            and trial.state.message.state == State.FAULT
            and trial.state.message.epoch > trial.inject_epoch
            and trial.fault.message.mask == FEEDBACK_MISSING
            and fresh(trial.state, self.now_ns, STATE_LIFETIME)
            and fresh(trial.fault, self.now_ns, FAULT_LIFETIME)
        ):
            trial.expected_fault_epoch = trial.state.message.epoch

    @staticmethod
    def _supply_off(flags):
        return bool(flags & SUPPLY_VALID and not flags & SUPPLY_PRESENT)

    def _start(self, run, frame, record_ids):
        baseline = frame.kind == EventType.BASELINE_START
        trial = Trial(
            run.run_id,
            run.scenario,
            run.first_rx_ns,
            frame.tick_us if baseline else None,
            None,
        )
        self.trial = trial
        if not self.healthy_recording:
            trial.add_issue("RECORDING_FAILED")
        if self.low_space_record_id is not None:
            trial.add_issue("RECORDING_SPACE_LOW")
            if self.link_low_space_evidence:
                trial.add_evidence(self.low_space_record_id)
        if not baseline or not self.idle_evidence:
            trial.add_issue("MID_RUN_ATTACH")
        if self.fpga_status_watchdog and (
            self.fpga_status_ns is None
            or self.now_ns - self.fpga_status_ns >= STATUS_LIFETIME_NS
        ):
            trial.add_issue("FPGA_STATUS_UNAVAILABLE")
        if (
            baseline
            and self.config.gpio_verified
            and not self.config.functional_only
            and not frame.flags & READY_HIGH
        ):
            trial.add_issue("FPGA_READY_NOT_OBSERVED")
        trial.add_evidence(*self.idle_evidence, *record_ids)
        self.idle_evidence = ()
        now = self.now_ns
        if not self.can.motor_known(now) or not (
            fresh(self.can.state, now, STATE_LIFETIME)
            and fresh(self.can.fault, now, FAULT_LIFETIME)
        ):
            trial.add_issue("CAN_BASELINE_UNAVAILABLE")
            return
        trial.motor_boot = self.can.highest_epoch >> 16
        trial.state = self.can.state
        trial.fault = self.can.fault
        trial.add_evidence(
            self.can.hello.record_id, trial.state.record_id, trial.fault.record_id
        )
        state = trial.state.message
        if arm := self.arm_evidence:
            if state.state == State.TEST_ARMED and (
                state.epoch,
                state.last_cmd_seq,
            ) == (arm.epoch, arm.cmd_seq):
                trial.add_evidence(*arm.record_ids)
                trial.arm_record_id = arm.record_ids[0]
                if arm.failed:
                    trial.add_issue("ARM_TEST_RESULT_CONFLICT")
            self.arm_evidence = None
        if (
            not corroborated(trial.state, trial.fault)
            or state.state != State.TEST_ARMED
            or trial.fault.message.mask
            or state.flags & NC2_OPEN
            or not self._supply_off(state.flags)
        ):
            trial.add_issue("TEST_PRECONDITION_FAILED")

    def _receive_uart(self, observation, record_id):
        if observation.duplicate:
            return
        record_ids = observation.record_ids or (record_id,)
        frame, run = observation.frame, observation.run
        if (
            Issue.COUNTER_DISCONTINUITY in observation.issues
            or Issue.SESSION_QUARANTINED in observation.issues
        ):
            self.fpga_status_ns = None
        elif frame is not None and frame.kind == EventType.STATUS:
            self.fpga_status_ns = self.now_ns
        if observation.interrupted_run is not None:
            self._interrupt(observation.interrupted_run, *record_ids)
        if (
            self.trial is None
            and run is None
            and frame is not None
            and frame.kind == EventType.STATUS
            and frame.phase == Phase.COMPLETED
            and frame.run_seq
            and frame.scenario
            and not observation.issues
        ):
            self._record_mid_run_attach(observation, record_ids)
            return
        if frame and frame.kind == EventType.STATUS and frame.phase == Phase.IDLE:
            if (
                frame.scenario == 0
                and not observation.issues
                and (run is None or run.status != RunStatus.OPEN)
            ):
                self.idle_evidence = record_ids
        new_run = (
            run is not None
            and frame is not None
            and (self.trial is None or self.trial.run_id != run.run_id)
        )
        if new_run:
            if self.latest_result and self.latest_result["run_id"] == run.run_id:
                return
            if self.trial is not None:
                self.trial.add_issue("OVERLAPPING_RUN")
                self._close(self.now_ns)
            self._start(run, frame, record_ids)
        trial = self.trial
        if trial is None:
            if observation.issues:
                self.idle_evidence = ()
            return
        trial.add_evidence(*record_ids)
        for issue in (*observation.issues, *(run.issues if run else ())):
            trial.add_issue(f"UART_{issue.value}")
        if new_run:
            trial.baseline_accepted = not trial.issues
        if frame is None or run is None:
            return
        if frame.kind == EventType.INJECT and trial.inject_tick is None:
            state = trial.state
            if not (
                fresh(state, self.now_ns, STATE_LIFETIME)
                and state.message.state == State.TEST_ACTIVE
                and state.message.epoch == self.can.highest_epoch
            ):
                trial.add_issue("TEST_ACTIVE_UNOBSERVED")
            else:
                trial.inject_epoch = state.message.epoch
            trial.inject_tick = frame.tick_us
        if frame.flags & DIAG_HIGH:
            trial.diag_seen = True
            if trial.inject_tick is None:
                trial.early_fault = True
        if frame.kind == EventType.DIAG_EDGE and frame.flags & TIMING_VALID:
            if trial.latency_us is None:
                trial.latency_us = frame.value
        if frame.kind in (EventType.END, EventType.ABORT) and trial.end_ns is None:
            trial.terminal = run.status
            trial.end_tick = frame.tick_us
            trial.end_ns = self.now_ns
            trial.state_at_end = trial.state
            trial.fault_at_end = trial.fault

    def _interrupt(self, run, *record_ids):
        if self.trial is not None and self.trial.run_id == run.run_id:
            self.trial.add_evidence(*record_ids)
            for issue in run.issues:
                self.trial.add_issue(f"UART_{issue.value}")
            self._close(self.now_ns)
        self.idle_evidence = ()

    def _record_mid_run_attach(self, observation, record_ids):
        frame = observation.frame
        run_id = f"{observation.fpga_session}:{frame.run_seq}"
        if self.latest_result and self.latest_result["run_id"] == run_id:
            return
        self.idle_evidence = ()
        self.trial = Trial(run_id, frame.scenario, self.now_ns, None, None)
        self.trial.add_issue("MID_RUN_ATTACH")
        self.trial.add_evidence(*record_ids)
        self._close(self.now_ns)

    def _receive_service(self, record):
        event = record.payload
        trial = self.trial
        if event == "clock":
            if self._deadline() is not None and self.now_ns >= self._deadline():
                self._close(self._deadline())
            return
        if event == "recording_space_low":
            self.low_space_record_id = record.record_id
            return
        if event == "recording_space_recovered":
            self.low_space_record_id = None
            return
        if event == "recording_failed":
            self.healthy_recording = False
            self.can.samples.reset()
        if event in ("can_reconnect", "can_retry_failed", "fpga_retry_failed"):
            return
        if event == "can_rx_overflow":
            self.can_socket_drops += record.detail["dropped"]
        if event in ("can_disconnect", "can_rx_overflow"):
            self.can.disconnect(self.now_ns)
            self.sent_command = self.arm_evidence = None
        if event in ("fpga_disconnect", "fpga_reconnect"):
            if trial is not None and record.detail is not None:
                trial.add_evidence(*self.uart.pending_record_ids)
            operation = (
                self.uart.disconnect
                if event == "fpga_disconnect"
                else self.uart.reconnect
            )
            operation(self.now_ns)
            self.idle_evidence = ()
            self.fpga_status_ns = None
        if trial is None:
            return
        trial.add_evidence(record.record_id)
        if event in ("stop", "reset"):
            self._mark_control_change(trial, event.upper())
            if event == "reset" and not trial.control_after_end:
                trial.interrupted_by = "RESET"
                self._close(self.now_ns)
        else:
            trial.add_issue(event.upper())
            self._close(self.now_ns)

    def _mark_control_change(self, trial, reason):
        if trial.terminal == RunStatus.END_RECEIVED:
            # 조작 뒤 CAN 상태는 완료된 파형의 근거로 묶을 수 없다.
            trial.control_after_end = True
        else:
            trial.interrupted_by = trial.interrupted_by or reason

    def _close(self, end_ns):
        trial = self.trial
        if trial is not None:
            if (
                trial.pending_reset_id is not None
                and trial.terminal == RunStatus.END_RECEIVED
            ):
                trial.add_issue("RESET_OUTCOME_UNKNOWN")
            if trial.end_ns is not None and self.uart.has_pending_frame:
                self._mark_pending_uart(trial)
            self.latest_result = result_context(trial, self.config, end_ns)
            self.results.append(self.latest_result)
            self.trial = None

    def _mark_pending_uart(self, trial):
        trial.add_issue("UART_FRAMING")
        trial.add_evidence(*self.uart.pending_record_ids)

    def finish(self):
        self.results = []
        if self.trial is not None:
            if self.uart.has_pending_frame:
                self._mark_pending_uart(self.trial)
            observations = self.uart.finish(self.now_ns)
            for observation in observations:
                for issue in observation.issues:
                    self.trial.add_issue(f"UART_{issue.value}")
            deadline = self._deadline()
            if deadline is None or self.now_ns < deadline:
                self.trial.add_issue("RECORD_ENDED_EARLY")
            self._close(
                deadline
                if deadline is not None and self.now_ns >= deadline
                else self.now_ns
            )
        return self.results

    def snapshot(self):
        if self.trial is None:
            return self.latest_result or {"result": "IDLE"}
        return {
            "run_id": self.trial.run_id,
            "scenario_id": self.trial.scenario_id,
            "result": "RUNNING",
            "reason": "RESULT_PENDING" if self._deadline() else None,
        }

    def fail_recording(self, last_saved_run_id):
        self.healthy_recording = False
        if self.trial is not None:
            self.trial.add_issue("RECORDING_FAILED")
            self._close(self.now_ns)

        result = self.latest_result
        if result is not None and result["run_id"] != last_saved_run_id:
            self.latest_result = {
                **result,
                "result": "INCONCLUSIVE",
                "reason": "RECORDING_FAILED",
                "issues": list(dict.fromkeys((*result["issues"], "RECORDING_FAILED"))),
                "evidence_saved": False,
            }
