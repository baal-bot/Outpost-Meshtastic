import asyncio

import pytest

from outpost.clock import VirtualClock
from outpost.config import ReconnectConfig
from outpost.transport.models import LinkState
from outpost.transport.simulated import SimulatedRadioLink
from outpost.transport.supervisor import RadioSupervisor


def test_liveness_timeout_uses_injected_clock() -> None:
    clock = VirtualClock()
    supervisor = RadioSupervisor(
        SimulatedRadioLink(), ReconnectConfig(), clock, liveness_timeout_s=300
    )
    assert supervisor.is_stale() is False
    clock.advance(299)
    assert supervisor.is_stale() is False
    clock.advance(1)
    assert supervisor.is_stale() is True


@pytest.mark.asyncio
async def test_radio_loss_reconnects_with_bounded_backoff(monkeypatch) -> None:
    class RecoveringLink(SimulatedRadioLink):
        def __init__(self) -> None:
            super().__init__()
            self.connect_attempts = 0
            self.close_calls = 0

        async def connect(self) -> None:
            self.connect_attempts += 1
            if self.connect_attempts == 1:
                raise OSError("USB radio disappeared")
            self._state = LinkState.UP

        async def close(self) -> None:
            self.close_calls += 1
            await super().close()

    class YieldingClock(VirtualClock):
        async def sleep(self, seconds: float) -> None:
            self.advance(seconds)
            await asyncio.sleep(0)

    link = RecoveringLink()
    clock = YieldingClock()
    supervisor = RadioSupervisor(
        link,
        ReconnectConfig(initial_s=2, max_s=5, jitter=0),
        clock,
        liveness_timeout_s=300,
    )
    original_progress = supervisor._progress

    def stop_after_recovery() -> None:
        original_progress()
        if link.connect_attempts == 2:
            supervisor.running = False

    monkeypatch.setattr(supervisor, "_progress", stop_after_recovery)

    await asyncio.wait_for(supervisor.run(), timeout=1)

    assert link.connect_attempts == 2
    assert link.close_calls == 1
    # The first failed connection uses the configured exponential backoff and
    # never exceeds the configured maximum.
    assert clock.monotonic() == 4
