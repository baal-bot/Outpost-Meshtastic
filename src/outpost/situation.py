from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal, Protocol

from outpost.clock import Clock
from outpost.router.models import TrustLevel
from outpost.store import Database

NARRATION_NAMESPACE = "situation_narration"
NARRATION_FAILURE_TTL = 5 * 60
NARRATION_SUCCESS_TTL = 24 * 60 * 60
NARRATION_CACHE_MAX = 64
SECTION_ORDER = (
    "alerts",
    "incidents",
    "welfare",
    "weather",
    "community",
    "delivery",
    "network",
)
SECTION_LIMITS = {
    "alerts": 6,
    "incidents": 6,
    "welfare": 4,
    "weather": 5,
    "community": 5,
    "delivery": 12,
    "network": 4,
}
STALE_AFTER = {
    "alerts": 6 * 60 * 60,
    "incidents": 24 * 60 * 60,
    "welfare": 6 * 60 * 60,
    "weather": 6 * 60 * 60,
    "community": 24 * 60 * 60,
    "delivery": 24 * 60 * 60,
    "network": 5 * 60,
}
DAY = 86_400
DELIVERY_BASELINE_DAYS = 14
DELIVERY_CURRENT_MIN = 5
DELIVERY_BASELINE_MIN = 20
DELIVERY_RATE_DROP_POINTS = 10.0
SNR_CURRENT_MIN = 3
SNR_BASELINE_MIN = 6
SNR_DROP_DB = 6.0
COORDINATE_PAIR = re.compile(
    r"(?<![\d.])"
    r"[+-]?(?:90(?:\.0+)?|(?:[0-8]?\d)(?:\.\d+)?)"
    r"\s*,\s*"
    r"[+-]?(?:180(?:\.0+)?|(?:1[0-7]\d|[0-9]?\d)(?:\.\d+)?)"
    r"(?![\d.])"
)
COORDINATE_SPACE_PAIR = re.compile(
    r"(?<![\d.])[+-]?(?:[0-8]?\d)\.\d+\s+[+-]?(?:1[0-7]\d|[0-9]?\d)\.\d+(?![\d.])"
)
URL = re.compile(r"https?://\S+", re.IGNORECASE)


class BriefingCapability(StrEnum):
    PUBLIC = "public"
    MEMBER = "member"
    RESPONDER = "responder"
    OPERATOR = "operator"

    @classmethod
    def from_trust(cls, trust: str) -> BriefingCapability:
        level = TrustLevel.parse(trust)
        if level >= TrustLevel.OPERATOR:
            return cls.OPERATOR
        if level >= TrustLevel.RESPONDER:
            return cls.RESPONDER
        return cls.MEMBER

    @property
    def trust(self) -> TrustLevel:
        return {
            BriefingCapability.PUBLIC: TrustLevel.GUEST,
            BriefingCapability.MEMBER: TrustLevel.MEMBER,
            BriefingCapability.RESPONDER: TrustLevel.RESPONDER,
            BriefingCapability.OPERATOR: TrustLevel.OPERATOR,
        }[self]


@dataclass(frozen=True)
class BriefingViewer:
    """A durable human identity whose own briefing cursor may advance."""

    kind: Literal["member", "web_account"]
    id: int

    def __post_init__(self) -> None:
        if self.kind not in {"member", "web_account"}:
            raise ValueError("Unknown briefing viewer kind.")
        if self.id < 1:
            raise ValueError("Briefing viewer IDs must be positive.")


class SituationNarrator(Protocol):
    async def narrate_situation(
        self, snapshot: dict[str, Any], required_refs: tuple[str, ...]
    ) -> tuple[str | None, str]: ...


