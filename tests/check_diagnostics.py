"""모의 CAN·UART 입력의 판정과 원본 기록 재현을 검증한다."""

import json
import os
import tempfile
import unittest
from binascii import crc_hqx
from pathlib import Path
from stat import S_ISDIR
from struct import pack
from unittest.mock import patch
from uuid import UUID

from backend.can_rx.codec import CanId, State
from backend.diagnostics.engine import DiagnosticEngine
from backend.diagnostics.inputs import Input
from backend.diagnostics.journal import Journal, evaluate, load_manifest, verify_results
from backend.diagnostics.verdict import Config

SESSION = str(UUID(int=1))
EPOCH = 0x10001
MS = 1_000_000
FIXTURE = Path(__file__).with_name("fixtures") / "diagnostic-no-edge.jsonl"


def can(at, kind, data):
    return dict(
        source="can",
        rx_mono_ns=str(at * MS),
        can_id=int(kind),
        dlc=8,
        hex=data.hex(" "),
    )


def status(at, state, *, epoch=EPOCH, mask=0):
    return [
        can(at, CanId.MOTOR_HELLO, pack("<IBBBB", epoch, 2, 1, 0x1F, 0)),
        can(at, CanId.MOTOR_STATE, pack("<IBBBB", epoch, state, 2, 0x04, 0)),
        can(at, CanId.MOTOR_FAULT, pack("<IHBB", epoch, mask, int(bool(mask)), 2)),
    ]


def uart(at, tx, kind, phase, *, run=1, scenario=2, value=0, flags=1):
    body = pack(
        "<BBHHBBIIH", 1, kind, tx, run, scenario, phase, at * 1000, value, flags
    )
    raw = b"\xa5\x5a" + body + pack("<H", crc_hqx(body, 0xFFFF))
    return dict(source="fpga", rx_mono_ns=str(at * MS), hex=raw.hex(" "))


def service(at, event="clock"):
    return dict(source="service", rx_mono_ns=str(at * MS), event=event)


def scenario_rows(
    scenario=2, *, latency=200_000, fault=4, recovery="held", advance_epoch=True
):
    end = 3000 if scenario == 1 else 2000
    rows = status(0, State.TEST_ARMED)
    rows += [uart(0, 0, 1, 0, run=0, scenario=0), uart(0, 1, 2, 1, scenario=scenario)]
    for at in range(200, 1001, 200):
        rows += status(at, State.TEST_ACTIVE)
    rows += [uart(1000, 2, 3, 2, scenario=scenario)]
    fault_epoch = EPOCH + 1 if fault and advance_epoch else EPOCH
    for at in range(1200, end + 1, 200):
        if latency is not None and at == 1200:
            rows += [uart(at, 3, 4, 2, scenario=scenario, value=latency, flags=7)]
        rows += status(
            at,
            State.FAULT if fault else State.TEST_ACTIVE,
            epoch=fault_epoch,
            mask=fault,
        )
    next_seq = 3 if latency is None else 4
    rows += [
        uart(
            end,
            next_seq,
            5,
            3,
            scenario=scenario,
            flags=5 if latency is not None else 1,
        )
    ]
    if scenario == 3 and recovery != "missing":
        rows += status(
            end + 200,
            State.FAULT if recovery == "held" else State.STOPPED,
            epoch=fault_epoch if recovery == "held" else fault_epoch + 1,
            mask=fault if recovery == "held" else 0,
        )
    rows += [service(end + 500)]
    return rows


def run_engine(rows, config=None):
    engine = DiagnosticEngine(SESSION, config or Config(gpio_verified=True))
    results = []
    for index, row in enumerate(rows, 1):
        _, completed = engine.ingest(Input.from_row(row, SESSION, index))
        results.extend(completed)
    results.extend(engine.finish())
    return results


