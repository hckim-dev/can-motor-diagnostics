"""CAN·UART·서비스 입력을 공통 순번과 단조 시각으로 정규화한다."""

from dataclasses import dataclass

from ..can_control.wire import Opcode
from ..can_rx.codec import CanId, Frame

SERVICES = {
    "clock",
    "can_disconnect",
    "can_reconnect",
    "can_retry_failed",
    "can_rx_overflow",
    "fpga_disconnect",
    "fpga_reconnect",
    "fpga_retry_failed",
    "stop",
    "reset",
    "recording_failed",
    "recording_space_low",
    "recording_space_recovered",
}
SERVICE_DETAIL_FIELDS = {
    "can_disconnect": {"reason"},
    "can_reconnect": {"attempt"},
    "can_retry_failed": {"attempt", "reason"},
    "can_rx_overflow": {"dropped"},
    "fpga_disconnect": {"reason"},
    "fpga_reconnect": {"attempt"},
    "fpga_retry_failed": {"attempt", "reason"},
    "recording_space_low": {"free_bytes"},
    "recording_space_recovered": {"free_bytes"},
}


@dataclass(frozen=True)
class Input:
    backend_session: str
    ingest_seq: int
    rx_mono_ns: int
    source: str
    payload: Frame | bytes | str
    detail: dict | None = None

    @property
    def record_id(self) -> str:
        return f"{self.backend_session}:{self.ingest_seq}"

    @classmethod
    def from_row(cls, row, session, seq):
        source = row["source"]
        raw_time = row["rx_mono_ns"]
        if (
            not isinstance(raw_time, str)
            or not raw_time.isascii()
            or not raw_time.isdecimal()
        ):
            raise ValueError("rx_mono_ns는 음이 아닌 정수 문자열이어야 한다")
        if source in ("can", "can_tx"):
            can_id, dlc = row["can_id"], row["dlc"]
            if type(can_id) is not int or type(dlc) is not int:
                raise ValueError("CAN ID와 DLC는 정수여야 한다")
            flags = {
                name: row.get(name, False) for name in ("extended", "rtr", "error")
            }
            if any(type(flag) is not bool for flag in flags.values()):
                raise ValueError("CAN 프레임 플래그는 불리언이어야 한다")
            payload = Frame(can_id, bytes.fromhex(row["hex"]), dlc, **flags)
        elif source == "fpga":
            payload = bytes.fromhex(row["hex"])
        elif source == "service" and row["event"] in SERVICES:
            payload = row["event"]
        else:
            raise ValueError("지원하지 않는 판정 입력")
        detail = row.get("detail")
        if "detail" in row:
            if source in ("can", "can_tx"):
                rx_result = source == "can" and can_id == CanId.COMMAND_RESULT
                tx_command = source == "can_tx" and can_id in (
                    CanId.COMMAND,
                    CanId.OP_PERMIT,
                )
                fields = {"request_id", "opcode"} if rx_result else {"request_id"}
                if (
                    not (rx_result or tx_command)
                    or not isinstance(detail, dict)
                    or detail.keys() != fields
                    or not isinstance(detail["request_id"], str)
                    or not detail["request_id"]
                    or (
                        rx_result
                        and (
                            not isinstance(detail["opcode"], str)
                            or detail["opcode"] not in Opcode.__members__
                        )
                    )
                ):
                    raise ValueError("CAN 조작 근거 형식 오류")
            elif (
                source != "service"
                or not isinstance(detail, dict)
                or detail.keys() != SERVICE_DETAIL_FIELDS.get(payload)
                or ("reason" in detail and not isinstance(detail["reason"], str))
                or (
                    "free_bytes" in detail
                    and (
                        type(detail["free_bytes"]) is not int
                        or detail["free_bytes"] < 0
                    )
                )
                or any(
                    type(value) is not int or value <= 0
                    for key, value in detail.items()
                    if key in ("attempt", "dropped")
                )
            ):
                raise ValueError("서비스 세부 정보 형식 오류")
        elif source == "service" and payload in (
            "can_reconnect",
            "can_retry_failed",
            "can_rx_overflow",
            "fpga_retry_failed",
            "recording_space_low",
            "recording_space_recovered",
        ):
            raise ValueError("서비스 세부 정보 누락")
        record = cls(session, seq, int(raw_time), source, payload, detail)
        for name in ("backend_session", "ingest_seq", "record_id"):
            if name in row and row[name] != getattr(record, name):
                raise ValueError("기록의 세션 또는 입력 순번 불일치")
        return record

    def as_row(self):
        row = {
            "backend_session": self.backend_session,
            "ingest_seq": self.ingest_seq,
            "record_id": self.record_id,
            "rx_mono_ns": str(self.rx_mono_ns),
            "source": self.source,
        }
        if isinstance(self.payload, Frame):
            row.update(
                can_id=self.payload.can_id,
                dlc=self.payload.dlc,
                hex=self.payload.data.hex(" "),
                extended=self.payload.extended,
                rtr=self.payload.rtr,
                error=self.payload.error,
            )
            if self.detail is not None:
                row["detail"] = self.detail
        elif isinstance(self.payload, bytes):
            row["hex"] = self.payload.hex(" ")
        else:
            row["event"] = self.payload
            if self.detail is not None:
                row["detail"] = self.detail
        return row