@dataclass(frozen=True)
class BriefingSource:
    id: str
    label: str
    observed_at: int | None
    stale_after_seconds: int
    href: str
    stale: bool = False
    conflict: bool = False

    def json(self, now: int) -> dict[str, Any]:
        value = asdict(self)
        value["age_seconds"] = max(0, now - self.observed_at) if self.observed_at is not None else 0
        value["age"] = _age(value["age_seconds"])
        return value

    def fact(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BriefingItem:
    id: str
    ref: str
    section: str
    severity: str
    title: str
    detail: str
    state: str
    source_ids: tuple[str, ...]
    href: str
    hazard: bool = False
    uncertainty: str | None = None

    def json(self) -> dict[str, Any]:
        value = asdict(self)
        value["source_ids"] = list(self.source_ids)
        return value


@dataclass(frozen=True)
class BriefingChange:
    kind: str
    item_id: str
    ref: str
    section: str
    title: str
    href: str

    def json(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class _SnapshotSelection:
    prior: dict[str, Any] | None
    current_id: int
    change_window: dict[str, Any]


def _safe_text(value: object, limit: int = 140) -> str:
    text = " ".join(str(value or "").split())
    text = COORDINATE_PAIR.sub("[location withheld]", text)
    text = COORDINATE_SPACE_PAIR.sub("[location withheld]", text)
    text = URL.sub("[link withheld]", text)
    return text[:limit].rstrip()


def _age(seconds: object) -> str:
    try:
        value = max(0, int(str(seconds or 0)))
    except ValueError:
        value = 0
    if value < 60:
        return "now"
    if value < 3600:
        return f"{value // 60}m"
    if value < 86_400:
        return f"{value // 3600}h"
    return f"{value // 86_400}d"


def _severity_rank(value: str) -> int:
    return {"critical": 4, "urgent": 3, "caution": 2, "info": 1}.get(value, 0)


def _optional_number(value: object) -> float | None:
    try:
        return float(str(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


def _forecast_hazard(day: Mapping[str, Any]) -> bool:
    summary = str(day.get("summary") or "").casefold()
    keywords = (
        "thunder",
        "tornado",
        "hurricane",
        "severe",
        "hail",
        "flood",
        "blizzard",
        "freezing",
        "ice",
        "heavy rain",
        "heavy snow",
    )
    high = _optional_number(day.get("high_c"))
    low = _optional_number(day.get("low_c"))
    wind = _optional_number(day.get("wind_kph"))
    return (
        any(word in summary for word in keywords)
        or (high is not None and high >= 38)
        or (low is not None and low <= -18)
        or (wind is not None and wind >= 60)
    )


class SituationBriefingService:
    """Build role-filtered situation facts without relying on inference."""

    def __init__(
        self,
        database: Database,
        clock: Clock,
        status_provider: Callable[[], Mapping[str, Any]],
        *,
        narrator: SituationNarrator | None = None,
        modules: Callable[[], Mapping[str, bool]] | None = None,
    ) -> None:
        self.database = database
        self.clock = clock
        self.status_provider = status_provider
        self.narrator = narrator
        self.modules = modules or (lambda: {name: True for name in ("bbs", "watch", "env", "fed")})
        self._snapshot_lock = asyncio.Lock()

    async def snapshot(
        self,
        capability: BriefingCapability,
        *,
        include_ai: bool = False,
        viewer: BriefingViewer | None = None,
        since: int | None = None,
    ) -> dict[str, Any]:
        if since is not None and since < 0:
            raise ValueError("Briefing since time must be a non-negative Unix timestamp.")
        async with self._snapshot_lock:
            return await self._snapshot(
                capability,
                include_ai=include_ai,
                viewer=viewer,
                since=since,
            )

    async def _snapshot(
        self,
        capability: BriefingCapability,
        *,
        include_ai: bool,
        viewer: BriefingViewer | None,
        since: int | None,
    ) -> dict[str, Any]:
        now = int(self.clock.now().timestamp())
        items, sources = await self._collect(capability, now)
        source_map = {source.id: source for source in sources}
        facts = [self._item_fact(item, source_map) for item in items]
        digest_input = {
            "capability": capability.value,
            "items": facts,
            "sources": [source.fact() for source in sources],
        }
        fact_digest = hashlib.sha256(
            json.dumps(digest_input, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        selection = await self._select_snapshot(
            capability,
            fact_digest,
            facts,
            now,
            viewer=viewer,
            since=since,
        )
        if selection.prior and selection.prior.get("digest") == fact_digest:
            changes: list[BriefingChange] = []
        else:
            changes = await self._changes(selection.prior, facts)
        if viewer is not None:
            await self._advance_marker(viewer, capability, selection.current_id, now)
        digest = hashlib.sha256(
            json.dumps(
                {
                    "facts": fact_digest,
                    "changes": [change.json() for change in changes],
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()

        sections = [
            {
                "id": section,
                "label": section.title(),
                "items": [item.json() for item in items if item.section == section],
                "max_items": SECTION_LIMITS[section],
                "stale_after_seconds": STALE_AFTER[section],
            }
            for section in SECTION_ORDER
        ]
        value: dict[str, Any] = {
            "generated_at": now,
            "capability": capability.value,
            "digest": digest,
            "sections": sections,
            "items": [item.json() for item in items],
            "sources": [source.json(now) for source in sources],
            "changes": [change.json() for change in changes],
            "change_window": selection.change_window,
            "policy": {
                "section_order": list(SECTION_ORDER),
                "section_limits": SECTION_LIMITS,
                "stale_after_seconds": STALE_AFTER,
                "resolution_rule": "explicit terminal records only; absence is not resolution",
                "privacy": (
                    "No exact member/check-in coordinates, private mail, operator notes, "
                    "or unauthorized board content."
                ),
            },
            "ai": {"requested": include_ai, "text": None, "outcome": "not_requested"},
        }
        if include_ai:
            value["ai"] = await self._narration(value, capability)
        return value

    async def _collect(
        self, capability: BriefingCapability, now: int
    ) -> tuple[list[BriefingItem], list[BriefingSource]]:
        enabled = self.modules()
        items: list[BriefingItem] = []
        sources: list[BriefingSource] = []
        if enabled.get("watch", True):
            section_items, section_sources = await self._alerts(now)
            items.extend(section_items)
            sources.extend(section_sources)
            section_items, section_sources = await self._incidents(now)
            items.extend(section_items)
            sources.extend(section_sources)
            section_items, section_sources = await self._welfare(capability, now)
            items.extend(section_items)
            sources.extend(section_sources)
        if enabled.get("env", True):
            section_items, section_sources = await self._weather(now)
            items.extend(section_items)
            sources.extend(section_sources)
        if enabled.get("bbs", True):
            section_items, section_sources = await self._community(capability, now)
            items.extend(section_items)
            sources.extend(section_sources)
        section_items, section_sources = await self._delivery(capability, now)
        items.extend(section_items)
        sources.extend(section_sources)
        section_items, section_sources = await self._network(now, enabled.get("fed", True))
        items.extend(section_items)
        sources.extend(section_sources)
        section_rank = {section: index for index, section in enumerate(SECTION_ORDER)}
        items.sort(
            key=lambda item: (
                section_rank[item.section],
                not item.hazard,
                -_severity_rank(item.severity),
                item.id,
            )
        )
        return items, sorted(sources, key=lambda source: source.id)

    async def _alerts(self, now: int) -> tuple[list[BriefingItem], list[BriefingSource]]:
        rows = await self.database.read(
            """
            SELECT a.id,a.incident_id,a.severity,a.headline,a.source,a.raised_at,a.expires_at,
                   a.ack_required,COUNT(aa.member_id) ack_count,
                   COALESCE(i.unverified,0) unverified,COALESCE(i.dispute_count,0) disputes
            FROM alert a
            LEFT JOIN alert_ack aa ON aa.alert_id=a.id
            LEFT JOIN incident i ON i.id=a.incident_id
            WHERE a.cancelled_at IS NULL AND (a.expires_at IS NULL OR a.expires_at>?)
            GROUP BY a.id
            ORDER BY CASE a.severity WHEN 'critical' THEN 4 WHEN 'urgent' THEN 3 ELSE 2 END DESC,
                     a.raised_at DESC,a.id DESC LIMIT ?
            """,
            (now, SECTION_LIMITS["alerts"]),
        )
        incident_versions: dict[int, set[tuple[str, str]]] = {}
        for row in rows:
            if row["incident_id"] is not None:
                incident_versions.setdefault(int(row["incident_id"]), set()).add(
                    (str(row["severity"]), _safe_text(row["headline"]).casefold())
                )
        items: list[BriefingItem] = []
        sources: list[BriefingSource] = []
        for row in rows:
            item_id = int(row["id"])
            source_id = f"alert:{item_id}"
            raised_at = int(row["raised_at"])
            stale = now - raised_at > STALE_AFTER["alerts"]
            conflict = bool(
                row["incident_id"] is not None
                and len(incident_versions.get(int(row["incident_id"]), set())) > 1
            )
            uncertainty = (
                "conflicting active alerts"
                if conflict
                else "linked report is disputed"
                if int(row["disputes"] or 0)
                else "linked report is unverified"
                if int(row["unverified"] or 0)
                else "stale source"
                if stale
                else None
            )
            ack_required = int(row["ack_required"] or 0)
            ack_detail = (
                f" · acknowledgements {int(row['ack_count'])}/{ack_required}"
                if ack_required
                else ""
            )
            items.append(
                BriefingItem(
                    f"alert:{item_id}",
                    f"A{item_id}",
                    "alerts",
                    str(row["severity"]),
                    _safe_text(row["headline"]),
                    f"{str(row['source']).upper()} alert{ack_detail}",
                    "active",
                    (source_id,),
                    f"/watch.html?alert={item_id}",
                    hazard=True,
                    uncertainty=uncertainty,
                )
            )
            sources.append(
                BriefingSource(
                    source_id,
                    f"{str(row['source']).upper()} alert {item_id}",
                    raised_at,
                    STALE_AFTER["alerts"],
                    f"/watch.html?alert={item_id}",
                    stale=stale,
                    conflict=conflict,
                )
            )
        return items, sources

    async def _incidents(self, now: int) -> tuple[list[BriefingItem], list[BriefingSource]]:
        rows = await self.database.read(
            """
            SELECT id,local_ref,type,severity,status,title,updated_at,confirm_count,dispute_count,
                   source,unverified,flagged_for_review
            FROM incident
            WHERE status IN ('open','monitoring') AND severity IN ('critical','urgent')
              AND merged_into_id IS NULL
            ORDER BY CASE severity WHEN 'critical' THEN 4 ELSE 3 END DESC,updated_at DESC,id DESC
            LIMIT ?
            """,
            (SECTION_LIMITS["incidents"],),
        )
        items: list[BriefingItem] = []
        sources: list[BriefingSource] = []
        for row in rows:
            item_id = int(row["id"])
            reference = int(row["local_ref"])
            source_id = f"incident:{reference}"
            updated_at = int(row["updated_at"])
            stale = now - updated_at > STALE_AFTER["incidents"]
            uncertainty = (
                "disputed"
                if int(row["dispute_count"] or 0)
                else "unverified"
                if int(row["unverified"] or 0)
                else "operator review required"
                if int(row["flagged_for_review"] or 0)
                else "stale source"
                if stale
                else None
            )
            items.append(
                BriefingItem(
                    f"incident:{item_id}",
                    f"I{reference}",
                    "incidents",
                    str(row["severity"]),
                    _safe_text(row["title"]),
                    f"{_safe_text(row['type'], 40)} · {row['status']} · "
                    f"{int(row['confirm_count'])} confirmed",
                    str(row["status"]),
                    (source_id,),
                    f"/watch.html?incident={item_id}",
                    uncertainty=uncertainty,
                )
            )
            sources.append(
                BriefingSource(
                    source_id,
                    f"Incident {reference}",
                    updated_at,
                    STALE_AFTER["incidents"],
                    f"/watch.html?incident={item_id}",
                    stale=stale,
                    conflict=bool(int(row["dispute_count"] or 0)),
                )
            )
        return items, sources

    async def _welfare(
        self, capability: BriefingCapability, now: int
    ) -> tuple[list[BriefingItem], list[BriefingSource]]:
        events = await self.database.read(
            "SELECT id,name,opened_at,roster_policy,event_kind,responder_group_id "
            "FROM watch_event "
            "WHERE closed_at IS NULL ORDER BY id DESC LIMIT 1"
        )
        if not events:
            return [], []
        event = events[0]
        event_id = int(event["id"])
        drill = event["event_kind"] == "drill"
        params: tuple[object, ...]
        if drill:
            roster_from = (
                "welfare_event_roster r JOIN member m ON m.id=r.member_id "
                "LEFT JOIN latest l ON l.member_id=m.id AND l.n=1"
            )
            member_filter = "r.event_id=?"
            params = (event_id, event_id)
        elif event["responder_group_id"] is not None:
            roster_from = "member m LEFT JOIN latest l ON l.member_id=m.id AND l.n=1"
            member_filter = (
                "m.trust IN ('responder','operator') AND EXISTS ("
                "SELECT 1 FROM responder_group_member gm WHERE gm.member_id=m.id "
                "AND gm.group_id=?)"
            )
            params = (event_id, int(event["responder_group_id"]))
        elif event["roster_policy"] == "responders":
            roster_from = "member m LEFT JOIN latest l ON l.member_id=m.id AND l.n=1"
            member_filter = "m.trust IN ('responder','operator')"
            params = (event_id,)
        elif event["roster_policy"] == "subscribed":
            roster_from = "member m LEFT JOIN latest l ON l.member_id=m.id AND l.n=1"
            member_filter = (
                "m.trust IN ('member','trusted','responder','operator') "
                "AND json_extract(m.prefs,'$.roster')=1"
            )
            params = (event_id,)
        else:
            roster_from = "member m LEFT JOIN latest l ON l.member_id=m.id AND l.n=1"
            member_filter = "m.trust IN ('member','trusted','responder','operator')"
            params = (event_id,)
        rows = await self.database.read(
            f"""
            WITH latest AS (
              SELECT member_id,status,created_at,
                     ROW_NUMBER() OVER (PARTITION BY member_id ORDER BY created_at DESC,id DESC) n
              FROM checkin WHERE event_id=?
            )
            SELECT m.id,m.handle,COALESCE(l.status,'unaccounted') status,l.created_at
            FROM {roster_from}
            WHERE {member_filter}
            ORDER BY COALESCE(m.handle,printf('member-%d',m.id))
            """,  # noqa: S608 -- the filter is selected from fixed literals above.
            params,
        )
        counts = {name: 0 for name in ("ok", "need_help", "evacuated", "unaccounted")}
        names: list[str] = []
        newest = int(event["opened_at"])
        for row in rows:
            status = str(row["status"])
            counts[status] = counts.get(status, 0) + 1
            if row["created_at"] is not None:
                newest = max(newest, int(row["created_at"]))
            if (
                capability in {BriefingCapability.RESPONDER, BriefingCapability.OPERATOR}
                and status in {"need_help", "unaccounted"}
                and len(names) < 3
            ):
                names.append(f"@{_safe_text(row['handle'], 12)}" if row["handle"] else "unnamed")
        overdue = counts["need_help"] + counts["unaccounted"]
        stale = now - newest > STALE_AFTER["welfare"]
        title = (
            _safe_text(event["name"], 80)
            if capability is not BriefingCapability.PUBLIC
            else "Active welfare drill"
            if drill
            else "Active welfare event"
        )
        detail = (
            f"{'DRILL · ' if drill else ''}{counts['need_help']} need help · "
            f"{counts['unaccounted']} unaccounted · "
            f"{counts['ok']} OK · {counts['evacuated']} evacuated"
        )
        if names:
            detail += f" · attention: {', '.join(names)}"
        source_id = f"welfare:{event_id}"
        severity = (
            "urgent"
            if counts["need_help"]
            else ("info" if drill else "caution" if counts["unaccounted"] else "info")
        )
        return [
            BriefingItem(
                f"welfare:{event_id}",
                f"W{event_id}",
                "welfare",
                severity,
                title,
                detail,
                "attention"
                if counts["need_help"]
                else "drill"
                if drill
                else ("attention" if overdue else "accounted"),
                (source_id,),
                f"/watch.html?event={event_id}",
                hazard=bool(counts["need_help"] if drill else overdue),
                uncertainty="stale roster" if stale else None,
            )
        ], [
            BriefingSource(
                source_id,
                f"Welfare event {event_id}",
                newest,
                STALE_AFTER["welfare"],
                f"/watch.html?event={event_id}",
                stale=stale,
            )
        ]

    async def _weather(self, now: int) -> tuple[list[BriefingItem], list[BriefingSource]]:
        rows = await self.database.read(
            "SELECT cache_key,provider,payload,fetched_at FROM env_cache "
            "WHERE cache_key LIKE 'forecast:%' ORDER BY fetched_at DESC LIMIT 1"
        )
        if not rows:
            return [], []
        row = rows[0]
        try:
            payload = json.loads(str(row["payload"]))
            days = payload.get("daily", []) if isinstance(payload, dict) else []
        except json.JSONDecodeError:
            return [], []
        fetched_at = int(row["fetched_at"])
        stale = now - fetched_at > STALE_AFTER["weather"]
        location_key = hashlib.sha256(str(row["cache_key"]).encode()).hexdigest()[:8]
        source_id = f"forecast:{str(row['provider'])}:{location_key}"
        source = BriefingSource(
            source_id,
            f"{str(row['provider'])} forecast",
            fetched_at,
            STALE_AFTER["weather"],
            "/environment.html#forecast",
            stale=stale,
        )
        items: list[BriefingItem] = []
        if not isinstance(days, list):
            return [], [source]
        for index, raw_day in enumerate(days[: SECTION_LIMITS["weather"]]):
            if not isinstance(raw_day, dict):
                continue
            hazard = _forecast_hazard(raw_day)
            high = _optional_number(raw_day.get("high_c"))
            low = _optional_number(raw_day.get("low_c"))
            rain = _optional_number(raw_day.get("precipitation_probability"))
            wind = _optional_number(raw_day.get("wind_kph"))
            values = []
            if high is not None or low is not None:
                values.append(
                    f"{round(high) if high is not None else '—'}°/"
                    f"{round(low) if low is not None else '—'}°C"
                )
            if rain is not None:
                values.append(f"rain {round(rain)}%")
            if wind is not None:
                values.append(f"wind {round(wind)} km/h")
            stamp = _safe_text(raw_day.get("start_time") or raw_day.get("name") or index, 40)
            items.append(
                BriefingItem(
                    f"forecast:{stamp}",
                    f"F{index + 1}",
                    "weather",
                    "caution" if hazard else "info",
                    _safe_text(raw_day.get("summary") or "Forecast unavailable", 100),
                    " · ".join(values) or "Forecast values unavailable",
                    "hazard" if hazard else "forecast",
                    (source_id,),
                    "/environment.html#forecast",
                    hazard=hazard,
                    uncertainty="stale forecast" if stale else None,
                )
            )
        items.sort(key=lambda item: (not item.hazard, item.id))
        return items, [source]

    async def _community(
        self, capability: BriefingCapability, now: int
    ) -> tuple[list[BriefingItem], list[BriefingSource]]:
        rows = await self.database.read(
            """
            SELECT t.id,t.subject,t.last_post_at,t.post_count,b.slug,b.title,b.min_read_trust
            FROM thread t JOIN board b ON b.id=t.board_id
            WHERE t.hidden=0 AND b.archived=0
            ORDER BY t.last_post_at DESC,t.id DESC LIMIT 30
            """
        )
        items: list[BriefingItem] = []
        sources: list[BriefingSource] = []
        for row in rows:
            try:
                allowed = capability.trust >= TrustLevel.parse(str(row["min_read_trust"]))
            except KeyError:
                allowed = False
            if not allowed:
                continue
            item_id = int(row["id"])
            source_id = f"thread:{item_id}"
            updated_at = int(row["last_post_at"])
            stale = now - updated_at > STALE_AFTER["community"]
            items.append(
                BriefingItem(
                    f"thread:{item_id}",
                    f"C{item_id}",
                    "community",
                    "info",
                    _safe_text(row["subject"]),
                    f"{_safe_text(row['title'], 60)} · {int(row['post_count'])} posts",
                    "updated",
                    (source_id,),
                    f"/bbs.html?thread={item_id}",
                    uncertainty="older update" if stale else None,
                )
            )
            sources.append(
                BriefingSource(
                    source_id,
                    f"Board {str(row['slug'])}",
                    updated_at,
                    STALE_AFTER["community"],
                    f"/bbs.html?thread={item_id}",
                    stale=stale,
                )
            )
            if len(items) >= SECTION_LIMITS["community"]:
                break
        return items, sources

    async def _delivery(
        self, capability: BriefingCapability, now: int
    ) -> tuple[list[BriefingItem], list[BriefingSource]]:
        """Compare confirmed delivery and receive quality with a preceding 14-day baseline."""
        current_start = now - DAY
        baseline_start = current_start - DELIVERY_BASELINE_DAYS * DAY
        channel_rows = await self.database.read(
            """
            SELECT channel,
              SUM(CASE WHEN created_at>=? THEN 1 ELSE 0 END) current_total,
              SUM(CASE WHEN created_at>=? AND outcome='acked' THEN 1 ELSE 0 END) current_success,
              SUM(CASE WHEN created_at<? THEN 1 ELSE 0 END) baseline_total,
              SUM(CASE WHEN created_at<? AND outcome='acked' THEN 1 ELSE 0 END) baseline_success,
              MAX(created_at) newest
            FROM message_log INDEXED BY idx_msglog_replay_range
            WHERE direction='out' AND channel BETWEEN 0 AND 7
              AND created_at>=? AND created_at<?
              AND outcome IN (
                'acked','naked','nak','timeout','failed','dropped','rejected'
              )
            GROUP BY channel ORDER BY channel
            """,
            (
                current_start,
                current_start,
                current_start,
                current_start,
                baseline_start,
                now,
            ),
        )
        reason_rows = await self.database.read(
            """
            WITH counts AS (
              SELECT channel,COALESCE(NULLIF(drop_reason,''),outcome) reason,COUNT(*) count
              FROM message_log INDEXED BY idx_msglog_replay_range
              WHERE direction='out' AND channel BETWEEN 0 AND 7
                AND created_at>=? AND created_at<?
                AND outcome IN ('naked','nak','timeout','failed','dropped','rejected')
              GROUP BY channel,reason
            ), ranked AS (
              SELECT channel,reason,count,
                ROW_NUMBER() OVER (PARTITION BY channel ORDER BY count DESC,reason) rank
              FROM counts
            )
            SELECT channel,reason,count FROM ranked WHERE rank=1 ORDER BY channel
            """,
            (current_start, now),
        )
        latency_rows = await self.database.read(
            """
            WITH ranked AS (
              SELECT channel,latency_ms,
                ROW_NUMBER() OVER (PARTITION BY channel ORDER BY latency_ms) position,
                COUNT(*) OVER (PARTITION BY channel) sample_count
              FROM message_log INDEXED BY idx_msglog_replay_range
              WHERE direction='out' AND channel BETWEEN 0 AND 7 AND outcome='acked'
                AND latency_ms IS NOT NULL AND created_at>=? AND created_at<?
            )
            SELECT channel,CAST(ROUND(AVG(latency_ms)) AS INTEGER) median_ms,
                   MAX(sample_count) samples
            FROM ranked
            WHERE position IN ((sample_count+1)/2,(sample_count+2)/2)
            GROUP BY channel ORDER BY channel
            """,
            (current_start, now),
        )
        snr_rows = await self.database.read(
            """
            SELECT m.id,m.handle,m.mesh_id,
              SUM(CASE WHEN ml.created_at>=? THEN 1 ELSE 0 END) current_samples,
              AVG(CASE WHEN ml.created_at>=? THEN ml.rx_snr END) current_snr,
              SUM(CASE WHEN ml.created_at<? THEN 1 ELSE 0 END) baseline_samples,
              AVG(CASE WHEN ml.created_at<? THEN ml.rx_snr END) baseline_snr,
              AVG(CASE WHEN ml.created_at>=? THEN ml.rx_snr END)
                - AVG(CASE WHEN ml.created_at<? THEN ml.rx_snr END) delta_snr,
              MAX(ml.created_at) newest
            FROM message_log AS ml INDEXED BY idx_msglog_replay_range
            JOIN member m ON m.mesh_id=ml.peer_mesh_id
            WHERE ml.direction='in' AND ml.transport='radio' AND ml.rx_snr IS NOT NULL
              AND ml.created_at>=? AND ml.created_at<?
              AND m.directory_state='active'
              AND m.trust IN ('member','trusted','responder','operator')
            GROUP BY m.id
            ORDER BY (current_samples>=? AND baseline_samples>=?) DESC,
                     delta_snr ASC,m.id
            LIMIT 256
            """,
            (
                current_start,
                current_start,
                current_start,
                current_start,
                current_start,
                current_start,
                baseline_start,
                now,
                SNR_CURRENT_MIN,
                SNR_BASELINE_MIN,
            ),
        )
        reasons = {int(row["channel"]): str(row["reason"]) for row in reason_rows}
        latencies = {int(row["channel"]): int(row["median_ms"]) for row in latency_rows}
        items: list[BriefingItem] = []
        sources: list[BriefingSource] = []

        if not channel_rows:
            source_id = "message-log:delivery-history"
            items.append(
                BriefingItem(
                    "delivery:history",
                    "D?",
                    "delivery",
                    "info",
                    "Confirmed delivery trend needs history",
                    f"Need {DELIVERY_CURRENT_MIN} terminal outcomes in 24h and "
                    f"{DELIVERY_BASELINE_MIN} in the preceding {DELIVERY_BASELINE_DAYS}d",
                    "insufficient",
                    (source_id,),
                    "/radio.html",
                    uncertainty="insufficient history",
                )
            )
            sources.append(
                BriefingSource(
                    source_id,
                    "Bounded terminal message history",
                    None,
                    STALE_AFTER["delivery"],
                    "/radio.html",
                )
            )
        for row in channel_rows:
            channel = int(row["channel"])
            current_total = int(row["current_total"] or 0)
            current_success = int(row["current_success"] or 0)
            baseline_total = int(row["baseline_total"] or 0)
            baseline_success = int(row["baseline_success"] or 0)
            observed_at = int(row["newest"])
            source_id = f"message-log:channel:{channel}:delivery"
            enough = (
                current_total >= DELIVERY_CURRENT_MIN and baseline_total >= DELIVERY_BASELINE_MIN
            )
            if enough:
                current_rate = current_success * 100.0 / current_total
                baseline_rate = baseline_success * 100.0 / baseline_total
                delta = current_rate - baseline_rate
                degrading = delta <= -DELIVERY_RATE_DROP_POINTS
                improving = delta >= DELIVERY_RATE_DROP_POINTS
                median = latencies.get(channel)
                latency = (
                    f"median ACK {median / 1_000:.1f}s"
                    if median is not None
                    else "median ACK awaiting samples"
                )
                failure = reasons.get(channel)
                detail = (
                    f"24h {current_success}/{current_total} ({current_rate:.0f}%) · prior "
                    f"{DELIVERY_BASELINE_DAYS}d {baseline_success}/{baseline_total} "
                    f"({baseline_rate:.0f}%) · trend {delta:+.0f} points · {latency}"
                )
                if failure is not None:
                    detail += f" · leading failure {_safe_text(failure, 40)}"
                items.append(
                    BriefingItem(
                        f"delivery:channel:{channel}",
                        f"D{channel}",
                        "delivery",
                        "caution" if degrading else "info",
                        f"Channel {channel} confirmed delivery {current_rate:.0f}%",
                        detail,
                        "degrading" if degrading else "improving" if improving else "steady",
                        (source_id,),
                        "/radio.html",
                        hazard=degrading,
                    )
                )
            else:
                detail = (
                    f"24h {current_total} terminal outcomes (need {DELIVERY_CURRENT_MIN}) · "
                    f"prior {DELIVERY_BASELINE_DAYS}d {baseline_total} "
                    f"(need {DELIVERY_BASELINE_MIN})"
                )
                failure = reasons.get(channel)
                if failure is not None:
                    detail += f" · leading failure {_safe_text(failure, 40)}"
                items.append(
                    BriefingItem(
                        f"delivery:channel:{channel}",
                        f"D{channel}",
                        "delivery",
                        "info",
                        f"Channel {channel} delivery trend needs history",
                        detail,
                        "insufficient",
                        (source_id,),
                        "/radio.html",
                        uncertainty="insufficient history",
                    )
                )
            sources.append(
                BriefingSource(
                    source_id,
                    f"Terminal message history · channel {channel}",
                    observed_at,
                    STALE_AFTER["delivery"],
                    "/radio.html",
                    stale=now - observed_at > STALE_AFTER["delivery"],
                )
            )

        qualified_snr = [
            row
            for row in snr_rows
            if int(row["current_samples"] or 0) >= SNR_CURRENT_MIN
            and int(row["baseline_samples"] or 0) >= SNR_BASELINE_MIN
            and row["current_snr"] is not None
            and row["baseline_snr"] is not None
        ]
        remaining = max(0, SECTION_LIMITS["delivery"] - len(items))
        detailed = capability in {BriefingCapability.RESPONDER, BriefingCapability.OPERATOR}
        if qualified_snr and remaining:
            if detailed:
                for row in qualified_snr[:remaining]:
                    member_id = int(row["id"])
                    label = str(row["handle"] or row["mesh_id"])
                    current_snr = float(row["current_snr"])
                    baseline_snr = float(row["baseline_snr"])
                    delta = current_snr - baseline_snr
                    degrading = delta <= -SNR_DROP_DB
                    improving = delta >= SNR_DROP_DB
                    observed_at = int(row["newest"])
                    source_id = f"message-log:member:{member_id}:snr"
                    items.append(
                        BriefingItem(
                            f"delivery:member:{member_id}",
                            f"R{member_id}",
                            "delivery",
                            "caution" if degrading else "info",
                            f"Receive path @{_safe_text(label, 20)} {current_snr:+.1f} dB",
                            f"24h {int(row['current_samples'])} samples · prior "
                            f"{DELIVERY_BASELINE_DAYS}d {int(row['baseline_samples'])} · "
                            f"trend {delta:+.1f} dB",
                            "degrading" if degrading else "improving" if improving else "steady",
                            (source_id,),
                            "/operator.html",
                            hazard=degrading,
                        )
                    )
                    sources.append(
                        BriefingSource(
                            source_id,
                            f"Radio receive history · @{_safe_text(label, 20)}",
                            observed_at,
                            STALE_AFTER["delivery"],
                            "/operator.html",
                            stale=now - observed_at > STALE_AFTER["delivery"],
                        )
                    )
            else:
                deltas = [
                    float(row["current_snr"]) - float(row["baseline_snr"]) for row in qualified_snr
                ]
                degrading_count = sum(delta <= -SNR_DROP_DB for delta in deltas)
                worst = min(deltas)
                observed_at = max(int(row["newest"]) for row in qualified_snr)
                source_id = "message-log:member-snr"
                items.append(
                    BriefingItem(
                        "delivery:member-snr",
                        "DR",
                        "delivery",
                        "caution" if degrading_count else "info",
                        "Member receive paths declining"
                        if degrading_count
                        else "Member receive paths stable",
                        f"{len(qualified_snr)} enrolled path(s) compared · "
                        f"worst trend {worst:+.1f} dB",
                        "degrading" if degrading_count else "steady",
                        (source_id,),
                        "/radio.html",
                        hazard=bool(degrading_count),
                    )
                )
                sources.append(
                    BriefingSource(
                        source_id,
                        "De-identified enrolled-radio receive trends",
                        observed_at,
                        STALE_AFTER["delivery"],
                        "/radio.html",
                        stale=now - observed_at > STALE_AFTER["delivery"],
                    )
                )
        elif remaining:
            best_current = max((int(row["current_samples"] or 0) for row in snr_rows), default=0)
            best_baseline = max((int(row["baseline_samples"] or 0) for row in snr_rows), default=0)
            observed_values = [int(row["newest"]) for row in snr_rows if row["newest"] is not None]
            snr_observed_at: int | None = max(observed_values) if observed_values else None
            source_id = "message-log:member-snr"
            items.append(
                BriefingItem(
                    "delivery:member-snr",
                    "DR",
                    "delivery",
                    "info",
                    "Receive-quality trend needs history",
                    f"No enrolled radio has {SNR_CURRENT_MIN} recent and "
                    f"{SNR_BASELINE_MIN} baseline SNR samples · "
                    f"best {best_current}/{best_baseline}",
                    "insufficient",
                    (source_id,),
                    "/radio.html",
                    uncertainty="insufficient history",
                )
            )
            sources.append(
                BriefingSource(
                    source_id,
                    "Enrolled-radio receive history",
                    snr_observed_at,
                    STALE_AFTER["delivery"],
                    "/radio.html",
                    stale=bool(
                        snr_observed_at is not None
                        and now - snr_observed_at > STALE_AFTER["delivery"]
                    ),
                )
            )
        return items[: SECTION_LIMITS["delivery"]], sources

    async def _network(
        self, now: int, federation_enabled: bool
    ) -> tuple[list[BriefingItem], list[BriefingSource]]:
        runtime = self.status_provider()
        radio = str(runtime.get("radio") or "unknown")
        raw_inbound = runtime.get("inbound")
        raw_queues = runtime.get("queues")
        raw_power = runtime.get("radio_power")
        raw_mode = runtime.get("runtime")
        inbound: Mapping[str, Any] = raw_inbound if isinstance(raw_inbound, dict) else {}
        queues: Mapping[str, Any] = raw_queues if isinstance(raw_queues, dict) else {}
        power: Mapping[str, Any] = raw_power if isinstance(raw_power, dict) else {}
        queue_total = sum(int(value or 0) for value in queues.values())
        backlog = int(inbound.get("backlog") or 0)
        mode: Mapping[str, Any] = raw_mode if isinstance(raw_mode, dict) else {}
        simulated = mode.get("simulated") is True
        mode_name = str(mode.get("mode") or "live")
        items: list[BriefingItem] = []
        sources: list[BriefingSource] = []
        if simulated:
            items.append(
                BriefingItem(
                    "network:runtime-mode",
                    "N0",
                    "network",
                    "caution",
                    f"{mode_name.title()} mode · simulated transmission",
                    "Recorded traffic · scratch database · no RF or MQTT output",
                    mode_name,
                    ("runtime:mode",),
                    "/",
                )
            )
            sources.append(
                BriefingSource(
                    "runtime:mode",
                    "Isolated replay runtime",
                    None,
                    STALE_AFTER["network"],
                    "/",
                )
            )
        items.append(
            BriefingItem(
                "network:radio",
                "N1",
                "network",
                "caution" if radio != "up" else "info",
                f"{'Simulated radio' if simulated else 'Radio'} {radio}",
                f"{backlog} inbound waiting · {queue_total} governed outbound",
                radio,
                ("runtime:radio",),
                "/radio.html",
            )
        )
        sources.append(
            BriefingSource(
                "runtime:radio",
                "Simulated radio runtime" if simulated else "Live radio runtime",
                None,
                STALE_AFTER["network"],
                "/radio.html",
            )
        )
        level = power.get("battery_level")
        display_level = int(level) if isinstance(level, (int, float)) else None
        reported = power.get("reported") is True and display_level is not None
        condition = str(power.get("condition") or "not_reported")
        raw_trend = power.get("trend")
        trend: Mapping[str, Any] = raw_trend if isinstance(raw_trend, dict) else {}
        direction = str(trend.get("direction") or "unavailable")
        delta = trend.get("delta_percent")
        elapsed = trend.get("elapsed_hours")
        power_title = f"Radio power {display_level}%" if reported else "Radio power not reported"
        if reported and isinstance(delta, (int, float)) and isinstance(elapsed, (int, float)):
            power_detail = f"{direction} {abs(int(delta))} point(s) over {float(elapsed):.1f}h"
        elif reported:
            power_detail = "Trend will appear after another sampled reading"
        else:
            power_detail = "No battery reported; the node may use external power"
        raw_shedding = power.get("shedding")
        shedding: Mapping[str, Any] = raw_shedding if isinstance(raw_shedding, dict) else {}
        if shedding.get("active") is True:
            power_detail += " · discretionary traffic paused"
        power_observed = power.get("observed_at")
        observed_at = int(power_observed) if isinstance(power_observed, (int, float)) else None
        items.append(
            BriefingItem(
                "network:power",
                "N2",
                "network",
                "critical"
                if condition == "critical"
                else "caution"
                if condition == "warning"
                else "info",
                power_title,
                power_detail,
                condition,
                ("runtime:power",),
                "/radio.html",
                hazard=condition in {"warning", "critical"},
            )
        )
        sources.append(
            BriefingSource(
                "runtime:power",
                "Connected radio power telemetry",
                observed_at,
                STALE_AFTER["network"],
                "/radio.html",
                stale=bool(observed_at is not None and now - observed_at > STALE_AFTER["network"]),
            )
        )
        member = (
            await self.database.read(
                "SELECT COUNT(*) total,COALESCE(SUM(last_seen>=?),0) active FROM member "
                "WHERE trust IN ('member','trusted','responder','operator')",
                (now - 86_400,),
            )
        )[0]
        newest_member = await self.database.read("SELECT MAX(last_seen) value FROM member")
        member_observed = newest_member[0]["value"]
        items.append(
            BriefingItem(
                "network:members",
                "N3",
                "network",
                "info",
                "Member activity",
                f"{int(member['active'])} heard in 24h · {int(member['total'])} approved",
                "current",
                ("directory:activity",),
                "/operator.html",
            )
        )
        sources.append(
            BriefingSource(
                "directory:activity",
                "Member directory aggregates",
                int(member_observed) if member_observed is not None else None,
                STALE_AFTER["network"],
                "/operator.html",
                stale=bool(member_observed is not None and now - int(member_observed) > 86_400),
            )
        )
        if federation_enabled:
            peers = (
                await self.database.read(
                    "SELECT COUNT(*) total,COALESCE(SUM(state='active'),0) active,"
                    "MAX(last_seen_at) newest FROM fed_peer"
                )
            )[0]
            items.append(
                BriefingItem(
                    "network:federation",
                    "N4",
                    "network",
                    "info",
                    "Federation peers",
                    f"{int(peers['active'])} active · {int(peers['total'])} configured",
                    "current",
                    ("federation:peers",),
                    "/federation.html",
                )
            )
            sources.append(
                BriefingSource(
                    "federation:peers",
                    "Federation peer state",
                    int(peers["newest"]) if peers["newest"] is not None else None,
                    STALE_AFTER["network"],
                    "/federation.html",
                )
            )
        return items[: SECTION_LIMITS["network"]], sources

    @staticmethod
    def _item_fact(item: BriefingItem, sources: Mapping[str, BriefingSource]) -> dict[str, Any]:
        value = item.json()
        value["source_states"] = [
            {
                "id": source_id,
                "stale": sources[source_id].stale,
                "conflict": sources[source_id].conflict,
            }
            for source_id in item.source_ids
            if source_id in sources
        ]
        return value

    @staticmethod
    def _stored_snapshot(
        row: Mapping[str, Any] | sqlite3.Row | None,
    ) -> dict[str, Any] | None:
        if row is None:
            return None
        try:
            facts = json.loads(str(row["facts_json"]))
        except (KeyError, TypeError, json.JSONDecodeError):
            return None
        if not isinstance(facts, list):
            return None
        return {
            "id": int(row["id"]),
            "digest": str(row["digest"]),
            "facts": facts,
            "created_at": int(row["created_at"]),
        }

    @staticmethod
    def _brief_time(value: int) -> str:
        return datetime.fromtimestamp(value, UTC).strftime("%Y-%m-%d %H:%M UTC")

    @classmethod
    def _change_window(
        cls,
        kind: Literal["viewer", "explicit", "previous"],
        since: int | None,
        prior: dict[str, Any] | None,
    ) -> dict[str, Any]:
        anchor_at = int(prior["created_at"]) if prior is not None else None
        if kind == "viewer":
            if since is None:
                label = "First look; no prior briefing is recorded"
            elif prior is None:
                label = (
                    f"Since your last look at {cls._brief_time(since)}; "
                    "the earlier comparison snapshot is no longer retained"
                )
            else:
                label = f"Since your last look at {cls._brief_time(since)}"
        elif kind == "explicit":
            assert since is not None
            if prior is None:
                label = (
                    f"Since requested time {cls._brief_time(since)}; "
                    "no comparison snapshot is retained at or before that time"
                )
            else:
                label = f"Since requested time {cls._brief_time(since)}"
        elif prior is None:
            label = "First retained briefing snapshot"
        else:
            assert anchor_at is not None
            label = f"Since the prior briefing snapshot at {cls._brief_time(anchor_at)}"
        return {
            "kind": kind if prior is not None or since is not None else "first_look",
            "since": since,
            "anchor_at": anchor_at,
            "complete": prior is not None,
            "label": label,
        }

    async def _select_snapshot(
        self,
        capability: BriefingCapability,
        digest: str,
        facts: list[dict[str, Any]],
        now: int,
        *,
        viewer: BriefingViewer | None,
        since: int | None,
    ) -> _SnapshotSelection:
        async with self.database.transaction() as transaction:
            latest_rows = await transaction.read(
                "SELECT id,digest,facts_json,created_at FROM situation_snapshot "
                "WHERE capability=? ORDER BY id DESC LIMIT 1",
                (capability.value,),
            )
            latest = self._stored_snapshot(latest_rows[0] if latest_rows else None)
            if latest is not None and latest["digest"] == digest:
                current = latest
            else:
                current_id = await transaction.write(
                    "INSERT INTO situation_snapshot(capability,digest,facts_json,created_at) "
                    "VALUES(?,?,?,?)",
                    (
                        capability.value,
                        digest,
                        json.dumps(facts, sort_keys=True, separators=(",", ":")),
                        now,
                    ),
                )
                current = {
                    "id": current_id,
                    "digest": digest,
                    "facts": facts,
                    "created_at": now,
                }

            marker_seen_at: int | None = None
            if since is not None:
                anchor_rows = await transaction.read(
                    "SELECT id,digest,facts_json,created_at FROM situation_snapshot "
                    "WHERE capability=? AND created_at<=? "
                    "ORDER BY created_at DESC,id DESC LIMIT 1",
                    (capability.value, since),
                )
                prior = self._stored_snapshot(anchor_rows[0] if anchor_rows else None)
                change_window = self._change_window("explicit", since, prior)
            elif viewer is not None:
                scope = f"sitrep:{capability.value}"
                if viewer.kind == "member":
                    marker_rows = await transaction.read(
                        "SELECT last_seen_at,last_seen_id FROM read_marker "
                        "WHERE member_id=? AND scope=?",
                        (viewer.id, scope),
                    )
                else:
                    marker_rows = await transaction.read(
                        "SELECT last_seen_at,last_seen_id FROM web_read_marker "
                        "WHERE account_id=? AND scope=?",
                        (viewer.id, scope),
                    )
                marker = marker_rows[0] if marker_rows else None
                marker_seen_at = int(marker["last_seen_at"]) if marker is not None else None
                anchor_rows = []
                if marker is not None and marker["last_seen_id"] is not None:
                    anchor_rows = await transaction.read(
                        "SELECT id,digest,facts_json,created_at FROM situation_snapshot "
                        "WHERE id=? AND capability=?",
                        (int(marker["last_seen_id"]), capability.value),
                    )
                prior = self._stored_snapshot(anchor_rows[0] if anchor_rows else None)
                change_window = self._change_window("viewer", marker_seen_at, prior)
            else:
                prior = latest
                change_window = self._change_window(
                    "previous",
                    int(latest["created_at"]) if latest is not None else None,
                    prior,
                )
        return _SnapshotSelection(prior, int(current["id"]), change_window)

    async def _changes(
        self,
        prior: dict[str, Any] | None,
        facts: list[dict[str, Any]],
    ) -> list[BriefingChange]:
        if prior is None or not isinstance(prior.get("facts"), list):
            return []
        previous: dict[str, dict[str, Any]] = {
            str(item.get("id")): item
            for item in prior["facts"]
            if isinstance(item, dict) and item.get("id")
        }
        changes: list[BriefingChange] = []
        for item in facts:
            old = previous.get(str(item["id"]))
            if old is None:
                kind = "new"
            elif old != item:
                kind = "changed"
            else:
                continue
            changes.append(
                BriefingChange(
                    kind,
                    str(item["id"]),
                    str(item["ref"]),
                    str(item["section"]),
                    str(item["title"]),
                    str(item["href"]),
                )
            )
        changes.extend(await self._explicit_resolutions(previous, facts))
        rank = {section: index for index, section in enumerate(SECTION_ORDER)}
        return sorted(changes, key=lambda change: (rank.get(change.section, 99), change.item_id))

    async def _explicit_resolutions(
        self, previous: Mapping[str, dict[str, Any]], facts: list[dict[str, Any]]
    ) -> list[BriefingChange]:
        result: list[BriefingChange] = []
        current = {str(item["id"]) for item in facts}
        missing = {item_id: item for item_id, item in previous.items() if item_id not in current}
        incident_ids = [
            int(item_id.removeprefix("incident:"))
            for item_id in missing
            if item_id.startswith("incident:")
        ]
        incidents = []
        if incident_ids:
            placeholders = ",".join("?" for _value in incident_ids)
            incidents = await self.database.read(
                f"SELECT id,status FROM incident WHERE id IN ({placeholders}) "  # noqa: S608
                "AND status NOT IN ('open','monitoring')",
                incident_ids,
            )
        for row in incidents:
            item_id = f"incident:{int(row['id'])}"
            item = missing[item_id]
            result.append(
                BriefingChange(
                    "resolved",
                    item_id,
                    str(item["ref"]),
                    "incidents",
                    str(item["title"]),
                    str(item["href"]),
                )
            )
        alert_ids = [
            int(item_id.removeprefix("alert:"))
            for item_id in missing
            if item_id.startswith("alert:")
        ]
        alerts = []
        if alert_ids:
            placeholders = ",".join("?" for _value in alert_ids)
            alerts = await self.database.read(
                f"SELECT id FROM alert WHERE id IN ({placeholders}) "  # noqa: S608
                "AND (all_clear_at IS NOT NULL OR cancelled_at IS NOT NULL)",
                alert_ids,
            )
        for row in alerts:
            item_id = f"alert:{int(row['id'])}"
            item = missing[item_id]
            result.append(
                BriefingChange(
                    "resolved",
                    item_id,
                    str(item["ref"]),
                    "alerts",
                    str(item["title"]),
                    str(item["href"]),
                )
            )
        event_ids = [
            int(item_id.removeprefix("welfare:"))
            for item_id in missing
            if item_id.startswith("welfare:")
        ]
        events = []
        if event_ids:
            placeholders = ",".join("?" for _value in event_ids)
            events = await self.database.read(
                f"SELECT id FROM watch_event WHERE id IN ({placeholders}) "  # noqa: S608
                "AND closed_at IS NOT NULL",
                event_ids,
            )
        for row in events:
            item_id = f"welfare:{int(row['id'])}"
            item = missing[item_id]
            result.append(
                BriefingChange(
                    "resolved",
                    item_id,
                    str(item["ref"]),
                    "welfare",
                    "Welfare event closed",
                    str(item["href"]),
                )
            )
        return result

    async def _advance_marker(
        self,
        viewer: BriefingViewer,
        capability: BriefingCapability,
        snapshot_id: int,
        now: int,
    ) -> None:
        scope = f"sitrep:{capability.value}"
        if viewer.kind == "member":
            await self.database.write(
                "INSERT INTO read_marker(member_id,scope,last_seen_at,last_seen_id) "
                "VALUES(?,?,?,?) ON CONFLICT(member_id,scope) DO UPDATE SET "
                "last_seen_at=excluded.last_seen_at,last_seen_id=excluded.last_seen_id",
                (viewer.id, scope, now, snapshot_id),
            )
        else:
            await self.database.write(
                "INSERT INTO web_read_marker(account_id,scope,last_seen_at,last_seen_id) "
                "VALUES(?,?,?,?) ON CONFLICT(account_id,scope) DO UPDATE SET "
                "last_seen_at=excluded.last_seen_at,last_seen_id=excluded.last_seen_id",
                (viewer.id, scope, now, snapshot_id),
            )

    async def _narration(
        self, snapshot: dict[str, Any], capability: BriefingCapability
    ) -> dict[str, Any]:
        key = f"{capability.value}:{snapshot['digest']}"
        now = int(self.clock.now().timestamp())
        rows = await self.database.read(
            "SELECT v FROM kv WHERE ns=? AND k=? AND (expires_at IS NULL OR expires_at>?)",
            (NARRATION_NAMESPACE, key, now),
        )
        if rows:
            try:
                cached = json.loads(str(rows[0]["v"]))
            except json.JSONDecodeError:
                cached = {}
            if isinstance(cached, dict):
                return {
                    "requested": True,
                    "text": cached.get("text"),
                    "outcome": str(cached.get("outcome") or "unavailable"),
                    "cached": True,
                }
        required = tuple(
            str(item["ref"])
            for item in snapshot["items"]
            if item["section"] == "alerts"
            or (item["section"] == "incidents" and item["severity"] in {"urgent", "critical"})
            or (item["section"] == "welfare" and item["hazard"])
            or (item["section"] == "weather" and item["hazard"])
        )
        if self.narrator is None:
            text, outcome = None, "disabled"
        else:
            authorized = {
                "generated_at": snapshot["generated_at"],
                "capability": snapshot["capability"],
                "items": snapshot["items"],
                "sources": snapshot["sources"],
                "changes": snapshot["changes"],
            }
            try:
                text, outcome = await self.narrator.narrate_situation(authorized, required)
            except Exception:
                text, outcome = None, "provider_error"
        text = _safe_text(text, 1200) if text else None
        stale_required = any(
            source["stale"]
            and any(
                source["id"] in item["source_ids"] and item["ref"] in required
                for item in snapshot["items"]
            )
            for source in snapshot["sources"]
        )
        if text and (
            any(reference not in text for reference in required)
            or (stale_required and "stale" not in text.casefold())
            or COORDINATE_PAIR.search(text)
            or COORDINATE_SPACE_PAIR.search(text)
            or URL.search(text)
        ):
            text, outcome = None, "validation_rejected"
        ttl = NARRATION_SUCCESS_TTL if text else NARRATION_FAILURE_TTL
        await self.database.write(
            "INSERT INTO kv(ns,k,v,expires_at,updated_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(ns,k) DO UPDATE SET v=excluded.v,expires_at=excluded.expires_at,"
            "updated_at=excluded.updated_at",
            (
                NARRATION_NAMESPACE,
                key,
                json.dumps({"text": text, "outcome": outcome}, separators=(",", ":")),
                now + ttl,
                now,
            ),
        )
        await self.database.write(
            "DELETE FROM kv WHERE ns=? AND k IN ("
            "SELECT k FROM kv WHERE ns=? ORDER BY updated_at DESC,k LIMIT -1 OFFSET ?)",
            (NARRATION_NAMESPACE, NARRATION_NAMESPACE, NARRATION_CACHE_MAX),
        )
        return {"requested": True, "text": text, "outcome": outcome, "cached": False}
