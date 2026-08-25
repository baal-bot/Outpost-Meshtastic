from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from outpost.clock import Clock
from outpost.config import Config
from outpost.store import Database


@dataclass(frozen=True)
class DigestDelivery:
    member_id: int
    mesh_id: str
    cadence: str
    text: str
    thread_count: int
    last_thread_id: int


class DigestService:
    def __init__(self, database: Database, clock: Clock, config: Config) -> None:
        self.database, self.clock, self.config = database, clock, config

    async def due(self) -> list[DigestDelivery]:
        deliveries: list[DigestDelivery] = []
        if self.config.bbs.immediate_enabled:
            deliveries.extend(await self._collect("immediate"))
        deliveries.extend(await self._collect("daily"))
        return deliveries

    async def _collect(self, cadence: str) -> list[DigestDelivery]:
        now = int(self.clock.now().timestamp())
        members = await self.database.read(
            """
            SELECT DISTINCT m.id,m.mesh_id,m.prefs,COALESCE(ds.last_thread_id,0) last_thread_id,
                   ds.last_sent_at
            FROM subscription s JOIN member m ON m.id=s.member_id
            LEFT JOIN digest_state ds ON ds.member_id=m.id AND ds.cadence=?
            WHERE s.cadence=? AND m.unreachable_since IS NULL
              AND (m.muted_until IS NULL OR m.muted_until<=?)
            """,
            (cadence, cadence, now),
        )
        results = []
        for member in members:
            if cadence == "daily" and not self._daily_due(member["prefs"], member["last_sent_at"]):
                continue
            if cadence == "immediate":
                sent = await self.database.read(
                    """
                    SELECT COUNT(*) AS count FROM digest_delivery_log
                    WHERE member_id=? AND cadence='immediate' AND created_at>?
                    """,
                    (member["id"], now - 3_600),
                )
                if int(sent[0]["count"]) >= self.config.bbs.immediate_max_per_hour:
                    continue
            threads = await self.database.read(
                """
                SELECT t.id,b.slug,t.subject FROM subscription s
                JOIN board b ON b.id=s.board_id JOIN thread t ON t.board_id=b.id
                WHERE s.member_id=? AND s.cadence=? AND t.id>? AND t.hidden=0
                ORDER BY t.id LIMIT 8
                """,
                (member["id"], cadence, member["last_thread_id"]),
            )
            if not threads:
                continue
            summary = " · ".join(f"{row['slug']}: {row['subject'][:34]}" for row in threads)
            results.append(
                DigestDelivery(
                    member["id"],
                    member["mesh_id"],
                    cadence,
                    f"{cadence.title()} digest · {summary}",
                    len(threads),
                    int(threads[-1]["id"]),
                )
            )
        return results

    def _daily_due(self, prefs_json: str, last_sent_at: int | None) -> bool:
        import json

        prefs = json.loads(prefs_json)
        hour = int(prefs.get("digest_hour", 8))
        local_now = self.clock.now().astimezone(ZoneInfo(self.config.node.timezone))
        if local_now.hour < hour:
            return False
        if last_sent_at is None:
            return True
        last = datetime.fromtimestamp(last_sent_at, self.clock.now().tzinfo).astimezone(
            ZoneInfo(self.config.node.timezone)
        )
        return last.date() < local_now.date()

    async def mark_scheduled(self, delivery: DigestDelivery) -> None:
        now = int(self.clock.now().timestamp())
        await self.database.write(
            """
            INSERT INTO digest_state(member_id,cadence,last_thread_id,last_sent_at)
            VALUES(?,?,?,?) ON CONFLICT(member_id,cadence) DO UPDATE SET
              last_thread_id=excluded.last_thread_id,last_sent_at=excluded.last_sent_at
            """,
            (delivery.member_id, delivery.cadence, delivery.last_thread_id, now),
        )
        await self.database.write(
            """
            INSERT INTO digest_delivery_log(member_id,cadence,thread_count,created_at)
            VALUES(?,?,?,?)
            """,
            (delivery.member_id, delivery.cadence, delivery.thread_count, now),
        )
