from __future__ import annotations

import asyncio
import json
import uuid
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any, Literal, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from prometheus_client import Gauge
from pydantic import BaseModel

from outpost.clock import Clock
from outpost.config import Config
from outpost.store import Database


class BackupInventory(Protocol):
    def list(self) -> list[dict[str, object]]: ...


class IntentInventory(Protocol):
    def status(self) -> dict[str, object]: ...


Severity = Literal["safety", "operations", "configuration"]


@dataclass(frozen=True)
class CheckDefinition:
    name: str
    severity: Severity
    title: str
    impact: str
    remediation: str


@dataclass(frozen=True)
class CheckResult:
    name: str
    severity: Severity
    title: str
    passed: bool
    detail: str
    impact: str
    remediation: str
    evidence: dict[str, object]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


CHECK_DEFINITIONS = (
    CheckDefinition(
        "responder_audience",
        "safety",
        "A responder can receive urgent help",
        "HELPME and targeted urgent alerts cannot reach another person.",
        "Review a known radio in Members and grant it Responder or Operator trust.",
    ),
    CheckDefinition(
        "escalation_audiences",
        "safety",
        "Every alert escalation audience has a recipient",
        "An alert can stall at a configured stage without notifying anyone.",
        "Promote an appropriate radio or change the affected escalation stage audience.",
    ),
    CheckDefinition(
        "maintenance_freshness",
        "operations",
        "Retention maintenance completed recently",
        "Retention, integrity housekeeping, and rotating snapshots may have stopped.",
        "Run maintenance from Backups and inspect the store-maintenance task if it fails.",
    ),
    CheckDefinition(
        "backup_rotation",
        "operations",
        "Backup rotation is within policy",
        "Unbounded snapshots can exhaust appliance storage.",
        "Run maintenance, then inspect backup rotation errors and filesystem permissions.",
    ),
    CheckDefinition(
        "alert_delivery_history",
        "safety",
        "Recent alert stages admitted at least one delivery",
        "A recent safety alert stage reached zero recipients.",
        "Open Mail for the failed delivery record, correct its audience, and retry the alert.",
    ),
    CheckDefinition(
        "intent_map",
        "configuration",
        "The tolerant command map parsed cleanly",
        "Natural-language command aliases may silently stop routing.",
        "Correct or restore router.intents_file, then run the readiness check again.",
    ),
    CheckDefinition(
        "configured_keys_effective",
        "configuration",
        "Explicit configuration keys have runtime consumers",
        "An accepted setting can appear active while changing no behavior.",
        "Remove the ineffective key or upgrade to a build that implements it.",
    ),
    CheckDefinition(
        "timezone",
        "configuration",
        "The configured timezone resolves",
        "Maintenance and local schedules cannot be evaluated reliably.",
        "Set node.timezone to an installed IANA timezone such as America/New_York.",
    ),
)
CHECK_NAMES = frozenset(check.name for check in CHECK_DEFINITIONS)
_DEFINITION = {check.name: check for check in CHECK_DEFINITIONS}

# Temporary denylist for any accepted strict-schema field found to lack a
# runtime consumer. It should remain empty; the source-reference test prevents
# newly declared fields from silently reaching a release.
KNOWN_INEFFECTIVE_CONFIG_KEYS: frozenset[str] = frozenset()

SELF_CHECK_STATE = Gauge(
    "outpost_self_check_state",
    "Whether a named Outpost readiness assertion currently passes",
    ("check", "severity"),
)
SELF_CHECK_LAST_RUN = Gauge(
    "outpost_self_check_last_run_timestamp_seconds",
    "Unix timestamp of the most recent readiness self-check",
)


def _configured_leaf_paths(value: object, prefix: tuple[str, ...] = ()) -> set[str]:
    paths: set[str] = set()
    if isinstance(value, BaseModel):
        for name in value.model_fields_set:
            child = getattr(value, name)
            child_prefix = (*prefix, name)
            nested = _configured_leaf_paths(child, child_prefix)
            if nested:
                paths.update(nested)
            else:
                paths.add(".".join(child_prefix))
    elif isinstance(value, dict):
        for child in value.values():
            paths.update(_configured_leaf_paths(child, prefix))
    elif isinstance(value, (list, tuple)):
        for child in value:
            paths.update(_configured_leaf_paths(child, prefix))
    return paths


