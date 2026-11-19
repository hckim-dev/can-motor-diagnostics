"""모의 CAN의 송신 순서, 마감시간, TCP 결과를 검증한다."""

import asyncio
import json
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from struct import pack
from unittest.mock import patch

from check_can_rx import EPOCH, fault, hello, state

from backend.can_control.boot_id import next_boot_id
from backend.can_control.controller import CanController
from backend.can_control.mock_motor import (
    INTERNAL_FAULT,
    OP_LINK_LOSS_FAULT,
    MemoryCan,
    MockMotor,
)
from backend.can_control.wire import (
    MAX_BOOT_ID,
    MAX_CMD_SEQ,
    Opcode,
    encode_command,
    encode_operator_hello,
    encode_permit,
)
from backend.can_rx.codec import CanId, Frame, Mode, State, decode_frame
from backend.can_rx.samples import NS_PER_MS
from backend.can_rx.state import STOP_REQUESTED, CanReceiver
from backend.tcp_can_mock import CanControl, TcpCanMock

FIXTURES = Path(__file__).with_name("fixtures")
TEST_SUPPLY_OFF = 0x04


def response(epoch, seq, status=1, detail=0, resulting_state=State.TEST_ARMED):
    return Frame(
        CanId.COMMAND_RESULT,
        pack("<IBBBB", epoch, seq, status, detail, resulting_state),
    )


class WireTest(unittest.TestCase):
    def test_jetson_frames_match_shared_example(self):
        rows = [
            json.loads(line)
            for line in (FIXTURES / "can-2.1.jsonl").read_text().splitlines()
        ]
        expected = [
            (int(row["can_id"], 16), bytes.fromhex(row["hex"]), row["dlc"])
            for row in rows
            if int(row["can_id"], 16) in (0x701, 0x080, 0x100)
        ]
        frames = (
            encode_operator_hello(1),
            encode_permit(EPOCH, 1, ready=True),
            encode_command(EPOCH, 1, Opcode.ARM_TEST, 0),
            encode_permit(EPOCH, 2, inhibit=True),
        )
        self.assertEqual([(f.can_id, f.data, f.dlc) for f in frames], expected)

    def test_boot_counter_is_persistent_and_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "boot-id"
            self.assertEqual(next_boot_id(path), 1)
            self.assertEqual(next_boot_id(path), 2)
            for invalid in ("", "zero", "0", str(MAX_BOOT_ID)):
                with self.subTest(invalid=invalid):
                    path.write_text(invalid)
                    with self.assertRaises(ValueError):
                        next_boot_id(path)
                    self.assertEqual(path.read_text(), invalid)