class VerdictTest(unittest.TestCase):
    def test_split_baseline_retains_every_source_record_id(self):
        rows = scenario_rows()
        baseline = rows[4]
        raw = bytes.fromhex(baseline["hex"])
        rows[4:5] = [
            {**baseline, "hex": fragment.hex()}
            for fragment in (raw[:1], raw[1:10], raw[10:])
        ]
        (result,) = run_engine(rows)
        self.assertEqual(result["result"], "PASS")
        self.assertEqual(
            result["evidence_ids"][:4], [f"{SESSION}:{i}" for i in (4, 5, 6, 7)]
        )

    def test_gpio_trial_requires_ready_at_baseline_start(self):
        rows = scenario_rows()
        baseline_index = 4
        rows[baseline_index] = uart(0, 1, 2, 1, flags=0)
        (unready,) = run_engine(rows)
        self.assertEqual(
            (unready["result"], unready["reason"]),
            ("INCONCLUSIVE", "FPGA_READY_NOT_OBSERVED"),
        )
        self.assertIn(f"{SESSION}:{baseline_index + 1}", unready["evidence_ids"])

        (functional,) = run_engine(
            rows, Config(gpio_verified=True, functional_only=True)
        )
        self.assertEqual(functional["reason"], "TIMING_UNAVAILABLE")

        rows[baseline_index] = uart(0, 1, 2, 1, flags=1)
        diag_index = next(
            index
            for index, row in enumerate(rows)
            if row["source"] == "fpga" and row["rx_mono_ns"] == str(1200 * MS)
        )
        rows[diag_index] = uart(1200, 3, 4, 2, value=200_000, flags=6)
        rows[-2] = uart(2000, 4, 5, 3, flags=6)
        (ready_fell_after_fault,) = run_engine(rows)
        self.assertEqual(ready_fell_after_fault["result"], "PASS")

    def test_fault_must_be_confirmed_in_new_epoch_after_inject(self):
        for scenario, reason in (
            (2, "FAULT_EPOCH_UNCONFIRMED"),
            (3, "GAP_INITIAL_FAULT_UNOBSERVED"),
        ):
            with self.subTest(scenario=scenario):
                (result,) = run_engine(scenario_rows(scenario, advance_epoch=False))
                self.assertEqual(
                    (result["result"], result["reason"]), ("INCONCLUSIVE", reason)
                )

    def test_partial_uart_frame_at_deadline_prevents_pass(self):
        rows = scenario_rows()
        frame = uart(2200, 5, 1, 3)
        raw = bytes.fromhex(frame["hex"])
        first = {**frame, "hex": raw[:10].hex()}
        second = {**frame, "rx_mono_ns": str(2300 * MS), "hex": raw[10:].hex()}

        (partial,) = run_engine([*rows[:-1], first, rows[-1]])
        self.assertEqual(
            (partial["result"], partial["reason"]), ("INCONCLUSIVE", "UART_FRAMING")
        )
        self.assertIn(f"{SESSION}:{len(rows)}", partial["evidence_ids"])

        (completed,) = run_engine([*rows[:-1], first, second, rows[-1]])
        self.assertEqual(completed["result"], "PASS")

    def test_three_scenarios_and_valid_failures(self):
        for data, expected in (
            (scenario_rows(1, latency=None, fault=0), ("PASS", "NORMAL_CONFIRMED")),
            (scenario_rows(), ("PASS", "EXPECTED_FAULT_CONFIRMED")),
            (scenario_rows(3), ("PASS", "EXPECTED_FAULT_CONFIRMED")),
            (scenario_rows(latency=None, fault=0), ("FAIL", "NO_DIAG_RESPONSE")),
            (scenario_rows(latency=300_001), ("FAIL", "GPIO_LATENCY_EXCEEDED")),
            (scenario_rows(latency=300_000), ("PASS", "EXPECTED_FAULT_CONFIRMED")),
            (scenario_rows(fault=8), ("FAIL", "UNEXPECTED_FAULT")),
            (scenario_rows(fault=0), ("FAIL", "EXPECTED_FAULT_MISSING")),
            (scenario_rows(3, recovery="cleared"), ("FAIL", "FAULT_CLEARED_AFTER_GAP")),
            (
                scenario_rows(3, recovery="missing"),
                ("INCONCLUSIVE", "GAP_RECOVERY_EVIDENCE_MISSING"),
            ),
        ):
            with self.subTest(expected=expected):
                (result,) = run_engine(data)
                self.assertEqual((result["result"], result["reason"]), expected)

    def test_incomplete_evidence_never_passes(self):
        rows = scenario_rows()
        corrupted = rows.copy()
        index = next(
            i
            for i, row in enumerate(rows)
            if row["source"] == "fpga" and row["rx_mono_ns"] == str(1200 * MS)
        )
        corrupted[index] = {**rows[index], "hex": rows[index]["hex"][:-2] + "00"}
        for data, config, reason in (
            (rows, Config(), "TIMING_UNAVAILABLE"),
            (rows[4:], None, "MID_RUN_ATTACH"),
            (rows[:-1], None, "RECORD_ENDED_EARLY"),
            (corrupted, None, "UART_CRC"),
            (rows[:6] + rows[15:], None, "CAN_STALE"),
        ):
            with self.subTest(reason=reason):
                (result,) = run_engine(data, config)
                self.assertEqual(
                    (result["result"], result["reason"]), ("INCONCLUSIVE", reason)
                )

    def test_mid_run_completed_status_reports_inconclusive_once(self):
        (result,) = run_engine([uart(0, 1, 1, 3), uart(100, 2, 1, 3)])
        self.assertEqual(
            (result["result"], result["reason"]), ("INCONCLUSIVE", "MID_RUN_ATTACH")
        )
        self.assertEqual(result["evidence_ids"], [f"{SESSION}:1"])

    def test_pending_window_and_no_reopen_on_status(self):
        engine = DiagnosticEngine(SESSION, Config(gpio_verified=True))
        rows = scenario_rows()
        for i, row in enumerate(rows[:-1], 1):
            _, completed = engine.ingest(Input.from_row(row, SESSION, i))
            self.assertFalse(completed)
        self.assertEqual(engine.snapshot()["reason"], "RESULT_PENDING")
        _, completed = engine.ingest(Input.from_row(rows[-1], SESSION, len(rows)))
        self.assertEqual(len(completed), 1)
        _, completed = engine.ingest(
            Input.from_row(uart(2600, 5, 1, 3), SESSION, len(rows) + 1)
        )
        self.assertFalse(completed)
        self.assertFalse(engine.finish())

    def test_explicit_abort_and_reset(self):
        prefix = scenario_rows()[:5]
        for event in ("stop", "reset", None):
            with self.subTest(event=event):
                rows = prefix.copy()
                if event:
                    rows.append(service(5, event))
                rows += [uart(10, 2, 6, 3), service(510)]
                (result,) = run_engine(rows)
                self.assertEqual(
                    result["result"], "ABORTED" if event else "INCONCLUSIVE"
                )

    def test_recording_failure_never_passes(self):
        (result,) = run_engine([service(0, "recording_failed")] + scenario_rows())
        self.assertEqual(
            (result["result"], result["reason"]), ("INCONCLUSIVE", "RECORDING_FAILED")
        )

    def test_gap_requires_fresh_post_end_pair(self):
        rows = scenario_rows(3, recovery="missing")[:-1]
        next_state = status(2010, State.FAULT, epoch=EPOCH + 1, mask=4)[1]
        next_fault = status(2400, State.FAULT, epoch=EPOCH + 1, mask=4)[2]
        (result,) = run_engine(rows + [next_state, next_fault, service(2500)])
        self.assertEqual(
            (result["result"], result["reason"]),
            ("INCONCLUSIVE", "GAP_RECOVERY_EVIDENCE_MISSING"),
        )

    def test_gap_cannot_confirm_fault_from_a_new_epoch_after_end(self):
        rows = scenario_rows(3, recovery="missing")[:-1]
        new_fault = status(2210, State.FAULT, epoch=EPOCH + 2, mask=4)
        (result,) = run_engine([*rows, *new_fault, service(2500)])

        self.assertEqual(
            (result["result"], result["reason"]),
            ("INCONCLUSIVE", "GAP_RECOVERY_EPOCH_CHANGED"),
        )

    def test_gap_orders_equal_timestamp_recovery_frames_by_ingest_sequence(self):
        *before_end, end, clock = scenario_rows(3, recovery="missing")
        recovery = status(2000, State.FAULT, epoch=EPOCH + 1, mask=4)[1:]
        (after,) = run_engine([*before_end, end, *recovery, clock])
        (before,) = run_engine([*before_end, *recovery, end, clock])

        self.assertEqual(after["result"], "PASS")
        self.assertEqual(
            (before["result"], before["reason"]),
            ("INCONCLUSIVE", "GAP_RECOVERY_EVIDENCE_MISSING"),
        )

    def test_gap_clear_remains_failure_after_fault_relatches(self):
        rows = scenario_rows(3, recovery="missing")[:-1]
        clear = status(2110, State.STOPPED, epoch=EPOCH + 2)
        relatch = status(2210, State.FAULT, epoch=EPOCH + 3, mask=4)
        (result,) = run_engine([*rows, *clear, *relatch, service(2500)])
        self.assertEqual(
            (result["result"], result["reason"]), ("FAIL", "FAULT_CLEARED_AFTER_GAP")
        )
        self.assertIn(f"{SESSION}:{len(rows) + 2}", result["evidence_ids"])
        self.assertIn(f"{SESSION}:{len(rows) + 3}", result["evidence_ids"])

    def test_operator_stop_after_end_does_not_reclassify_normal_run(self):
        rows = scenario_rows(1, latency=None, fault=0)
        (result,) = run_engine(rows[:-1] + [service(3100, "stop"), rows[-1]])
        self.assertEqual(
            (result["result"], result["reason"]), ("PASS", "NORMAL_CONFIRMED")
        )

    def test_control_after_end_cannot_borrow_later_fault_evidence(self):
        for event in ("stop", "reset"):
            with self.subTest(event=event):
                rows = scenario_rows(fault=0)
                late = status(2100, State.FAULT, epoch=EPOCH + 1, mask=4)
                control = service(2100, event)

                (unconfirmed,) = run_engine([*rows[:-1], control, *late, rows[-1]])
                self.assertEqual(
                    (unconfirmed["result"], unconfirmed["reason"]),
                    ("INCONCLUSIVE", "CONTROL_CHANGED_BEFORE_FAULT_CONFIRMATION"),
                )
                self.assertIn(f"{SESSION}:{len(rows)}", unconfirmed["evidence_ids"])
                self.assertNotIn(
                    f"{SESSION}:{len(rows) + 2}", unconfirmed["evidence_ids"]
                )

                (confirmed,) = run_engine([*rows[:-1], *late, control, rows[-1]])
                self.assertEqual(confirmed["result"], "PASS")

    def test_can_estop_and_mode_change_cut_off_post_end_evidence(self):
        rows = scenario_rows(fault=0)
        late = status(2100, State.FAULT, epoch=EPOCH + 1, mask=4)
        for mode, flags in ((2, 0x84), (1, 0x04)):
            with self.subTest(mode=mode, flags=flags):
                before = can(
                    2100,
                    CanId.MOTOR_STATE,
                    pack("<IBBBB", EPOCH, State.STOPPED, mode, flags, 0),
                )
                after = can(
                    2100,
                    CanId.MOTOR_STATE,
                    pack("<IBBBB", EPOCH + 1, State.STOPPED, mode, flags, 0),
                )
                (unconfirmed,) = run_engine([*rows[:-1], before, *late, rows[-1]])
                self.assertEqual(
                    (unconfirmed["result"], unconfirmed["reason"]),
                    ("INCONCLUSIVE", "CONTROL_CHANGED_BEFORE_FAULT_CONFIRMATION"),
                )
                self.assertIn(f"{SESSION}:{len(rows)}", unconfirmed["evidence_ids"])
                (confirmed,) = run_engine([*rows[:-1], *late, after, rows[-1]])
                self.assertEqual(confirmed["result"], "PASS")


