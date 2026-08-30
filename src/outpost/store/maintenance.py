from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import asdict, dataclass
from typing import Any
from zoneinfo import ZoneInfo

from outpost.clock import Clock
from outpost.config import Config

from .backups import BackupService
from .database import Database

DAY = 86_400
HOUR = 3_600


@dataclass(frozen=True)
class TablePolicy:
    table: str
    domain: str
    policy: str
    detail: str
    protected: bool = False


# This is deliberately exhaustive for product-owned tables. FTS5 shadow tables are grouped
# with post_fts because they are an implementation detail and follow their parent records.
TABLE_POLICIES = (
    TablePolicy("schema_version", "system", "preserve", "Migration evidence; bounded by releases."),
    TablePolicy("runtime_setting", "system", "preserve", "Current durable configuration only."),
    TablePolicy("web_credential", "system", "preserve", "Legacy setup credential bridge.", True),
    TablePolicy("web_account", "system", "preserve", "Named operator identities and roles.", True),
    TablePolicy("web_session", "system", "expire", "Delete immediately after session expiry."),
    TablePolicy("web_login_attempt", "system", "retain", "Authentication history retention."),
    TablePolicy("message_log", "system", "compact", "Age limit plus an absolute row ceiling."),
    TablePolicy("outbound_work", "system", "retain", "Terminal work only; active work is durable."),
    TablePolicy("outbound_attempt", "system", "cascade", "Follows its durable outbound item."),
    TablePolicy(
        "safety_floor_attempt", "system", "retain", "Short replay/coalescing safety window."
    ),
    TablePolicy("kv", "system", "expire", "Only entries with an elapsed expiry are removed."),
    TablePolicy(
        "audit_log",
        "system",
        "preserve",
        "Security and operator evidence is never aged out.",
        True,
    ),
    TablePolicy(
        "member",
        "directory",
        "preserve",
        "Identity and trust history requires operator action.",
        True,
    ),
    TablePolicy(
        "member_trust_history",
        "directory",
        "preserve",
        "Reviewed trust-change evidence is never aged out.",
        True,
    ),
    TablePolicy(
        "member_position",
        "directory",
        "expire",
        "Exact positions expire on their stored deadline.",
        True,
    ),
    TablePolicy("channel_dir", "directory", "preserve", "Operator-managed directory."),
    TablePolicy("board", "community", "preserve", "Operator-managed configuration."),
    TablePolicy("thread", "community", "retain", "Unpinned threads use board/global retention."),
    TablePolicy("post", "community", "cascade", "Follows its thread."),
    TablePolicy(
        "post_fts*",
        "community",
        "compact",
        "Search index follows posts; bounded merges reclaim it.",
    ),
    TablePolicy("read_marker", "community", "preserve", "Bounded by member and scope."),
    TablePolicy("subscription", "community", "preserve", "Operator/member preference."),
    TablePolicy("mail", "community", "retain", "Message age or explicit delivery expiry."),
    TablePolicy("digest_state", "community", "preserve", "One cursor per member and cadence."),
    TablePolicy("digest_delivery_log", "community", "retain", "Digest delivery history retention."),
    TablePolicy(
        "pending_incident_location",
        "watch",
        "expire",
        "Delete immediately after workflow expiry.",
        True,
    ),
    TablePolicy(
        "incident", "watch", "retain", "Terminal incidents only; active incidents protected."
    ),
    TablePolicy("incident_update", "watch", "cascade", "Follows its incident."),
    TablePolicy("incident_origin", "watch", "cascade", "Follows its retained incident."),
    TablePolicy("incident_provenance", "watch", "cascade", "Follows its retained incident."),
    TablePolicy(
        "incident_match_decision", "watch", "cascade", "Follows either referenced incident."
    ),
    TablePolicy("alert", "watch", "retain", "Only concluded or expired alerts age out."),
    TablePolicy("alert_ack", "watch", "cascade", "Follows its alert."),
    TablePolicy("alert_audience", "watch", "cascade", "Follows its alert."),
    TablePolicy("watch_event", "watch", "retain", "Closed events only; open events protected."),
    TablePolicy("checkin", "watch", "retain", "Welfare history retention.", True),
    TablePolicy("checkin_solicitation", "watch", "cascade", "Follows its watch event."),
    TablePolicy("env_cache", "environment", "expire", "Short provider cache retention."),
    TablePolicy("cap_point_cache", "environment", "expire", "Short provider cache retention."),
    TablePolicy("cap_alert", "environment", "retain", "CAP review/history retention."),
    TablePolicy("earthquake", "environment", "retain", "Earthquake review/history retention."),
    TablePolicy("same_event", "environment", "retain", "SAME event history retention."),
    TablePolicy("waypoint", "environment", "preserve", "Operator-managed reference data."),
    TablePolicy(
        "fed_peer",
        "federation",
        "preserve",
        "Trust and pairing state requires operator action.",
        True,
    ),
    TablePolicy(
        "fed_peer_successor", "federation", "preserve", "Identity adoption evidence.", True
    ),
    TablePolicy("fed_cursor", "federation", "preserve", "Bounded cursor per peer and stream."),
    TablePolicy("fed_service_circuit", "federation", "preserve", "Bounded state per peer/service."),
    TablePolicy(
        "fed_seen", "federation", "retain", "Replay/deduplication history retention.", True
    ),
    TablePolicy(
        "fed_outbox", "federation", "retain", "Sent/long-expired frames; live work protected."
    ),
    TablePolicy(
        "fed_service_request", "federation", "retain", "Service request/result retention.", True
    ),
    TablePolicy("fed_service_usage", "federation", "retain", "Short rolling quota windows."),
    TablePolicy(
        "fed_inbox_item", "federation", "retain", "Reviewed records; approval queue protected."
    ),
    TablePolicy(
        "fed_mail_delivery", "federation", "retain", "Terminal deliveries; live relay protected."
    ),
    TablePolicy(
        "fed_post_delivery", "federation", "retain", "Delivered posts; reconciliation protected."
    ),
)

