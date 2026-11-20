"""전원 차단용 SocketCAN 경로를 가상 소켓으로 검증한다."""

import asyncio
import errno
import json
import socket
import tempfile
import time
import unittest
import uuid
from collections import deque
from pathlib import Path
from unittest.mock import Mock, patch

from check_monitor import connected, observe, read_frame, snapshot_where

from backend.can_control.controller import CanController
from backend.can_control.mock_motor import MockMotor
from backend.can_control.wire import Opcode, encode_operator_hello, encode_permit
from backend.can_rx.codec import CanId, Mode, State, decode_frame
from backend.can_rx.samples import NS_PER_MS
from backend.can_rx.state import CONTROLS_READY, STOP_REQUESTED, CanReceiver
from backend.hardware.live_control import (
    MAX_RX_PER_POLL,
    RETRY_NS,
    SocketCanControl,
    serve_live_control,
)
from backend.hardware.socketcan import (
    CAN_ERR_BUSOFF,
    CAN_ERR_FLAG,
    CAN_FRAME,
    CanRead,
    pack_frame,
    read_can_observation,
    unpack_frame,
)
from backend.hardware.tx_probe import send_hello_probe


class DatagramCan:
    def __init__(self):
        self.peer, self.socket = socket.socketpair(type=socket.SOCK_DGRAM)
        self.peer.setblocking(False)
        self.socket.setblocking(False)
        self.send_errno = None

    def close(self):
        self.socket.close()
        self.peer.close()

    def send(self, packet):
        if self.send_errno is not None and unpack_frame(packet).can_id == CanId.COMMAND:
            raise OSError(self.send_errno, "모의 송신 실패")
        return self.socket.send(packet)

    def recvmsg(self, *args):
        return self.socket.recvmsg(*args)

    def inject(self, *frames):
        for frame in frames:
            self.peer.send(pack_frame(frame))

    def transmitted(self):
        frames = []
        while True:
            try:
                packet = self.peer.recv(CAN_FRAME.size)
            except BlockingIOError:
                return frames
            frames.append(unpack_frame(packet))


