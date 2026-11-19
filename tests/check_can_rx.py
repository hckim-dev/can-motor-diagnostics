"""CAN 바이트 예제와 모의 수신 시각으로 디코딩 및 상태 처리를 검증한다."""

import asyncio
import json
import unittest
from pathlib import Path
from struct import pack
from unittest.mock import AsyncMock

from backend.can_rx.codec import CanId, Frame, InvalidFrame, decode_frame
from backend.can_rx.fixture import feed_frames, load_frames
from backend.can_rx.samples import NS_PER_MS
from backend.can_rx.state import CanReceiver
from backend.tcp_mock import TcpMock, wait_for_stop

FIXTURES = Path(__file__).with_name("fixtures")
EPOCH = 0x00010001
BOOT = 1


def hello(epoch=EPOCH, version=(2, 1), capabilities=0x1F):
    return Frame(CanId.MOTOR_HELLO, pack("<IBBBB", epoch, *version, capabilities, 0))


def state(epoch=EPOCH, flags=0, mode=2):
    return Frame(CanId.MOTOR_STATE, pack("<IBBBB", epoch, 1, mode, flags, 0))


def fault(epoch=EPOCH, mask=0, seq=0):
    return Frame(CanId.MOTOR_FAULT, pack("<IHBB", epoch, mask, seq, 2))


def speed(seq, rpm=59, pwm=450):
    return Frame(CanId.SPEED, pack("<HhHH", seq, rpm, 60, pwm))


def electric(seq, current=250, bus=6000, flags=0x1B, age=2):
    return Frame(CanId.ELECTRIC, pack("<HhHBB", seq, current, bus, flags, age))


def timing(seq, tick, boot=BOOT):
    return Frame(CanId.SAMPLE_TIME, pack("<HIH", seq, tick, boot))


class DecodeTest(unittest.TestCase):
    def test_shared_can_frames(self):
        records = (
            json.loads(line)
            for line in (FIXTURES / "can-2.1.jsonl").read_text().splitlines()
        )
        messages = [
            decode_frame(
                Frame(int(row["can_id"], 16), bytes.fromhex(row["hex"]), row["dlc"])
            )
            for row in records
        ]
        operator, motor, initial, fault_msg, permit, arm, result, armed, revoke = (
            messages
        )
        self.assertEqual(operator.version, motor.version)
        self.assertEqual(motor.version, (2, 1))
        self.assertEqual(
            (motor.epoch, initial.state.name, fault_msg.mask), (EPOCH, "STOPPED", 0)
        )
        self.assertEqual((permit.flags, arm.opcode, arm.argument), (1, 4, 0))
        self.assertEqual(
            (result.request_epoch, result.cmd_seq, result.result), (EPOCH, 1, 1)
        )
        self.assertEqual((armed.state.name, revoke.flags), ("TEST_ARMED", 4))

    def test_signed_sentinel_and_electric_mode(self):
        self.assertEqual(decode_frame(speed(1, rpm=-1)).actual_rpm, -1)
        self.assertIsNone(decode_frame(speed(1, rpm=-32768, pwm=65535)).actual_rpm)
        old = decode_frame(electric(1, current=-32768, flags=0x1A, age=15))
        self.assertIsNone(old.current_ma)
        self.assertEqual(
            (old.bus_mv, old.mode.name, old.state.name, old.sensor_age_ms),
            (6000, "TEST", "STOPPED", 150),
        )

    def test_invalid_frames_and_unknown_id(self):
        for frame in (
            Frame(CanId.MOTOR_STATE, b"\0" * 8, dlc=7),
            Frame(CanId.MOTOR_STATE, b"\0" * 8, extended=True),
            Frame(CanId.MOTOR_STATE, b"\0" * 8, rtr=True),
            Frame(CanId.MOTOR_STATE, b"\0" * 8, error=True),
            hello(capabilities=0x80),
            state(mode=9),
            fault(mask=0x200),
            speed(1, pwm=1001),
            electric(1, current=-32768),
            electric(1, age=255),
            electric(1, flags=0x80),
            timing(1, 0, boot=0),
        ):
            with self.subTest(frame=frame), self.assertRaises(InvalidFrame):
                decode_frame(frame)
        self.assertIsNone(decode_frame(Frame(0x321, b"\x00", dlc=1)))


