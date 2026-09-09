from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

from outpost.timekeeping import TimeMonitor, TimeStatus


class Clock(Protocol):
    def monotonic(self) -> float: ...
    def now(self) -> datetime: ...
    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def __init__(self) -> None:
        self._time_monitor = TimeMonitor(self.now().timestamp(), time.monotonic())
        self.time_status()  # Capture source evidence before slow application construction.

    def time_status(self) -> TimeStatus:
        return self._time_monitor.sample(self.now().timestamp(), time.monotonic())

    def monotonic(self) -> float:
        return asyncio.get_running_loop().time()

    def time_evidence_monotonic(self) -> float:
        """Read clock evidence during synchronous diagnostics/CLI construction."""
        return time.monotonic()

    def now(self) -> datetime:
        return datetime.now(UTC)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


@dataclass
class VirtualClock:
    value: float = 0.0
    epoch: datetime = field(default_factory=lambda: datetime(2026, 1, 1, tzinfo=UTC))

    def time_status(self) -> TimeStatus:
        return TimeStatus("simulated", "synthetic_clock", True, source="simulation")

    def monotonic(self) -> float:
        return self.value

    def now(self) -> datetime:
        return self.epoch + timedelta(seconds=self.value)

    async def sleep(self, seconds: float) -> None:
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("clock cannot move backwards")
        self.value += seconds
