from __future__ import annotations

import os
import socket
from collections.abc import Callable

from outpost.clock import Clock


def notify(message: str) -> bool:
    address = os.getenv("NOTIFY_SOCKET")
    if not address:
        return False
    if address.startswith("@"):
        address = "\0" + address[1:]
    client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        client.connect(address)
        client.sendall(message.encode())
        return True
    finally:
        client.close()


async def watchdog(
    clock: Clock,
    healthy: Callable[[], bool] | None = None,
    interval_s: float = 60,
) -> None:
    while True:
        if healthy is not None and not healthy():
            return
        notify("WATCHDOG=1")
        await clock.sleep(interval_s)