class ReceiveTest(unittest.TestCase):
    def setUp(self):
        self.rx = CanReceiver("backend-test")

    def ingest(self, at_ms, *frames):
        return [self.rx.ingest(frame, at_ms * NS_PER_MS) for frame in frames]

    def snapshot(self, at_ms):
        return self.rx.snapshot(at_ms * NS_PER_MS)

    def sample(self, at_ms, seq, tick, boot=BOOT):
        return self.ingest(at_ms, speed(seq), electric(seq), timing(seq, tick, boot))

    def test_initial_state_and_version(self):
        self.ingest(0, state(), fault())
        initial = self.snapshot(0)
        self.assertEqual(
            (initial["motor"]["state"], initial["motor"]["link"]),
            ("UNKNOWN", "offline"),
        )
        self.assertIsNone(initial["motor"]["fault_mask"])

        self.ingest(5, hello(version=(2, 0), capabilities=0x0F), state())
        self.assertEqual(self.snapshot(5)["motor"]["state"], "UNKNOWN")

        self.ingest(10, hello(), state(flags=0x80))
        self.assertTrue(self.snapshot(10)["operator"]["estop_active"])
        self.ingest(11, hello(version=(2, 0), capabilities=0x0F))
        self.assertIsNone(self.snapshot(11)["operator"]["estop_active"])
        self.ingest(12, hello())
        self.assertEqual(self.snapshot(12)["motor"]["state"], "UNKNOWN")

    def test_incompatible_hello_cannot_advance_control_session(self):
        operator = Frame(CanId.OP_HELLO, pack("<IBBBB", 1, 2, 1, 0, 0))
        permit = Frame(CanId.OP_PERMIT, pack("<IHBB", EPOCH, 1, 1, 0))
        self.ingest(0, hello(), state(), operator, permit)
        self.assertTrue(self.snapshot(0)["operator"]["controls_ready"])

        unsupported_motor = hello(EPOCH + 5, version=(2, 0), capabilities=0x0F)
        unsupported_operator = Frame(CanId.OP_HELLO, pack("<IBBBB", 5, 2, 0, 0, 0))
        self.ingest(10, unsupported_motor, unsupported_operator, permit)
        invalid = self.snapshot(10)
        self.assertEqual(invalid["motor"]["state"], "UNKNOWN")
        self.assertIsNone(invalid["operator"]["controls_ready"])

        next_epoch = EPOCH + 1
        restarted = Frame(CanId.OP_HELLO, pack("<IBBBB", 2, 2, 1, 0, 0))
        next_permit = Frame(CanId.OP_PERMIT, pack("<IHBB", next_epoch, 1, 1, 0))
        self.ingest(11, restarted, hello(next_epoch), state(next_epoch), next_permit)
        self.ingest(12, hello(), operator, permit)
        recovered = self.snapshot(12)
        self.assertEqual(recovered["motor"]["epoch"], next_epoch)
        self.assertTrue(recovered["operator"]["controls_ready"])

    def test_independent_expiry_and_bad_frame_cannot_refresh(self):
        self.ingest(0, hello(), state(flags=0x80), fault(mask=4, seq=1))
        self.ingest(10, speed(42), electric(42, age=15), timing(42, 100))
        self.assertIsNotNone(self.snapshot(60)["motor"]["sample"]["bus_mv"])
        reading = self.snapshot(61)["motor"]["sample"]
        self.assertEqual(
            (reading["actual_rpm"], reading["bus_mv"], reading["sensor_age_ms"]),
            (59, None, 201),
        )

        self.ingest(299, state(mode=9))
        aged = self.snapshot(300)
        self.assertEqual(aged["motor"]["state"], "UNKNOWN")
        self.assertEqual(aged["motor"]["fault_mask"], 4)
        self.assertIsNone(aged["operator"]["estop_active"])
        self.assertEqual(self.rx.errors[CanId.MOTOR_STATE], 1)

        self.ingest(700, hello(), state())
        self.ingest(950, state())
        self.assertEqual(self.snapshot(1000)["motor"]["link"], "fresh")
        self.assertIsNone(self.snapshot(1000)["motor"]["fault_mask"])

    def test_evidence_partial_and_no_late_overwrite(self):
        self.ingest(0, hello())
        records = self.ingest(10, speed(12), timing(12, 50))
        self.assertIsNone(self.snapshot(109)["motor"]["sample"]["seq"])
        partial = self.snapshot(110)["motor"]["sample"]
        self.assertEqual(
            (partial["seq"], partial["actual_rpm"], partial["sample_complete"]),
            (12, 59, False),
        )
        self.assertIsNone(partial["bus_mv"])
        self.assertEqual(
            self.rx.samples.latest.evidence_ids, tuple(row.record_id for row in records)
        )

        self.ingest(111, electric(12))
        self.assertFalse(self.snapshot(111)["motor"]["sample"]["sample_complete"])
        self.assertEqual(records[0].record_id, "backend-test:2")
        self.assertEqual(records[0].rx_mono_ns, records[1].rx_mono_ns)

    def test_invalid_values_and_missing_timestamp(self):
        self.ingest(0, hello(), speed(1), electric(1))
        self.assertIsNone(self.snapshot(100)["motor"]["sample"]["seq"])
        observations = self.ingest(
            110,
            timing(2, 50),
            speed(2, rpm=-1),
            electric(2, current=-32768),
        )
        self.assertIsNone(self.snapshot(209)["motor"]["sample"]["seq"])
        reading = self.snapshot(210)["motor"]["sample"]
        self.assertEqual((reading["actual_rpm"], reading["quality"]), (-1, "invalid"))
        self.assertIsNone(reading["current_ma"])
        self.assertFalse(reading["sample_complete"])
        self.assertIsNotNone(observations[-1].error)
        self.assertIn(observations[-1].record_id, self.rx.samples.latest.evidence_ids)

    def test_same_boot_advanced_epoch_preserves_wrap_and_drops_old_state(self):
        self.ingest(0, hello(), state(), fault())
        self.sample(1, 65535, 0xFFFFFFF0)
        acquisition = self.rx.samples.latest.acquisition_id
        self.ingest(5, state(EPOCH + 1))
        self.assertEqual(self.snapshot(5)["motor"]["state"], "UNKNOWN")
        self.ingest(6, hello(), state())
        self.assertIsNone(self.snapshot(6)["motor"]["epoch"])
        self.ingest(7, hello(EPOCH + 1), state())
        self.assertEqual(self.snapshot(7)["motor"]["state"], "UNKNOWN")
        self.ingest(8, state(EPOCH + 1))
        self.sample(9, 0, 20)
        self.assertEqual(self.rx.samples.latest.seq64, 65536)
        self.assertEqual(self.rx.samples.latest.acquisition_id, acquisition)
        self.assertEqual(self.snapshot(9)["motor"]["sample"]["tick_ms"], 20)

    def test_boot_change_quarantine_and_reconnect(self):
        self.ingest(0, hello())
        self.sample(1, 1, 1)
        old_acquisition = self.rx.samples.latest.acquisition_id
        new_epoch = 0x00020001
        self.ingest(
            10, timing(1, 10, boot=2), hello(new_epoch), hello(), state(new_epoch)
        )
        self.sample(309, 1, 10, boot=2)
        self.assertIsNone(self.snapshot(309)["motor"]["sample"]["seq"])
        self.ingest(310, timing(1, 310, boot=1))
        self.sample(310, 1, 11, boot=2)
        self.assertEqual(self.rx.samples.latest.boot, 2)
        self.assertNotEqual(self.rx.samples.latest.acquisition_id, old_acquisition)
        self.rx.disconnect(320 * NS_PER_MS)
        self.assertEqual(self.snapshot(320)["motor"]["link"], "offline")
        self.ingest(330, hello(new_epoch))
        self.sample(629, 2, 20, boot=2)
        self.assertIsNone(self.snapshot(629)["motor"]["sample"]["seq"])
        self.sample(630, 2, 21, boot=2)
        self.assertEqual(self.snapshot(630)["motor"]["sample"]["seq"], 2)

    def test_fault_repeats_and_permit_replay(self):
        op = Frame(CanId.OP_HELLO, pack("<IBBBB", 1, 2, 1, 0, 0))
        permit = Frame(CanId.OP_PERMIT, pack("<IHBB", EPOCH, 1, 1, 0))
        self.ingest(0, hello(), op, permit, fault(mask=4, seq=255))
        self.ingest(199, permit)
        self.assertIsNone(self.snapshot(200)["operator"]["controls_ready"])
        self.ingest(250, fault(mask=0, seq=0))
        self.ingest(300, fault(mask=4, seq=255))
        self.ingest(600, fault(mask=0, seq=0))
        self.ingest(1200, hello())
        self.assertEqual(self.snapshot(1550)["motor"]["fault_mask"], 0)
        self.assertIsNone(self.snapshot(1600)["motor"]["fault_mask"])

    def test_fault_order_survives_same_epoch_recovery(self):
        for recovery in ("disconnect", "hello_timeout", "version_mismatch"):
            with self.subTest(recovery=recovery):
                self.rx = CanReceiver()
                self.ingest(0, hello(), fault(mask=4, seq=254))
                self.ingest(10, fault(mask=0, seq=255))
                if recovery == "disconnect":
                    self.rx.disconnect(20 * NS_PER_MS)
                    recovered_ms = 30
                elif recovery == "version_mismatch":
                    self.ingest(20, hello(version=(2, 0), capabilities=0x0F))
                    recovered_ms = 30
                else:
                    recovered_ms = 1500

                self.ingest(recovered_ms, hello(), fault(mask=4, seq=254))
                self.assertIsNone(self.snapshot(recovered_ms)["motor"]["fault_mask"])
                self.ingest(recovered_ms + 1, fault(mask=4, seq=255))
                self.assertIsNone(
                    self.snapshot(recovered_ms + 1)["motor"]["fault_mask"]
                )
                self.ingest(recovered_ms + 2, fault(mask=0, seq=255))
                self.assertEqual(
                    self.snapshot(recovered_ms + 2)["motor"]["fault_mask"], 0
                )
                self.ingest(recovered_ms + 3, fault(mask=4, seq=0))
                self.assertEqual(
                    self.snapshot(recovered_ms + 3)["motor"]["fault_mask"], 4
                )

                self.ingest(
                    recovered_ms + 4, hello(EPOCH + 1), fault(EPOCH + 1, seq=255)
                )
                self.assertEqual(
                    self.snapshot(recovered_ms + 4)["motor"]["fault_mask"], 0
                )

    def test_old_boot_timing_drops_unanchored_measurements(self):
        self.ingest(0, hello())
        self.ingest(10, hello(0x00020001))
        self.ingest(311, speed(7, rpm=-1), electric(7), timing(7, 130, boot=1))
        accepted = self.sample(320, 7, 140, boot=2)
        latest = self.rx.samples.latest
        self.assertEqual(latest.boot, 2)
        self.assertEqual(latest.evidence_ids, tuple(row.record_id for row in accepted))
        self.assertEqual(self.snapshot(320)["motor"]["sample"]["actual_rpm"], 59)

    def test_recovered_hello_requires_a_new_permit(self):
        operator = Frame(CanId.OP_HELLO, pack("<IBBBB", 1, 2, 1, 0, 0))
        permit = Frame(CanId.OP_PERMIT, pack("<IHBB", EPOCH, 1, 1, 0))
        new_permit = Frame(CanId.OP_PERMIT, pack("<IHBB", EPOCH, 2, 1, 0))
        for early, late in ((hello(), operator), (operator, hello())):
            with self.subTest(early=early.can_id):
                self.rx = CanReceiver()
                self.ingest(0, late)
                self.ingest(1400, early, permit)
                self.assertTrue(self.snapshot(1400)["operator"]["controls_ready"])
                self.assertIsNone(self.snapshot(1500)["operator"]["controls_ready"])
                self.ingest(1501, late, permit)
                self.assertIsNone(self.snapshot(1501)["operator"]["controls_ready"])
                self.ingest(1502, new_permit)
                self.assertTrue(self.snapshot(1502)["operator"]["controls_ready"])

    def test_reconnect_same_session_rejects_old_permit_and_accepts_wrap(self):
        operator = Frame(CanId.OP_HELLO, pack("<IBBBB", 1, 2, 1, 0, 0))
        last_permit = Frame(CanId.OP_PERMIT, pack("<IHBB", EPOCH, 65535, 1, 0))
        wrapped_permit = Frame(CanId.OP_PERMIT, pack("<IHBB", EPOCH, 0, 1, 0))
        self.ingest(0, hello(), operator, last_permit)
        self.assertTrue(self.snapshot(0)["operator"]["controls_ready"])
        self.rx.disconnect(10 * NS_PER_MS)
        self.ingest(20, hello(), operator, last_permit)
        self.assertIsNone(self.snapshot(20)["operator"]["controls_ready"])
        self.ingest(21, wrapped_permit)
        self.assertTrue(self.snapshot(21)["operator"]["controls_ready"])

    def test_new_epoch_can_restart_permit_sequence(self):
        epoch = EPOCH + 1
        for boot in (1, 2):
            with self.subTest(epoch=epoch, boot=boot):
                self.rx = CanReceiver()
                old_operator = Frame(CanId.OP_HELLO, pack("<IBBBB", 1, 2, 1, 0, 0))
                old_permit = Frame(CanId.OP_PERMIT, pack("<IHBB", EPOCH, 18, 1, 0))
                self.ingest(0, hello(), old_operator, old_permit)
                self.rx.disconnect(10 * NS_PER_MS)
                operator = Frame(CanId.OP_HELLO, pack("<IBBBB", boot, 2, 1, 0, 0))
                first_permit = Frame(CanId.OP_PERMIT, pack("<IHBB", epoch, 1, 1, 0))
                self.ingest(20, operator, hello(epoch), first_permit)
                self.assertTrue(self.snapshot(20)["operator"]["controls_ready"])

    def test_operator_restart_blocks_permits_until_new_motor_hello(self):
        operator = Frame(CanId.OP_HELLO, pack("<IBBBB", 1, 2, 1, 0, 0))
        restarted = Frame(CanId.OP_HELLO, pack("<IBBBB", 2, 2, 1, 0, 0))
        old_permit = Frame(CanId.OP_PERMIT, pack("<IHBB", EPOCH, 18, 1, 0))
        self.ingest(0, hello(), operator, old_permit)
        self.assertTrue(self.snapshot(0)["operator"]["controls_ready"])

        newer_epoch = EPOCH + 1
        new_permit = Frame(CanId.OP_PERMIT, pack("<IHBB", newer_epoch, 1, 1, 0))
        self.ingest(10, restarted, old_permit, new_permit)
        self.assertIsNone(self.snapshot(10)["operator"]["controls_ready"])
        self.ingest(11, hello(newer_epoch), old_permit)
        self.assertIsNone(self.snapshot(11)["operator"]["controls_ready"])

        self.ingest(12, new_permit, restarted)
        self.assertTrue(self.snapshot(12)["operator"]["controls_ready"])

    def test_invalid_last_part_does_not_close_join_window(self):
        self.ingest(0, hello())
        self.ingest(10, timing(8, 20), speed(8), electric(8, current=-32768))
        self.assertIsNone(self.snapshot(108)["motor"]["sample"]["seq"])
        self.ingest(109, electric(8))
        reading = self.snapshot(109)["motor"]["sample"]
        self.assertEqual((reading["seq"], reading["quality"]), (8, "valid"))

    def test_valid_part_recovers_before_sample_finalization(self):
        self.ingest(0, hello())
        rejected = self.ingest(10, timing(8, 10, boot=0), speed(8, pwm=1001))
        accepted = self.ingest(11, speed(8), electric(8), timing(8, 20))
        reading = self.snapshot(11)["motor"]["sample"]
        self.assertEqual((reading["seq"], reading["quality"]), (8, "valid"))
        self.assertTrue(reading["sample_complete"])
        self.assertTrue(all(row.error for row in rejected))
        self.assertEqual(
            self.rx.samples.latest.evidence_ids,
            tuple(row.record_id for row in accepted),
        )

    def test_timed_fixture_projects_decoded_measurements(self):
        for at_ms, frame in load_frames(FIXTURES / "can-rx-timed.jsonl"):
            self.ingest(at_ms, frame)
        snap = self.snapshot(600)
        self.assertEqual(snap["motor"]["mode"], "REAL")
        reading = snap["motor"]["sample"]
        self.assertEqual(
            (
                reading["actual_rpm"],
                reading["pwm_permille"],
                reading["current_ma"],
                reading["quality"],
            ),
            (59, 450, 250, "valid"),
        )

    def test_late_sample_cannot_replace_newer_complete_sample(self):
        self.ingest(0, hello(), timing(1, 50))
        self.sample(50, 2, 100)
        self.ingest(55, speed(1), electric(1))
        self.assertEqual(self.snapshot(100)["motor"]["sample"]["seq"], 2)


