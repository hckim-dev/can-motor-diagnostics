"""Linux SocketCAN에서 Classical CAN 프레임을 읽는다."""

import errno
import socket
from dataclasses import dataclass
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
# Python 3.10에 없는 경우 Linux UAPI 값을 사용한다.
SO_RXQ_OVFL = getattr(socket, "SO_RXQ_OVFL", 40)
DROP_COUNT = Struct("=I")


@dataclass(frozen=True)
class CanRead:
    frame: Frame | None
    dropped_total: int | None
    error: ValueError | None = None


class CanDropCounter:
    def __init__(self):
        self.total = 0

    def observe(self, total):
        if total is None:
            return 0
        delta = (total - self.total) % (1 << (8 * DROP_COUNT.size))
        self.total = total
        return delta


def can_error_reason(error, *, invalid="INVALID_SOCKET_FRAME"):
    if isinstance(error, ValueError):
        return invalid
    number = getattr(error, "errno", None)
    if number in (errno.ENODEV, errno.ENXIO, errno.ENOENT):
        return "INTERFACE_MISSING"
    if number == errno.ENETDOWN:
        return "INTERFACE_DOWN"
    return "SOCKET_ERROR"


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


def open_can(interface: str, *, count_overflow=False):
    try:
        socket.if_nametoindex(interface)
    except OSError as exc:
        if exc.errno not in (None, errno.ENODEV, errno.ENXIO):
            raise
        raise OSError(errno.ENODEV, f"CAN 인터페이스 {interface}가 없습니다") from exc

    device = Path("/sys/class/net") / interface
    if int((device / "type").read_text()) != ARPHRD_CAN:
        raise ValueError(f"{interface}는 CAN 인터페이스가 아닙니다")
    if not int((device / "flags").read_text(), 0) & NETWORK_UP:
        raise OSError(errno.ENETDOWN, f"CAN 인터페이스 {interface}가 DOWN 상태입니다")

    sock = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
    try:
        sock.setsockopt(socket.SOL_CAN_RAW, socket.CAN_RAW_ERR_FILTER, CAN_ERR_BUSOFF)
        if count_overflow:
            sock.setsockopt(socket.SOL_SOCKET, SO_RXQ_OVFL, 1)
        sock.bind((interface,))
        sock.setblocking(False)
    except BaseException:
        sock.close()
        raise
    return sock


def read_external_frame(sock) -> Frame | None:
    raw, _, flags, _ = sock.recvmsg(CAN_FRAME.size)
    return external_frame(raw, flags)


def external_frame(raw, flags):
    if flags & socket.MSG_TRUNC:
        raise ValueError("SocketCAN 프레임이 잘렸습니다")
    if flags & socket.MSG_DONTROUTE:
        return None
    return unpack_frame(raw)


def read_can_observation(sock):
    raw, control, flags, _ = sock.recvmsg(
        CAN_FRAME.size, socket.CMSG_SPACE(DROP_COUNT.size)
    )
    if flags & socket.MSG_CTRUNC:
        raise ValueError("SocketCAN 부가 정보가 잘렸습니다")
    dropped = None
    for level, kind, data in control:
        if level == socket.SOL_SOCKET and kind == SO_RXQ_OVFL:
            if len(data) != DROP_COUNT.size:
                raise ValueError("SocketCAN 드롭 카운터 길이 오류")
            (dropped,) = DROP_COUNT.unpack(data)
            break
    try:
        frame = external_frame(raw, flags)
    except ValueError as exc:
        # 함께 도착한 드롭 카운터는 프레임 오류와 별도로 보존한다.
        return CanRead(frame=None, dropped_total=dropped, error=exc)
    return CanRead(frame=frame, dropped_total=dropped)
