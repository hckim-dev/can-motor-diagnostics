"""공통 UART 예제와 손상·분할 입력으로 수신 상태를 검증한다."""

import unittest
from binascii import crc_hqx
from pathlib import Path
from struct import pack

from backend.fpga_uart.codec import (
    EventType,
    FrameError,
    InvalidFrame,
    Phase,
    crc16,
    decode_frame,
)
from backend.fpga_uart.session import Issue, RunStatus, UartReceiver
from backend.fpga_uart.stream import FrameStream

GOLDEN = bytes.fromhex(
    Path(__file__)
    .with_name("fixtures")
    .joinpath("fpga-uart-1.0.hex")
    .read_text(encoding="ascii")
)


def packet(kind, tx, tick, *, run=1, scenario=2, phase=1, value=0, flags=1, version=1):
    body = pack(
        "<BBHHBBIIH", version, kind, tx, run, scenario, phase, tick, value, flags
    )
    return b"\xa5\x5a" + body + pack("<H", crc_hqx(body, 0xFFFF))


class FrameTest(unittest.TestCase):
    def test_golden_vector(self):
        self.assertEqual(crc16(b"123456789"), 0x29B1)
        self.assertEqual(crc16(GOLDEN[2:20]), 0xB09E)
        frame = decode_frame(GOLDEN)
        self.assertEqual(
            (frame.kind, frame.tx_seq, frame.run_seq), (EventType.INJECT, 3, 1)
        )
        self.assertEqual(
            (frame.scenario, frame.phase, frame.tick_us), (2, Phase.OBSERVE, 1_000_000)
        )
        self.assertEqual((frame.value, frame.flags), (0, 1))

    def test_all_frame_boundaries_and_combined_input(self):
        for cut in range(1, len(GOLDEN)):
            with self.subTest(cut=cut):
                stream = FrameStream()
                self.assertEqual(stream.feed(b"noise\xa5" + GOLDEN[:cut]), [])
                first, second = stream.feed(GOLDEN[cut:] + GOLDEN)
                self.assertEqual((first.raw, second.raw), (GOLDEN, GOLDEN))

    def test_crc_failure_resynchronizes_inside_candidate(self):
        stream = FrameStream()
        corrupt, good = stream.feed(b"\xa5\x5a\x00" + GOLDEN)
        self.assertEqual(corrupt.error, FrameError.CRC)
        self.assertEqual(good.frame, decode_frame(GOLDEN))

    def test_record_ids_follow_only_bytes_in_candidate(self):
        stream = FrameStream()
        self.assertEqual(stream.feed(b"noise\xa5", record_id="noise"), [])
        self.assertEqual(stream.feed(GOLDEN[:10], record_id="first"), [])
        self.assertEqual(stream.feed(GOLDEN[10:18], record_id="middle"), [])
        split, whole = stream.feed(GOLDEN[18:] + GOLDEN, record_id="last")
        self.assertEqual(split.record_ids, ("first", "middle", "last"))
        self.assertEqual(whole.record_ids, ("last",))
        self.assertEqual(stream.pending_record_ids, ())

    def test_finish_separates_truncation_from_a_valid_crc_suffix(self):
        # 이 STATUS의 CRC 끝 바이트가 sync의 첫 바이트와 같다.
        raw = packet(1, 93, 0)
        self.assertEqual(raw[-1], 0xA5)
        stream = FrameStream()
        self.assertEqual(stream.feed(raw)[0].raw, raw)
        self.assertIsNone(stream.finish())
        stream.feed(GOLDEN[:10])
        truncated = stream.finish()
        self.assertEqual(
            (truncated.raw, truncated.error), (GOLDEN[:10], FrameError.FRAMING)
        )
        self.assertIsNone(stream.finish())

    def test_crc_valid_but_unsupported_or_invalid_fields(self):
        for raw, error in (
            (packet(1, 1, 0, version=2), FrameError.VERSION),
            (packet(9, 1, 0), FrameError.FIELDS),
            (packet(1, 1, 0, flags=16), FrameError.FIELDS),
            (packet(3, 1, 0, value=10), FrameError.FIELDS),
            (packet(3, 1, 0, scenario=0), FrameError.FIELDS),
        ):
            with self.subTest(error=error):
                with self.assertRaises(InvalidFrame) as failure:
                    decode_frame(raw)
                self.assertEqual(failure.exception.reason, error)
                rejected, valid = FrameStream().feed(raw + GOLDEN)
                self.assertEqual(rejected.error, error)
                self.assertEqual(valid.raw, GOLDEN)


