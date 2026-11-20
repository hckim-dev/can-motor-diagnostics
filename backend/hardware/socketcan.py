"""Linux SocketCAN에서 Classical CAN 프레임을 읽는다."""

import socket
from pathlib import Path
from struct import Struct

from ..can_rx.codec import Frame

CAN_FRAME = Struct("=IB2xB8s")
CAN_EFF_FLAG = 1 << 31
CAN_RTR_FLAG = 1 << 30
CAN_ERR_FLAG = 1 << 29
CAN_SFF_MASK = 0x7FF
CAN_EFF_MASK = 0x1FFFFFFF
CAN_ERR_BUSOFF = 0x40
NETWORK_UP = 1
ARPHRD_CAN = 280


def unpack_frame(raw: bytes) -> Frame:
    if len(raw) != CAN_FRAME.size:
        raise ValueError("SocketCAN Classical 프레임 길이 오류")
    identifier, length, raw_dlc, data = CAN_FRAME.unpack(raw)
    if length > 8:
        raise ValueError("SocketCAN Classical 데이터 길이 오류")
    extended = bool(identifier & CAN_EFF_FLAG)
    remote = bool(identifier & CAN_RTR_FLAG)
    error = bool(identifier & CAN_ERR_FLAG)
    mask = CAN_EFF_MASK if extended or error else CAN_SFF_MASK
    dlc = raw_dlc if length == 8 and 9 <= raw_dlc <= 15 else length
    payload = b"" if remote else data[:length]
    return Frame(identifier & mask, payload, dlc, extended, remote, error)


def open_can(interface: str):
    socket.if_nametoindex(interface)
    device = Path("/sys/class/net") / interface
    if int((device / "type").read_text()) != ARPHRD_CAN:
        raise ValueError(f"{interface}는 CAN 인터페이스가 아닙니다")
    if not int((device / "flags").read_text(), 0) & NETWORK_UP:
        raise OSError(f"CAN 인터페이스 {interface}가 DOWN 상태입니다")

    sock = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
    try:
        sock.setsockopt(socket.SOL_CAN_RAW, socket.CAN_RAW_ERR_FILTER, CAN_ERR_BUSOFF)
        sock.bind((interface,))
        sock.setblocking(False)
    except BaseException:
        sock.close()
        raise
    return sock


def read_external_frame(sock) -> Frame | None:
    raw, _, flags, _ = sock.recvmsg(CAN_FRAME.size)
    if flags & socket.MSG_TRUNC:
        raise ValueError("SocketCAN 프레임이 잘렸습니다")
    if flags & socket.MSG_DONTROUTE:
        return None
    return unpack_frame(raw)