class ControllerTest(unittest.TestCase):
    def setUp(self):
        self.rx = CanReceiver()
        self.frames = []
        self.blocked = set()
        self.control = CanController(self.rx, self.send, boot_id=1, rpm_max=400)
        self.input(0, hello(), state(flags=TEST_SUPPLY_OFF), fault())

    def send(self, frame):
        if frame.can_id in self.blocked:
            return False
        self.frames.append(frame)
        return True

    def input(self, ms, *frames):
        for frame in frames:
            self.control.receive(frame, ms * NS_PER_MS)

    def hb(self, ms):
        self.control.heartbeat(ms * NS_PER_MS)

    def poll(self, ms):
        self.control.poll(ms * NS_PER_MS)

    def request(self, ms, request_id="arm", opcode=Opcode.ARM_TEST, argument=0):
        return self.control.request(request_id, opcode, argument, ms * NS_PER_MS)

    def outcomes(self):
        result = list(self.control.outcomes)
        self.control.outcomes.clear()
        return result

    def commands(self):
        return [
            decode_frame(frame)
            for frame in self.frames
            if frame.can_id == CanId.COMMAND
        ]

    def test_periodic_permit_and_expiry(self):
        self.poll(0)
        self.assertEqual([frame.can_id for frame in self.frames], [CanId.OP_HELLO])
        self.hb(1)
        self.poll(1)
        self.poll(50)
        self.poll(51)
        permits = [
            decode_frame(frame)
            for frame in self.frames
            if frame.can_id == CanId.OP_PERMIT
        ]
        self.assertEqual(
            [(item.alive_seq, item.flags) for item in permits], [(0, 1), (1, 1)]
        )

        self.input(250, state(flags=TEST_SUPPLY_OFF))
        self.poll(299)
        self.poll(301)
        self.assertEqual(decode_frame(self.frames[-1]).flags, STOP_REQUESTED)
        self.assertEqual(self.request(302).reason, "HEARTBEAT_REQUIRED")

    def test_state_at_expiry_does_not_keep_previous_permit(self):
        for arrival_ms in (299, 300):
            with self.subTest(arrival_ms=arrival_ms):
                self.setUp()
                self.hb(0)
                for ms in range(0, 300, 50):
                    self.poll(ms)
                self.hb(250)
                self.assertIsNone(self.request(299))

                self.input(arrival_ms, state(flags=TEST_SUPPLY_OFF))
                self.poll(arrival_ms)
                if arrival_ms < 300:
                    self.assertEqual(self.commands()[0].opcode, Opcode.ARM_TEST)
                    self.assertFalse(self.outcomes())
                    continue

                self.assertEqual(decode_frame(self.frames[-1]).flags, STOP_REQUESTED)
                (cancelled,) = self.outcomes()
                self.assertEqual(
                    (cancelled.status, cancelled.reason),
                    ("FAILED", "CANCELLED_BY_CAN_LOSS"),
                )
                self.assertEqual(self.request(301, "again").reason, "INHIBIT")
                next_epoch = EPOCH + 1
                self.input(
                    302, hello(next_epoch), state(next_epoch, flags=TEST_SUPPLY_OFF)
                )
                self.poll(302)
                self.assertIsNotNone(self.control.permit_sent_ns)
                self.assertFalse(self.commands())

    def test_incompatible_motor_never_grants(self):
        self.input(1, hello(version=(2, 0), capabilities=0x0F))
        self.hb(2)
        self.poll(2)
        self.assertEqual(self.request(3).reason, "MOTOR_STATE_UNAVAILABLE")
        self.assertFalse(any(frame.can_id == CanId.OP_PERMIT for frame in self.frames))

    def test_blocked_operator_hello_never_grants(self):
        self.blocked.add(CanId.OP_HELLO)
        self.hb(1)
        self.poll(1)
        self.poll(51)
        self.assertFalse(any(frame.can_id == CanId.OP_PERMIT for frame in self.frames))

    def test_late_send_is_not_retried(self):
        self.blocked.add(CanId.COMMAND)
        self.hb(1)
        self.assertIsNone(self.request(1))
        self.poll(50)
        self.assertFalse(self.outcomes())
        self.blocked.clear()
        self.poll(51)
        (outcome,) = self.outcomes()
        self.assertEqual(
            (outcome.status, outcome.reason, outcome.cmd_seq),
            ("FAILED", "SEND_TIMEOUT", None),
        )
        self.assertFalse(self.commands())

    def test_queued_command_rechecks_mode_before_send(self):
        self.blocked.add(CanId.COMMAND)
        self.hb(1)
        self.assertIsNone(self.request(1))
        self.poll(1)
        self.input(2, state(flags=TEST_SUPPLY_OFF, mode=1))
        self.blocked.clear()
        self.poll(3)
        (outcome,) = self.outcomes()
        self.assertEqual(
            (outcome.status, outcome.reason, outcome.cmd_seq),
            ("FAILED", "BAD_MODE", None),
        )
        self.assertFalse(self.commands())

    def test_result_keys_and_accepted_do_not_extend_deadline(self):
        self.hb(1)
        self.assertIsNone(self.request(1))
        self.poll(1)
        self.input(2, response(EPOCH, 2))
        self.input(100, state(flags=TEST_SUPPLY_OFF))
        self.hb(101)
        self.poll(101)
        self.input(200, response(EPOCH, 1, status=0), state(flags=TEST_SUPPLY_OFF))
        self.hb(200)
        self.poll(200)
        self.assertEqual([item.status for item in self.outcomes()], ["ACCEPTED"])
        self.poll(300)
        self.assertFalse(self.outcomes())
        self.input(301, response(EPOCH, 1))
        (timeout,) = self.outcomes()
        self.assertEqual(
            (timeout.status, timeout.reason, timeout.cmd_seq),
            ("UNKNOWN_OUTCOME", "RESULT_TIMEOUT", 1),
        )
        self.assertEqual(len(self.commands()), 1)

    def test_stop_preempts_unsent_command_and_needs_new_epoch(self):
        self.hb(1)
        self.blocked.add(CanId.COMMAND)
        self.assertIsNone(self.request(1))
        self.poll(1)
        self.blocked.clear()
        self.assertIsNone(self.request(2, "stop", Opcode.STOP))
        self.poll(2)
        (cancelled,) = self.outcomes()
        self.assertEqual(
            (cancelled.status, cancelled.reason, cancelled.cmd_seq),
            ("FAILED", "CANCELLED_BY_STOP", None),
        )
        self.assertEqual(self.commands()[0].opcode, Opcode.STOP)
        inhibit_index = next(
            index
            for index, frame in enumerate(self.frames)
            if frame.can_id == CanId.OP_PERMIT
            and decode_frame(frame).flags & STOP_REQUESTED
        )
        first_command = next(
            i for i, frame in enumerate(self.frames) if frame.can_id == CanId.COMMAND
        )
        self.assertLess(inhibit_index, first_command)

        self.input(3, state(flags=TEST_SUPPLY_OFF))
        self.poll(3)
        self.assertIsNotNone(self.control.inhibit)
        self.input(
            4,
            hello(EPOCH + 1),
            state(EPOCH + 1, flags=TEST_SUPPLY_OFF),
            fault(EPOCH + 1),
        )
        self.assertIsNone(self.control.inhibit)
        self.assertFalse(self.outcomes())
        self.input(
            5, response(EPOCH, 2, status=2, detail=1, resulting_state=State.STOPPED)
        )
        (rejected,) = self.outcomes()
        self.assertEqual(
            (rejected.request_id, rejected.reason, rejected.motor_epoch),
            ("stop", "BAD_EPOCH", EPOCH),
        )
        self.poll(5)
        self.assertEqual(len(self.commands()), 1)
        self.assertEqual(decode_frame(self.frames[-1]).flags, 1)

    def test_blocked_inhibit_retries_without_extending_stop_deadline(self):
        for recovery_ms in (3, 52):
            with self.subTest(recovery_ms=recovery_ms):
                self.setUp()
                self.hb(1)
                self.poll(1)
                self.frames.clear()
                self.blocked.add(CanId.OP_PERMIT)

                self.assertIsNone(self.request(2, "stop", Opcode.STOP))
                self.poll(2)
                self.assertFalse(self.frames)
                self.assertIsNone(self.control.inhibit.sent_after_record)

                self.blocked.clear()
                self.poll(recovery_ms)
                inhibit = decode_frame(self.frames[0])
                self.assertEqual(
                    (inhibit.flags, inhibit.alive_seq), (STOP_REQUESTED, 1)
                )
                self.poll(recovery_ms + 1)
                if recovery_ms == 3:
                    self.assertEqual(
                        [frame.can_id for frame in self.frames],
                        [CanId.OP_PERMIT, CanId.COMMAND],
                    )
                    self.assertEqual(self.commands()[0].opcode, Opcode.STOP)
                    self.assertFalse(self.outcomes())
                else:
                    self.assertEqual(len(self.frames), 1)
                    (timeout,) = self.outcomes()
                    self.assertEqual(
                        (timeout.status, timeout.reason), ("FAILED", "SEND_TIMEOUT")
                    )

    def test_stop_reissues_inhibit_when_new_epoch_is_still_active(self):
        self.hb(1)
        self.assertIsNone(self.request(1, "stop", Opcode.STOP))
        self.poll(1)
        active_epoch = EPOCH + 1
        self.input(2, hello(active_epoch))
        self.poll(2)
        active = Frame(
            CanId.MOTOR_STATE,
            pack("<IBBBB", active_epoch, State.RUNNING, Mode.REAL, 0x66, 0),
        )
        self.input(3, active)
        self.poll(3)
        permits = [
            decode_frame(frame)
            for frame in self.frames
            if frame.can_id == CanId.OP_PERMIT
        ]
        self.assertEqual(
            [(item.epoch, item.flags) for item in permits],
            [(EPOCH, STOP_REQUESTED), (active_epoch, STOP_REQUESTED)],
        )
        self.input(4, state(active_epoch, flags=TEST_SUPPLY_OFF))
        self.assertIsNotNone(self.control.inhibit)
        self.input(
            5, hello(active_epoch + 1), state(active_epoch + 1, flags=TEST_SUPPLY_OFF)
        )
        self.assertIsNone(self.control.inhibit)

    def test_epoch_change_and_disconnect_cancel_sent_commands(self):
        for reason in (
            "CANCELLED_BY_SESSION",
            "CANCELLED_BY_DISCONNECT",
            "CANCELLED_BY_CAN_LOSS",
        ):
            with self.subTest(reason=reason):
                self.setUp()
                self.hb(1)
                self.assertIsNone(self.request(1))
                self.poll(1)
                if reason == "CANCELLED_BY_SESSION":
                    self.input(
                        2, hello(EPOCH + 1), state(EPOCH + 1, flags=TEST_SUPPLY_OFF)
                    )
                elif reason == "CANCELLED_BY_CAN_LOSS":
                    self.control.disconnect_can(2 * NS_PER_MS)
                else:
                    self.control.disconnect_client(2 * NS_PER_MS)
                self.input(3, response(EPOCH, 1))
                (result,) = self.outcomes()
                self.assertEqual(
                    (result.status, result.reason, result.cmd_seq),
                    ("UNKNOWN_OUTCOME", reason, 1),
                )
                self.assertEqual(len(self.commands()), 1)

    def test_explicit_can_reconnect_discards_stale_input_and_resends_hello(self):
        self.hb(1)
        self.assertIsNone(self.request(1, "stop", Opcode.STOP))
        self.poll(1)
        self.control.disconnect_can(2 * NS_PER_MS)
        (result,) = self.outcomes()
        self.assertEqual(
            (result.request_id, result.status, result.reason),
            ("stop", "UNKNOWN_OUTCOME", "CANCELLED_BY_CAN_LOSS"),
        )

        self.frames.clear()
        self.input(3, hello(EPOCH + 1), state(EPOCH + 1, flags=TEST_SUPPLY_OFF))
        self.poll(3)
        self.assertFalse(self.frames)
        self.assertEqual(self.rx.highest_epoch, EPOCH)
        self.assertEqual(self.request(3, "arm-offline").reason, "CAN_OFFLINE")

        self.control.reconnect_can(4 * NS_PER_MS)
        self.poll(4)
        self.assertEqual(self.frames[0].can_id, CanId.OP_HELLO)
        self.assertEqual(decode_frame(self.frames[-1]).flags, STOP_REQUESTED)
        self.input(5, hello(EPOCH + 1), state(EPOCH + 1, flags=TEST_SUPPLY_OFF))
        self.poll(5)
        self.assertEqual(decode_frame(self.frames[-1]).flags, 1)
        self.assertFalse(self.commands())

    def test_can_reconnect_requires_new_inhibit_after_prior_stop(self):
        self.hb(1)
        self.assertIsNone(self.request(1, "stop", Opcode.STOP))
        self.poll(1)
        self.control.disconnect_can(2 * NS_PER_MS)
        self.control.reconnect_can(3 * NS_PER_MS)
        self.frames.clear()

        next_epoch = EPOCH + 1
        self.input(3, hello(next_epoch), state(next_epoch, flags=TEST_SUPPLY_OFF))
        self.poll(3)
        (inhibit,) = [
            decode_frame(frame)
            for frame in self.frames
            if frame.can_id == CanId.OP_PERMIT
        ]
        self.assertEqual((inhibit.epoch, inhibit.flags), (next_epoch, STOP_REQUESTED))
        self.assertEqual(self.request(4).reason, "INHIBIT")

        next_epoch += 1
        self.input(5, hello(next_epoch), state(next_epoch, flags=TEST_SUPPLY_OFF))
        self.poll(5)
        self.assertEqual(decode_frame(self.frames[-1]).flags, 1)
        self.assertFalse(self.commands())

    def test_duplicate_waits_for_newer_queued_result(self):
        control = CanControl(self.control)
        session = control.open_session("session")
        request = {
            "stream_id": "session",
            "request_id": str(uuid.uuid4()),
            "opcode": "ARM_TEST",
            "argument": 0,
        }
        self.hb(1)
        with patch("backend.tcp_can_mock.time.monotonic_ns", return_value=NS_PER_MS):
            self.assertIsNone(session.handle("control_request", request, 0))
        self.poll(1)
        self.input(2, response(EPOCH, 1, status=0))
        (accepted,) = control.drain("session")
        self.assertEqual(accepted["status"], "ACCEPTED")

        self.input(3, response(EPOCH, 1))
        with patch(
            "backend.tcp_can_mock.time.monotonic_ns", return_value=3 * NS_PER_MS
        ):
            self.assertIsNone(session.handle("control_request", request, 0))
            (completed,) = control.drain("session")
            self.assertEqual(completed["status"], "COMPLETED")
            self.assertEqual(session.handle("control_request", request, 0), completed)
        self.assertEqual(len(self.commands()), 1)

    def test_stop_keeps_sent_request_unknown_and_ignores_late_result(self):
        self.hb(1)
        self.assertIsNone(self.request(1))
        self.poll(1)
        self.assertIsNone(self.request(2, "stop", Opcode.STOP))
        self.poll(2)
        (cancelled,) = self.outcomes()
        self.assertEqual(
            (
                cancelled.request_id,
                cancelled.status,
                cancelled.reason,
                cancelled.cmd_seq,
            ),
            ("arm", "UNKNOWN_OUTCOME", "CANCELLED_BY_STOP", 1),
        )
        self.input(3, response(EPOCH, 1))
        self.assertFalse(self.outcomes())
        self.assertEqual([entry.cmd_seq for entry in self.commands()], [1, 2])

    def test_sequence_exhaustion_keeps_stop_inhibit_available(self):
        self.hb(1)
        self.control.cmd_seq = MAX_CMD_SEQ - 1
        self.assertIsNone(self.request(1))
        self.poll(1)
        self.assertEqual(self.commands()[-1].cmd_seq, MAX_CMD_SEQ)
        self.input(2, response(EPOCH, MAX_CMD_SEQ))
        self.assertEqual(self.request(3, "next").reason, "SEQUENCE_EXHAUSTED")
        outcome = self.request(4, "stop", Opcode.STOP)
        self.assertEqual(outcome.reason, "STOP_INHIBIT_ONLY")
        self.poll(4)
        self.assertEqual(decode_frame(self.frames[-1]).flags, STOP_REQUESTED)
        self.assertEqual(len(self.commands()), 1)

    def test_real_start_requires_nc2_and_configured_rpm(self):
        self.input(1, hello(capabilities=0x0F), state(flags=0x66, mode=1))
        self.hb(1)
        self.assertEqual(
            self.request(2, "start", Opcode.START_REAL, 100).reason, "NC2_UNAVAILABLE"
        )
        self.input(3, hello())
        self.assertEqual(
            self.request(4, "start", Opcode.START_REAL, 401).reason, "BAD_ARGUMENT"
        )
        self.assertIsNone(self.request(5, "start", Opcode.START_REAL, 400))
        self.poll(5)
        self.assertEqual(
            (self.commands()[-1].opcode, self.commands()[-1].argument), (1, 400)
        )

    def test_start_never_reaches_motor_when_cancelled_before_bus_step(self):
        for reason in (
            "CANCELLED_BY_STOP",
            "CANCELLED_BY_DISCONNECT",
            "CANCELLED_BY_CAN_LOSS",
        ):
            with self.subTest(reason=reason):
                motor = MockMotor(Mode.REAL)
                bus = MemoryCan(motor)
                controller = CanController(CanReceiver(), bus.try_send, 1)
                controller.heartbeat(0)
                bus.step(controller, 0)
                self.assertIsNone(
                    controller.request("start", Opcode.START_REAL, 60, NS_PER_MS)
                )

                if reason == "CANCELLED_BY_STOP":
                    controller.request("stop", Opcode.STOP, 0, 2 * NS_PER_MS)
                elif reason == "CANCELLED_BY_DISCONNECT":
                    controller.disconnect_client(2 * NS_PER_MS)
                else:
                    controller.disconnect_can(2 * NS_PER_MS)

                with patch.object(
                    motor, "apply_command", wraps=motor.apply_command
                ) as apply:
                    bus.step(controller, 3 * NS_PER_MS)
                opcodes = [call.args[0].opcode for call in apply.call_args_list]
                expected = [Opcode.STOP] if reason == "CANCELLED_BY_STOP" else []
                self.assertEqual(opcodes, expected)
                self.assertEqual(motor.state, State.STOPPED)
                cancelled = controller.outcomes.popleft()
                self.assertEqual(
                    (cancelled.request_id, cancelled.status, cancelled.reason),
                    ("start", "FAILED", reason),
                )

    def test_mock_motor_reset_updates_fault_observation(self):
        motor = MockMotor()
        motor.state = State.FAULT
        motor.fault_mask = 4
        bus = MemoryCan(motor)
        receiver = CanReceiver()
        controller = CanController(receiver, bus.try_send, 1)
        controller.heartbeat(0)
        bus.step(controller, 0)
        self.assertIsNone(controller.request("reset", Opcode.RESET, 0, NS_PER_MS))
        bus.step(controller, NS_PER_MS)
        self.assertEqual(controller.outcomes.popleft().status, "COMPLETED")
        snapshot = receiver.snapshot(NS_PER_MS)["motor"]
        self.assertEqual((snapshot["state"], snapshot["fault_mask"]), ("STOPPED", 0))

    def test_can_permit_loss_requires_reset_and_new_start(self):
        motor = MockMotor(Mode.REAL)
        bus = MemoryCan(motor)
        controller = CanController(CanReceiver(), bus.try_send, 1)
        controller.heartbeat(0)
        bus.step(controller, 0)
        self.assertIsNone(controller.request("start", Opcode.START_REAL, 60, NS_PER_MS))
        bus.step(controller, NS_PER_MS)
        epoch = motor.epoch
        self.assertEqual(controller.outcomes.popleft().status, "COMPLETED")

        controller.disconnect_can(2 * NS_PER_MS)
        bus.step(controller, 199 * NS_PER_MS)
        self.assertEqual(motor.state, State.RUNNING)
        bus.step(controller, 200 * NS_PER_MS)
        self.assertEqual((motor.state, motor.epoch), (State.FAULT, epoch + 1))
        self.assertEqual(
            (motor.fault_mask, motor.fault_event_seq), (OP_LINK_LOSS_FAULT, 1)
        )
        bus.step(controller, 201 * NS_PER_MS)
        self.assertEqual((motor.epoch, motor.fault_event_seq), (epoch + 1, 1))

        controller.reconnect_can(202 * NS_PER_MS)
        controller.heartbeat(202 * NS_PER_MS)
        bus.step(controller, 202 * NS_PER_MS)
        bus.step(controller, 300 * NS_PER_MS)
        refused = controller.request(
            "before-reset", Opcode.START_REAL, 60, 301 * NS_PER_MS
        )
        self.assertEqual((refused.status, refused.reason), ("REJECTED", "BAD_STATE"))
        self.assertEqual(
            (motor.state, motor.fault_mask), (State.FAULT, OP_LINK_LOSS_FAULT)
        )

        self.assertIsNone(controller.request("reset", Opcode.RESET, 0, 301 * NS_PER_MS))
        bus.step(controller, 301 * NS_PER_MS)
        self.assertEqual(controller.outcomes.popleft().status, "COMPLETED")
        self.assertEqual((motor.state, motor.fault_mask), (State.STOPPED, 0))
        self.assertIsNone(
            controller.request("new-start", Opcode.START_REAL, 60, 302 * NS_PER_MS)
        )
        bus.step(controller, 302 * NS_PER_MS)
        self.assertEqual(controller.outcomes.popleft().status, "COMPLETED")
        self.assertEqual(motor.state, State.RUNNING)

    def test_last_sequence_renews_epoch_after_output_is_off(self):
        motor = MockMotor()
        motor.state = State.FAULT
        motor.fault_mask = 4
        bus = MemoryCan(motor)
        controller = CanController(CanReceiver(), bus.try_send, 1)
        controller.heartbeat(0)
        bus.step(controller, 0)
        epoch = motor.epoch
        controller.cmd_seq = motor.last_cmd_seq = MAX_CMD_SEQ - 1

        self.assertIsNone(controller.request("reset", Opcode.RESET, 0, NS_PER_MS))
        bus.step(controller, NS_PER_MS)
        (reset,) = controller.outcomes
        self.assertEqual(
            (reset.status, reset.motor_epoch, reset.cmd_seq),
            ("COMPLETED", epoch, MAX_CMD_SEQ),
        )
        self.assertEqual((motor.state, motor.epoch), (State.STOPPED, epoch + 1))
        self.assertEqual((controller.epoch, controller.cmd_seq), (epoch + 1, 0))

        controller.outcomes.clear()
        self.assertIsNone(controller.request("arm", Opcode.ARM_TEST, 0, 2 * NS_PER_MS))
        bus.step(controller, 2 * NS_PER_MS)
        (arm,) = controller.outcomes
        self.assertEqual(
            (arm.status, arm.motor_epoch, arm.cmd_seq), ("COMPLETED", epoch + 1, 1)
        )

    def test_mock_backend_restart_drops_old_motion_and_waits_for_new_request(self):
        motor = MockMotor(Mode.REAL)
        bus = MemoryCan(motor)
        first = CanController(CanReceiver(), bus.try_send, 1)
        first.heartbeat(0)
        bus.step(first, 0)
        self.assertIsNone(first.request("start", Opcode.START_REAL, 60, NS_PER_MS))
        bus.step(first, NS_PER_MS)
        self.assertEqual(first.outcomes.popleft().status, "COMPLETED")
        moving_epoch = motor.epoch
        self.assertEqual(motor.state, State.RUNNING)

        restarted = CanController(CanReceiver(), bus.try_send, 2)
        bus.step(restarted, 2 * NS_PER_MS)
        self.assertGreater(motor.epoch, moving_epoch)
        self.assertEqual(motor.state, State.STOPPED)
        restarted.heartbeat(3 * NS_PER_MS)
        bus.step(restarted, 3 * NS_PER_MS)
        self.assertEqual(motor.state, State.STOPPED)
        self.assertFalse(restarted.outcomes)