class JournalTest(unittest.TestCase):
    def test_failed_directory_sync_leaves_no_replayable_manifest(self):
        fsync = os.fsync

        def fail_directory(fd):
            if S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("디렉터리 동기화 실패")
            return fsync(fd)

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "original"
            with (
                self.assertRaisesRegex(OSError, "디렉터리 동기화 실패"),
                patch(
                    "backend.diagnostics.journal.os.fsync", side_effect=fail_directory
                ),
                Journal(target, Config()),
            ):
                pass
            with self.assertRaises(FileNotFoundError):
                load_manifest(target)
            self.assertTrue((target / "inputs.jsonl").is_file())

    def test_replay_requires_original_config_and_matching_results(self):
        with tempfile.TemporaryDirectory() as directory:
            original = Path(directory) / "original"
            config = Config(gpio_verified=True)
            with (
                FIXTURE.open(encoding="utf-8") as source,
                Journal(original, config) as journal,
            ):
                expected = evaluate(source, journal, config)
            verify_results(original, expected)
            with self.assertRaisesRegex(
                ValueError, "판정이 원본 결과와 일치하지 않는다"
            ):
                verify_results(original, [{**expected[0], "result": "FAIL"}])

            path = original / "manifest.json"
            manifest = json.loads(path.read_text(encoding="utf-8"))
            del manifest["config"]["gpio_limit_us"]
            path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "시험 설정이 불완전하다"):
                load_manifest(original)

    def test_same_record_replays_same_result_and_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            original = base / "original"
            replayed = base / "replayed"
            config = Config(gpio_verified=True)
            with (
                FIXTURE.open(encoding="utf-8") as source,
                Journal(original, config) as journal,
            ):
                expected = evaluate(source, journal, config)
            manifest, replay_config = load_manifest(original)
            with (
                (original / "inputs.jsonl").open(encoding="utf-8") as source,
                Journal(replayed, replay_config, original=manifest) as journal,
            ):
                actual = evaluate(
                    source, journal, replay_config, pace=lambda now: now + 500
                )
            self.assertEqual(actual, expected)
            self.assertEqual(actual[0]["result"], "PASS")
            record_rows = [
                json.loads(line)
                for line in (original / "inputs.jsonl").read_text().splitlines()
            ]
            self.assertTrue(
                set(actual[0]["evidence_ids"])
                <= {row["record_id"] for row in record_rows}
            )
            replay_manifest, _ = load_manifest(replayed)
            self.assertEqual(replay_manifest["replay_of"], manifest["backend_session"])
            self.assertNotEqual(
                replay_manifest["backend_session"], manifest["backend_session"]
            )

            with self.assertRaises(FileExistsError), Journal(original, config):
                pass
            with (original / "inputs.jsonl").open("a", encoding="utf-8") as file:
                number = len(record_rows) + 1
                row = Input.from_row(
                    service(2600), manifest["evidence_session"], number
                ).as_row()
                file.write(json.dumps({**row, "rx_utc": None}) + "\n")
            with self.assertRaisesRegex(ValueError, "해시 불일치"):
                with (
                    (original / "inputs.jsonl").open(encoding="utf-8") as source,
                    Journal(
                        base / "invalid", replay_config, original=manifest
                    ) as journal,
                ):
                    evaluate(
                        source,
                        journal,
                        replay_config,
                        pace=lambda _: self.fail("손상된 원본을 재생함"),
                    )
            self.assertFalse((base / "invalid" / "manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
