from __future__ import annotations

import json
from collections import defaultdict, deque

from outpost.clock import Clock
from outpost.store.database import Database

LIMITS = {
    "guest": (4, 30),
    "member": (10, 120),
    "trusted": (20, 300),
    "responder": (20, 300),
    "operator": (60, 2**31 - 1),
}
DEFENSIVE_COMMANDS = {"ALERT", "HELP", "REPORT", "OK", "HELPME"}
SAFETY_FLOOR = {"REPORT", "OK", "HELPME"}


class RateLimiter:
    """Persistent per-member limits plus the node-wide defensive circuit breaker."""

    def __init__(
        self,
        clock: Clock,
        global_per_minute: int = 60,
        database: Database | None = None,
    ) -> None:
        self.clock, self.global_per_minute, self.database = clock, global_per_minute, database
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._loaded: set[str] = set()
        self._global: deque[float] = deque()
        self._defensive_until = 0.0

    @staticmethod
    def _prune(events: deque[float], threshold: float) -> None:
        while events and events[0] <= threshold:
            events.popleft()

    async def _load(self, member_id: str) -> None:
        if member_id in self._loaded or self.database is None:
            self._loaded.add(member_id)
            return
        rows = await self.database.read(
            "SELECT v FROM kv WHERE ns='rate_limit' AND k=?",
            (member_id,),
        )
        if rows:
            try:
                values = json.loads(rows[0]["v"])
                self._events[member_id].extend(float(value) for value in values)
            except (TypeError, ValueError, json.JSONDecodeError):
                self._events[member_id].clear()
        self._loaded.add(member_id)

    async def _persist(self, member_id: str, now: float) -> None:
        if self.database is None:
            return
        payload = json.dumps(list(self._events[member_id]), separators=(",", ":"))
        await self.database.write(
            """
            INSERT INTO kv(ns,k,v,expires_at,updated_at) VALUES('rate_limit',?,?,?,?)
            ON CONFLICT(ns,k) DO UPDATE SET
              v=excluded.v,expires_at=excluded.expires_at,updated_at=excluded.updated_at
            """,
            (member_id, payload, int(now + 3_600), int(now)),
        )

    @property
    def defensive(self) -> bool:
        return self.clock.now().timestamp() < self._defensive_until

    async def allow(self, member_id: str, trust: str, command: str = "") -> bool:
        now = self.clock.now().timestamp()
        await self._load(member_id)
        self._prune(self._global, now - 60)
        if len(self._global) >= self.global_per_minute:
            self._defensive_until = max(self._defensive_until, now + 60)
        if self.defensive and command.upper() not in DEFENSIVE_COMMANDS:
            return False
        # These inputs must be accepted even when ordinary member buckets are exhausted.
        # Their replies still pass through the airtime governor.
        if command.upper() in SAFETY_FLOOR:
            return True
        events = self._events[member_id]
        self._prune(events, now - 3_600)
        per_minute, per_hour = LIMITS.get(trust, LIMITS["guest"])
        recent_minute = sum(timestamp > now - 60 for timestamp in events)
        if recent_minute >= per_minute or len(events) >= per_hour:
            return False
        events.append(now)
        self._global.append(now)
        await self._persist(member_id, now)
        return True
