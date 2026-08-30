from __future__ import annotations

import builtins
import json
import math
import re
import uuid
from dataclasses import asdict, dataclass
from typing import Any

from outpost.clock import Clock
from outpost.store import Database, Transaction
from outpost.store.members import Member

ACTIVE = ("open", "monitoring")
TERMINAL = ("resolved", "false_alarm", "expired")
SEVERITY_RANK = {"critical": 4, "urgent": 3, "caution": 2, "info": 1}
MATCH_WINDOW_SECONDS = 2 * 60 * 60
MATCH_RADIUS_M = 1_000
TAXONOMY: dict[str, tuple[tuple[str, ...], str, int]] = {
    "hazard": (("haz", "tree", "flood", "ice", "obstruction"), "caution", 48),
    "road": (("rd", "road", "closure", "washout", "bridge"), "caution", 72),
    "fire": (("smoke", "flames", "wildfire"), "urgent", 12),
    "medical": (("med", "injury", "ambulance", "unconscious"), "urgent", 6),
    "police": (("pol", "suspicious", "crime", "theft", "breakin"), "caution", 24),
    "utility": (("util", "power", "outage", "watermain", "gas"), "info", 48),
    "missing": (("lost", "missing", "mp", "overdue"), "urgent", 168),
    "animal": (("wildlife", "dog", "bear", "livestock"), "info", 24),
    "weather": (("wx", "storm", "tornado", "hail"), "caution", 12),
    "resource": (("shelter", "water", "fuel", "supplies", "food"), "info", 168),
    "other": ((), "info", 24),
}
ALIASES = {alias: kind for kind, (aliases, _, _) in TAXONOMY.items() for alias in aliases}
COORDS = re.compile(r"(?<![\d.])(-?\d{1,2}(?:\.\d+)?)\s*,?\s+(-?\d{1,3}(?:\.\d+)?)(?![\d.])")
POSITION_SHARE_NOTICE = re.compile(
    r"^📍\s+Meshtastic\s+\S+\s+has shared their position(?:\s+and requested a response "
    r"with your position)?\.?$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Incident:
    id: int
    uid: str
    local_ref: int
    type: str
    severity: str
    status: str
    title: str
    body: str | None
    lat: float | None
    lon: float | None
    location_text: str | None
    reporter_id: int | None
    reporter_label: str
    created_at: int
    updated_at: int
    expires_at: int | None
    resolved_at: int | None
    resolved_by: str | None
    resolution_note: str | None
    confirm_count: int
    dispute_count: int
    location_unconfirmed: int
    position_suppressed: int
    unverified: int
    flagged_for_review: int
    merged_into_id: int | None
    reconciliation_review: int
    notification_state: str | None
    notification_count: int

    def json(self) -> dict[str, Any]:
        return asdict(self)


class IncidentService:
    def __init__(
        self,
        database: Database,
        clock: Clock,
        origin_node: str = "local",
        position_retention_hours: int = 168,
        history_retention_days: int = 30,
        position_max_age_minutes: int = 30,
        dedupe_radius_m: int = 500,
        dedupe_window_minutes: int = 120,
    ) -> None:
        self.database, self.clock, self.origin_node = database, clock, origin_node
        self.position_retention_seconds = position_retention_hours * 3_600
        self.history_retention_days = history_retention_days
        self.position_max_age_seconds = position_max_age_minutes * 60
        self.dedupe_radius_m = dedupe_radius_m
        self.dedupe_window_minutes = dedupe_window_minutes

    @staticmethod
    def infer(text: str) -> str:
        words = [str(value) for value in re.findall(r"[a-z0-9]+", text.lower())]
        if words and words[0] in TAXONOMY:
            return words[0]
        for word in words:
            if word in ALIASES:
                return ALIASES[word]
            if word in TAXONOMY:
                return word
        return "other"

    @staticmethod
    def is_position_share_notice(text: str) -> bool:
        return POSITION_SHARE_NOTICE.fullmatch(text.strip()) is not None

    @staticmethod
    def emergency_keyword(text: str, keywords: list[str]) -> str | None:
        for keyword in keywords:
            pattern = rf"(?<![a-z0-9]){re.escape(keyword.lower())}(?![a-z0-9])"
            if re.search(pattern, text.lower()):
                return keyword
        return None

    @staticmethod
    def coordinates(text: str) -> tuple[float | None, float | None]:
        match = COORDS.search(text)
        if not match:
            return None, None
        lat, lon = float(match.group(1)), float(match.group(2))
        return (lat, lon) if -90 <= lat <= 90 and -180 <= lon <= 180 else (None, None)

    @staticmethod
    def _member_label(member: Member) -> str:
        return member.handle or member.mesh_id

    @staticmethod
    def distance_m(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
        radius = 6_371_000.0
        p1, p2 = math.radians(a_lat), math.radians(b_lat)
        dp, dl = math.radians(b_lat - a_lat), math.radians(b_lon - a_lon)
        value = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        return radius * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value))

    def _row(self, row: Any) -> Incident:
        fields = Incident.__dataclass_fields__
        return Incident(**{key: row[key] for key in fields})

    @staticmethod
    def _snapshot(incident: Incident) -> dict[str, object]:
        return {
            key: getattr(incident, key)
            for key in (
                "type",
                "severity",
                "status",
                "title",
                "body",
                "lat",
                "lon",
                "location_text",
                "expires_at",
            )
        }

    async def _append_provenance(
        self,
        store: Database | Transaction,
        incident_id: int,
        origin_uid: str,
        source_node: str,
        event_kind: str,
        payload: dict[str, object],
        *,
        actor: str,
        recorded_at: int,
        source_updated_at: int | None = None,
    ) -> None:
        await store.write(
            "INSERT INTO incident_provenance(incident_id,origin_uid,source_node,event_kind,"
            "payload_json,source_updated_at,recorded_at,actor) VALUES(?,?,?,?,?,?,?,?)",
            (
                incident_id,
                origin_uid,
                source_node,
                event_kind,
                json.dumps(payload, separators=(",", ":"), sort_keys=True),
                source_updated_at,
                recorded_at,
                actor[:160],
            ),
        )

    async def duplicate(
        self,
        kind: str,
        title: str,
        lat: float | None,
        lon: float | None,
        radius_m: int | None = None,
        window_minutes: int | None = None,
    ) -> Incident | None:
        if lat is None or lon is None:
            return None
        effective_radius = self.dedupe_radius_m if radius_m is None else radius_m
        effective_window = self.dedupe_window_minutes if window_minutes is None else window_minutes
        cutoff = int(self.clock.now().timestamp()) - effective_window * 60
        rows = await self.database.read(
            "SELECT * FROM incident WHERE status IN ('open','monitoring') AND type=? "
            "AND merged_into_id IS NULL AND created_at>=? AND lat IS NOT NULL AND lon IS NOT NULL",
            (kind, cutoff),
        )
        tokens = set(re.findall(r"[a-z0-9]+", title.lower()))
        for row in rows:
            other = set(re.findall(r"[a-z0-9]+", row["title"].lower()))
            overlap = len(tokens & other) / max(1, len(tokens | other))
            if (
                overlap >= 0.2
                and self.distance_m(lat, lon, row["lat"], row["lon"]) <= effective_radius
            ):
                return self._row(row)
        return None

    async def recent_position(self, member: Member) -> tuple[float, float] | None:
        now = int(self.clock.now().timestamp())
        rows = await self.database.read(
            "SELECT lat,lon FROM member_position WHERE member_id=? AND expires_at>? "
            "AND received_at>=? ORDER BY received_at DESC LIMIT 1",
            (member.id, now, now - self.position_max_age_seconds),
        )
        return (float(rows[0]["lat"]), float(rows[0]["lon"])) if rows else None

    async def create(
        self,
        text: str,
        member: Member | None,
        *,
        force: bool = False,
        operator_label: str = "operator",
        coordinates: tuple[float, float] | None = None,
    ) -> tuple[Incident | None, Incident | None]:
        clean = text.strip()
        suppressed = "-nopos" in clean.lower().split()
        clean = re.sub(r"(?i)(?:^|\s)-nopos(?:\s|$)", " ", clean).strip()
        waypoint_name: str | None = None
        waypoint_match = re.search(r"(?i)(?:^|\s)-wp\s+(\S+)", clean)
        waypoint_coordinates: tuple[float, float] | None = None
        if waypoint_match:
            waypoint_token = waypoint_match.group(1).lower()
            rows = await self.database.read(
                "SELECT name,latitude,longitude FROM waypoint WHERE slug=? COLLATE NOCASE",
                (waypoint_token,),
            )
            if not rows:
                raise ValueError("Waypoint not found. Use WPS for saved names.")
            waypoint_name = str(rows[0]["name"])
            waypoint_coordinates = (float(rows[0]["latitude"]), float(rows[0]["longitude"]))
            clean = (clean[: waypoint_match.start()] + clean[waypoint_match.end() :]).strip()
        if not clean:
            raise ValueError("REPORT needs details.")
        kind = self.infer(clean)
        severity, expiry_hours = TAXONOMY[kind][1:]
        parsed_lat, parsed_lon = self.coordinates(clean)
        resolved_coordinates = waypoint_coordinates or coordinates
        if resolved_coordinates is None and parsed_lat is not None and parsed_lon is not None:
            resolved_coordinates = (parsed_lat, parsed_lon)
        if resolved_coordinates is None and member is not None and not suppressed:
            resolved_coordinates = await self.recent_position(member)
        lat, lon = (None, None) if suppressed else resolved_coordinates or (None, None)
        title = clean[:64]
        if not force:
            similar = await self.duplicate(kind, title, lat, lon)
            if similar:
                return None, similar
        refs = await self.database.read(
            "SELECT local_ref FROM incident WHERE status IN ('open','monitoring') "
            "ORDER BY local_ref"
        )
        used = {int(row[0]) for row in refs}
        local_ref = next(number for number in range(1, len(used) + 2) if number not in used)
        now = int(self.clock.now().timestamp())
        location_text = (
            waypoint_name
            if waypoint_name and lat is not None
            else f"{lat:.5f},{lon:.5f}"
            if lat is not None and lon is not None
            else clean
        )
        incident_id = await self.database.write(
            """INSERT INTO incident(uid,local_ref,type,severity,title,body,lat,lon,location_text,
               reporter_id,reporter_label,origin_node,created_at,updated_at,expires_at,
               location_unconfirmed,position_suppressed,unverified)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                str(uuid.uuid4()),
                local_ref,
                kind,
                severity,
                title,
                clean,
                lat,
                lon,
                location_text,
                member.id if member else None,
                self._member_label(member) if member else operator_label,
                self.origin_node,
                now,
                now,
                now + expiry_hours * 3600,
                int(lat is None),
                int(suppressed),
                int(member is not None and member.trust == "guest"),
            ),
        )
        created = await self.by_id(incident_id)
        assert created is not None
        await self.database.write(
            "INSERT INTO incident_origin(origin_uid,incident_id,original_incident_id,origin_node,"
            "source_kind,first_seen_at,last_seen_at,source_updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                created.uid,
                created.id,
                created.id,
                self.origin_node,
                "local",
                now,
                now,
                now,
            ),
        )
        await self._append_provenance(
            self.database,
            created.id,
            created.uid,
            self.origin_node,
            "created",
            self._snapshot(created),
            actor=self._member_label(member) if member else operator_label,
            recorded_at=now,
            source_updated_at=now,
        )
        return created, None

    async def record_position(
        self, member: Member, lat: float, lon: float, *, prompt: bool
    ) -> None:
        if not -90 <= lat <= 90 or not -180 <= lon <= 180:
            raise ValueError("invalid position")
        now = int(self.clock.now().timestamp())
        await self.database.write(
            """INSERT INTO member_position(member_id,lat,lon,received_at,source,expires_at)
               VALUES(?,?,?,?,'position_app',?) ON CONFLICT(member_id) DO UPDATE SET
               lat=excluded.lat,lon=excluded.lon,received_at=excluded.received_at,
               source=excluded.source,expires_at=excluded.expires_at""",
            (member.id, lat, lon, now, now + self.position_retention_seconds),
        )
        if prompt:
            await self.database.write(
                """INSERT INTO pending_incident_location(member_id,lat,lon,created_at,expires_at)
                   VALUES(?,?,?,?,?) ON CONFLICT(member_id) DO UPDATE SET
                   lat=excluded.lat,lon=excluded.lon,created_at=excluded.created_at,
                   expires_at=excluded.expires_at""",
                (member.id, lat, lon, now, now + 10 * 60),
            )

    async def pending_position(self, member: Member) -> tuple[float, float] | None:
        now = int(self.clock.now().timestamp())
        rows = await self.database.read(
            "SELECT lat,lon FROM pending_incident_location WHERE member_id=? AND expires_at>?",
            (member.id, now),
        )
        return (float(rows[0]["lat"]), float(rows[0]["lon"])) if rows else None

    async def create_from_pending(
        self, text: str, member: Member
    ) -> tuple[Incident | None, Incident | None] | None:
        position = await self.pending_position(member)
        if position is None:
            return None
        await self.database.write(
            "DELETE FROM pending_incident_location WHERE member_id=?", (member.id,)
        )
        clean = re.sub(r"(?i)^REPORT!?\s+", "", text.strip())
        return await self.create(clean, member, coordinates=position)

    async def emergency_trigger(
        self, member: Member, text: str, cooldown_minutes: int
    ) -> tuple[Incident, bool]:
        now = int(self.clock.now().timestamp())
        recent = await self.database.read(
            "SELECT * FROM incident WHERE reporter_id=? AND source='emergency_keyword' "
            "AND created_at>=? ORDER BY created_at DESC LIMIT 1",
            (member.id, now - cooldown_minutes * 60),
        )
        if recent:
            incident = self._row(recent[0])
            await self.operator_update(
                incident.id, "update", text[:500], actor=self._member_label(member)
            )
            updated = await self.by_id(incident.id)
            assert updated is not None
            return updated, False
        positions = await self.database.read(
            "SELECT lat,lon FROM member_position WHERE member_id=? AND expires_at>? "
            "AND received_at>=? ORDER BY received_at DESC LIMIT 1",
            (member.id, now, now - self.position_max_age_seconds),
        )
        coordinates = (
            (float(positions[0]["lat"]), float(positions[0]["lon"])) if positions else None
        )
        created, _ = await self.create(text, member, force=True, coordinates=coordinates)
        assert created is not None
        await self.database.write(
            "UPDATE incident SET type='other',severity='urgent',source='emergency_keyword',"
            "expires_at=? WHERE id=?",
            (now + 6 * 3600, created.id),
        )
        updated = await self.by_id(created.id)
        assert updated is not None
        return updated, True

    async def by_id(self, incident_id: int) -> Incident | None:
        rows = await self.database.read("SELECT * FROM incident WHERE id=?", (incident_id,))
        return self._row(rows[0]) if rows else None

    async def by_ref(self, local_ref: int) -> Incident | None:
        rows = await self.database.read(
            "SELECT * FROM incident WHERE local_ref=? ORDER BY updated_at DESC LIMIT 1",
            (local_ref,),
        )
        if not rows:
            return None
        value = self._row(rows[0])
        seen = {value.id}
        while value.merged_into_id is not None:
            if value.merged_into_id in seen:
                raise RuntimeError("incident merge cycle detected")
            seen.add(value.merged_into_id)
            target = await self.by_id(value.merged_into_id)
            if target is None:
                raise RuntimeError("incident merge target is missing")
            value = target
        return value

    async def list(
        self, *, status: str | None = None, kind: str | None = None, limit: int = 50
    ) -> list[Incident]:
        clauses, params = [], []
        if status:
            clauses.append("status=?")
            params.append(status)
        else:
            clauses.append("status IN ('open','monitoring')")
        clauses.append("merged_into_id IS NULL")
        if kind:
            clauses.append("type=?")
            params.append(kind)
        rows = await self.database.read(
            f"SELECT * FROM incident WHERE {' AND '.join(clauses)} ORDER BY "  # noqa: S608
            "CASE severity WHEN 'critical' THEN 4 WHEN 'urgent' THEN 3 "
            "WHEN 'caution' THEN 2 ELSE 1 END DESC, updated_at DESC LIMIT ?",
            (*params, limit),
        )
        return [self._row(row) for row in rows]

    async def history(
        self, *, kind: str | None = None, limit: int = 100
    ) -> builtins.list[Incident]:
        cutoff = int(self.clock.now().timestamp()) - self.history_retention_days * 86_400
        clauses = [
            "status IN ('resolved','false_alarm','expired')",
            "merged_into_id IS NULL",
            "COALESCE(resolved_at,expires_at,updated_at)>=?",
        ]
        params: builtins.list[object] = [cutoff]
        if kind:
            clauses.append("type=?")
            params.append(kind)
        rows = await self.database.read(
            f"SELECT * FROM incident WHERE {' AND '.join(clauses)} "  # noqa: S608
            "ORDER BY COALESCE(resolved_at,expires_at,updated_at) DESC,id DESC LIMIT ?",
            (*params, max(1, min(limit, 200))),
        )
        return [self._row(row) for row in rows]

    async def origins(self, incident_id: int) -> builtins.list[dict[str, Any]]:
        rows = await self.database.read(
            "SELECT origin_uid,origin_node,source_kind,first_seen_at,last_seen_at,"
            "source_updated_at,original_incident_id FROM incident_origin "
            "WHERE incident_id=? ORDER BY first_seen_at,origin_uid",
            (incident_id,),
        )
        return [dict(row) for row in rows]

    async def provenance(self, incident_id: int, limit: int = 200) -> builtins.list[dict[str, Any]]:
        rows = await self.database.read(
            "SELECT DISTINCT p.id,p.incident_id,p.origin_uid,p.source_node,p.event_kind,"
            "p.payload_json,p.source_updated_at,p.recorded_at,p.actor "
            "FROM incident_provenance p WHERE p.incident_id=? OR p.origin_uid IN "
            "(SELECT origin_uid FROM incident_origin WHERE incident_id=?) "
            "ORDER BY p.recorded_at DESC,p.id DESC LIMIT ?",
            (incident_id, incident_id, max(1, min(limit, 500))),
        )
        values: builtins.list[dict[str, Any]] = []
        for row in reversed(rows):
            value = dict(row)
            try:
                value["payload"] = json.loads(str(value.pop("payload_json")))
            except json.JSONDecodeError:
                value["payload"] = {"error": "invalid historical payload"}
            values.append(value)
        return values

    async def match_candidates(self, incident_id: int) -> builtins.list[dict[str, Any]]:
        source = await self.by_id(incident_id)
        if (
            source is None
            or source.merged_into_id is not None
            or source.lat is None
            or source.lon is None
        ):
            return []
        rows = await self.database.read(
            "SELECT * FROM incident candidate WHERE candidate.id<>? "
            "AND candidate.merged_into_id IS NULL AND candidate.type=? "
            "AND ABS(candidate.created_at-?)<=? "
            "AND candidate.lat IS NOT NULL AND candidate.lon IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM incident_match_decision decision "
            "WHERE ((decision.source_incident_id=? "
            "AND decision.target_incident_id=candidate.id) OR "
            "(decision.source_incident_id=candidate.id AND decision.target_incident_id=?)) "
            "AND decision.state='rejected')",
            (
                source.id,
                source.type,
                source.created_at,
                MATCH_WINDOW_SECONDS,
                source.id,
                source.id,
            ),
        )
        source_tokens = set(re.findall(r"[a-z0-9]+", source.title.lower()))
        candidates: builtins.list[dict[str, Any]] = []
        for row in rows:
            distance = self.distance_m(source.lat, source.lon, row["lat"], row["lon"])
            if distance > MATCH_RADIUS_M:
                continue
            candidate_tokens = set(re.findall(r"[a-z0-9]+", str(row["title"]).lower()))
            overlap = len(source_tokens & candidate_tokens) / max(
                1, len(source_tokens | candidate_tokens)
            )
            if overlap < 0.15:
                continue
            time_delta = abs(source.created_at - int(row["created_at"]))
            score = round(
                0.4
                + 0.3 * (1 - distance / MATCH_RADIUS_M)
                + 0.2 * (1 - time_delta / MATCH_WINDOW_SECONDS)
                + 0.1 * overlap,
                4,
            )
            candidates.append(
                {
                    "id": int(row["id"]),
                    "uid": str(row["uid"]),
                    "local_ref": int(row["local_ref"]),
                    "title": str(row["title"]),
                    "severity": str(row["severity"]),
                    "status": str(row["status"]),
                    "distance_m": round(distance),
                    "time_delta_minutes": round(time_delta / 60),
                    "title_overlap": round(overlap, 3),
                    "score": score,
                    "reasons": [
                        f"same type: {source.type}",
                        f"{round(distance)} m apart (limit {MATCH_RADIUS_M} m)",
                        f"{round(time_delta / 60)} min apart (limit 120 min)",
                        f"title overlap {overlap:.0%}",
                    ],
                }
            )
        candidates.sort(key=lambda value: (-float(value["score"]), int(value["id"])))
        return candidates[:10]

    async def merge(self, source_id: int, target_id: int, actor: str) -> Incident:
        candidates = {int(value["id"]): value for value in await self.match_candidates(source_id)}
        if target_id not in candidates:
            raise ValueError("incidents are outside the bounded match rules")
        now = int(self.clock.now().timestamp())
        async with self.database.transaction() as transaction:
            rows = await transaction.read(
                "SELECT * FROM incident WHERE id IN (?,?) ORDER BY id", (source_id, target_id)
            )
            values = {int(row["id"]): self._row(row) for row in rows}
            source, target = values.get(source_id), values.get(target_id)
            if source is None or target is None:
                raise ValueError("incident not found")
            if source.merged_into_id is not None or target.merged_into_id is not None:
                raise ValueError("only canonical incidents can be merged")
            severity = max((target.severity, source.severity), key=SEVERITY_RANK.__getitem__)
            expires = max(
                (value for value in (target.expires_at, source.expires_at) if value is not None),
                default=None,
            )
            use_source_location = target.lat is None and source.lat is not None
            await transaction.write(
                "UPDATE incident SET severity=?,expires_at=?,lat=?,lon=?,location_text=?,"
                "location_unconfirmed=?,updated_at=?,reconciliation_review=0 WHERE id=?",
                (
                    severity,
                    expires,
                    source.lat if use_source_location else target.lat,
                    source.lon if use_source_location else target.lon,
                    source.location_text if use_source_location else target.location_text,
                    int(not (use_source_location or target.lat is not None)),
                    now,
                    target.id,
                ),
            )
            await transaction.write(
                "UPDATE incident SET merged_into_id=?,reconciliation_review=0 WHERE id=?",
                (target.id, source.id),
            )
            await transaction.write(
                "UPDATE incident_origin SET incident_id=?,last_seen_at=? WHERE incident_id=?",
                (target.id, now, source.id),
            )
            candidate = candidates[target_id]
            await transaction.write(
                "INSERT INTO incident_match_decision(source_incident_id,target_incident_id,state,"
                "score,reasons_json,reviewed_at,reviewed_by) VALUES(?,?,'merged',?,?,?,?) "
                "ON CONFLICT(source_incident_id,target_incident_id) DO UPDATE SET state='merged',"
                "score=excluded.score,reasons_json=excluded.reasons_json,"
                "reviewed_at=excluded.reviewed_at,reviewed_by=excluded.reviewed_by",
                (
                    source.id,
                    target.id,
                    candidate["score"],
                    json.dumps(candidate["reasons"], separators=(",", ":")),
                    now,
                    actor[:160],
                ),
            )
            policy: dict[str, object] = {
                "source_incident_id": source.id,
                "target_incident_id": target.id,
                "status": "target retained",
                "description": "target retained",
                "location": "source used only when target missing",
                "severity": "highest retained",
                "expiration": "latest retained",
                "resolution": "source advisory only",
            }
            await self._append_provenance(
                transaction,
                target.id,
                target.uid,
                self.origin_node,
                "merge",
                policy,
                actor=actor,
                recorded_at=now,
            )
            await self._append_provenance(
                transaction,
                source.id,
                source.uid,
                source.uid.split(":", 1)[0] if source.uid.startswith("!") else self.origin_node,
                "merged_into",
                policy,
                actor=actor,
                recorded_at=now,
            )
        merged = await self.by_id(target_id)
        assert merged is not None
        return merged

    async def unmerge(self, source_id: int, actor: str) -> Incident:
        source = await self.by_id(source_id)
        if source is None or source.merged_into_id is None:
            raise ValueError("incident is not merged")
        target_id = source.merged_into_id
        target = await self.by_id(target_id)
        assert target is not None
        now = int(self.clock.now().timestamp())
        payload: dict[str, object] = {
            "source_incident_id": source.id,
            "target_incident_id": target.id,
            "note": "canonical field corrections remain explicit operator actions",
        }
        async with self.database.transaction() as transaction:
            await transaction.write(
                "UPDATE incident SET merged_into_id=NULL,reconciliation_review=1 WHERE id=?",
                (source.id,),
            )
            await transaction.write(
                "UPDATE incident_origin SET incident_id=?,last_seen_at=? WHERE incident_id=? "
                "AND original_incident_id IN (SELECT id FROM incident "
                "WHERE id=? OR merged_into_id=?)",
                (source.id, now, target.id, source.id, source.id),
            )
            await transaction.write(
                "UPDATE incident_match_decision SET state='unmerged',reviewed_at=?,reviewed_by=? "
                "WHERE source_incident_id=? AND target_incident_id=?",
                (now, actor[:160], source.id, target.id),
            )
            for incident, event in ((target, "unmerge"), (source, "unmerged")):
                await self._append_provenance(
                    transaction,
                    incident.id,
                    incident.uid,
                    self.origin_node,
                    event,
                    payload,
                    actor=actor,
                    recorded_at=now,
                )
        restored = await self.by_id(source_id)
        assert restored is not None
        return restored

    async def reject_match(self, source_id: int, target_id: int, actor: str) -> None:
        candidates = {int(value["id"]): value for value in await self.match_candidates(source_id)}
        candidate = candidates.get(target_id)
        if candidate is None:
            raise ValueError("match candidate not found")
        source = await self.by_id(source_id)
        assert source is not None
        now = int(self.clock.now().timestamp())
        await self.database.write(
            "INSERT INTO incident_match_decision(source_incident_id,target_incident_id,state,"
            "score,reasons_json,reviewed_at,reviewed_by) VALUES(?,?,'rejected',?,?,?,?) "
            "ON CONFLICT(source_incident_id,target_incident_id) DO UPDATE SET state='rejected',"
            "score=excluded.score,reasons_json=excluded.reasons_json,"
            "reviewed_at=excluded.reviewed_at,reviewed_by=excluded.reviewed_by",
            (
                source_id,
                target_id,
                candidate["score"],
                json.dumps(candidate["reasons"], separators=(",", ":")),
                now,
                actor[:160],
            ),
        )
        await self._append_provenance(
            self.database,
            source.id,
            source.uid,
            self.origin_node,
            "match_rejected",
            {"target_incident_id": target_id},
            actor=actor,
            recorded_at=now,
        )

    async def operator_patch(
        self,
        incident_id: int,
        *,
        status: str | None,
        severity: str | None,
        resolution: str | None,
        actor: str,
    ) -> Incident:
        incident = await self.by_id(incident_id)
        if incident is None:
            raise ValueError("incident not found")
        if incident.merged_into_id is not None:
            raise ValueError("update the canonical incident instead")
        if status in TERMINAL and not (resolution or "").strip():
            raise ValueError("terminal incident status requires a resolution note")
        changes: dict[str, object] = {}
        assignments: builtins.list[str] = []
        params: builtins.list[object] = []
        if status is not None:
            assignments.append("status=?")
            params.append(status)
            changes["status"] = status
            if status in TERMINAL:
                assignments.extend(("resolved_at=?", "resolved_by=?"))
                params.extend((int(self.clock.now().timestamp()), actor[:160]))
            else:
                assignments.extend(("resolved_at=NULL", "resolved_by=NULL"))
        if severity is not None:
            assignments.append("severity=?")
            params.append(severity)
            changes["severity"] = severity
        if resolution is not None:
            note = resolution.strip()[:500]
            assignments.append("resolution_note=?")
            params.append(note or None)
            changes["resolution_note"] = note or None
        if not assignments:
            raise ValueError("no incident changes supplied")
        now = int(self.clock.now().timestamp())
        assignments.extend(("updated_at=?", "reconciliation_review=0"))
        params.extend((now, incident.id))
        await self.database.write(
            f"UPDATE incident SET {','.join(assignments)} WHERE id=?",  # noqa: S608
            tuple(params),
        )
        await self._append_provenance(
            self.database,
            incident.id,
            incident.uid,
            self.origin_node,
            "operator_correction",
            changes,
            actor=actor,
            recorded_at=now,
            source_updated_at=now,
        )
        updated = await self.by_id(incident.id)
        assert updated is not None
        return updated

    async def react(self, local_ref: int, member: Member, kind: str, note: str = "") -> Incident:
        if kind not in {"confirm", "dispute"}:
            raise ValueError("invalid reaction")
        incident = await self.by_ref(local_ref)
        if incident is None or incident.status not in ACTIVE:
            raise ValueError("No active incident.")
        prior = await self.database.read(
            "SELECT id FROM incident_update WHERE incident_id=? AND author_id=? AND kind=?",
            (incident.id, member.id, kind),
        )
        if prior:
            return incident
        seq_rows = await self.database.read(
            "SELECT COALESCE(MAX(seq),0)+1 FROM incident_update WHERE incident_id=?", (incident.id,)
        )
        now, seq = int(self.clock.now().timestamp()), int(seq_rows[0][0])
        await self.database.write(
            "INSERT INTO incident_update(uid,incident_id,seq,author_id,author_label,kind,body,"
            "created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                str(uuid.uuid4()),
                incident.id,
                seq,
                member.id,
                self._member_label(member),
                kind,
                note or None,
                now,
            ),
        )
        column = "confirm_count" if kind == "confirm" else "dispute_count"
        await self.database.write(
            f"UPDATE incident SET {column}={column}+1,updated_at=?,"  # noqa: S608
            "flagged_for_review=(dispute_count + ? > confirm_count + ?) WHERE id=?",
            (now, int(kind == "dispute"), int(kind == "confirm"), incident.id),
        )
        updated = await self.by_id(incident.id)
        assert updated is not None
        await self._append_provenance(
            self.database,
            incident.id,
            incident.uid,
            self.origin_node,
            kind,
            {"note": note or None, "member": self._member_label(member)},
            actor=self._member_label(member),
            recorded_at=now,
            source_updated_at=now,
        )
        return updated

    async def updates(self, incident_id: int, limit: int = 2) -> builtins.list[dict[str, Any]]:
        rows = await self.database.read(
            "SELECT seq,author_label,kind,body,created_at FROM incident_update "
            "WHERE incident_id=? ORDER BY seq DESC LIMIT ?",
            (incident_id, limit),
        )
        return [dict(row) for row in rows]

    async def operator_update(
        self, incident_id: int, kind: str, body: str = "", *, actor: str = "web:operator"
    ) -> Incident:
        if kind not in {"ack", "update"}:
            raise ValueError("Operator action must be ack or update.")
        incident = await self.by_id(incident_id)
        if incident is None or incident.status not in ACTIVE:
            raise ValueError("No active incident.")
        note = body.strip()[:500]
        if kind == "update" and not note:
            raise ValueError("An update note is required.")
        seq_rows = await self.database.read(
            "SELECT COALESCE(MAX(seq),0)+1 FROM incident_update WHERE incident_id=?",
            (incident.id,),
        )
        now, seq = int(self.clock.now().timestamp()), int(seq_rows[0][0])
        await self.database.write(
            "INSERT INTO incident_update(uid,incident_id,seq,author_label,kind,body,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), incident.id, seq, actor, kind, note or None, now),
        )
        status = "monitoring" if kind == "ack" else incident.status
        await self.database.write(
            "UPDATE incident SET status=?,updated_at=? WHERE id=?", (status, now, incident.id)
        )
        updated = await self.by_id(incident.id)
        assert updated is not None
        await self._append_provenance(
            self.database,
            incident.id,
            incident.uid,
            self.origin_node,
            "acknowledged" if kind == "ack" else "operator_update",
            {"body": note or None, "status": status},
            actor=actor,
            recorded_at=now,
            source_updated_at=now,
        )
        return updated

    async def expire_due(self) -> builtins.list[Incident]:
        now = int(self.clock.now().timestamp())
        rows = await self.database.read(
            "SELECT id FROM incident WHERE status IN ('open','monitoring') "
            "AND merged_into_id IS NULL AND expires_at IS NOT NULL AND expires_at<=? ORDER BY id",
            (now,),
        )
        expired: builtins.list[Incident] = []
        for row in rows:
            incident_id = int(row["id"])
            seq_rows = await self.database.read(
                "SELECT COALESCE(MAX(seq),0)+1 FROM incident_update WHERE incident_id=?",
                (incident_id,),
            )
            await self.database.write(
                "INSERT INTO incident_update(uid,incident_id,seq,author_label,kind,body,"
                "created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    str(uuid.uuid4()),
                    incident_id,
                    int(seq_rows[0][0]),
                    "system",
                    "status_change",
                    "Automatically expired",
                    now,
                ),
            )
            await self.database.write(
                "UPDATE incident SET status='expired',updated_at=? "
                "WHERE id=? AND status IN ('open','monitoring')",
                (now, incident_id),
            )
            value = await self.by_id(incident_id)
            if value is not None and value.status == "expired":
                await self._append_provenance(
                    self.database,
                    value.id,
                    value.uid,
                    self.origin_node,
                    "expired",
                    {"status": "expired"},
                    actor="system",
                    recorded_at=now,
                    source_updated_at=now,
                )
                expired.append(value)
        return expired