DOMAIN_LABELS = {
    "system": "System & security",
    "directory": "Members & directory",
    "community": "BBS & mail",
    "watch": "Watch & incidents",
    "environment": "Environment",
    "federation": "Federation",
}

# Internal FTS tables and SQLite's schema allocation still need a storage owner.
INTERNAL_TABLE_DOMAINS = {
    "sqlite_schema": "system",
    "post_fts": "community",
    "post_fts_config": "community",
    "post_fts_content": "community",
    "post_fts_data": "community",
    "post_fts_docsize": "community",
    "post_fts_idx": "community",
}


@dataclass(frozen=True)
class CleanupRule:
    key: str
    label: str
    domain: str
    table: str
    predicate: str
    params: tuple[Any, ...]


@dataclass(frozen=True)
class CleanupEstimate:
    key: str
    label: str
    domain: str
    table: str
    rows: int
    estimated_bytes: int


@dataclass(frozen=True)
class MaintenancePreview:
    generated_at: int
    total_rows: int
    estimated_bytes: int
    rules: tuple[CleanupEstimate, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "total_rows": self.total_rows,
            "estimated_bytes": self.estimated_bytes,
            "rules": [asdict(rule) for rule in self.rules],
        }


@dataclass(frozen=True)
class MaintenanceResult:
    eligible: dict[str, int]
    removed: dict[str, int]
    estimated_bytes: int
    backups_removed: int
    limited: bool
    batch_rows: int
    max_rows: int
    failures: dict[str, str]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def _removed(self, key: str) -> int:
        return int(self.removed.get(key, 0))

    # Compatibility properties retain the original service result surface.
    @property
    def threads(self) -> int:
        return self._removed("threads")

    @property
    def mail(self) -> int:
        return self._removed("mail")

    @property
    def member_positions(self) -> int:
        return self._removed("member_positions")

    @property
    def pending_positions(self) -> int:
        return self._removed("pending_positions")

    @property
    def messages(self) -> int:
        return self._removed("messages")

    @property
    def kv(self) -> int:
        return self._removed("kv")

    @property
    def safety_floor(self) -> int:
        return self._removed("safety_floor")

    @property
    def federation_services(self) -> int:
        return self._removed("federation_services")

    @property
    def federation_service_usage(self) -> int:
        return self._removed("federation_service_usage")

    @property
    def environment_cache(self) -> int:
        return self._removed("environment_cache")

    @property
    def alert_point_cache(self) -> int:
        return self._removed("alert_point_cache")


