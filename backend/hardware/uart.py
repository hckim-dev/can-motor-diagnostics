"""FPGA UART 수신 포트를 115200 8N1로 열고 설정을 복구한다."""

import errno
import os
import termios
from contextlib import contextmanager

READ_SIZE = 4096


@contextmanager
def open_uart(path, *, discard_pending=False):
    fd = os.open(path, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        previous = termios.tcgetattr(fd)
        _, _, _, _, _, _, cc = previous
        raw = [
            0,
            0,
            termios.CS8 | termios.CREAD | termios.CLOCAL,
            0,
            termios.B115200,
            termios.B115200,
            cc.copy(),
        ]
        raw[6][termios.VMIN] = 1
        raw[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW, raw)
        try:
            if discard_pending:
                termios.tcflush(fd, termios.TCIFLUSH)
            yield fd
        finally:
            try:
                termios.tcsetattr(fd, termios.TCSANOW, previous)
            except termios.error as exc:
                if exc.args[0] not in (errno.EIO, errno.ENODEV, errno.ENXIO):
                    raise
    finally:
        os.close(fd)


def read_uart(fd):
    data = os.read(fd, READ_SIZE)
    if not data:
        raise EOFError("FPGA UART 연결이 종료됐습니다")
    return data


def uart_error_reason(error):
    if isinstance(error, EOFError):
        return "EOF"
    if isinstance(error, ValueError):
        return "INVALID_PORT"
    number = (
        error.args[0]
        if isinstance(error, termios.error) and error.args
        else getattr(error, "errno", None)
    )
    if number in (errno.ENOENT, errno.ENODEV, errno.ENXIO):
        return "PORT_MISSING"
    if number in (errno.ENOTTY, errno.EINVAL):
        return "INVALID_PORT"
    return "IO_ERROR"
