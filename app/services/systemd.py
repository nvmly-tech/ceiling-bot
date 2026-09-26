"""sd_notify без зависимостей: сообщения systemd для Type=notify и WatchdogSec."""

import os
import socket


def notify(message: str) -> bool:
    """Отправить сообщение systemd (READY=1, WATCHDOG=1, STATUS=…). False — запущены не под systemd."""
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return False
    if address.startswith("@"):
        address = "\0" + address[1:]  # абстрактный сокет Linux
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.connect(address)
        sock.sendall(message.encode())
    return True


def watchdog_interval() -> float | None:
    """Как часто слать WATCHDOG=1: треть от WatchdogSec (systemd передаёт его в WATCHDOG_USEC)."""
    usec = os.environ.get("WATCHDOG_USEC")
    if not usec or os.environ.get("WATCHDOG_PID") not in (None, str(os.getpid())):
        return None
    return int(usec) / 1_000_000 / 3
