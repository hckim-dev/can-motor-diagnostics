"""전원 차단 상태에서 OP_HELLO 한 프레임의 송신 제출을 확인한다."""

import math
import socket
import time

from ..can_control.boot_id import next_boot_id
from ..can_control.wire import encode_operator_hello
from .socketcan import CAN_FRAME, open_can


def send_hello_probe(interface, boot_file, timeout, *, motor_power_off=False):
    if not motor_power_off:
        raise ValueError("모터 6V 물리 차단 확인이 필요합니다")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("CAN 송신 점검 시간은 유한한 양수여야 합니다")

    with open_can(interface) as sock:
        # 소켓이 처리할 수 없는 시간은 부팅 ID 발급과 송신 전에 거부한다.
        sock.settimeout(timeout)
        sock.setsockopt(socket.SOL_CAN_RAW, socket.CAN_RAW_RECV_OWN_MSGS, 1)
        boot_id = next_boot_id(boot_file)
        frame = encode_operator_hello(boot_id)
        packet = CAN_FRAME.pack(frame.can_id, frame.dlc, 0, frame.data)
        deadline = time.monotonic() + timeout
        if sock.send(packet) != len(packet):
            raise OSError("OP_HELLO를 온전히 제출하지 못했습니다")

        echoed = False
        while (remaining := deadline - time.monotonic()) > 0:
            sock.settimeout(remaining)
            try:
                raw, _, flags, _ = sock.recvmsg(CAN_FRAME.size)
            except TimeoutError:
                break
            if (
                flags & socket.MSG_CONFIRM
                and not flags & socket.MSG_TRUNC
                and raw == packet
            ):
                echoed = True
                break

    return {
        "interface": interface,
        "can_id": frame.can_id,
        "hex": frame.data.hex(" "),
        "boot_id": boot_id,
        "submitted": True,
        "socket_echo": echoed,
    }