class MockMotorTest(unittest.TestCase):
    def create_armed_motor(self):
        motor = MockMotor()
        motor.receive(encode_operator_hello(1), 0)
        motor.receive(encode_permit(motor.epoch, 0, ready=True), 0)
        command = encode_command(motor.epoch, 1, Opcode.ARM_TEST, 0)
        (result,) = motor.receive(command, 0)
        self.assertEqual(motor.state, State.TEST_ARMED)
        return motor, command, result

    def test_armed_state_matches_shared_can_example(self):
        motor, _, _ = self.create_armed_motor()
        (armed_frame,) = [
            frame for frame in motor.state_frames() if frame.can_id == CanId.MOTOR_STATE
        ]
        with (FIXTURES / "can-2.1.jsonl").open(encoding="utf-8") as source:
            state_payloads = [
                bytes.fromhex(row["hex"])
                for row in map(json.loads, source)
                if int(row["can_id"], 16) == CanId.MOTOR_STATE
            ]
        self.assertEqual(armed_frame.data, state_payloads[-1])

        frames = motor.tick(200 * NS_PER_MS)
        (fault_state,) = [
            decode_frame(frame) for frame in frames if frame.can_id == CanId.MOTOR_STATE
        ]
        self.assertEqual((fault_state.state, fault_state.flags), (State.FAULT, 0x6C))

    def test_identical_command_reuses_result_and_stop_overrides_bad_sequence(self):
        motor, arm, result = self.create_armed_motor()
        epoch = motor.epoch
        self.assertEqual(motor.receive(arm, 1), (result,))
        conflicting = encode_command(epoch, 1, Opcode.RESET, 0)
        (rejection,) = motor.receive(conflicting, 2)
        self.assertEqual(decode_frame(rejection).detail, 2)
        self.assertEqual(motor.state, State.TEST_ARMED)

        stop = encode_command(epoch, 1, Opcode.STOP, 0)
        (stop_result,) = motor.receive(stop, 3)
        self.assertEqual(decode_frame(stop_result).detail, 2)
        self.assertEqual((motor.state, motor.epoch), (State.STOPPED, epoch + 1))
        (late,) = motor.receive(stop, 4)
        self.assertEqual(decode_frame(late).detail, 1)
        self.assertEqual(motor.epoch, epoch + 1)

    def test_clearing_ready_alone_revokes_mock_permit(self):
        motor, _, _ = self.create_armed_motor()
        epoch = motor.epoch
        motor.receive(encode_permit(epoch, 1), 1)
        self.assertIsNone(motor.permit_until_ns)
        self.assertEqual((motor.state, motor.epoch), (State.STOPPED, epoch + 1))

    def test_malformed_frames_do_not_interrupt_bus_delivery(self):
        motor = MockMotor()
        bus = MemoryCan(motor)
        controller = CanController(CanReceiver(), bus.try_send, 1)
        bus.step(controller, 0)
        controller.heartbeat(NS_PER_MS)
        bus.try_send(Frame(CanId.OP_PERMIT, bytes(7), dlc=7))
        bus.try_send(Frame(CanId.COMMAND, bytes(7), dlc=7))
        self.assertIsNone(controller.request("arm", Opcode.ARM_TEST, 0, NS_PER_MS))

        bus.step(controller, NS_PER_MS)
        self.assertEqual(motor.errors, {CanId.OP_PERMIT: 1, CanId.COMMAND: 1})
        self.assertEqual(controller.outcomes.popleft().status, "COMPLETED")
        self.assertEqual(motor.state, State.TEST_ARMED)

    def test_malformed_permit_cannot_extend_deadline(self):
        motor, _, _ = self.create_armed_motor()
        malformed = Frame(CanId.OP_PERMIT, pack("<IHBB", motor.epoch, 1, 1, 1))

        self.assertEqual(motor.receive(malformed, 199 * NS_PER_MS), ())
        self.assertEqual(motor.permit_until_ns, 200 * NS_PER_MS)
        self.assertEqual(motor.receive(malformed, 200 * NS_PER_MS), ())
        self.assertEqual(
            (motor.state, motor.fault_mask), (State.FAULT, OP_LINK_LOSS_FAULT)
        )
        self.assertEqual(motor.errors[CanId.OP_PERMIT], 2)

    def test_receive_enforces_permit_expiry_before_processing_frame(self):
        for can_id in (CanId.OP_PERMIT, CanId.COMMAND):
            for arrival_ms in (199, 200):
                with self.subTest(can_id=can_id, arrival_ms=arrival_ms):
                    motor = MockMotor(Mode.REAL)
                    epoch = motor.epoch
                    motor.receive(encode_operator_hello(1), 0)
                    motor.receive(encode_permit(epoch, 0, ready=True), 0)
                    frame = (
                        encode_permit(epoch, 1, ready=True)
                        if can_id == CanId.OP_PERMIT
                        else encode_command(epoch, 1, Opcode.START_REAL, 60)
                    )

                    replies = motor.receive(frame, arrival_ms * NS_PER_MS)
                    if arrival_ms == 199:
                        self.assertEqual(motor.epoch, epoch)
                        if can_id == CanId.OP_PERMIT:
                            self.assertEqual(motor.permit_until_ns, 399 * NS_PER_MS)
                        else:
                            self.assertEqual(decode_frame(replies[0]).result, 1)
                            self.assertEqual(motor.state, State.RUNNING)
                        continue

                    self.assertEqual(
                        (motor.epoch, motor.state), (epoch + 1, State.FAULT)
                    )
                    self.assertIsNone(motor.permit_until_ns)
                    self.assertEqual(motor.fault_mask, OP_LINK_LOSS_FAULT)
                    if can_id == CanId.COMMAND:
                        result = decode_frame(replies[0])
                        self.assertEqual(
                            (result.request_epoch, result.detail), (epoch, 1)
                        )

    def test_last_sequence_waits_for_stop_while_active(self):
        motor = MockMotor()
        epoch = motor.epoch
        motor.receive(encode_operator_hello(1), 0)
        motor.receive(encode_permit(epoch, 0, ready=True), 0)
        motor.last_cmd_seq = MAX_CMD_SEQ - 1
        arm = encode_command(epoch, MAX_CMD_SEQ, Opcode.ARM_TEST, 0)

        (completed,) = motor.receive(arm, 1)
        self.assertEqual(decode_frame(completed).result, 1)
        self.assertEqual((motor.state, motor.epoch), (State.TEST_ARMED, epoch))
        self.assertEqual(motor.receive(arm, 2), (completed,))

        motor.receive(encode_permit(epoch, 1, inhibit=True), 3)
        self.assertEqual((motor.state, motor.epoch), (State.STOPPED, epoch + 1))
        (rejected,) = motor.receive(arm, 4)
        self.assertEqual(decode_frame(rejected).detail, 1)

    def test_session_exhaustion_keeps_valid_epoch_and_inhibits_motion(self):
        for mode, opcode, argument in (
            (Mode.REAL, Opcode.START_REAL, 60),
            (Mode.TEST, Opcode.ARM_TEST, 0),
        ):
            with self.subTest(mode=mode):
                motor = MockMotor(mode)
                terminal_epoch = 0x0001FFFF
                motor.epoch = terminal_epoch - 1
                motor.receive(encode_operator_hello(1), 0)
                motor.receive(encode_permit(motor.epoch, 0, inhibit=True), 0)
                self.assertEqual(motor.epoch, terminal_epoch)

                motor.receive(encode_permit(terminal_epoch, 0, ready=True), 1)
                command = encode_command(terminal_epoch, 1, opcode, argument)
                (completed,) = motor.receive(command, 1)
                self.assertEqual(decode_frame(completed).result, 1)
                motor.receive(encode_permit(terminal_epoch, 1, inhibit=True), 2)
                hello_msg, state_msg, fault_msg = map(decode_frame, motor.tick(2))
                self.assertEqual(hello_msg.epoch, terminal_epoch)
                self.assertEqual(state_msg.state, State.FAULT)
                self.assertEqual(fault_msg.mask, INTERNAL_FAULT)

                motor.receive(encode_operator_hello(2), 3)
                motor.receive(encode_permit(terminal_epoch, 2, ready=True), 3)
                self.assertIsNone(motor.permit_until_ns)
                for pending in (
                    command,
                    encode_command(terminal_epoch, 2, opcode, argument),
                    encode_command(terminal_epoch, 3, Opcode.RESET, 0),
                ):
                    (rejected,) = motor.receive(pending, 3)
                    result = decode_frame(rejected)
                    self.assertEqual((result.result, result.detail), (2, 5))
                self.assertEqual(motor.fault_event_seq, fault_msg.event_seq)