class ReceiverTest(unittest.TestCase):
    def test_complete_run_with_wraps_and_duplicate(self):
        receiver = UartReceiver()
        baseline = packet(2, 65534, 0xFFFFFFF0)
        inject = packet(3, 65535, 0xFFFFFFF8, phase=2)
        edge = packet(4, 0, 16, phase=2, value=24, flags=7)
        end = packet(5, 1, 32, phase=3)
        opened, injected, duplicate, captured, finished = receiver.feed(
            baseline + inject + inject + edge + end, 0
        )
        self.assertTrue(duplicate.duplicate)
        self.assertEqual(captured.frame.value, 24)
        self.assertFalse(opened.run.injected)
        self.assertTrue(injected.run.injected)
        self.assertEqual(finished.run.run_id, opened.run.run_id)
        self.assertEqual(
            (finished.run.status, finished.run.issues), (RunStatus.END_RECEIVED, ())
        )

    def test_crc_gap_and_overflow_stay_with_run_after_status(self):
        receiver = UartReceiver()
        receiver.feed(packet(2, 1, 0), 0)
        damaged = bytearray(packet(3, 2, 10, phase=2))
        damaged[-1] ^= 1
        bad, status, end = receiver.feed(
            damaged + packet(1, 3, 20, phase=2, flags=9) + packet(5, 4, 30, phase=3), 1
        )
        self.assertEqual(bad.issues, (FrameError.CRC,))
        self.assertIn(Issue.TX_GAP, status.issues)
        self.assertEqual(end.run.status, RunStatus.END_RECEIVED)
        self.assertEqual(
            end.run.issues,
            (FrameError.CRC, Issue.TX_GAP, Issue.FPGA_INVALID, Issue.EVENT_ORDER),
        )

    def test_status_overflow_remains_latched_until_new_session(self):
        receiver = UartReceiver()
        idle_overflow = packet(1, 0, 0, run=0, scenario=0, phase=0, flags=8)
        clean_idle = packet(1, 1, 10, run=0, scenario=0, phase=0)
        baseline = packet(2, 2, 20)
        inject = packet(3, 3, 30, phase=2)
        end = packet(5, 4, 40, phase=3)
        *_, started, _, ended = receiver.feed(
            idle_overflow + clean_idle + baseline + inject + end, 0
        )
        self.assertEqual(started.run.issues, (Issue.FPGA_INVALID,))
        self.assertEqual(ended.run.issues, (Issue.FPGA_INVALID,))

        receiver.reconnect(1)
        started, _, ended = receiver.feed(baseline + inject + end, 1)
        self.assertEqual(ended.run.issues, ())
        self.assertEqual(started.run.run_id, ended.run.run_id)

    def test_event_invalid_does_not_latch_session_error(self):
        receiver = UartReceiver()
        frames = packet(2, 1, 0, flags=9) + packet(6, 2, 10) + packet(2, 3, 20, run=2)
        _, aborted, started = receiver.feed(frames, 0)
        self.assertEqual(aborted.run.issues, (Issue.FPGA_INVALID,))
        self.assertEqual(started.run.issues, ())

    def test_late_attach_and_new_run_without_previous_terminal(self):
        receiver = UartReceiver()
        attached, new, aborted = receiver.feed(
            GOLDEN + packet(2, 4, 1_000_010, run=3) + packet(6, 5, 1_000_020, run=3), 1
        )
        self.assertIn(Issue.MISSING_BASELINE, attached.run.issues)
        self.assertIn(Issue.MISSING_TERMINAL, new.interrupted_run.issues)
        self.assertIn(Issue.RUN_GAP, new.run.issues)
        self.assertEqual(aborted.run.status, RunStatus.ABORT_RECEIVED)

    def test_counter_regression_quarantines_until_reconnect(self):
        for reset in (
            packet(2, 1, 110, run=65535),
            packet(2, 11, 20, run=65535),
            packet(1, 11, 110, run=0, scenario=0, phase=0),
            packet(1, 10, 110, run=65535),
        ):
            with self.subTest(reset=reset):
                receiver = UartReceiver()
                receiver.feed(packet(2, 10, 100, run=65535), 0)
                old_session = receiver.fpga_session
                discontinuity, ignored = receiver.feed(reset + packet(2, 12, 120), 1)
                self.assertEqual(discontinuity.issues, (Issue.COUNTER_DISCONTINUITY,))
                self.assertEqual(
                    discontinuity.interrupted_run.fpga_session, old_session
                )
                self.assertEqual(ignored.issues, (Issue.SESSION_QUARANTINED,))
                self.assertNotEqual(receiver.fpga_session, old_session)
                damaged = GOLDEN[:-1] + bytes([GOLDEN[-1] ^ 1])
                (rejected,) = receiver.feed(damaged, 2)
                self.assertEqual(
                    rejected.issues, (FrameError.CRC, Issue.SESSION_QUARANTINED)
                )
                receiver.reconnect(2)
                (new,) = receiver.feed(packet(2, 0, 0), 2)
                self.assertEqual(new.run.issues, ())

    def test_disconnect_flushes_partial_bytes_and_invalidates_open_run(self):
        receiver = UartReceiver()
        receiver.feed(packet(2, 2, 0) + GOLDEN[:10], 0)
        old = receiver.disconnect(1)
        self.assertEqual(
            (old.status, old.issues), (RunStatus.INTERRUPTED, (Issue.DISCONNECTED,))
        )
        self.assertEqual(receiver.feed(GOLDEN[10:], 2), [])
        receiver.reconnect(3)
        self.assertEqual(receiver.feed(GOLDEN[10:], 3), [])
        (new,) = receiver.feed(packet(2, 0, 0), 3)
        self.assertNotEqual(new.run.run_id, old.run_id)

    def test_run_timeout_uses_host_and_device_clock(self):
        receiver = UartReceiver()
        receiver.feed(packet(2, 1, 0), 0)
        expired, started = receiver.feed(packet(2, 2, 1, run=2), 60_000_000_000)
        self.assertEqual(expired.interrupted_run.issues, (Issue.RUN_TIMEOUT,))
        self.assertEqual(started.run.status, RunStatus.OPEN)
        (late,) = receiver.feed(
            packet(5, 3, 60_000_001, run=2, phase=3), 60_000_000_001
        )
        self.assertEqual(late.run.status, RunStatus.INTERRUPTED)
        self.assertEqual(late.run.issues, (Issue.RUN_TIMEOUT,))

    def test_second_timed_edge_invalidates_run(self):
        receiver = UartReceiver()
        *_, repeated, ended = receiver.feed(
            packet(2, 1, 0)
            + packet(3, 2, 10, phase=2)
            + packet(4, 3, 20, phase=2, value=10, flags=7)
            + packet(4, 4, 30, phase=2, value=20, flags=7)
            + packet(5, 5, 40, phase=3),
            0,
        )
        self.assertEqual(repeated.issues, (Issue.EVENT_ORDER,))
        self.assertEqual(ended.run.issues, (Issue.EVENT_ORDER,))

    def test_end_with_capture_flag_requires_timed_diag_edge(self):
        for end_flags, edge_flags, expected in (
            (1, None, ()),
            (5, None, (Issue.MISSING_DIAG_EDGE,)),
            (5, 3, (Issue.MISSING_DIAG_EDGE,)),
            (5, 7, ()),
        ):
            with self.subTest(end_flags=end_flags, edge_flags=edge_flags):
                receiver = UartReceiver()
                frames = packet(2, 1, 0) + packet(3, 2, 10, phase=2)
                end_seq = 3
                if edge_flags is not None:
                    frames += packet(4, 3, 20, phase=2, value=10, flags=edge_flags)
                    end_seq += 1
                frames += packet(5, end_seq, 30, phase=3, flags=end_flags)
                frames += packet(1, end_seq + 1, 40, phase=3)
                *_, ended, status = receiver.feed(frames, 0)
                self.assertEqual(ended.run.status, RunStatus.END_RECEIVED)
                self.assertEqual(ended.run.issues, expected)
                self.assertEqual(status.run.issues, expected)

    def test_truncation_at_eof_invalidates_open_run(self):
        receiver = UartReceiver()
        receiver.feed(packet(2, 1, 0) + GOLDEN[:10], 0)
        (truncated,) = receiver.finish(1)
        self.assertEqual(truncated.issues, (FrameError.FRAMING,))
        self.assertEqual(truncated.run.issues, (FrameError.FRAMING,))


if __name__ == "__main__":
    unittest.main()
