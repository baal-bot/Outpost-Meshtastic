from __future__ import annotations

import random

from outpost.clock import Clock
from outpost.config import ReconnectConfig

from .metrics import RADIO_RECONNECTS
from .models import RadioLink


class RadioSupervisor:
    def __init__(
        self,
        link: RadioLink,
        reconnect: ReconnectConfig,
        clock: Clock,
        liveness_timeout_s: float = 300,
    ) -> None:
        self.link, self.reconnect, self.clock = link, reconnect, clock
        self.liveness_timeout_s = liveness_timeout_s
        self.running = False

    def is_stale(self) -> bool:
        return self.clock.monotonic() - self.link.last_activity >= self.liveness_timeout_s

    async def run(self) -> None:
        self.running = True
        failures = 0
        while self.running:
            try:
                await self.link.connect()
                failures = 0
                while self.running and self.link.state.value == "up":
                    await self.clock.sleep(1)
                    if self.is_stale():
                        await self.link.close()
                        break
            except Exception:
                failures += 1
            if self.running:
                await self.link.close()
                RADIO_RECONNECTS.inc()
                base = min(self.reconnect.initial_s * (2**failures), self.reconnect.max_s)
                jitter = 1 + random.uniform(  # noqa: S311 - reconnect jitter is not cryptographic.
                    -self.reconnect.jitter, self.reconnect.jitter
                )
                await self.clock.sleep(base * jitter)

    async def stop(self) -> None:
        self.running = False
        await self.link.close()
