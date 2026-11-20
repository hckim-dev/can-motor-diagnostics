"""측정 기록과 운전 회차의 경계 및 저장 실패를 검증한다."""

import csv
import errno
import json
import os
import tempfile
import unittest
from binascii import crc_hqx
from pathlib import Path
from struct import pack
from unittest.mock import patch

from backend.can_rx.codec import CanId, Mode, State
from backend.diagnostics.inputs import Input
from backend.diagnostics.journal import Journal, evaluate, load_manifest, record_input
from backend.diagnostics.verdict import Config

EPOCH = 0x10001
MS = 1_000_000
FIXTURE = Path(__file__).with_name("fixtures") / "diagnostic-no-edge.jsonl"


def can(at, kind, data):
    return {
        "source": "can",
        "rx_mono_ns": str(at * MS),
        "can_id": int(kind),
        "dlc": 8,
        "hex": data.hex(),
    }


def hello(at, epoch=EPOCH):
    return can(at, CanId.MOTOR_HELLO, pack("<IBBBB", epoch, 2, 1, 0x1F, 0))


def state(at, motor_state, epoch=EPOCH, mode=Mode.REAL):
    return can(at, CanId.MOTOR_STATE, pack("<IBBBB", epoch, motor_state, mode, 4, 0))


def service(at, event):
    return {"source": "service", "rx_mono_ns": str(at * MS), "event": event}


def timing(at, seq, boot=1):
    return can(at, CanId.SAMPLE_TIME, pack("<HIH", seq, at, boot))


def speed(at, seq, pwm=450):
    return can(at, CanId.SPEED, pack("<HhHH", seq, 59, 60, pwm))


def electric(at, seq, motor_state=State.RUNNING, mode=Mode.REAL, age=2):
    flags = motor_state << 4 | mode << 2 | 3
    return can(at, CanId.ELECTRIC, pack("<HhHBB", seq, 250, 6000, flags, age))


def sample(at, seq, *, boot=1, motor_state=State.RUNNING, mode=Mode.REAL):
    return [
        timing(at, seq, boot),
        electric(at, seq, motor_state, mode),
        speed(at, seq),
    ]


def json_rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def sample_rows(directory):
    with (directory / "samples.csv").open(encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file))


class MeasurementRecordingTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.output = self.base / "recorded"

    def record(self, rows):
        source = self.base / "source.jsonl"
        source.write_text("\n".join(map(json.dumps, rows)) + "\n", encoding="utf-8")
        config = Config()
        with (
            source.open(encoding="utf-8") as file,
            Journal(self.output, config) as journal,
        ):
            evaluate(file, journal, config)
        return (
            sample_rows(self.output),
            json_rows(self.output / "operations.jsonl"),
            json_rows(self.output / "raw_can.jsonl"),
        )

    def test_operations_use_observed_state_and_split_after_loss(self):
        rows = [hello(0), state(1, State.STOPPED)]
        rows += sample(2, 1, motor_state=State.STOPPED)
        rows += [hello(20, EPOCH + 1), state(21, State.RUNNING, EPOCH + 1)]
        rows += sample(30, 2)
        rows += [service(35, "stop"), service(36, "can_disconnect")]
        rows += [hello(40, EPOCH + 1), state(41, State.RUNNING, EPOCH + 1)]
        rows += [state(300, State.RUNNING, EPOCH + 1)]
        rows += sample(340, 3)
        rows += [hello(345, EPOCH + 2), state(346, State.STOPPED, EPOCH + 2)]
        rows += sample(350, 4, motor_state=State.STOPPED)
        samples, operations, raw = self.record(rows)

        self.assertEqual(
            [
                (op["start_observed"], op["end_observed"], op["end_reason"])
                for op in operations
            ],
            [(True, False, "CAN_DISCONNECT"), (False, True, "STOPPED")],
        )
        first, second = operations
        self.assertEqual(first["previous_stop_record_id"], raw[1]["record_id"])
        self.assertEqual(first["end_rx_mono_ns"], str(36 * MS))
        self.assertNotEqual(first["operation_id"], second["operation_id"])
        self.assertEqual(
            [row["operation_id"] for row in samples],
            ["", first["operation_id"], second["operation_id"], ""],
        )
        self.assertNotEqual(samples[1]["acquisition_id"], samples[2]["acquisition_id"])

    def test_timeout_boot_change_fault_and_end_keep_observation_flags(self):
        new_epoch = 0x20001
        rows = [
            hello(0),
            state(0, State.RUNNING),
            state(300, State.RUNNING),
            hello(310, new_epoch),
            state(311, State.RUNNING, new_epoch),
            hello(320, new_epoch + 1),
            can(321, CanId.MOTOR_FAULT, pack("<IHBB", new_epoch + 1, 4, 1, Mode.REAL)),
            state(322, State.FAULT, new_epoch + 1),
            hello(330, new_epoch + 2),
            state(331, State.STOPPED, new_epoch + 2),
            hello(340, new_epoch + 3),
            state(341, State.RUNNING, new_epoch + 3),
        ]
        _, operations, _ = self.record(rows)
        self.assertEqual(
            [(op["end_reason"], op["end_observed"]) for op in operations],
            [
                ("STATE_TIMEOUT", False),
                ("MOTOR_BOOT_CHANGED", False),
                ("FAULT", True),
                ("RECORD_ENDED", False),
            ],
        )
        self.assertIsNone(operations[0]["end_record_id"])
        self.assertIsNone(operations[0]["end_rx_utc"])
        self.assertEqual(operations[0]["end_rx_mono_ns"], str(300 * MS))
        self.assertFalse(operations[1]["start_observed"])
        self.assertTrue(operations[3]["start_observed"])

    def test_new_boot_evidence_ends_operation_before_hello(self):
        rows = [
            hello(0),
            state(1, State.STOPPED),
            state(2, State.RUNNING),
            timing(30, 1, boot=2),
            service(40, "clock"),
        ]
        _, operations, raw = self.record(rows)

        (operation,) = operations
        self.assertEqual(operation["end_reason"], "MOTOR_BOOT_CHANGED")
        self.assertFalse(operation["end_observed"])
        self.assertEqual(operation["end_rx_mono_ns"], str(30 * MS))
        self.assertEqual(operation["end_record_id"], raw[-1]["record_id"])

    def test_raw_can_boot_uses_the_frame_instead_of_current_receiver_state(self):
        rows = [
            hello(0, 0x20001),
            state(1, State.RUNNING, 0x20001),
            speed(2, 1),
            timing(3, 1, boot=1),
            timing(4, 2, boot=3),
        ]
        _, _, raw = self.record(rows)

        self.assertEqual([row["motor_boot"] for row in raw], [2, 2, None, 1, 3])

    def test_recording_gap_splits_operation_and_pending_samples(self):
        rows = [
            hello(0),
            state(1, State.STOPPED),
            state(2, State.RUNNING),
            timing(10, 1),
            electric(11, 1),
            service(12, "recording_failed"),
            speed(13, 1),
            *sample(14, 2),
            state(20, State.RUNNING),
            *sample(21, 3),
            state(22, State.STOPPED),
        ]
        samples, operations, _ = self.record(rows)

        self.assertEqual(
            [
                (op["start_observed"], op["end_observed"], op["end_reason"])
                for op in operations
            ],
            [(True, False, "RECORDING_FAILED"), (False, True, "STOPPED")],
        )
        self.assertEqual(
            [row["sample_complete"] for row in samples], ["False", "True", "True"]
        )
        self.assertEqual(
            [row["operation_id"] for row in samples],
            [operations[0]["operation_id"], "", operations[1]["operation_id"]],
        )
        self.assertNotEqual(samples[0]["acquisition_id"], samples[1]["acquisition_id"])

    def test_partial_out_of_order_stale_and_cross_boundary_samples(self):
        rows = [
            hello(0),
            state(0, State.RUNNING),
            timing(10, 65535),
            electric(11, 65535, age=20),
            *sample(30, 0),
            speed(35, 65535),
            timing(40, 1),
            speed(41, 1, pwm=1001),
            timing(42, 2),
            electric(43, 2),
            state(44, State.STOPPED, mode=Mode.TEST),
            speed(45, 2),
            timing(46, 3),
            electric(47, 3),
            service(48, "can_disconnect"),
            speed(49, 4),
        ]
        samples, operations, raw = self.record(rows)
        self.assertEqual(
            [int(row["sample_seq64"]) for row in samples],
            [65536, 65535, 65538, 65537, 65539],
        )
        by_seq = {int(row["seq"]): row for row in samples}
        self.assertEqual(by_seq[65535]["bus_mv"], "")
        self.assertEqual(by_seq[65535]["bus_mv_validity"], "stale")
        self.assertEqual(by_seq[65535]["actual_rpm"], "59")
        self.assertEqual(by_seq[1]["quality"], "invalid")
        self.assertEqual(by_seq[1]["mode"], "UNKNOWN")
        self.assertEqual(by_seq[1]["actual_rpm_validity"], "invalid")
        self.assertEqual(by_seq[2]["operation_id"], operations[0]["operation_id"])
        self.assertEqual(by_seq[3]["operation_id"], "")
        self.assertEqual(by_seq[3]["sample_complete"], "False")
        self.assertEqual(operations[0]["end_reason"], "MODE_CHANGED")
        self.assertEqual(
            set(by_seq[1]["evidence_ids"].split(";")),
            {raw[8]["record_id"], raw[9]["record_id"]},
        )

        # 기존 v1 기록에도 입력·판정만 있으면 측정 로그를 재구성할 수 있다.
        (self.output / "samples.csv").unlink()
        (self.output / "operations.jsonl").unlink()
        manifest, config = load_manifest(self.output)
        replayed = self.base / "replayed"
        with (
            (self.output / "inputs.jsonl").open(encoding="utf-8") as source,
            Journal(replayed, config, original=manifest) as journal,
        ):
            evaluate(source, journal, config)
        self.assertEqual(
            sample_rows(replayed), [dict(row, source="replay") for row in samples]
        )
        self.assertEqual(json_rows(replayed / "operations.jsonl"), operations)

    def test_test_run_needs_accepted_baseline_and_matching_frame_context(self):
        fixture = json_rows(FIXTURE)
        rows = fixture[:4]
        rows += [timing(0, 1)]
        rows += [fixture[4]]
        rows += [electric(1, 1, State.TEST_ARMED, Mode.TEST), speed(1, 1)]
        rows += sample(2, 2, motor_state=State.TEST_ARMED, mode=Mode.TEST)
        rows += sample(3, 3, motor_state=State.STOPPED, mode=Mode.TEST)
        samples, operations, _ = self.record(rows)
        self.assertEqual(operations, [])
        self.assertEqual(samples[0]["run_id"], "")
        events = json_rows(self.output / "events.jsonl")
        self.assertEqual(samples[1]["run_id"], events[0]["context"]["run_id"])
        self.assertEqual(samples[2]["run_id"], "")
        self.assertTrue(all(not row["operation_id"] for row in samples))

    def test_test_sample_without_idle_baseline_has_no_run_id(self):
        fixture = json_rows(FIXTURE)
        rows = [*fixture[:3], fixture[4]]
        rows += sample(1, 1, motor_state=State.TEST_ARMED, mode=Mode.TEST)
        samples, operations, _ = self.record(rows)

        self.assertEqual(samples[0]["run_id"], "")
        self.assertEqual(operations, [])

    def test_test_sample_after_uart_gap_at_baseline_has_no_run_id(self):
        fixture = json_rows(FIXTURE)
        baseline = bytearray.fromhex(fixture[4]["hex"])
        baseline[4:6] = pack("<H", 2)
        baseline[-2:] = pack("<H", crc_hqx(baseline[2:-2], 0xFFFF))
        rows = [*fixture[:4], dict(fixture[4], hex=baseline.hex())]
        rows += sample(1, 1, motor_state=State.TEST_ARMED, mode=Mode.TEST)
        samples, _, _ = self.record(rows)

        self.assertEqual(samples[0]["run_id"], "")
        (event,) = json_rows(self.output / "events.jsonl")
        self.assertEqual(event["context"]["reason"], "UART_TX_GAP")

    def test_eof_keeps_anchored_partials_and_omits_unanchored_input(self):
        samples, operations, _ = self.record(
            [
                hello(0),
                state(0, State.RUNNING),
                timing(1, 1),
                electric(2, 1),
                speed(3, 2),
            ]
        )
        self.assertEqual([row["seq"] for row in samples], ["1"])
        self.assertEqual(samples[0]["sample_complete"], "False")
        self.assertEqual(samples[0]["operation_id"], operations[0]["operation_id"])

    def test_measurement_failures_leave_no_manifest(self):
        for filename in ("samples.csv", "operations.jsonl"):
            with self.subTest(filename=filename):
                target = self.base / filename
                config = Config()
                fsync = os.fsync
                with (
                    self.assertRaises(OSError) as raised,
                    Journal(target, config) as journal,
                ):
                    fd = journal.files[filename].fileno()

                    def fail_selected(file_descriptor):
                        if file_descriptor == fd:
                            raise OSError(errno.ENOSPC, "기록 공간 부족")
                        return fsync(file_descriptor)

                    with patch(
                        "backend.diagnostics.journal.os.fsync",
                        side_effect=fail_selected,
                    ):
                        journal.result({"result": "PASS"})
                self.assertEqual(raised.exception.errno, errno.ENOSPC)
                self.assertFalse((target / "manifest.json").exists())
                self.assertEqual((target / "events.jsonl").stat().st_size, 0)

    def test_flush_publishes_buffered_measurements_before_record_closes(self):
        config = Config()
        rows = [
            hello(0),
            state(0, State.RUNNING),
            *sample(10, 1),
            service(1000, "clock"),
        ]
        with Journal(self.output, config) as journal:
            engine = journal.new_engine(config)
            for index, row in enumerate(rows, 1):
                record = Input.from_row(row, journal.evidence_session, index)
                record_input(journal, engine, record)
            self.assertEqual(sample_rows(self.output)[0]["actual_rpm"], "59")
            self.assertFalse((self.output / "manifest.json").exists())
            journal.finish(engine)

    def test_sample_write_failure_preserves_raw_input_without_completion(self):
        config = Config()
        rows = [hello(0), state(0, State.RUNNING), *sample(10, 1)]
        with self.assertRaises(OSError), Journal(self.output, config) as journal:
            engine = journal.new_engine(config)
            failure = OSError(errno.ENOSPC, "기록 공간 부족")
            with patch.object(
                journal.measurements.writer, "writerow", side_effect=failure
            ):
                for index, row in enumerate(rows, 1):
                    record = Input.from_row(row, journal.evidence_session, index)
                    record_input(journal, engine, record)

        self.assertFalse((self.output / "manifest.json").exists())
        self.assertEqual(len(json_rows(self.output / "raw_can.jsonl")), len(rows))

    def test_unfinished_capture_does_not_publish_manifest(self):
        config = Config()
        rows = [hello(0), state(0, State.RUNNING), *sample(10, 1)]
        with (
            self.assertRaisesRegex(ValueError, "기록 종료 처리가 완료되지 않았습니다"),
            Journal(self.output, config) as journal,
        ):
            engine = journal.new_engine(config)
            for index, row in enumerate(rows, 1):
                record = Input.from_row(row, journal.evidence_session, index)
                record_input(journal, engine, record)

        self.assertFalse((self.output / "manifest.json").exists())
        self.assertTrue(sample_rows(self.output)[0]["operation_id"])
