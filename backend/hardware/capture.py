"""실물 수신을 공통 단조 시각과 입력 순번으로 기록한다."""

import selectors
import time
from collections import Counter
from contextlib import ExitStack

from ..can_rx.codec import CanId
from ..diagnostics.inputs import Input
from ..diagnostics.journal import FLUSH_INTERVAL_NS, Journal, record_input
from ..diagnostics.verdict import Config
from ..fpga_uart.codec import FrameError
from ..fpga_uart.session import Issue
from .socketcan import (
    CAN_ERR_BUSOFF,
    CanDropCounter,
    CanRead,
    can_error_reason,
    open_can,
    read_can_observation,
)
from .uart import open_uart, read_uart, uart_error_reason

TIME_UNIT_NS = 1_000_000_000


def receive_inputs(devices, readers, duration_ns):
    drops = CanDropCounter()
    with selectors.DefaultSelector() as selector:
        for source, device in devices.items():
            selector.register(device, selectors.EVENT_READ, source)
        started = time.monotonic_ns()
        deadline = started + duration_ns
        next_clock = started + FLUSH_INTERVAL_NS
        while selector.get_map():
            now = time.monotonic_ns()
            if now >= deadline:
                break
            if now >= next_clock:
                next_clock = now + FLUSH_INTERVAL_NS
                yield now, "service", "clock", None, None
                continue
            timeout = (min(deadline, next_clock) - now) / TIME_UNIT_NS
            for key, _ in selector.select(timeout):
                if time.monotonic_ns() > deadline:
                    break
                source = key.data
                try:
                    payload = readers[source](key.fileobj)
                except BlockingIOError:
                    continue
                except (OSError, EOFError, ValueError) as exc:
                    now = time.monotonic_ns()
                    if now > deadline:
                        break
                    selector.unregister(key.fileobj)
                    event = "can_disconnect" if source == "can" else "fpga_disconnect"
                    reason = (
                        can_error_reason(exc)
                        if source == "can"
                        else uart_error_reason(exc)
                    )
                    detail = {"reason": reason}
                    yield now, "service", event, str(exc), detail
                    continue
                now = time.monotonic_ns()
                if now > deadline:
                    break
                if isinstance(payload, CanRead):
                    if delta := drops.observe(payload.dropped_total):
                        yield (
                            now,
                            "service",
                            "can_rx_overflow",
                            f"SocketCAN 수신 큐 드롭 {delta}건",
                            {"dropped": delta},
                        )
                    if payload.error is not None:
                        selector.unregister(key.fileobj)
                        yield (
                            now,
                            "service",
                            "can_disconnect",
                            str(payload.error),
                            {"reason": can_error_reason(payload.error)},
                        )
                        continue
                    payload = payload.frame
                if payload is None:
                    continue
                yield now, source, payload, None, None
                if (
                    source == "can"
                    and payload.error
                    and payload.can_id & CAN_ERR_BUSOFF
                ):
                    selector.unregister(key.fileobj)
                    yield (
                        now,
                        "service",
                        "can_disconnect",
                        "CAN bus-off",
                        {"reason": "BUS_OFF"},
                    )
        # 저장 지연으로 마감을 넘겨도 이후 시간을 관측 근거로 쓰지 않는다.
        yield min(time.monotonic_ns(), deadline), "service", "clock", None, None


def capture(devices, readers, journal, config, duration_ns):
    engine = journal.new_engine(config)
    counts = Counter()
    errors = []
    for seq, (now, source, payload, error, detail) in enumerate(
        receive_inputs(devices, readers, duration_ns), 1
    ):
        record = Input(journal.evidence_session, seq, now, source, payload, detail)
        received, completed = record_input(journal, engine, record)
        counts.update(
            "fpga_status_timeouts"
            for result in completed
            if "FPGA_STATUS_TIMEOUT" in result["issues"]
        )
        if error:
            errors.append(error)
        if source == "can":
            counts["can_frames"] += 1
            if (
                received is engine.can.state
                and received.frame.can_id == CanId.MOTOR_STATE
            ):
                counts["can_motor_states"] += 1
        elif source == "fpga":
            for observation in received:
                issues = observation.issues
                counts["fpga_crc_errors"] += FrameError.CRC in issues
                counts["fpga_version_errors"] += FrameError.VERSION in issues
                counts["fpga_field_errors"] += FrameError.FIELDS in issues
                counts["fpga_tx_gaps"] += Issue.TX_GAP in issues
                counts["fpga_run_gaps"] += Issue.RUN_GAP in issues
                counts["fpga_invalid_observations"] += Issue.FPGA_INVALID in issues
                if Issue.COUNTER_DISCONTINUITY in issues:
                    counts["fpga_session_discontinuities"] += 1
                    errors.append(
                        "FPGA UART 순번 또는 시각 불연속으로 세션이 격리됐습니다"
                    )
                elif (
                    observation.frame is not None
                    and not observation.duplicate
                    and Issue.SESSION_QUARANTINED not in issues
                ):
                    counts["fpga_frames"] += 1
    journal.finish(engine)
    if "can" in devices:
        counts.update(journal.manifest["can_rx"])

    missing = []
    if "can" in devices and not counts["can_motor_states"]:
        missing.append("호환 MOTOR_HELLO에 이어 유효한 MOTOR_STATE를 받지 못했습니다")
    if "fpga" in devices and not counts["fpga_frames"]:
        missing.append("유효한 FPGA UART 프레임을 받지 못했습니다")
    for name, message in (
        ("fpga_crc_errors", "FPGA UART CRC 오류"),
        ("fpga_version_errors", "FPGA UART 지원하지 않는 프레임 버전"),
        ("fpga_field_errors", "FPGA UART 프레임 필드 오류"),
        ("fpga_tx_gaps", "FPGA UART 전송 순번 누락"),
        ("fpga_run_gaps", "FPGA UART 시험 순번 누락"),
        ("fpga_invalid_observations", "FPGA overflow/invalid 상태 관측"),
        ("fpga_status_timeouts", "FPGA UART STATUS 만료"),
    ):
        if counts[name]:
            errors.append(f"{message} {counts[name]}건")
    return {
        "ok": not (errors or missing),
        "counts": dict(counts),
        "errors": errors + missing,
    }


def capture_hardware(output, interface, uart, duration_ns):
    config = Config(functional_only=True)
    devices = {}
    readers = {"can": read_can_observation, "fpga": read_uart}
    hardware = {
        "can_interface": interface,
        "fpga_uart": str(uart) if uart else None,
        "fpga_status_watchdog": uart is not None,
    }
    with ExitStack() as stack:
        if interface:
            devices["can"] = stack.enter_context(
                open_can(interface, count_overflow=True)
            )
        if uart:
            devices["fpga"] = stack.enter_context(open_uart(uart, discard_pending=True))
        with Journal(output, config, hardware=hardware) as journal:
            return capture(devices, readers, journal, config, duration_ns)
