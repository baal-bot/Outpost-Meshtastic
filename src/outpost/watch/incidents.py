from __future__ import annotations

import math
import re
import uuid
from dataclasses import asdict, dataclass
from typing import Any

from outpost.clock import Clock
from outpost.store import Database
from outpost.store.members import Member

ACTIVE = ("open", "monitoring")
SEVERITY_RANK = {"critical": 4, "urgent": 3, "caution": 2, "info": 1}
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
    confirm_count: int
    dispute_count: int
    location_unconfirmed: int
    position_suppressed: int
    unverified: int
    flagged_for_review: int

    def json(self) -> dict[str, Any]:
        return asdict(self)


class IncidentService:
    def __init__(
        self,
        database: Database,
        clock: Clock,
        origin_node: str = "local",
        position_retention_hours: int = 168,
    ) -> None:
        self.database, self.clock, self.origin_node = database, clock, origin_node
        self.position_retention_seconds = position_retention_hours * 3_600

    @staticmethod
    def infer(text: str) -> str:
        words = re.findall(r"[a-z0-9]+", text.lower())
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

    async def duplicate(
        self,
        kind: str,
        title: str,
        lat: float | None,
        lon: float | None,
        radius_m: int = 500,
        window_minutes: int = 120,
    ) -> Incident | None:
        if lat is None or lon is None:
            return None
        cutoff = int(self.clock.now().timestamp()) - window_minutes * 60
        rows = await self.database.read(
            "SELECT * FROM incident WHERE status IN ('open','monitoring') AND type=? "
            "AND created_at>=? AND lat IS NOT NULL AND lon IS NOT NULL",
            (kind, cutoff),
        )
        tokens = set(re.findall(r"[a-z0-9]+", title.lower()))
        for row in rows:
            other = set(re.findall(r"[a-z0-9]+", row["title"].lower()))
            overlap = len(tokens & other) / max(1, len(tokens | other))
            if overlap >= 0.2 and self.distance_m(lat, lon, row["lat"], row["lon"]) <= radius_m:
                return self._row(row)
        return None

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
        lat, lon = (
            (None, None)
            if suppressed
            else (waypoint_coordinates or coordinates or self.coordinates(clean))
        )
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
        return await self.by_id(incident_id), None

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
            "SELECT lat,lon FROM member_position WHERE member_id=? AND expires_at>?",
            (member.id, now),
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
        return self._row(rows[0]) if rows else None

    async def list(
        self, *, status: str | None = None, kind: str | None = None, limit: int = 50
    ) -> list[Incident]:
        clauses, params = [], []
        if status:
            clauses.append("status=?")
            params.append(status)
        else:
            clauses.append("status IN ('open','monitoring')")
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
        return updated

    async def updates(self, incident_id: int, limit: int = 2) -> list[dict[str, Any]]:
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
        return updated

    async def expire_due(self) -> list[Incident]:
        now = int(self.clock.now().timestamp())
        rows = await self.database.read(
            "SELECT id FROM incident WHERE status IN ('open','monitoring') "
            "AND expires_at IS NOT NULL AND expires_at<=? ORDER BY id",
            (now,),
        )
        expired: list[Incident] = []
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
                expired.append(value)
        return expired