class SelfCheckService:
    """Semantic readiness assertions over durable state and loaded policy."""

    history_days = 7

    def __init__(
        self,
        database: Database,
        config: Config,
        clock: Clock,
        backups: BackupInventory,
        intents: IntentInventory,
    ) -> None:
        self.database = database
        self.config = config
        self.clock = clock
        self.backups = backups
        self.intents = intents
        self._run_lock = asyncio.Lock()
        self._cached: dict[str, Any] = self._empty_report()

    @staticmethod
    def _empty_report() -> dict[str, Any]:
        return {
            "status": "never_run",
            "generated_at": None,
            "trigger": None,
            "safety_failures": 0,
            "failed_checks": [],
            "checks": [],
        }

    def snapshot(self) -> dict[str, Any]:
        return deepcopy(self._cached)

    async def latest(self) -> dict[str, Any]:
        if self._cached.get("generated_at") is not None:
            return self.snapshot()
        rows = await self.database.read(
            "SELECT value FROM runtime_setting WHERE key='readiness.self_check'"
        )
        if rows:
            try:
                value = json.loads(rows[0]["value"])
                if isinstance(value, dict):
                    self._cached = value
            except (json.JSONDecodeError, TypeError):
                pass
        return self.snapshot()

    @staticmethod
    def _result(
        name: str,
        passed: bool,
        detail: str,
        evidence: dict[str, object],
    ) -> CheckResult:
        definition = _DEFINITION[name]
        return CheckResult(
            name,
            definition.severity,
            definition.title,
            passed,
            detail,
            definition.impact,
            definition.remediation,
            evidence,
        )

    async def _responder_audience(self) -> CheckResult:
        rows = await self.database.read(
            "SELECT COUNT(*) count FROM member WHERE trust IN ('responder','operator') "
            "AND directory_state='active'"
        )
        count = int(rows[0]["count"])
        return self._result(
            "responder_audience",
            count > 0,
            f"{count} active radio{'s' if count != 1 else ''} can receive responder traffic.",
            {"recipient_count": count},
        )

    async def _escalation_audiences(self) -> CheckResult:
        rows = await self.database.read(
            "SELECT trust,COUNT(*) count FROM member WHERE directory_state='active' GROUP BY trust"
        )
        counts = {str(row["trust"]): int(row["count"]) for row in rows}
        missing: list[str] = []
        policies = self.config.watch.escalation
        for severity in ("caution", "urgent", "critical"):
            policy = getattr(policies, severity)
            for index, stage in enumerate(policy.stages):
                if stage.notify == "all":
                    recipients = 1
                elif stage.notify == "responders":
                    recipients = counts.get("responder", 0) + counts.get("operator", 0)
                else:
                    recipients = (
                        counts.get("trusted", 0)
                        + counts.get("responder", 0)
                        + counts.get("operator", 0)
                    )
                if recipients == 0:
                    missing.append(f"{severity}:{index}:{stage.notify}")
        return self._result(
            "escalation_audiences",
            not missing,
            (
                "Every configured escalation stage has a current audience."
                if not missing
                else f"{len(missing)} escalation stage(s) currently match no radio."
            ),
            {"missing_stages": missing},
        )

    async def _maintenance_freshness(self, now: int) -> CheckResult:
        rows = await self.database.read(
            "SELECT value,updated_at FROM runtime_setting WHERE key='maintenance.last_date'"
        )
        recorded_at = int(rows[0]["updated_at"]) if rows else None
        age = now - recorded_at if recorded_at is not None else None
        valid_value = False
        if rows:
            try:
                value = json.loads(rows[0]["value"])
                if isinstance(value, str):
                    date.fromisoformat(value)
                    valid_value = True
            except (json.JSONDecodeError, TypeError, ValueError):
                valid_value = False
        passed = bool(valid_value and age is not None and 0 <= age <= 48 * 3_600)
        detail = (
            f"Maintenance completed {age // 3_600} hour(s) ago."
            if age is not None and age >= 0
            else "No valid maintenance completion has been recorded."
        )
        return self._result(
            "maintenance_freshness",
            passed,
            detail,
            {"last_run_at": recorded_at, "age_seconds": age},
        )

    def _backup_rotation(self) -> CheckResult:
        policy = self.config.store.backup
        try:
            items = self.backups.list()
        except OSError as error:
            return self._result(
                "backup_rotation",
                False,
                f"Backup inventory could not be read ({type(error).__name__}).",
                {"file_count": None, "keep": policy.keep, "error_type": type(error).__name__},
            )
        scheduled = sum(item.get("kind", "scheduled") == "scheduled" for item in items)
        pre_upgrade = sum(item.get("kind") == "pre_upgrade" for item in items)
        passed = scheduled <= policy.keep and pre_upgrade <= policy.pre_upgrade_keep
        return self._result(
            "backup_rotation",
            passed,
            (
                f"{scheduled} scheduled and {pre_upgrade} pre-upgrade snapshot(s) are retained; "
                f"policy allows {policy.keep} and {policy.pre_upgrade_keep}."
            ),
            {
                "file_count": len(items),
                "scheduled_count": scheduled,
                "scheduled_keep": policy.keep,
                "pre_upgrade_count": pre_upgrade,
                "pre_upgrade_keep": policy.pre_upgrade_keep,
                "pre_rollback_days": policy.pre_rollback_days,
            },
        )

    async def _alert_delivery_history(self, now: int) -> CheckResult:
        cutoff = now - self.history_days * 86_400
        rows = await self.database.read(
            "SELECT target,created_at FROM audit_log WHERE action='safety.delivery.zero' "
            "AND target LIKE 'alert:%:stage:%' AND created_at>=? "
            "ORDER BY created_at DESC LIMIT 20",
            (cutoff,),
        )
        return self._result(
            "alert_delivery_history",
            not rows,
            (
                f"No zero-admission alert stage was recorded in {self.history_days} days."
                if not rows
                else f"{len(rows)} recent alert stage(s) admitted no delivery."
            ),
            {
                "history_days": self.history_days,
                "events": [
                    {"target": str(row["target"]), "created_at": int(row["created_at"])}
                    for row in rows
                ],
            },
        )

    def _intent_map(self) -> CheckResult:
        status = self.intents.status()
        exists = status.get("exists") is True
        error = status.get("error")
        rejected_value = status.get("rejected", 0)
        loaded_value = status.get("loaded", 0)
        rejected = int(rejected_value) if isinstance(rejected_value, (int, str)) else 0
        loaded = int(loaded_value) if isinstance(loaded_value, (int, str)) else 0
        passed = exists and error is None and rejected == 0
        detail = (
            f"Loaded {loaded} configured intent mapping(s); none rejected."
            if passed
            else (
                f"Loaded {loaded} mapping(s), rejected {rejected}; "
                f"{error or 'invalid entries found'}."
            )
        )
        return self._result("intent_map", passed, detail, status)

    def _configured_keys_effective(self) -> CheckResult:
        configured = _configured_leaf_paths(self.config)
        ineffective = sorted(configured & KNOWN_INEFFECTIVE_CONFIG_KEYS)
        return self._result(
            "configured_keys_effective",
            not ineffective,
            (
                f"All {len(configured)} explicitly configured leaf keys have audited consumers."
                if not ineffective
                else f"{len(ineffective)} explicitly configured key(s) have no runtime effect."
            ),
            {"configured_count": len(configured), "ineffective_keys": ineffective},
        )

    def _timezone(self) -> CheckResult:
        name = self.config.node.timezone
        try:
            ZoneInfo(name)
            passed = True
            detail = f"Timezone {name} is available."
        except (ZoneInfoNotFoundError, ValueError):
            passed = False
            detail = f"Timezone {name} is not available on this appliance."
        return self._result("timezone", passed, detail, {"timezone": name})

    async def _sync_inbox(self, results: list[CheckResult], now: int) -> None:
        for result in results:
            if result.severity != "safety":
                continue
            conversation_key = f"system:self-check:{result.name}"
            if result.passed:
                await self.database.write(
                    "UPDATE mail SET state='delivered',delivered_at=?,"
                    "operator_read_at=COALESCE(operator_read_at,?) "
                    "WHERE conversation_key=? AND state='failed'",
                    (now, now, conversation_key),
                )
                continue
            body = f"{result.detail} Impact: {result.impact} Remediation: {result.remediation}"
            existing = await self.database.read(
                "SELECT id FROM mail WHERE conversation_key=? LIMIT 1", (conversation_key,)
            )
            if existing:
                await self.database.write(
                    "UPDATE mail SET subject=?,body=?,created_at=?,state='failed',"
                    "delivered_at=NULL,operator_read_at=NULL,archived_at=NULL,expires_at=? "
                    "WHERE id=?",
                    (
                        f"Readiness failed: {result.title}",
                        body,
                        now,
                        now + 30 * 86_400,
                        existing[0]["id"],
                    ),
                )
            else:
                await self.database.write(
                    "INSERT INTO mail(uid,from_label,to_label,subject,body,created_at,state,"
                    "expires_at,conversation_key,message_kind,mail_direction,"
                    "participant_handle,operator_actor) VALUES(?,?,?,?,?,?,'failed',?,?,"
                    "'system','local','outpost','system:self-check')",
                    (
                        str(uuid.uuid4()),
                        "outpost",
                        "operator",
                        f"Readiness failed: {result.title}",
                        body,
                        now,
                        now + 30 * 86_400,
                        conversation_key,
                    ),
                )

    async def run(self, trigger: str = "manual") -> dict[str, Any]:
        async with self._run_lock:
            now = int(self.clock.now().timestamp())
            results = [
                await self._responder_audience(),
                await self._escalation_audiences(),
                await self._maintenance_freshness(now),
                self._backup_rotation(),
                await self._alert_delivery_history(now),
                self._intent_map(),
                self._configured_keys_effective(),
                self._timezone(),
            ]
            failed = [result for result in results if not result.passed]
            safety_failures = sum(result.severity == "safety" for result in failed)
            report = {
                "status": "failed" if safety_failures else "degraded" if failed else "ready",
                "generated_at": now,
                "trigger": trigger[:40],
                "safety_failures": safety_failures,
                "failed_checks": [result.name for result in failed],
                "checks": [result.as_dict() for result in results],
            }
            await self.database.write(
                "INSERT INTO runtime_setting(key,value,updated_at) "
                "VALUES('readiness.self_check',?,?) ON CONFLICT(key) DO UPDATE SET "
                "value=excluded.value,updated_at=excluded.updated_at",
                (json.dumps(report, separators=(",", ":")), now),
            )
            await self._sync_inbox(results, now)
            for result in results:
                SELF_CHECK_STATE.labels(result.name, result.severity).set(int(result.passed))
            SELF_CHECK_LAST_RUN.set(now)
            self._cached = report
            return self.snapshot()
