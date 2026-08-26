from __future__ import annotations

import json
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from outpost.clock import Clock
from outpost.config import Config

from .backups import BackupService
from .database import Database


@dataclass(frozen=True)
class MaintenanceResult:
    threads: int
    mail: int
    messages: int
    kv: int
    safety_floor: int
    federation_services: int
    federation_service_usage: int
    environment_cache: int
    alert_point_cache: int
    backups_removed: int


class MaintenanceService:
    def __init__(
        self, database: Database, backups: BackupService, clock: Clock, config: Config
    ) -> None:
        self.database, self.backups, self.clock, self.config = database, backups, clock, config

    async def due(self) -> bool:
        local = self.clock.now().astimezone(ZoneInfo(self.config.node.timezone))
        if local.hour < self.config.store.maintenance_hour:
            return False
        rows = await self.database.read(
            "SELECT value FROM runtime_setting WHERE key='maintenance.last_date'"
        )
        return not rows or json.loads(rows[0]["value"]) != local.date().isoformat()

    async def run(self) -> MaintenanceResult:
        now = int(self.clock.now().timestamp())
        retention = self.config.store.retention
        thread_cutoff = now - retention.posts_days * 86_400
        mail_cutoff = now - retention.mail_days * 86_400
        message_cutoff = now - retention.message_log_days * 86_400
        safety_cutoff = now - self.config.security.safety_attempt_retention_hours * 3_600
        service_cutoff = now - 7 * 86_400
        provider_cache_cutoff = now - 2 * 86_400
        counts = {}
        queries = {
            "threads": (
                "SELECT COUNT(*) AS count FROM thread WHERE pinned=0 AND last_post_at<?",
                thread_cutoff,
            ),
            "mail": ("SELECT COUNT(*) AS count FROM mail WHERE created_at<?", mail_cutoff),
            "messages": (
                "SELECT COUNT(*) AS count FROM message_log WHERE created_at<?",
                message_cutoff,
            ),
            "kv": (
                "SELECT COUNT(*) AS count FROM kv WHERE expires_at IS NOT NULL AND expires_at<?",
                now,
            ),
            "safety_floor": (
                "SELECT COUNT(*) AS count FROM safety_floor_attempt WHERE last_seen_at<?",
                safety_cutoff,
            ),
            "federation_services": (
                "SELECT COUNT(*) AS count FROM fed_service_request WHERE updated_at<?",
                service_cutoff,
            ),
            "federation_service_usage": (
                "SELECT COUNT(*) AS count FROM fed_service_usage WHERE window_start<?",
                provider_cache_cutoff,
            ),
            "environment_cache": (
                "SELECT COUNT(*) AS count FROM env_cache WHERE fetched_at<?",
                provider_cache_cutoff,
            ),
            "alert_point_cache": (
                "SELECT COUNT(*) AS count FROM cap_point_cache WHERE fetched_at<?",
                provider_cache_cutoff,
            ),
        }
        for key, (sql, value) in queries.items():
            rows = await self.database.read(sql, (value,))
            counts[key] = int(rows[0]["count"])
        await self.database.write(
            "DELETE FROM thread WHERE pinned=0 AND last_post_at<?", (thread_cutoff,)
        )
        await self.database.write("DELETE FROM mail WHERE created_at<?", (mail_cutoff,))
        await self.database.write("DELETE FROM message_log WHERE created_at<?", (message_cutoff,))
        await self.database.write(
            """
            DELETE FROM message_log WHERE id IN (
              SELECT id FROM message_log ORDER BY id DESC LIMIT -1 OFFSET ?
            )
            """,
            (retention.message_log_max_rows,),
        )
        await self.database.write(
            "DELETE FROM kv WHERE expires_at IS NOT NULL AND expires_at<?", (now,)
        )
        await self.database.write(
            "DELETE FROM safety_floor_attempt WHERE last_seen_at<?", (safety_cutoff,)
        )
        await self.database.write(
            "DELETE FROM fed_service_request WHERE updated_at<?", (service_cutoff,)
        )
        await self.database.write(
            "DELETE FROM fed_service_usage WHERE window_start<?", (provider_cache_cutoff,)
        )
        await self.database.write(
            "DELETE FROM env_cache WHERE fetched_at<?", (provider_cache_cutoff,)
        )
        await self.database.write(
            "DELETE FROM cap_point_cache WHERE fetched_at<?", (provider_cache_cutoff,)
        )
        await self.database.write("INSERT INTO post_fts(post_fts) VALUES('optimize')")
        await self.database.write("PRAGMA optimize")
        await self.database.write("PRAGMA incremental_vacuum")
        if self.config.store.backup.enabled:
            await self.backups.create()
        removed = self.backups.rotate(self.config.store.backup.keep)
        local_date = (
            self.clock.now().astimezone(ZoneInfo(self.config.node.timezone)).date().isoformat()
        )
        await self.database.write(
            """
            INSERT INTO runtime_setting(key,value,updated_at) VALUES('maintenance.last_date',?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at
            """,
            (json.dumps(local_date), now),
        )
        result = MaintenanceResult(
            threads=counts["threads"],
            mail=counts["mail"],
            messages=counts["messages"],
            kv=counts["kv"],
            safety_floor=counts["safety_floor"],
            federation_services=counts["federation_services"],
            federation_service_usage=counts["federation_service_usage"],
            environment_cache=counts["environment_cache"],
            alert_point_cache=counts["alert_point_cache"],
            backups_removed=removed,
        )
        await self.database.write(
            """
            INSERT INTO audit_log(actor_kind,actor_ref,action,target,detail,created_at)
            VALUES('system','maintenance','maintenance.run','database',?,?)
            """,
            (json.dumps(result.__dict__), now),
        )
        return result
