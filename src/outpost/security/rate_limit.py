from __future__ import annotations

import asyncio
import hashlib
import json
import re
import unicodedata
from collections import defaultdict, deque
from dataclasses import dataclass

from outpost.clock import Clock
from outpost.store.database import Database
from outpost.transport.metrics import SAFETY_FLOOR_ATTEMPTS

LIMITS = {
    "guest": (4, 30),
    "member": (10, 120),
    "trusted": (20, 300),
    "responder": (20, 300),
    "operator": (60, 2**31 - 1),
}
DEFENSIVE_COMMANDS = {"ALERT", "HELP", "MENU", "REPORT", "REPORT!", "OK", "HELPME"}
SAFETY_FLOOR = {"REPORT", "REPORT!", "OK", "HELPME"}
NAVIGATION_COMMANDS = {"HELP", "MENU", "HOME", "BACK", "WHERE"}
NAVIGATION_LIMITS = {
    "guest": (12, 60),
    "member": (20, 120),
    "trusted": (30, 180),
    "responder": (30, 180),
    "operator": (60, 300),
}
MAX_RECORDED_EVENTS = 2048


@dataclass(frozen=True)
class SafetyFloorDecision:
    accepted: bool
    attempt_count: int
    coalesced_count: int
    fingerprint: str | None = None