class TcpCanControlTest(unittest.IsolatedAsyncioTestCase):
    def test_operator_snapshot_tracks_permit_lifetime(self):
        mock = TcpCanMock(boot_id=1)
        mock.controller.heartbeat(0)

        with patch("backend.tcp_mock.time.monotonic_ns", return_value=0):
            operator = mock.snapshot(None, 0)["operator"]
            self.assertEqual(
                (operator["link"], operator["age_ms"], operator["controls_ready"]),
                ("offline", None, None),
            )
            mock.bus.step(mock.controller, 0)

        for now_ms, link, ready in ((199, "fresh", True), (200, "stale", None)):
            with (
                self.subTest(now_ms=now_ms),
                patch(
                    "backend.tcp_mock.time.monotonic_ns",
                    return_value=now_ms * NS_PER_MS,
                ),
            ):
                operator = mock.snapshot(None, 0)["operator"]
                self.assertEqual(
                    (operator["link"], operator["age_ms"], operator["controls_ready"]),
                    (link, now_ms, ready),
                )

    async def test_tcp_arm_duplicate_stop_and_reconnect(self):
        mock = TcpCanMock(boot_id=1)
        bus = asyncio.create_task(mock.run_bus())
        try:
            async with await asyncio.start_server(
                mock.handle_client, "127.0.0.1", 0
            ) as server:
                address = server.sockets[0].getsockname()
                reader, writer = await asyncio.open_connection(*address)
                try:
                    stream = (await self.wait_for(reader, "hello"))["stream_id"]
                    await self.wait_for(
                        reader,
                        "snapshot",
                        lambda row: row["data"]["motor"]["state"] == "STOPPED",
                    )
                    await self.send(writer, "control_heartbeat", stream)
                    arm_id = str(uuid.uuid4())
                    await self.send(
                        writer,
                        "control_request",
                        stream,
                        request_id=arm_id,
                        opcode="ARM_TEST",
                        argument=0,
                    )
                    arm = (await self.wait_for(reader, "control_result"))["data"]
                    self.assertEqual(
                        (arm["request_id"], arm["status"], arm["cmd_seq"]),
                        (arm_id, "COMPLETED", 1),
                    )
                    await self.wait_for(
                        reader,
                        "snapshot",
                        lambda row: row["data"]["motor"]["state"] == "TEST_ARMED",
                    )

                    await self.send(writer, "future_extension", stream)
                    await self.send(writer, "control_heartbeat", stream)
                    await self.send(
                        writer,
                        "control_request",
                        stream,
                        request_id=arm_id,
                        opcode="ARM_TEST",
                        argument=0,
                    )
                    self.assertEqual(
                        (await self.wait_for(reader, "control_result"))["data"], arm
                    )
                    stop_id = str(uuid.uuid4())
                    await self.send(
                        writer,
                        "control_request",
                        stream,
                        request_id=stop_id,
                        opcode="STOP",
                        argument=0,
                    )
                    stop_result = (await self.wait_for(reader, "control_result"))[
                        "data"
                    ]
                    self.assertEqual(
                        (stop_result["request_id"], stop_result["reason"]),
                        (stop_id, "BAD_EPOCH"),
                    )
                    stopped = await self.wait_for(
                        reader,
                        "snapshot",
                        lambda row: (
                            row["data"]["motor"]["state"] == "STOPPED"
                            and row["data"]["motor"]["epoch"] > arm["motor_epoch"]
                        ),
                    )
                    self.assertEqual(
                        stopped["data"]["motor"]["epoch"], arm["motor_epoch"] + 1
                    )
                finally:
                    writer.close()
                    await writer.wait_closed()

                first_reader, first_writer, greeting = await self.reconnect(address)
                try:
                    new_stream = greeting["stream_id"]
                    self.assertNotEqual(new_stream, stream)
                    await self.send(
                        first_writer,
                        "control_request",
                        new_stream,
                        request_id=arm_id,
                        opcode="ARM_TEST",
                        argument=0,
                    )
                    result = (await self.wait_for(first_reader, "control_result"))[
                        "data"
                    ]
                    self.assertEqual(result["reason"], "REQUEST_FROM_OLD_STREAM")
                finally:
                    first_writer.close()
                    await first_writer.wait_closed()
        finally:
            mock.close()
            bus.cancel()
            await asyncio.gather(bus, return_exceptions=True)

    async def reconnect(self, address):
        deadline = time.monotonic() + 2
        while True:
            reader, writer = await asyncio.open_connection(*address)
            try:
                return reader, writer, await self.wait_for(reader, "hello")
            except EOFError:
                writer.close()
                await writer.wait_closed()
                if time.monotonic() >= deadline:
                    raise
                await asyncio.sleep(0.02)

    @staticmethod
    async def send(writer, kind, stream, **data):
        message = {
            "schema": "can-do/1.1",
            "type": kind,
            "data": {"stream_id": stream, **data},
        }
        writer.write((json.dumps(message) + "\n").encode())
        await writer.drain()

    async def wait_for(self, reader, kind, predicate=lambda row: True):
        async def search():
            while line := await reader.readline():
                message = json.loads(line)
                if message["type"] == kind and predicate(message):
                    return message
            raise EOFError("예상한 TCP 메시지 전에 연결이 종료됐다")

        return await asyncio.wait_for(search(), 2)


if __name__ == "__main__":
    unittest.main()