class LiveControlTest(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.devices = deque()
        self.device = self.new_device()
        self.bus = SocketCanControl(self.devices.popleft, now_ns=lambda: self.now)
        self.controller = CanController(
            CanReceiver("test"), self.bus.try_send, boot_id=1, power_off_test=True
        )
        self.controller.disconnect_can(self.now)
        self.motor = MockMotor()

    def new_device(self):
        device = DatagramCan()
        self.addCleanup(device.close)
        self.devices.append(device)
        return device

    def step(self, advance_ms=0):
        self.now += advance_ms * NS_PER_MS
        self.bus.step(self.controller)
        return self.device.transmitted()

    def deliver(self, frames):
        for frame in frames:
            self.device.inject(*self.motor.receive(frame, self.now))
        self.device.inject(*self.motor.state_frames())

    def ready(self):
        self.device.inject(*self.motor.state_frames())
        first = self.step()
        self.assertEqual(
            [frame.can_id for frame in first], [CanId.OP_HELLO, CanId.OP_PERMIT]
        )
        self.assertEqual(decode_frame(first[1]).flags, STOP_REQUESTED)
        self.deliver(first)
        self.controller.heartbeat(self.now)
        permits = self.step(10)
        self.assertEqual(len(permits), 1)
        self.assertEqual(decode_frame(permits[0]).flags, CONTROLS_READY)
        self.deliver(permits)

    def test_command_result_requires_mcu_reply_and_stop_preempts_unsent_arm(self):
        self.ready()
        self.assertIsNone(self.controller.request("arm", Opcode.ARM_TEST, 0, self.now))
        (command,) = self.step(10)
        self.assertEqual(decode_frame(command).opcode, Opcode.ARM_TEST)
        self.assertFalse(self.controller.outcomes)
        self.deliver([command])
        self.assertEqual(self.step(10), [])
        self.assertEqual(self.controller.outcomes.pop().status, "COMPLETED")

        self.motor.stop_and_renew()
        self.deliver([])
        self.deliver(self.step(10))
        queued = self.controller.request("queued", Opcode.ARM_TEST, 0, self.now)
        self.assertIsNone(queued)
        self.assertIsNone(self.controller.request("stop", Opcode.STOP, 0, self.now))
        inhibit, stop = self.step(10)
        self.assertEqual(decode_frame(inhibit).flags, STOP_REQUESTED)
        self.assertEqual(decode_frame(stop).opcode, Opcode.STOP)
        self.assertEqual(self.controller.outcomes.pop().reason, "CANCELLED_BY_STOP")

    def test_backpressure_times_out_and_does_not_replay_command(self):
        self.ready()
        self.device.send_errno = errno.ENOBUFS
        self.controller.request("arm", Opcode.ARM_TEST, 0, self.now)
        self.assertEqual(self.step(49), [])
        self.device.send_errno = None
        self.assertFalse(any(frame.can_id == CanId.COMMAND for frame in self.step(1)))
        outcome = self.controller.outcomes.pop()
        self.assertEqual((outcome.status, outcome.reason), ("FAILED", "SEND_TIMEOUT"))

    def test_stop_submits_inhibit_while_receive_queue_still_has_frames(self):
        self.ready()
        old_hello = self.motor.state_frames()[0]
        self.device.inject(*([old_hello] * MAX_RX_PER_POLL))
        self.motor.stop_and_renew()
        self.deliver([])
        self.controller.request("stop", Opcode.STOP, 0, self.now)

        (early_inhibit,) = self.step(10)
        self.assertEqual(decode_frame(early_inhibit).flags, STOP_REQUESTED)
        self.assertIsNone(self.controller.inhibit.sent_after_record)

        (verified_inhibit,) = self.step(10)
        self.assertIsNotNone(self.controller.inhibit)
        self.assertEqual(decode_frame(verified_inhibit).flags, STOP_REQUESTED)
        self.assertEqual(decode_frame(verified_inhibit).epoch, self.motor.epoch)
        self.assertIsNotNone(self.controller.inhibit.sent_after_record)
        self.deliver([verified_inhibit])
        (permit,) = self.step(10)
        self.assertEqual(decode_frame(permit).flags, CONTROLS_READY)

    def test_pending_stop_can_submit_once_receive_queue_drains(self):
        self.ready()
        hello = self.motor.state_frames()[0]
        self.device.inject(*([hello] * (MAX_RX_PER_POLL + 1)))
        self.controller.request("stop", Opcode.STOP, 0, self.now)

        (early_inhibit,) = self.step(10)
        self.assertEqual(decode_frame(early_inhibit).flags, STOP_REQUESTED)
        inhibit, stop = self.step(10)
        self.assertEqual(decode_frame(inhibit).flags, STOP_REQUESTED)
        self.assertEqual(decode_frame(stop).opcode, Opcode.STOP)

    def test_periodic_permit_and_result_timeout_do_not_resend_command(self):
        self.ready()
        self.controller.request("arm", Opcode.ARM_TEST, 0, self.now)
        (command,) = self.step()
        self.assertEqual(command.can_id, CanId.COMMAND)

        self.assertEqual(self.step(49), [])
        (permit,) = self.step(1)
        self.assertEqual(decode_frame(permit).flags, CONTROLS_READY)

        for _ in range(5):
            self.deliver([])
            self.controller.heartbeat(self.now)
            self.assertTrue(
                all(frame.can_id != CanId.COMMAND for frame in self.step(50))
            )
        outcome = self.controller.outcomes.pop()
        self.assertEqual(
            (outcome.status, outcome.reason), ("UNKNOWN_OUTCOME", "RESULT_TIMEOUT")
        )

    def test_bus_off_discards_pending_command_and_requires_new_heartbeat(self):
        self.ready()
        self.controller.request("arm", Opcode.ARM_TEST, 0, self.now)
        error_frame = CAN_FRAME.pack(CAN_ERR_FLAG | CAN_ERR_BUSOFF, 8, 0, bytes(8))
        self.device.peer.send(error_frame)
        with self.assertLogs("backend.hardware.live_control", level="WARNING"):
            self.bus.step(self.controller)
        self.assertEqual(self.device.socket.fileno(), -1)
        self.assertEqual(self.controller.outcomes.pop().reason, "CANCELLED_BY_CAN_LOSS")

        self.device = self.new_device()
        self.device.inject(*self.motor.state_frames())
        self.now += RETRY_NS
        self.controller.heartbeat(self.now)
        recovered = self.step()
        self.assertEqual(
            [frame.can_id for frame in recovered], [CanId.OP_HELLO, CanId.OP_PERMIT]
        )
        self.assertEqual(decode_frame(recovered[1]).flags, STOP_REQUESTED)
        self.deliver(recovered)
        self.assertEqual(self.step(10), [])
        self.assertEqual(self.motor.state, State.FAULT)
        self.assertIsNone(self.controller.pending)
        self.assertEqual(
            self.controller.request("new", Opcode.ARM_TEST, 0, self.now).reason,
            "HEARTBEAT_REQUIRED",
        )

    def test_fatal_send_failure_cancels_command_and_closes_socket(self):
        self.ready()
        self.device.send_errno = errno.ENETDOWN
        self.controller.request("arm", Opcode.ARM_TEST, 0, self.now)
        with self.assertLogs("backend.hardware.live_control", level="WARNING"):
            self.bus.step(self.controller)
        outcome = self.controller.outcomes.pop()
        self.assertEqual(
            (outcome.status, outcome.reason), ("FAILED", "CANCELLED_BY_CAN_LOSS")
        )
        self.assertIsNone(self.bus.socket)
        self.assertFalse(self.controller.can_connected)
        self.assertEqual(self.device.socket.fileno(), -1)

    def test_receive_queue_drop_attempts_inhibit_before_closing_socket(self):
        self.ready()
        self.controller.request("arm", Opcode.ARM_TEST, 0, self.now)

        with (
            patch(
                "backend.hardware.live_control.read_can_observation",
                return_value=CanRead(frame=None, dropped_total=1),
            ),
            patch.object(self.device, "send", wraps=self.device.send) as send,
            self.assertLogs("backend.hardware.live_control", level="WARNING"),
        ):
            self.bus.step(self.controller)

        send.assert_called_once()
        frame = decode_frame(unpack_frame(send.call_args.args[0]))
        self.assertEqual(frame.flags, STOP_REQUESTED)
        self.assertIsNone(self.controller.inhibit.sent_after_record)
        self.assertEqual(self.controller.outcomes.pop().reason, "CANCELLED_BY_CAN_LOSS")
        self.assertIsNone(self.bus.socket)

    def test_malformed_received_packet_attempts_inhibit_before_socket_close(self):
        self.ready()
        self.device.peer.send(b"short")
        with (
            patch.object(self.device, "send", wraps=self.device.send) as send,
            self.assertLogs("backend.hardware.live_control", level="WARNING"),
        ):
            self.bus.step(self.controller)

        send.assert_called_once()
        frame = decode_frame(unpack_frame(send.call_args.args[0]))
        self.assertEqual(frame.flags, STOP_REQUESTED)
        self.assertIsNone(self.controller.inhibit.sent_after_record)
        self.assertIsNone(self.bus.socket)

    def test_mcu_reboot_revokes_permission_without_socket_loss(self):
        self.ready()
        self.controller.request("old-arm", Opcode.ARM_TEST, 0, self.now)
        old_boot = self.motor.epoch >> 16
        self.motor = MockMotor()
        self.motor.epoch = ((old_boot + 1) << 16) | 1
        self.deliver([])

        sent = self.step(10)
        self.assertFalse(
            any(
                frame.can_id == CanId.OP_PERMIT
                and decode_frame(frame).flags & CONTROLS_READY
                for frame in sent
            )
        )
        hello, inhibit = sent
        self.assertEqual(hello.can_id, CanId.OP_HELLO)
        self.assertEqual(decode_frame(inhibit).flags, STOP_REQUESTED)
        self.assertEqual(self.controller.outcomes.pop().reason, "CANCELLED_BY_SESSION")
        self.assertIs(self.bus.socket, self.device)

        self.deliver([hello, inhibit])
        self.assertEqual(self.step(10), [])
        result = self.controller.request("no-heartbeat", Opcode.ARM_TEST, 0, self.now)
        self.assertEqual(result.reason, "HEARTBEAT_REQUIRED")

        self.controller.heartbeat(self.now)
        (permit,) = self.step(10)
        self.assertEqual(decode_frame(permit).flags, CONTROLS_READY)
        self.assertIsNone(self.controller.pending)

    def test_real_mode_cannot_gain_permit_or_send_start(self):
        self.ready()
        self.motor.mode = Mode.REAL
        self.deliver([])
        (permit,) = self.step(10)
        self.assertEqual(decode_frame(permit).flags, STOP_REQUESTED)
        self.assertEqual(
            self.controller.request("real", Opcode.START_REAL, 100, self.now).reason,
            "POWER_OFF_TEST_ONLY",
        )

    def test_only_own_operator_echo_is_admitted_for_live_control(self):
        sock = Mock()
        hello = pack_frame(encode_operator_hello(1))
        permit = pack_frame(encode_permit(self.motor.epoch, 1, ready=True))
        motor_state = pack_frame(self.motor.state_frames()[1])
        other = socket.MSG_DONTROUTE
        own = other | socket.MSG_CONFIRM
        sock.recvmsg.side_effect = [
            (permit, [], 0, None),
            (hello, [], 0, None),
            (hello, [], other, None),
            (hello, [], own, None),
            (permit, [], own, None),
            (motor_state, [], 0, None),
            (permit, [], 0, None),
            (hello, [], own, None),
        ]
        expected = (
            None,
            None,
            None,
            CanId.OP_HELLO,
            CanId.OP_PERMIT,
            CanId.MOTOR_STATE,
        )
        for can_id in expected:
            with self.subTest(can_id=can_id):
                frame = read_can_observation(sock, own_operator=True).frame
                self.assertEqual(frame.can_id if frame is not None else None, can_id)
        self.assertEqual(read_can_observation(sock).frame.can_id, CanId.OP_PERMIT)
        self.assertIsNone(read_can_observation(sock).frame)


class LiveControlEntryTest(unittest.IsolatedAsyncioTestCase):
    async def test_operator_boot_file_is_reserved_until_service_exits(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("backend.hardware.live_control.run_tcp_server") as server,
            patch("backend.hardware.tx_probe.open_can") as open_probe,
        ):
            boot_id = Path(directory) / "boot-id"

            async def verify_exclusive(*args):
                server.side_effect = None
                with self.assertRaises(BlockingIOError):
                    await serve_live_control(
                        "can-test", boot_id, 0, motor_power_off=True
                    )
                with self.assertRaises(BlockingIOError):
                    send_hello_probe("can-test", boot_id, 0.5, motor_power_off=True)
                open_probe.return_value.__enter__.return_value.send.assert_not_called()
                self.assertEqual(boot_id.read_text().strip(), "1")

            server.side_effect = verify_exclusive
            await serve_live_control("can-test", boot_id, 0, motor_power_off=True)
            server.side_effect = None
            await serve_live_control("can-test", boot_id, 0, motor_power_off=True)
            self.assertEqual(boot_id.read_text().strip(), "2")

    async def test_power_off_confirmation_precedes_sockets_and_boot_id(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("backend.hardware.live_control.open_can") as opener,
        ):
            boot_id = Path(directory) / "boot-id"
            with self.assertRaises(ValueError):
                await serve_live_control("can0", boot_id, 0)
            self.assertFalse(boot_id.exists())
            opener.assert_not_called()

    async def test_tcp_and_socket_command_flow_and_disconnect_inhibit(self):
        device = DatagramCan()
        self.addCleanup(device.close)
        motor = MockMotor()
        sent = []

        async def emulate_motor():
            while True:
                now = time.monotonic_ns()
                device.inject(*motor.tick(now))
                for frame in device.transmitted():
                    sent.append(frame)
                    device.inject(*motor.receive(frame, now))
                await asyncio.sleep(0.005)

        async def send(writer, stream_id, kind, **fields):
            message = {
                "schema": "can-do/1.1",
                "type": kind,
                "data": {"stream_id": stream_id, **fields},
            }
            writer.write((json.dumps(message) + "\n").encode())
            await writer.drain()

        async def wait_disconnected(stream):
            while stream.client is not None:
                await asyncio.sleep(0.005)

        async def verify_tcp(stream, host, port, background):
            self.assertEqual(host, "127.0.0.1")
            server = await asyncio.start_server(stream.handle_client, host, port)
            workers = [
                asyncio.create_task(background()),
                asyncio.create_task(emulate_motor()),
            ]
            try:
                async with server:
                    port = server.sockets[0].getsockname()[1]
                    async with connected(port) as (reader, writer):
                        hello = await read_frame(reader)
                        self.assertEqual(hello["source"], "live")
                        stream_id = hello["stream_id"]
                        await snapshot_where(
                            reader,
                            lambda snap: (
                                snap["motor"]["state"] == "STOPPED"
                                and stream.controller.inhibit is None
                            ),
                        )
                        await send(writer, stream_id, "control_heartbeat")
                        request_id = str(uuid.uuid4())
                        await send(
                            writer,
                            stream_id,
                            "control_request",
                            request_id=request_id,
                            opcode="ARM_TEST",
                            argument=0,
                        )
                        result = await observe(
                            reader, lambda msg: msg["type"] == "control_result"
                        )
                        self.assertEqual(result["data"]["status"], "COMPLETED")
                        await snapshot_where(
                            reader, lambda snap: snap["motor"]["state"] == "TEST_ARMED"
                        )

                    await asyncio.wait_for(wait_disconnected(stream), 2)
                    async with connected(port) as (reader, writer):
                        hello = await read_frame(reader)
                        self.assertNotEqual(hello["stream_id"], stream_id)
                        await snapshot_where(
                            reader, lambda snap: snap["motor"]["state"] == "STOPPED"
                        )
                        await send(
                            writer,
                            hello["stream_id"],
                            "control_request",
                            request_id=request_id,
                            opcode="ARM_TEST",
                            argument=0,
                        )
                        result = await observe(
                            reader, lambda msg: msg["type"] == "control_result"
                        )
                        self.assertEqual(
                            result["data"]["reason"], "REQUEST_FROM_OLD_STREAM"
                        )
                        self.assertEqual(
                            [frame.can_id for frame in sent].count(CanId.COMMAND), 1
                        )
            finally:
                for worker in workers:
                    worker.cancel()
                results = await asyncio.gather(*workers, return_exceptions=True)
                for result in results:
                    if not isinstance(result, asyncio.CancelledError):
                        raise result
                stream.close()

        with (
            tempfile.TemporaryDirectory() as directory,
            patch("backend.hardware.live_control.open_can", return_value=device),
            patch(
                "backend.hardware.live_control.run_tcp_server", side_effect=verify_tcp
            ),
        ):
            boot_id = Path(directory) / "boot-id"
            await serve_live_control("can-test", boot_id, 0, motor_power_off=True)
            self.assertEqual(boot_id.read_text().strip(), "1")
            self.assertEqual(device.socket.fileno(), -1)