class RateLimiter:
    """Persistent per-member limits plus the node-wide defensive circuit breaker."""

    def __init__(
        self,
        clock: Clock,
        global_per_minute: int = 60,
        database: Database | None = None,
        *,
        safety_repeat_window_seconds: int = 120,
    ) -> None:
        self.clock, self.global_per_minute, self.database = clock, global_per_minute, database
        self.safety_repeat_window_seconds = safety_repeat_window_seconds
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._navigation_events: dict[str, deque[float]] = defaultdict(deque)
        self._loaded: set[str] = set()
        self._global: deque[float] = deque()
        self._defensive_until = 0.0
        self._safety_locks: dict[tuple[str, str], asyncio.Lock] = defaultdict(asyncio.Lock)
        self._safety_memory: dict[tuple[str, str, str], tuple[int, int, int]] = {}

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

    @staticmethod
    def _normalise_safety_payload(value: str) -> str:
        normalised = unicodedata.normalize("NFKC", value).casefold().strip()
        return re.sub(r"\s+", " ", normalised)

    async def _position_context(self, member_id: str) -> str:
        if self.database is None:
            return ""
        rows = await self.database.read(
            "SELECT p.lat,p.lon FROM member_position p "
            "JOIN member m ON m.id=p.member_id WHERE m.mesh_id=? AND p.expires_at>?",
            (member_id, int(self.clock.now().timestamp())),
        )
        if not rows:
            return ""
        return f"|{float(rows[0]['lat']):.5f},{float(rows[0]['lon']):.5f}"

    async def safety_floor_decision(
        self, member_id: str, command: str, payload: str
    ) -> SafetyFloorDecision:
        command = command.upper()
        if command not in SAFETY_FLOOR:
            return SafetyFloorDecision(True, 1, 0)
        position = await self._position_context(member_id)
        canonical = f"{command}|{self._normalise_safety_payload(payload)}{position}"
        fingerprint = hashlib.sha256(canonical.encode()).hexdigest()
        key = (member_id, command, fingerprint)
        lock_key = (member_id, command)
        now = int(self.clock.now().timestamp())
        async with self._safety_locks[lock_key]:
            if self.database is None:
                accepted_at, attempts, coalesced = self._safety_memory.get(key, (0, 0, 0))
                accepted = not accepted_at or now - accepted_at >= self.safety_repeat_window_seconds
                attempts += 1
                if accepted:
                    accepted_at = now
                else:
                    coalesced += 1
                self._safety_memory[key] = (accepted_at, attempts, coalesced)
            else:
                rows = await self.database.read(
                    "SELECT accepted_at,attempt_count,coalesced_count "
                    "FROM safety_floor_attempt WHERE member_mesh_id=? AND command=? "
                    "AND fingerprint=?",
                    key,
                )
                if not rows:
                    accepted, attempts, coalesced = True, 1, 0
                    await self.database.write(
                        "INSERT INTO safety_floor_attempt(member_mesh_id,command,fingerprint,"
                        "first_seen_at,last_seen_at,accepted_at) VALUES(?,?,?,?,?,?)",
                        (*key, now, now, now),
                    )
                else:
                    row = rows[0]
                    attempts = int(row["attempt_count"]) + 1
                    coalesced = int(row["coalesced_count"])
                    accepted_at = int(row["accepted_at"])
                    accepted = now - accepted_at >= self.safety_repeat_window_seconds
                    if accepted:
                        await self.database.write(
                            "UPDATE safety_floor_attempt SET last_seen_at=?,accepted_at=?,"
                            "attempt_count=?,accepted_count=accepted_count+1 "
                            "WHERE member_mesh_id=? AND command=? "
                            "AND fingerprint=?",
                            (now, now, attempts, *key),
                        )
                    else:
                        coalesced += 1
                        await self.database.write(
                            "UPDATE safety_floor_attempt SET last_seen_at=?,attempt_count=?,"
                            "coalesced_count=? WHERE member_mesh_id=? "
                            "AND command=? AND fingerprint=?",
                            (now, attempts, coalesced, *key),
                        )
        outcome = "accepted" if accepted else "coalesced"
        SAFETY_FLOOR_ATTEMPTS.labels(command.lower(), outcome).inc()
        return SafetyFloorDecision(
            accepted,
            attempts,
            coalesced,
            fingerprint=fingerprint,
        )

    async def release_safety_floor(
        self, member_id: str, command: str, fingerprint: str | None
    ) -> None:
        if fingerprint is None:
            return
        command = command.upper()
        key = (member_id, command, fingerprint)
        async with self._safety_locks[(member_id, command)]:
            if self.database is None:
                state = self._safety_memory.get(key)
                if state is not None:
                    _, attempts, coalesced = state
                    self._safety_memory[key] = (0, attempts, coalesced)
                return
            await self.database.write(
                "UPDATE safety_floor_attempt SET accepted_at=0 "
                "WHERE member_mesh_id=? AND command=? AND fingerprint=?",
                key,
            )

    async def allow(self, member_id: str, trust: str, command: str = "") -> bool:
        now = self.clock.now().timestamp()
        await self._load(member_id)
        self._prune(self._global, now - 60)
        if len(self._global) >= self.global_per_minute:
            self._defensive_until = max(self._defensive_until, now + 60)
        if self.defensive and command.upper() not in DEFENSIVE_COMMANDS:
            return False
        if command.upper() in NAVIGATION_COMMANDS:
            events = self._navigation_events[member_id]
            self._prune(events, now - 3_600)
            per_minute, per_hour = NAVIGATION_LIMITS.get(trust, NAVIGATION_LIMITS["guest"])
            recent_minute = sum(timestamp > now - 60 for timestamp in events)
            if recent_minute >= per_minute or len(events) >= per_hour:
                return False
            events.append(now)
            self._global.append(now)
            return True
        events = self._events[member_id]
        self._prune(events, now - 3_600)
        # These inputs must be accepted even when ordinary member buckets are exhausted.
        # Their replies still pass through the airtime governor.
        if command.upper() in SAFETY_FLOOR:
            events.append(now)
            self._global.append(now)
            while len(events) > MAX_RECORDED_EVENTS:
                events.popleft()
            # The coalescing decision persists an aggregate row; avoid rewriting a large
            # timestamp blob for every repeated emergency packet.
            return True
        per_minute, per_hour = LIMITS.get(trust, LIMITS["guest"])
        recent_minute = sum(timestamp > now - 60 for timestamp in events)
        if recent_minute >= per_minute or len(events) >= per_hour:
            return False
        events.append(now)
        self._global.append(now)
        await self._persist(member_id, now)
        return True
