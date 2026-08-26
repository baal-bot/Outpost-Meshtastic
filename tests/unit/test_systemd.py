from __future__ import annotations

import pytest

from outpost import systemd
from outpost.clock import VirtualClock


@pytest.mark.asyncio
async def test_watchdog_stops_heartbeating_when_application_is_unhealthy(monkeypatch) -> None:
    messages: list[str] = []
    checks = 0

    def healthy() -> bool:
        nonlocal checks
        checks += 1
        return checks == 1

    monkeypatch.setattr(systemd, "notify", lambda message: messages.append(message) or True)

    await systemd.watchdog(VirtualClock(), healthy, interval_s=1)

    assert messages == ["WATCHDOG=1"]