class TcpCanTest(unittest.IsolatedAsyncioTestCase):
    async def test_feeder_failure_reaches_server(self):
        stop = asyncio.Event()
        failing_feed = AsyncMock(side_effect=RuntimeError("예제 입력 실패"))
        with self.assertRaisesRegex(RuntimeError, "예제 입력 실패"):
            await asyncio.wait_for(wait_for_stop(stop, failing_feed), 1)

    async def test_fixture_to_tcp_and_expiry(self):
        mock = TcpMock(can_input=True)
        entries = load_frames(FIXTURES / "can-rx-timed.jsonl")
        async with await asyncio.start_server(
            mock.handle_client, "127.0.0.1", 0
        ) as server:
            host, port = server.sockets[0].getsockname()
            reader, writer = await asyncio.open_connection(host, port)
            try:
                hello_msg = await self.read(reader)
                self.assertEqual(
                    (hello_msg["type"], hello_msg["source"]), ("hello", "mock")
                )
                initial = (await self.read(reader))["data"]
                self.assertEqual(initial["motor"]["state"], "UNKNOWN")
                self.assertIsNone(initial["motor"]["sample"]["actual_rpm"])

                await feed_frames(entries, mock.can_receiver)
                current = await self.wait_motor(
                    reader, lambda motor: motor["sample"]["sample_complete"]
                )
                self.assertEqual(
                    (current["mode"], current["state"]), ("REAL", "RUNNING")
                )
                self.assertEqual(current["sample"]["actual_rpm"], 59)
                self.assertEqual(current["sample"]["bus_mv"], 6000)

                expired = await self.wait_motor(
                    reader, lambda motor: motor["state"] == "UNKNOWN"
                )
                self.assertEqual(expired["fault_mask"], 0)
                self.assertIsNone(expired["sample"]["bus_mv"])
            finally:
                writer.close()
                await writer.wait_closed()
                mock.close()

    async def wait_motor(self, reader, predicate):
        async def search():
            while True:
                motor = (await self.read(reader))["data"]["motor"]
                if predicate(motor):
                    return motor

        return await asyncio.wait_for(search(), 2)

    @staticmethod
    async def read(reader):
        return json.loads(await asyncio.wait_for(reader.readline(), 2))


if __name__ == "__main__":
    unittest.main()
