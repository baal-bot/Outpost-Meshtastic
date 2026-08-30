from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from typing import Any

from outpost.clock import Clock
from outpost.config import RadioPowerConfig
from outpost.store import Database


def normalize_battery_level(value: object) -> int | None:
    """Normalize Meshtastic's battery field; values above 100 mean external power."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    level = int(value)
    return level if 0 <= level <= 100 else None


def power_condition(level: int | None, config: RadioPowerConfig) -> str:
    if level is None:
        return "not_reported"
    if level <= config.critical_percent:
        return "critical"
    if level <= config.warning_percent:
        return "warning"
    return "normal"


class RadioPowerMonitor:
    """Keep bounded local-radio power history and a synchronous runtime snapshot."""

    def __init__(
        self,
        database: Database,
        clock: Clock,
        config: RadioPowerConfig,
        on_condition_change: Callable[[str], Awaitable[object]] | None = None,
    ) -> None:
        self.database, self.clock, self.config = database, clock, config
        self.on_condition_change = on_condition_change
        self._level: int | None = None
        self._observed_at: int | None = None
        self._persisted_at: int | None = None
        self._baseline_level: int | None = None
        self._baseline_at: int | None = None
        self._sample_count = 0

    async def restore(self) -> None:
        now = int(self.clock.now().timestamp())
        latest = await self.database.read(
            "SELECT captured_at,battery_level FROM radio_power_sample "
            "ORDER BY captured_at DESC,id DESC LIMIT 1"
        )
        if latest:
            self._persisted_at = self._observed_at = int(latest[0]["captured_at"])
            self._level = normalize_battery_level(latest[0]["battery_level"])
        await self._refresh_baseline(now)

    async def _refresh_baseline(self, now: int) -> None:
        cutoff = now - self.config.trend_hours * 3_600
        rows = await self.database.read(
            "SELECT captured_at,battery_level FROM radio_power_sample "
            "WHERE captured_at>=? AND battery_level IS NOT NULL "
            "ORDER BY captured_at,id LIMIT 1",
            (cutoff,),
        )
        counts = await self.database.read(
            "SELECT COUNT(*) count FROM radio_power_sample WHERE captured_at>=?",
            (cutoff,),
        )
        self._sample_count = int(counts[0]["count"])
        if rows:
            self._baseline_at = int(rows[0]["captured_at"])
            self._baseline_level = normalize_battery_level(rows[0]["battery_level"])
        else:
            self._baseline_at = None
            self._baseline_level = None

    async def observe(self, raw_level: int | None) -> None:
        level = normalize_battery_level(raw_level)
        now = int(self.clock.now().timestamp())
        previous_condition = power_condition(self._level, self.config)
        previous_reported = self._observed_at is not None and self._level is not None
        self._level, self._observed_at = level, now
        condition = power_condition(level, self.config)
        reported = level is not None
        due = (
            self._persisted_at is None
            or now - self._persisted_at >= self.config.sample_interval_s
            or condition != previous_condition
            or reported != previous_reported
        )
        if not due:
            return
        await self.database.write(
            "INSERT INTO radio_power_sample(captured_at,battery_level) VALUES(?,?)",
            (now, level),
        )
        self._persisted_at = now
        await self._refresh_baseline(now)
        if condition != previous_condition and self.on_condition_change is not None:
            await self.on_condition_change(condition)

    def snapshot(self) -> dict[str, Any]:
        condition = power_condition(self._level, self.config)
        delta: int | None = None
        elapsed_hours: float | None = None
        if (
            self._level is not None
            and self._baseline_level is not None
            and self._observed_at is not None
            and self._baseline_at is not None
            and self._observed_at > self._baseline_at
        ):
            delta = self._level - self._baseline_level
            elapsed_hours = (self._observed_at - self._baseline_at) / 3_600
        direction = (
            "unavailable"
            if delta is None
            else "falling"
            if delta <= -2
            else "rising"
            if delta >= 2
            else "steady"
        )
        return {
            "battery_level": self._level,
            "reported": self._level is not None,
            "condition": condition,
            "observed_at": self._observed_at,
            "trend": {
                "direction": direction,
                "delta_percent": delta,
                "elapsed_hours": elapsed_hours,
                "window_hours": self.config.trend_hours,
                "sample_count": self._sample_count,
            },
            "thresholds": {
                "warning_percent": self.config.warning_percent,
                "critical_percent": self.config.critical_percent,
            },
            "shedding": {
                "enabled": self.config.shed_discretionary,
                "below_percent": self.config.shed_below_percent,
                "active": bool(
                    self.config.shed_discretionary
                    and self._level is not None
                    and self._level <= self.config.shed_below_percent
                ),
                "classes": ["ai", "bulletin", "digest"],
            },
        }

    async def history(self, limit: int = 288) -> dict[str, Any]:
        cutoff = int(self.clock.now().timestamp()) - self.config.trend_hours * 3_600
        rows = await self.database.read(
            "SELECT id,captured_at,battery_level FROM ("
            "SELECT id,captured_at,battery_level FROM radio_power_sample "
            "WHERE captured_at>=? ORDER BY captured_at DESC,id DESC LIMIT ?) "
            "ORDER BY captured_at,id",
            (cutoff, limit),
        )
        return {
            **self.snapshot(),
            "samples": [
                {
                    "captured_at": int(row["captured_at"]),
                    "battery_level": normalize_battery_level(row["battery_level"]),
                }
                for row in rows
            ],
        }
