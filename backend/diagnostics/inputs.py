"""CAN·UART·서비스 입력을 공통 순번과 단조 시각으로 정규화한다."""

from dataclasses import dataclass

from ..can_rx.codec import Frame

SERVICES = {
    "clock",
    "can_disconnect",
    "fpga_disconnect",
    "fpga_reconnect",
    "stop",
    "reset",
    "recording_failed",
}


@dataclass(frozen=True)
class Input:
    backend_session: str
    ingest_seq: int
    rx_mono_ns: int
    source: str
    payload: Frame | bytes | str

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
        if source == "can":
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
        record = cls(session, seq, int(raw_time), source, payload)
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
        elif isinstance(self.payload, bytes):
            row["hex"] = self.payload.hex(" ")
        else:
            row["event"] = self.payload
        return row