class MaintenanceService:
    def __init__(
        self, database: Database, backups: BackupService, clock: Clock, config: Config
    ) -> None:
        self.database, self.backups, self.clock, self.config = database, backups, clock, config
        self._run_lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self._run_lock.locked()

    async def due(self) -> bool:
        local = self.clock.now().astimezone(ZoneInfo(self.config.node.timezone))
        if local.hour < self.config.store.maintenance_hour:
            return False
        rows = await self.database.read(
            "SELECT value FROM runtime_setting WHERE key='maintenance.last_date'"
        )
        return not rows or json.loads(rows[0]["value"]) != local.date().isoformat()

    async def health(self) -> dict[str, Any]:
        rows = await self.database.read(
            "SELECT value FROM runtime_setting WHERE key='maintenance.last_health'"
        )
        if not rows:
            return {"status": "never_run", "completed_at": None, "failures": {}}
        try:
            value = json.loads(rows[0]["value"])
        except (TypeError, json.JSONDecodeError):
            return {
                "status": "degraded",
                "completed_at": None,
                "failures": {"health_record": "Stored maintenance health is invalid."},
            }
        return (
            value
            if isinstance(value, dict)
            else {
                "status": "degraded",
                "completed_at": None,
                "failures": {"health_record": "Stored maintenance health is invalid."},
            }
        )

    async def audit_cleanup_foreign_keys(self) -> list[str]:
        """Return cleanup-target foreign keys without an explicit deletion policy."""
        targets = {rule.table for rule in self._rules(int(self.clock.now().timestamp()))}
        tables = await self.database.read(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
        trigger_rows = await self.database.read(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        )
        triggers = {str(row["name"]) for row in trigger_rows}
        managed = {("incident", "merged_into_id", "incident"): "incident_detach_merged_children"}
        missing: list[str] = []
        for table_row in tables:
            table = str(table_row["name"])
            rows = await self.database.read(f'PRAGMA foreign_key_list("{table}")')  # noqa: S608
            for row in rows:
                parent = str(row["table"])
                if parent not in targets or str(row["on_delete"]).upper() != "NO ACTION":
                    continue
                key = (table, str(row["from"]), parent)
                required_trigger = managed.get(key)
                if required_trigger is None or required_trigger not in triggers:
                    missing.append(f"{table}.{row['from']} -> {parent}")
        return sorted(missing)

    def _rules(self, now: int) -> tuple[CleanupRule, ...]:
        retention = self.config.store.retention
        auth_cutoff = now - retention.authentication_days * DAY
        digest_cutoff = now - retention.digest_days * DAY
        incident_cutoff = now - retention.incident_history_days * DAY
        watch_cutoff = now - retention.watch_history_days * DAY
        environment_cutoff = now - retention.environment_history_days * DAY
        provider_cutoff = now - retention.provider_cache_days * DAY
        service_cutoff = now - retention.federation_service_days * DAY
        federation_cutoff = now - retention.federation_history_days * DAY
        outbound_cutoff = now - retention.outbound_history_days * DAY
        return (
            CleanupRule(
                "member_positions",
                "Expired member positions",
                "directory",
                "member_position",
                "expires_at<=?",
                (now,),
            ),
            CleanupRule(
                "pending_positions",
                "Expired pending report positions",
                "watch",
                "pending_incident_location",
                "expires_at<=?",
                (now,),
            ),
            CleanupRule(
                "web_sessions",
                "Expired web sessions",
                "system",
                "web_session",
                "expires_at<=?",
                (now,),
            ),
            CleanupRule(
                "kv",
                "Expired transient state",
                "system",
                "kv",
                "expires_at IS NOT NULL AND expires_at<=?",
                (now,),
            ),
            CleanupRule(
                "safety_floor",
                "Elapsed safety replay windows",
                "system",
                "safety_floor_attempt",
                "last_seen_at<?",
                (now - self.config.security.safety_attempt_retention_hours * HOUR,),
            ),
            CleanupRule(
                "web_login_attempts",
                "Authentication attempt history",
                "system",
                "web_login_attempt",
                "created_at<?",
                (auth_cutoff,),
            ),
            CleanupRule(
                "environment_cache",
                "Stale provider cache",
                "environment",
                "env_cache",
                "fetched_at<?",
                (provider_cutoff,),
            ),
            CleanupRule(
                "alert_point_cache",
                "Stale alert-point cache",
                "environment",
                "cap_point_cache",
                "fetched_at<?",
                (provider_cutoff,),
            ),
            CleanupRule(
                "federation_service_usage",
                "Elapsed peer quota windows",
                "federation",
                "fed_service_usage",
                "window_start<?",
                (provider_cutoff,),
            ),
            CleanupRule(
                "federation_services",
                "Completed/expired peer service requests",
                "federation",
                "fed_service_request",
                "(status<>'pending' AND updated_at<?) OR expires_at<?",
                (service_cutoff, service_cutoff),
            ),
            CleanupRule(
                "federation_seen",
                "Federation replay/dedupe history",
                "federation",
                "fed_seen",
                "seen_at<?",
                (federation_cutoff,),
            ),
            CleanupRule(
                "federation_outbox",
                "Completed/long-expired federation frames",
                "federation",
                "fed_outbox",
                "(sent_at IS NOT NULL OR expires_at<?) AND COALESCE(sent_at,expires_at)<?",
                (now, federation_cutoff),
            ),
            CleanupRule(
                "federation_inbox",
                "Reviewed federation quarantine records",
                "federation",
                "fed_inbox_item",
                "state IN ('imported','rejected') AND COALESCE(reviewed_at,received_at)<?",
                (federation_cutoff,),
            ),
            CleanupRule(
                "federation_mail",
                "Terminal federation mail deliveries",
                "federation",
                "fed_mail_delivery",
                "state IN ('delivered','failed','expired') AND updated_at<?",
                (federation_cutoff,),
            ),
            CleanupRule(
                "federation_posts",
                "Delivered federation post receipts",
                "federation",
                "fed_post_delivery",
                "state='delivered' AND updated_at<?",
                (federation_cutoff,),
            ),
            CleanupRule(
                "outbound_work",
                "Terminal durable outbound work",
                "system",
                "outbound_work",
                "state IN ('sent','acked','failed','expired','cancelled','superseded','retracted') "
                "AND COALESCE(completed_at,expires_at)<?",
                (outbound_cutoff,),
            ),
            CleanupRule(
                "digest_deliveries",
                "Digest delivery history",
                "community",
                "digest_delivery_log",
                "created_at<?",
                (digest_cutoff,),
            ),
            CleanupRule(
                "checkins",
                "Welfare check-in history",
                "watch",
                "checkin",
                "created_at<?",
                (watch_cutoff,),
            ),
            CleanupRule(
                "watch_events",
                "Closed welfare events",
                "watch",
                "watch_event",
                "closed_at IS NOT NULL AND closed_at<?",
                (watch_cutoff,),
            ),
            CleanupRule(
                "alerts",
                "Concluded alert history",
                "watch",
                "alert",
                "COALESCE(cancelled_at,all_clear_at,expires_at) IS NOT NULL "
                "AND COALESCE(cancelled_at,all_clear_at,expires_at)<?",
                (watch_cutoff,),
            ),
            CleanupRule(
                "incidents",
                "Terminal incident history",
                "watch",
                "incident",
                "(merged_into_id IS NOT NULL AND updated_at<?) OR "
                "(status IN ('resolved','false_alarm','expired') "
                "AND COALESCE(resolved_at,expires_at,updated_at)<? "
                "AND NOT EXISTS (SELECT 1 FROM incident child "
                "WHERE child.merged_into_id=incident.id))",
                (incident_cutoff, incident_cutoff),
            ),
            CleanupRule(
                "cap_alerts",
                "CAP review history",
                "environment",
                "cap_alert",
                "updated_at<?",
                (environment_cutoff,),
            ),
            CleanupRule(
                "earthquakes",
                "Earthquake review history",
                "environment",
                "earthquake",
                "updated_at<?",
                (environment_cutoff,),
            ),
            CleanupRule(
                "same_events",
                "SAME event history",
                "environment",
                "same_event",
                "received_at<?",
                (environment_cutoff,),
            ),
            CleanupRule(
                "mail",
                "Expired/aged mail",
                "community",
                "mail",
                "expires_at<=? OR created_at<?",
                (now, now - retention.mail_days * DAY),
            ),
            CleanupRule(
                "threads",
                "Unpinned BBS threads",
                "community",
                "thread",
                "pinned=0 AND last_post_at < ? - COALESCE("
                "(SELECT b.retention_days FROM board b WHERE b.id=thread.board_id),?)*86400",
                (now, retention.posts_days),
            ),
            CleanupRule(
                "messages",
                "Packet/message history",
                "system",
                "message_log",
                "created_at<? OR id IN ("
                "SELECT id FROM message_log ORDER BY id DESC LIMIT -1 OFFSET ?)",
                (now - retention.message_log_days * DAY, retention.message_log_max_rows),
            ),
        )

    @staticmethod
    def _record_tables(existing: set[str]) -> tuple[str, ...]:
        names = {policy.table for policy in TABLE_POLICIES if not policy.table.endswith("*")}
        names.discard("post_fts")
        return tuple(sorted(names & existing))

    async def _inventory(self) -> tuple[dict[str, int], dict[str, int]]:
        schema = await self.database.read(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
        existing = {str(row["name"]) for row in schema}
        record_tables = self._record_tables(existing)
        row_counts: dict[str, int] = {}
        if record_tables:
            sql = " UNION ALL ".join(
                f"SELECT '{table}' table_name,COUNT(*) row_count FROM \"{table}\""  # noqa: S608
                for table in record_tables
            )
            row_counts = {
                str(row["table_name"]): int(row["row_count"])
                for row in await self.database.read(sql)
            }

        table_bytes: dict[str, int] = {}
        try:
            rows = await self.database.read(
                """
                SELECT COALESCE(m.tbl_name,d.name) table_name,SUM(d.pgsize) size_bytes
                FROM dbstat d LEFT JOIN sqlite_master m ON m.name=d.name
                GROUP BY COALESCE(m.tbl_name,d.name)
                """
            )
            table_bytes = {str(row["table_name"]): int(row["size_bytes"] or 0) for row in rows}
        except Exception:  # pragma: no cover - optional in downstream SQLite builds.
            table_bytes = {}
        return row_counts, table_bytes

    async def _preview(
        self,
        now: int,
        inventory: tuple[dict[str, int], dict[str, int]] | None = None,
    ) -> MaintenancePreview:
        row_counts, table_bytes = inventory or await self._inventory()
        estimates: list[CleanupEstimate] = []
        for rule in self._rules(now):
            rows = await self.database.read(
                f'SELECT COUNT(*) count FROM "{rule.table}" WHERE {rule.predicate}',  # noqa: S608
                rule.params,
            )
            count = int(rows[0]["count"])
            allocated = table_bytes.get(rule.table, 0)
            total = row_counts.get(rule.table, 0)
            estimated = round(allocated * count / total) if count and total else 0
            estimates.append(
                CleanupEstimate(
                    rule.key,
                    rule.label,
                    rule.domain,
                    rule.table,
                    count,
                    estimated,
                )
            )
        return MaintenancePreview(
            generated_at=now,
            total_rows=sum(item.rows for item in estimates),
            estimated_bytes=sum(item.estimated_bytes for item in estimates),
            rules=tuple(estimates),
        )

    async def preview(self) -> MaintenancePreview:
        return await self._preview(int(self.clock.now().timestamp()))

    async def storage_report(self) -> dict[str, Any]:
        now = int(self.clock.now().timestamp())
        inventory = await self._inventory()
        row_counts, table_bytes = inventory
        table_domains = {
            policy.table: policy.domain
            for policy in TABLE_POLICIES
            if not policy.table.endswith("*")
        }
        table_domains.update(INTERNAL_TABLE_DOMAINS)
        domains: dict[str, dict[str, Any]] = {
            key: {"key": key, "label": label, "rows": 0, "size_bytes": 0}
            for key, label in DOMAIN_LABELS.items()
        }
        for table, count in row_counts.items():
            domain = table_domains.get(table, "system")
            domains[domain]["rows"] += count
        for table, size in table_bytes.items():
            domain = table_domains.get(table, "system")
            domains[domain]["size_bytes"] += size

        prior_rows = await self.database.read(
            "SELECT value,updated_at FROM runtime_setting WHERE key='maintenance.storage_snapshot'"
        )
        prior: dict[str, Any] = {}
        growth_since: int | None = None
        if prior_rows:
            try:
                prior = dict(json.loads(prior_rows[0]["value"]))
                growth_since = int(prior.get("captured_at", prior_rows[0]["updated_at"]))
            except (TypeError, ValueError, json.JSONDecodeError):
                prior = {}
        prior_value = prior.get("domains")
        prior_domains = prior_value if isinstance(prior_value, dict) else {}
        for key, item in domains.items():
            previous = prior_domains.get(key)
            if isinstance(previous, dict):
                item["growth_rows"] = int(item["rows"]) - int(previous.get("rows", 0))
                item["growth_bytes"] = int(item["size_bytes"]) - int(previous.get("size_bytes", 0))
            else:
                item["growth_rows"] = None
                item["growth_bytes"] = None

        database_path = self.database.path
        backup_items = self.backups.list()
        disk = shutil.disk_usage(database_path.parent)
        last_rows = await self.database.read(
            "SELECT value FROM runtime_setting WHERE key='maintenance.last_date'"
        )
        last_maintenance = None
        if last_rows:
            try:
                last_maintenance = json.loads(last_rows[0]["value"])
            except (TypeError, json.JSONDecodeError):
                last_maintenance = None
        preview = await self._preview(now, inventory)
        wal_path = database_path.with_name(f"{database_path.name}-wal")
        shm_path = database_path.with_name(f"{database_path.name}-shm")
        return {
            "generated_at": now,
            "running": self.running,
            "database_bytes": database_path.stat().st_size if database_path.exists() else 0,
            "wal_bytes": wal_path.stat().st_size if wal_path.exists() else 0,
            "shm_bytes": shm_path.stat().st_size if shm_path.exists() else 0,
            "backup_bytes": sum(int(str(item["size_bytes"])) for item in backup_items),
            "backup_count": len(backup_items),
            "disk_free_bytes": disk.free,
            "disk_total_bytes": disk.total,
            "growth_since": growth_since,
            "last_maintenance": last_maintenance,
            "maintenance_health": await self.health(),
            "next_maintenance_hour": self.config.store.maintenance_hour,
            "domains": list(domains.values()),
            "cleanup": preview.as_dict(),
            "policies": [asdict(policy) for policy in TABLE_POLICIES],
            "audit_policy": "preserve_forever",
            "estimates_are_approximate": True,
        }

    async def _delete_batch(self, rule: CleanupRule, limit: int) -> int:
        async with self.database.transaction() as transaction:
            rows = await transaction.read(
                f'SELECT rowid rid FROM "{rule.table}" WHERE {rule.predicate} '  # noqa: S608
                "ORDER BY rowid LIMIT ?",
                (*rule.params, limit),
            )
            if not rows:
                return 0
            ids = tuple(int(row["rid"]) for row in rows)
            placeholders = ",".join("?" for _ in ids)
            await transaction.write(
                f'DELETE FROM "{rule.table}" WHERE rowid IN ({placeholders})',  # noqa: S608
                ids,
            )
        return len(ids)

    async def _save_storage_snapshot(self, report: dict[str, Any], now: int) -> None:
        snapshot = {
            "captured_at": now,
            "domains": {
                str(item["key"]): {
                    "rows": int(item["rows"]),
                    "size_bytes": int(item["size_bytes"]),
                }
                for item in report["domains"]
            },
        }
        await self.database.write(
            """
            INSERT INTO runtime_setting(key,value,updated_at)
            VALUES('maintenance.storage_snapshot',?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at
            """,
            (json.dumps(snapshot, separators=(",", ":")), now),
        )

    async def run(
        self, *, actor_kind: str = "system", actor_ref: str = "maintenance"
    ) -> MaintenanceResult:
        if self._run_lock.locked():
            raise RuntimeError("Maintenance is already running.")
        async with self._run_lock:
            now = int(self.clock.now().timestamp())
            preview = await self._preview(now)
            rules = self._rules(now)
            eligible = {item.key: item.rows for item in preview.rules}
            removed = {rule.key: 0 for rule in rules}
            failures: dict[str, str] = {}
            batch_rows = self.config.store.maintenance_batch_rows
            max_rows = self.config.store.maintenance_max_rows

            undefined_foreign_keys = await self.audit_cleanup_foreign_keys()
            if undefined_foreign_keys:
                failures["foreign_key_policy"] = "; ".join(undefined_foreign_keys)[:500]

            # Snapshot before cleanup so a bad local policy remains recoverable.
            backups_removed = self.backups.rotate(self.config.store.backup.keep)
            backup_ready = not self.config.store.backup.enabled
            if self.config.store.backup.enabled:
                try:
                    await self.backups.create()
                    backup_ready = True
                except Exception as error:
                    failures["backup"] = f"{type(error).__name__}: {error}"[:500]
                finally:
                    backups_removed += self.backups.rotate(self.config.store.backup.keep)

            active = list(rules) if backup_ready else []
            total_removed = 0
            while active and total_removed < max_rows:
                next_round: list[CleanupRule] = []
                for rule in active:
                    remaining = max_rows - total_removed
                    if remaining <= 0:
                        next_round.append(rule)
                        continue
                    try:
                        count = await self._delete_batch(rule, min(batch_rows, remaining))
                    except Exception as error:
                        failures[rule.key] = f"{type(error).__name__}: {error}"[:500]
                        await asyncio.sleep(0)
                        continue
                    removed[rule.key] += count
                    total_removed += count
                    if count > 0:
                        next_round.append(rule)
                    await asyncio.sleep(0)
                active = next_round

            # FTS merge touches at most 16 pages and vacuum at most 200 free pages. Keep each
            # operation isolated so a local optimization cannot suppress the others.
            for key, statement in (
                ("fts_merge", "INSERT INTO post_fts(post_fts,rank) VALUES('merge',16)"),
                ("optimize", "PRAGMA optimize"),
                ("vacuum", "PRAGMA incremental_vacuum(200)"),
            ):
                try:
                    await self.database.write(statement)
                except Exception as error:
                    failures[key] = f"{type(error).__name__}: {error}"[:500]
            try:
                backups_removed += self.backups.rotate(self.config.store.backup.keep)
            except Exception as error:
                failures["backup_rotation"] = f"{type(error).__name__}: {error}"[:500]
            local_date = (
                self.clock.now().astimezone(ZoneInfo(self.config.node.timezone)).date().isoformat()
            )
            await self.database.write(
                """
                INSERT INTO runtime_setting(key,value,updated_at)
                VALUES('maintenance.last_date',?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at
                """,
                (json.dumps(local_date), now),
            )
            result = MaintenanceResult(
                eligible=eligible,
                removed=removed,
                estimated_bytes=preview.estimated_bytes,
                backups_removed=backups_removed,
                limited=sum(eligible.values()) > total_removed or bool(failures),
                batch_rows=batch_rows,
                max_rows=max_rows,
                failures=failures,
            )
            health = {
                "status": "degraded" if failures else "healthy",
                "completed_at": now,
                "failures": failures,
            }
            await self.database.write(
                """
                INSERT INTO runtime_setting(key,value,updated_at)
                VALUES('maintenance.last_health',?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at
                """,
                (json.dumps(health, separators=(",", ":")), now),
            )
            for key, detail in failures.items():
                await self.database.write(
                    """
                    INSERT INTO audit_log(actor_kind,actor_ref,action,target,detail,created_at)
                    VALUES(?,?,'maintenance.rule_failed',?,?,?)
                    """,
                    (actor_kind, actor_ref, key, detail, now),
                )
            await self.database.write(
                """
                INSERT INTO audit_log(actor_kind,actor_ref,action,target,detail,created_at)
                VALUES(?,?,'maintenance.run','database',?,?)
                """,
                (
                    actor_kind,
                    actor_ref,
                    json.dumps(result.as_dict(), separators=(",", ":")),
                    now,
                ),
            )
            await self._save_storage_snapshot(await self.storage_report(), now)
            return result
