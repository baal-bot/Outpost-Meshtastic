# ruff: noqa: E501
from __future__ import annotations

import csv
import html
import io
import json
import math
import re
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any, cast

from outpost.clock import Clock
from outpost.config import RetentionConfig
from outpost.csv_safety import csv_safe_row
from outpost.store import Database

ALERT_STAGE_TOKEN = re.compile(r"^alert:(\d+):stage:(\d+):repeat:(\d+)$")
ALERT_TOKEN = re.compile(r"^alert:(\d+):")
INCIDENT_TOKEN = re.compile(r"^incident:(\d+):")
CHECKIN_TOKEN = re.compile(r"^checkin:(\d+):")
MESH_ID = re.compile(r"^![0-9a-fA-F]{8}$")
MESH_ID_IN_TEXT = re.compile(r"![0-9a-fA-F]{8}")
COORDINATES_IN_TEXT = re.compile(
    r"(?<![\d.])(-?\d{1,2}(?:\.\d+)?)(?:\s*,\s*|\s+)"
    r"(-?\d{1,3}(?:\.\d+)?)(?![\d.])"
)


def _iso(value: int | float) -> str:
    return datetime.fromtimestamp(value, UTC).isoformat().replace("+00:00", "Z")


class IncidentReportService:
    """Build every incident artifact from one privacy-aware ordered timeline."""

    def __init__(
        self,
        database: Database,
        clock: Clock,
        retention: RetentionConfig | None = None,
        *,
        coarse_precision_m: int = 500,
    ) -> None:
        self.database = database
        self.clock = clock
        self.retention = retention or RetentionConfig()
        self.coarse_precision_m = coarse_precision_m

    @staticmethod
    def _decode(value: object) -> Any:
        if not isinstance(value, str):
            return value
        try:
            return json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return value

    def _coarse_location(self, latitude: object, longitude: object) -> dict[str, Any] | None:
        if latitude is None or longitude is None:
            return None
        lat, lon = float(str(latitude)), float(str(longitude))
        lat_step = self.coarse_precision_m / 111_320
        lon_step = self.coarse_precision_m / max(1, 111_320 * math.cos(math.radians(lat)))
        return {
            "lat": round(round(lat / lat_step) * lat_step, 5),
            "lon": round(round(lon / lon_step) * lon_step, 5),
            "precision": "coarse",
            "precision_m": self.coarse_precision_m,
        }

    def _privacy_text(self, value: str, identities: dict[str, str]) -> str:
        try:
            structured = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            structured = None
        if isinstance(structured, (dict, list)):
            return json.dumps(
                self._privacy_scrub(structured, identities),
                sort_keys=True,
                ensure_ascii=False,
            )

        def coordinates(match: re.Match[str]) -> str:
            if "." not in match.group(1) and "." not in match.group(2):
                return match.group(0)
            latitude, longitude = float(match.group(1)), float(match.group(2))
            if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                return match.group(0)
            location = self._coarse_location(latitude, longitude)
            assert location is not None
            return f"{location['lat']},{location['lon']} (coarse)"

        value = COORDINATES_IN_TEXT.sub(coordinates, value)
        return MESH_ID_IN_TEXT.sub(
            lambda match: identities.get(match.group(0), "Unnamed member"), value
        )

    def _privacy_scrub(self, value: Any, identities: dict[str, str], key: str = "") -> Any:
        if isinstance(value, dict):
            scrubbed = {
                item_key: self._privacy_scrub(item, identities, item_key)
                for item_key, item in value.items()
            }
            latitude_key = next(
                (item_key for item_key in value if item_key.lower() in {"lat", "latitude"}),
                None,
            )
            longitude_key = next(
                (item_key for item_key in value if item_key.lower() in {"lon", "lng", "longitude"}),
                None,
            )
            if (
                latitude_key is not None
                and longitude_key is not None
                and value[latitude_key] is not None
                and value[longitude_key] is not None
                and value.get("precision") != "coarse"
            ):
                location = self._coarse_location(value[latitude_key], value[longitude_key])
                assert location is not None
                scrubbed[latitude_key] = location["lat"]
                scrubbed[longitude_key] = location["lon"]
                scrubbed["location_precision"] = "coarse"
            return scrubbed
        if isinstance(value, list):
            return [self._privacy_scrub(item, identities, key) for item in value]
        if isinstance(value, tuple):
            return [self._privacy_scrub(item, identities, key) for item in value]
        if isinstance(value, str) and key not in {"source_node"}:
            return self._privacy_text(value, identities)
        return value

    @staticmethod
    def _member_label(handle: object, mesh_id: object = None) -> str:
        if handle:
            return f"@{str(handle).lstrip('@')}"
        if mesh_id and not MESH_ID.fullmatch(str(mesh_id)):
            return str(mesh_id)
        return "Unnamed member"

    @classmethod
    def _actor_label(cls, value: object, identities: dict[str, str]) -> str | None:
        if value is None:
            return None
        actor = str(value)
        if actor in identities:
            return identities[actor]
        if MESH_ID.fullmatch(actor):
            return "Unnamed member"
        return actor

    @staticmethod
    def _event(
        event_id: str,
        timestamp: int | float,
        category: str,
        kind: str,
        title: str,
        *,
        actor: str | None = None,
        detail: str | None = None,
        location: dict[str, Any] | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        value: dict[str, Any] = {
            "id": event_id,
            "timestamp": int(timestamp),
            "timestamp_iso": _iso(timestamp),
            "category": category,
            "kind": kind,
            "title": title,
            "actor": actor,
            "detail": detail,
            "location": location,
        }
        value.update(fields)
        return value

    async def _incident_scope(self, incident_id: int) -> tuple[dict[str, Any], list[int]]:
        rows = await self.database.read(
            "SELECT i.*,m.handle reporter_handle,m.mesh_id reporter_mesh_id "
            "FROM incident i LEFT JOIN member m ON m.id=i.reporter_id WHERE i.id=?",
            (incident_id,),
        )
        if not rows:
            raise ValueError("Incident not found.")
        requested = dict(rows[0])
        canonical_id = int(requested["merged_into_id"] or requested["id"])
        if canonical_id != incident_id:
            rows = await self.database.read(
                "SELECT i.*,m.handle reporter_handle,m.mesh_id reporter_mesh_id "
                "FROM incident i LEFT JOIN member m ON m.id=i.reporter_id WHERE i.id=?",
                (canonical_id,),
            )
            if not rows:
                raise ValueError("Canonical incident not found.")
        incident = dict(rows[0])
        scope_rows = await self.database.read(
            "SELECT id FROM incident WHERE id=? OR merged_into_id=? ORDER BY id",
            (canonical_id, canonical_id),
        )
        return incident, [int(row["id"]) for row in scope_rows]

    async def _identity_maps(self) -> tuple[dict[int, str], dict[str, str], dict[int, str]]:
        rows = await self.database.read("SELECT id,mesh_id,handle FROM member")
        by_id: dict[int, str] = {}
        by_mesh: dict[str, str] = {}
        mesh_by_id: dict[int, str] = {}
        for row in rows:
            member_id, mesh_id = int(row["id"]), str(row["mesh_id"])
            label = self._member_label(row["handle"], mesh_id)
            by_id[member_id] = label
            by_mesh[mesh_id] = label
            mesh_by_id[member_id] = mesh_id
        return by_id, by_mesh, mesh_by_id

    @staticmethod
    def _placeholders(values: list[int]) -> str:
        return ",".join("?" for _value in values)

    async def build(
        self,
        incident_id: int,
        *,
        since: int | None = None,
        window_kind: str = "full",
        window_label: str | None = None,
    ) -> dict[str, Any]:
        if since is not None and since < 0:
            raise ValueError("Timeline since time must be a non-negative Unix timestamp.")
        now = int(self.clock.now().timestamp())
        if since is not None and since > now:
            raise ValueError("Timeline since time cannot be in the future.")
        incident, incident_ids = await self._incident_scope(incident_id)
        placeholders = self._placeholders(incident_ids)
        labels_by_id, labels_by_mesh, mesh_by_id = await self._identity_maps()
        canonical_id = int(incident["id"])
        incident_end = int(
            incident["resolved_at"]
            or (incident["expires_at"] if incident["status"] == "expired" else 0)
            or now
        )
        incident_start = int(incident["created_at"])
        events: list[dict[str, Any]] = []
        reporter = labels_by_id.get(
            int(incident["reporter_id"]) if incident["reporter_id"] is not None else -1,
            self._actor_label(incident["reporter_label"], labels_by_mesh) or "Unknown reporter",
        )
        events.append(
            self._event(
                f"incident:{canonical_id}:opened",
                incident_start,
                "incident",
                "opened",
                f"Incident #{incident['local_ref']} opened",
                actor=reporter,
                detail=str(incident["title"]),
                location=self._coarse_location(incident["lat"], incident["lon"]),
                status="open",
            )
        )
        if incident["status"] in {"resolved", "false_alarm", "expired"}:
            terminal_at = (
                incident["resolved_at"] or incident["expires_at"] or incident["updated_at"]
            )
            events.append(
                self._event(
                    f"incident:{canonical_id}:terminal",
                    terminal_at,
                    "incident",
                    str(incident["status"]),
                    f"Incident marked {str(incident['status']).replace('_', ' ')}",
                    actor=self._actor_label(incident["resolved_by"], labels_by_mesh),
                    detail=str(incident["resolution_note"] or "No resolution note recorded."),
                    status=str(incident["status"]),
                )
            )

        update_rows = await self.database.read(
            f"SELECT u.*,m.handle author_handle,m.mesh_id author_mesh_id "  # noqa: S608
            "FROM incident_update u LEFT JOIN member m ON m.id=u.author_id "
            f"WHERE u.incident_id IN ({placeholders}) ORDER BY u.created_at,u.id",
            incident_ids,
        )
        source_times = {incident_start}
        source_member_ids = {
            int(incident["reporter_id"]) for _value in (0,) if incident["reporter_id"] is not None
        }
        for row in update_rows:
            source_times.add(int(row["created_at"]))
            if row["author_id"] is not None:
                source_member_ids.add(int(row["author_id"]))
            actor = (
                labels_by_id.get(int(row["author_id"]))
                if row["author_id"] is not None
                else self._actor_label(row["author_label"], labels_by_mesh)
            )
            kind = str(row["kind"])
            events.append(
                self._event(
                    f"incident-update:{row['id']}",
                    row["created_at"],
                    "incident",
                    kind,
                    kind.replace("_", " ").title(),
                    actor=actor,
                    detail=str(row["body"]) if row["body"] is not None else None,
                    location=self._coarse_location(row["lat"], row["lon"]),
                    sequence=int(row["seq"]),
                )
            )

        alert_rows = await self.database.read(
            f"SELECT * FROM alert WHERE incident_id IN ({placeholders}) ORDER BY raised_at,id",  # noqa: S608
            incident_ids,
        )
        alert_ids = [int(row["id"]) for row in alert_rows]
        alerts_by_id = {int(row["id"]): dict(row) for row in alert_rows}
        for row in alert_rows:
            alert_id = int(row["id"])
            events.append(
                self._event(
                    f"alert:{alert_id}:raised",
                    row["raised_at"],
                    "alert",
                    "raised",
                    f"{str(row['severity']).title()} alert raised",
                    actor=self._actor_label(row["raised_by"], labels_by_mesh),
                    detail=str(row["headline"]),
                    location=self._coarse_location(row["lat"], row["lon"]),
                    alert_id=alert_id,
                    severity=str(row["severity"]),
                )
            )
            ended_at = row["all_clear_at"] or row["cancelled_at"]
            if ended_at is not None:
                events.append(
                    self._event(
                        f"alert:{alert_id}:all-clear",
                        ended_at,
                        "alert",
                        "all_clear",
                        "Alert all-clear issued",
                        detail=str(incident["resolution_note"] or row["headline"]),
                        alert_id=alert_id,
                        queued_count=int(row["all_clear_queued"]),
                    )
                )

        ack_rows: list[Any] = []
        audience_rows: list[Any] = []
        if alert_ids:
            alert_placeholders = self._placeholders(alert_ids)
            ack_rows = await self.database.read(
                "SELECT aa.*,m.mesh_id,m.handle FROM alert_ack aa JOIN member m "  # noqa: S608
                f"ON m.id=aa.member_id WHERE aa.alert_id IN ({alert_placeholders}) "
                "ORDER BY aa.acked_at,aa.alert_id,aa.member_id",
                alert_ids,
            )
            audience_rows = await self.database.read(
                "SELECT * FROM alert_audience "  # noqa: S608
                f"WHERE alert_id IN ({alert_placeholders}) "
                "ORDER BY first_admitted_at,alert_id,destination,channel",
                alert_ids,
            )
        acks_by_alert: dict[int, list[Any]] = defaultdict(list)
        for row in ack_rows:
            alert_id = int(row["alert_id"])
            acks_by_alert[alert_id].append(row)
            events.append(
                self._event(
                    f"alert-ack:{alert_id}:{row['member_id']}",
                    row["acked_at"],
                    "acknowledgement",
                    "acknowledged",
                    "Alert acknowledged",
                    actor=self._member_label(row["handle"], row["mesh_id"]),
                    detail=str(row["note"]) if row["note"] is not None else None,
                    alert_id=alert_id,
                )
            )

        solicitation_rows = await self.database.read(
            "SELECT s.*,m.mesh_id,m.handle,e.name event_name FROM checkin_solicitation s "
            "JOIN member m ON m.id=s.member_id JOIN watch_event e ON e.id=s.event_id "
            "WHERE s.queued_at BETWEEN ? AND ? ORDER BY s.queued_at,s.event_id,s.member_id",
            (incident_start, incident_end),
        )
        solicitation_outbox_ids = {int(row["queue_item_id"]) for row in solicitation_rows}
        outbox_rows = await self.database.read(
            "SELECT * FROM outbound_work WHERE (dedupe_token LIKE 'alert:%' "
            "OR dedupe_token LIKE 'incident:%' OR dedupe_token LIKE 'checkin:%' "
            "OR id IN (SELECT queue_item_id FROM checkin_solicitation "
            "WHERE queued_at BETWEEN ? AND ?)) AND created_at BETWEEN ? AND ? "
            "ORDER BY created_at,id",
            (incident_start, incident_end, incident_start, incident_end),
        )
        relevant_outbox: list[Any] = []
        stage_groups: dict[tuple[int, int, int], list[Any]] = defaultdict(list)
        for row in outbox_rows:
            token = str(row["dedupe_token"] or "")
            stage_match = ALERT_STAGE_TOKEN.fullmatch(token)
            alert_match = ALERT_TOKEN.match(token)
            incident_match = INCIDENT_TOKEN.match(token)
            checkin_match = CHECKIN_TOKEN.match(token)
            relevant = False
            if alert_match and int(alert_match.group(1)) in alerts_by_id:
                relevant = True
            elif incident_match and int(incident_match.group(1)) in incident_ids:
                relevant = True
            elif checkin_match:
                checkin_id = int(checkin_match.group(1))
                linked = await self.database.read(
                    "SELECT 1 FROM checkin WHERE id=? AND created_at BETWEEN ? AND ?",
                    (checkin_id, incident_start, incident_end),
                )
                relevant = bool(linked)
            elif int(row["id"]) in solicitation_outbox_ids:
                relevant = True
            if not relevant:
                continue
            relevant_outbox.append(row)
            if stage_match and int(stage_match.group(1)) in alerts_by_id:
                stage_groups[
                    (
                        int(stage_match.group(1)),
                        int(stage_match.group(2)),
                        int(stage_match.group(3)),
                    )
                ].append(row)

        audit_targets = [f"incident:{value}" for value in incident_ids]
        audit_targets.extend(f"alert:{value}" for value in alert_ids)
        audit_rows: list[Any] = []
        if audit_targets:
            clauses = " OR ".join("target=? OR target LIKE ?" for _value in audit_targets)
            params: list[str] = []
            for target in audit_targets:
                params.extend((target, f"{target}:%"))
            audit_rows = await self.database.read(
                f"SELECT * FROM audit_log WHERE {clauses} ORDER BY created_at,id",  # noqa: S608
                params,
            )
        zero_stage_audits: list[tuple[int, int, Any]] = []
        for row in audit_rows:
            target = str(row["target"] or "")
            match = re.fullmatch(r"alert:(\d+):stage:(\d+)", target)
            if match and str(row["action"]) == "safety.delivery.zero":
                zero_stage_audits.append((int(match.group(1)), int(match.group(2)), row))

        stage_entries: list[dict[str, Any]] = []
        by_alert_stage: dict[int, list[tuple[tuple[int, int, int], list[Any]]]] = defaultdict(list)
        for key, rows in stage_groups.items():
            by_alert_stage[key[0]].append((key, rows))
        stage_boundaries: dict[int, list[float]] = defaultdict(list)
        for alert_id, groups in by_alert_stage.items():
            stage_boundaries[alert_id].extend(
                min(float(row["created_at"]) for row in rows) for _key, rows in groups
            )
        for alert_id, _stage, row in zero_stage_audits:
            stage_boundaries[alert_id].append(float(row["created_at"]))
        for alert_id, groups in by_alert_stage.items():
            ordered = sorted(
                groups, key=lambda item: min(float(row["created_at"]) for row in item[1])
            )
            for key, rows in ordered:
                _alert_id, stage, repeat = key
                started_at = min(float(row["created_at"]) for row in rows)
                later_boundaries = [
                    value for value in stage_boundaries[alert_id] if value > started_at
                ]
                next_at = min(later_boundaries) if later_boundaries else now + 1
                destinations = sorted({str(row["destination"]) for row in rows})
                channels = sorted({int(row["channel"]) for row in rows})
                broadcast = "^all" in destinations
                destination_labels = [
                    "All members (broadcast)"
                    if destination == "^all"
                    else labels_by_mesh.get(destination, "Unnamed member")
                    for destination in destinations
                ]
                stage_acks = [
                    row
                    for row in acks_by_alert.get(alert_id, [])
                    if started_at <= int(row["acked_at"]) < next_at
                    and (broadcast or str(row["mesh_id"]) in destinations)
                ]
                stage_entries.append(
                    self._event(
                        f"alert:{alert_id}:stage:{stage}:repeat:{repeat}",
                        started_at,
                        "alert_stage",
                        "admitted",
                        f"Alert stage {stage + 1} admitted",
                        detail=(
                            "Broadcast audience; recipient count is not knowable from a channel send."
                            if broadcast
                            else f"{len(destinations)} direct recipient(s) addressed."
                        ),
                        alert_id=alert_id,
                        stage=stage + 1,
                        repeat=repeat,
                        addressed_count=len(destinations),
                        addressed_kind="broadcast_endpoint" if broadcast else "direct_recipients",
                        acknowledged_count=len({int(row["member_id"]) for row in stage_acks}),
                        destinations=destination_labels,
                        channels=channels,
                        zero_recipients=False,
                    )
                )

        for alert_id, stage, row in zero_stage_audits:
            if alert_id not in alerts_by_id:
                continue
            detail = self._decode(row["detail"])
            detail_map = detail if isinstance(detail, dict) else {}
            stage_entries.append(
                self._event(
                    f"alert:{alert_id}:stage:{stage}:empty:{row['id']}",
                    row["created_at"],
                    "alert_stage",
                    "empty_audience",
                    f"Alert stage {stage + 1} reached nobody",
                    detail=str(detail_map.get("reason") or "empty_audience").replace("_", " "),
                    alert_id=alert_id,
                    stage=stage + 1,
                    repeat=None,
                    addressed_count=0,
                    addressed_kind="direct_recipients",
                    acknowledged_count=0,
                    destinations=[],
                    channels=list(detail_map.get("channels") or []),
                    zero_recipients=True,
                )
            )

        # Old alerts may predate durable outbox tokens. Preserve an honest, explicitly
        # legacy audience entry rather than inventing a stage history that was never stored.
        staged_alerts = {int(event["alert_id"]) for event in stage_entries}
        legacy_by_alert: dict[int, list[Any]] = defaultdict(list)
        for row in audience_rows:
            if int(row["alert_id"]) not in staged_alerts:
                legacy_by_alert[int(row["alert_id"])].append(row)
        for alert_id, rows in legacy_by_alert.items():
            destinations = sorted({str(row["destination"]) for row in rows})
            alert = alerts_by_id[alert_id]
            stage_entries.append(
                self._event(
                    f"alert:{alert_id}:legacy-audience",
                    min(int(row["first_admitted_at"]) for row in rows),
                    "alert_stage",
                    "legacy_audience",
                    "Alert audience admitted (legacy record)",
                    detail="Per-stage identity was not retained by this older record.",
                    alert_id=alert_id,
                    stage=max(1, int(alert["escalation_stage"])),
                    repeat=None,
                    addressed_count=len(destinations),
                    addressed_kind=(
                        "broadcast_endpoint" if "^all" in destinations else "direct_recipients"
                    ),
                    acknowledged_count=len(acks_by_alert.get(alert_id, [])),
                    destinations=[
                        "All members (broadcast)"
                        if destination == "^all"
                        else labels_by_mesh.get(destination, "Unnamed member")
                        for destination in destinations
                    ],
                    channels=sorted({int(row["channel"]) for row in rows}),
                    zero_recipients=False,
                )
            )
        events.extend(stage_entries)

        relevant_ids = [int(row["id"]) for row in relevant_outbox]
        attempt_rows: list[Any] = []
        if relevant_ids:
            outbox_placeholders = self._placeholders(relevant_ids)
            attempt_rows = await self.database.read(
                "SELECT oa.*,ow.destination,ow.channel,ow.state outbox_state,"  # noqa: S608
                "ow.outcome outbox_outcome,ow.last_error,ow.dedupe_token,ow.created_at,"
                "ml.toa_ms actual_toa_ms,ml.outcome message_outcome "
                "FROM outbound_attempt oa JOIN outbound_work ow ON ow.id=oa.outbox_id "
                "LEFT JOIN message_log ml ON ml.id=oa.message_log_id "
                f"WHERE oa.outbox_id IN ({outbox_placeholders}) "
                "ORDER BY oa.started_at,oa.id",
                relevant_ids,
            )
        attempted_ids = {int(row["outbox_id"]) for row in attempt_rows}
        for row in attempt_rows:
            destination = str(row["destination"])
            actual = int(row["actual_toa_ms"]) if row["actual_toa_ms"] is not None else None
            outcome = (
                row["message_outcome"] or row["outbox_outcome"] or row["outcome"] or row["state"]
            )
            events.append(
                self._event(
                    f"transmission-attempt:{row['id']}",
                    row["completed_at"] or row["started_at"],
                    "transmission",
                    str(row["state"]),
                    "Mesh transmission attempt",
                    detail=str(row["error"] or row["last_error"] or outcome),
                    destination=(
                        "All members (broadcast)"
                        if destination == "^all"
                        else labels_by_mesh.get(destination, "Unnamed member")
                    ),
                    channel=int(row["channel"]),
                    attempt=int(row["attempt_no"]),
                    outcome=str(outcome),
                    actual_toa_ms=actual,
                    estimated_toa_ms=int(row["estimated_toa_ms"]),
                    packet_id=row["packet_id"],
                    correlation=str(row["dedupe_token"]),
                )
            )
        for row in relevant_outbox:
            if int(row["id"]) in attempted_ids:
                continue
            destination = str(row["destination"])
            events.append(
                self._event(
                    f"outbound-work:{row['id']}",
                    row["completed_at"] or row["created_at"],
                    "transmission",
                    str(row["state"]),
                    "Mesh transmission queued without an attempt",
                    detail=str(row["last_error"] or row["outcome"] or row["state"]),
                    destination=(
                        "All members (broadcast)"
                        if destination == "^all"
                        else labels_by_mesh.get(destination, "Unnamed member")
                    ),
                    channel=int(row["channel"]),
                    attempt=0,
                    outcome=str(row["outcome"] or row["state"]),
                    actual_toa_ms=None,
                    estimated_toa_ms=None,
                    packet_id=row["packet_id"],
                    correlation=str(row["dedupe_token"]),
                )
            )

        welfare_rows = await self.database.read(
            "SELECT c.*,m.mesh_id,m.handle,e.name event_name,e.roster_policy "
            "FROM checkin c JOIN member m ON m.id=c.member_id "
            "LEFT JOIN watch_event e ON e.id=c.event_id "
            "WHERE c.created_at BETWEEN ? AND ? ORDER BY c.created_at,c.id",
            (incident_start, incident_end),
        )
        for row in welfare_rows:
            events.append(
                self._event(
                    f"checkin:{row['id']}",
                    row["created_at"],
                    "welfare",
                    str(row["status"]),
                    f"Concurrent welfare check-in: {str(row['status']).replace('_', ' ')}",
                    actor=self._member_label(row["handle"], row["mesh_id"]),
                    detail=(
                        f"{row['event_name'] or 'No watch event'}"
                        + (f" · {row['note']}" if row["note"] else "")
                    ),
                    location=self._coarse_location(row["lat"], row["lon"]),
                    watch_event_id=row["event_id"],
                    notification_state=row["notification_state"],
                    notification_count=int(row["notification_count"]),
                    context_scope="overlapping_watch_window",
                )
            )
        for row in solicitation_rows:
            events.append(
                self._event(
                    f"checkin-solicitation:{row['event_id']}:{row['member_id']}",
                    row["queued_at"],
                    "welfare",
                    "solicited",
                    "Welfare check requested",
                    actor=self._member_label(row["handle"], row["mesh_id"]),
                    detail=str(row["event_name"]),
                    watch_event_id=int(row["event_id"]),
                    outbox_id=int(row["queue_item_id"]),
                )
            )

        for row in audit_rows:
            events.append(
                self._event(
                    f"audit:{row['id']}",
                    row["created_at"],
                    "audit",
                    str(row["action"]),
                    str(row["action"]).replace(".", " · ").replace("_", " "),
                    actor=self._actor_label(
                        f"{row['actor_kind']}:{row['actor_ref']}", labels_by_mesh
                    ),
                    detail=(
                        json.dumps(self._decode(row["detail"]), sort_keys=True, ensure_ascii=False)
                        if row["detail"] is not None
                        else None
                    ),
                    outcome=str(row["outcome"]),
                    target=row["target"],
                )
            )

        provenance_rows = await self.database.read(
            f"SELECT * FROM incident_provenance WHERE incident_id IN ({placeholders}) "  # noqa: S608
            "ORDER BY recorded_at,id",
            incident_ids,
        )
        for row in provenance_rows:
            events.append(
                self._event(
                    f"provenance:{row['id']}",
                    row["recorded_at"],
                    "provenance",
                    str(row["event_kind"]),
                    f"Provenance: {str(row['event_kind']).replace('_', ' ')}",
                    actor=self._actor_label(row["actor"], labels_by_mesh),
                    detail=json.dumps(
                        self._decode(row["payload_json"]), sort_keys=True, ensure_ascii=False
                    ),
                    source_node=str(row["source_node"]),
                    source_updated_at=row["source_updated_at"],
                )
            )

        origin_rows = await self.database.read(
            f"SELECT * FROM incident_origin WHERE incident_id IN ({placeholders}) "  # noqa: S608
            "ORDER BY first_seen_at,origin_uid",
            incident_ids,
        )
        for row in origin_rows:
            events.append(
                self._event(
                    f"origin:{row['origin_uid']}",
                    row["first_seen_at"],
                    "provenance",
                    "origin_observed",
                    "Incident origin observed",
                    detail=f"{row['source_kind']} origin from {row['origin_node']}",
                    source_node=str(row["origin_node"]),
                )
            )

        # Include only ingress packets that are tied by member identity and the exact
        # recorded incident/update second. Other contemporaneous mesh traffic is not
        # incident evidence and must not leak into an export.
        source_mesh_ids = [mesh_by_id[value] for value in source_member_ids if value in mesh_by_id]
        if source_times and source_mesh_ids:
            time_values = sorted(source_times)
            time_placeholders = self._placeholders(time_values)
            mesh_placeholders = ",".join("?" for _value in source_mesh_ids)
            packet_rows = await self.database.read(
                "SELECT * FROM message_log WHERE direction='in' "  # noqa: S608
                f"AND created_at IN ({time_placeholders}) "
                f"AND peer_mesh_id IN ({mesh_placeholders}) ORDER BY created_at,id",
                (*time_values, *source_mesh_ids),
            )
            for row in packet_rows:
                events.append(
                    self._event(
                        f"message:{row['id']}",
                        row["created_at"],
                        "mesh",
                        "source_message",
                        "Source mesh message received",
                        actor=labels_by_mesh.get(str(row["peer_mesh_id"]), "Unnamed member"),
                        detail=str(row["text"]) if row["text"] is not None else "Binary payload",
                        channel=int(row["channel"]),
                        outcome=str(row["outcome"] or "received"),
                        snr=row["rx_snr"],
                        rssi=row["rx_rssi"],
                        hops=row["hops"],
                        latency_ms=row["latency_ms"],
                        transport=row["transport"],
                    )
                )

        category_order = {
            "incident": 10,
            "mesh": 20,
            "alert": 30,
            "alert_stage": 40,
            "acknowledgement": 50,
            "welfare": 60,
            "transmission": 70,
            "audit": 80,
            "provenance": 90,
        }
        events.sort(
            key=lambda item: (
                int(item["timestamp"]),
                category_order.get(str(item["category"]), 99),
                str(item["id"]),
            )
        )
        full_events = events
        events = [
            event for event in full_events if since is None or int(event["timestamp"]) >= since
        ]
        stage_events = [event for event in events if event["category"] == "alert_stage"]
        transmission_events = [event for event in events if event["category"] == "transmission"]
        summary = {
            "event_count": len(full_events),
            "window_event_count": len(events),
            "alert_count": len(
                {
                    int(event["alert_id"])
                    for event in events
                    if event["category"] == "alert" and event.get("alert_id") is not None
                }
            ),
            "alert_stage_count": len(stage_events),
            "zero_recipient_stages": sum(bool(event["zero_recipients"]) for event in stage_events),
            "addressed_count": sum(int(event["addressed_count"]) for event in stage_events),
            "acknowledged_count": sum(event["category"] == "acknowledgement" for event in events),
            "welfare_checkin_count": sum(
                event["category"] == "welfare" and event["kind"] != "solicited" for event in events
            ),
            "transmission_attempt_count": len(transmission_events),
            "actual_airtime_ms": sum(
                int(event["actual_toa_ms"] or 0) for event in transmission_events
            ),
            "estimated_airtime_ms": sum(
                int(event["estimated_toa_ms"] or 0) for event in transmission_events
            ),
        }
        if window_label is None:
            window_label = (
                f"Since requested time {_iso(since)} (inclusive)"
                if since is not None
                else "Complete retained incident record"
            )
        incident_value = {
            "id": canonical_id,
            "requested_id": incident_id,
            "local_ref": int(incident["local_ref"]),
            "uid": str(incident["uid"]),
            "type": str(incident["type"]),
            "severity": str(incident["severity"]),
            "status": str(incident["status"]),
            "title": str(incident["title"]),
            "body": incident["body"],
            "reporter": reporter,
            "location_text": incident["location_text"],
            "location": self._coarse_location(incident["lat"], incident["lon"]),
            "created_at": incident_start,
            "created_at_iso": _iso(incident_start),
            "updated_at": int(incident["updated_at"]),
            "resolved_at": incident["resolved_at"],
            "resolved_by": self._actor_label(incident["resolved_by"], labels_by_mesh),
            "resolution_note": incident["resolution_note"],
            "merged_incident_ids": [value for value in incident_ids if value != canonical_id],
        }
        report = {
            "report_version": 1,
            "generated_at": now,
            "generated_at_iso": _iso(now),
            "incident": incident_value,
            "change_window": {
                "kind": window_kind,
                "since": since,
                "since_iso": _iso(since) if since is not None else None,
                "inclusive": True,
                "label": window_label,
            },
            "summary": summary,
            "privacy": {
                "location_precision": "coarse",
                "coarse_precision_m": self.coarse_precision_m,
                "exact_positions_included": False,
                "identity_policy": "handles preferred; unnamed mesh identities suppressed",
            },
            "retention": {
                "incident_history_days": self.retention.incident_history_days,
                "watch_history_days": self.retention.watch_history_days,
                "outbound_history_days": self.retention.outbound_history_days,
                "message_log_days": self.retention.message_log_days,
                "incident_evidence_protected": True,
            },
            "timeline": events,
        }
        return cast(dict[str, Any], self._privacy_scrub(report, labels_by_mesh))

    async def handover(self, incident_id: int, account_id: int) -> dict[str, Any]:
        now = int(self.clock.now().timestamp())
        incident, _incident_ids = await self._incident_scope(incident_id)
        scope = f"incident-report:{int(incident['id'])}"
        rows = await self.database.read(
            "SELECT last_seen_at FROM web_read_marker WHERE account_id=? AND scope=?",
            (account_id, scope),
        )
        since = int(rows[0]["last_seen_at"]) if rows else None
        label = (
            f"Since your last look at {_iso(since)} (inclusive)"
            if since is not None
            else "First handover look; showing the complete retained record"
        )
        report = await self.build(
            incident_id,
            since=since,
            window_kind="viewer" if since is not None else "first_look",
            window_label=label,
        )
        await self.database.write(
            "INSERT INTO web_read_marker(account_id,scope,last_seen_at,last_seen_id) "
            "VALUES(?,?,?,NULL) ON CONFLICT(account_id,scope) DO UPDATE SET "
            "last_seen_at=excluded.last_seen_at,last_seen_id=NULL",
            (account_id, scope, now),
        )
        return report

    @staticmethod
    def csv_export(report: dict[str, Any]) -> str:
        fields = (
            "event_id",
            "timestamp",
            "category",
            "kind",
            "title",
            "actor",
            "detail",
            "location",
            "alert_id",
            "stage",
            "repeat",
            "addressed_count",
            "addressed_kind",
            "acknowledged_count",
            "destinations",
            "channels",
            "destination",
            "channel",
            "attempt",
            "outcome",
            "actual_toa_ms",
            "estimated_toa_ms",
            "packet_id",
            "correlation",
            "snr",
            "rssi",
            "hops",
            "latency_ms",
            "transport",
            "watch_event_id",
            "context_scope",
            "source_node",
            "source_updated_at",
            "target",
        )
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for event in report["timeline"]:
            location = event.get("location")
            row = {
                "event_id": event["id"],
                "timestamp": event["timestamp_iso"],
                "category": event["category"],
                "kind": event["kind"],
                "title": event["title"],
                "actor": event.get("actor"),
                "detail": event.get("detail"),
                "location": (
                    f"{location['lat']},{location['lon']} ({location['precision']})"
                    if location
                    else None
                ),
                "alert_id": event.get("alert_id"),
                "stage": event.get("stage"),
                "repeat": event.get("repeat"),
                "addressed_count": event.get("addressed_count"),
                "addressed_kind": event.get("addressed_kind"),
                "acknowledged_count": event.get("acknowledged_count"),
                "destinations": json.dumps(event.get("destinations"), ensure_ascii=False)
                if event.get("destinations") is not None
                else None,
                "channels": json.dumps(event.get("channels"))
                if event.get("channels") is not None
                else None,
                "destination": event.get("destination"),
                "channel": event.get("channel"),
                "attempt": event.get("attempt"),
                "outcome": event.get("outcome"),
                "actual_toa_ms": event.get("actual_toa_ms"),
                "estimated_toa_ms": event.get("estimated_toa_ms"),
                "packet_id": event.get("packet_id"),
                "correlation": event.get("correlation"),
                "snr": event.get("snr"),
                "rssi": event.get("rssi"),
                "hops": event.get("hops"),
                "latency_ms": event.get("latency_ms"),
                "transport": event.get("transport"),
                "watch_event_id": event.get("watch_event_id"),
                "context_scope": event.get("context_scope"),
                "source_node": event.get("source_node"),
                "source_updated_at": event.get("source_updated_at"),
                "target": event.get("target"),
            }
            writer.writerow(csv_safe_row(row))
        return output.getvalue()

    @staticmethod
    def offline_html(report: dict[str, Any]) -> str:
        incident = report["incident"]
        summary = report["summary"]

        def esc(value: object) -> str:
            return html.escape("" if value is None else str(value))

        rows = []
        for event in report["timeline"]:
            delivery = ""
            if event["category"] == "alert_stage":
                audience = ", ".join(event.get("destinations") or []) or "none"
                channels = ", ".join(str(value) for value in event.get("channels") or []) or "none"
                delivery = (
                    f"<br><b>Addressed:</b> {esc(event.get('addressed_count', 0))} · "
                    f"<b>Acknowledged:</b> {esc(event.get('acknowledged_count', 0))}"
                    f"<br><b>Audience:</b> {esc(audience)} · <b>Channels:</b> {esc(channels)}"
                )
            if event["category"] == "transmission":
                actual = event.get("actual_toa_ms")
                delivery = (
                    f"<br><b>Destination:</b> {esc(event.get('destination'))} · "
                    f"<b>Channel:</b> {esc(event.get('channel'))}"
                    f"<br><b>Outcome:</b> {esc(event.get('outcome'))} · "
                    f"<b>Time on air:</b> {esc(str(actual) + ' ms' if actual is not None else 'not measured')}"
                )
            location = event.get("location")
            location_text = (
                f"<br><b>Coarse location:</b> {esc(location['lat'])}, {esc(location['lon'])}"
                if location
                else ""
            )
            rows.append(
                "<tr>"
                f"<td><time>{esc(event['timestamp_iso'])}</time></td>"
                f"<td><span class=tag>{esc(event['category'])}</span></td>"
                f"<td><strong>{esc(event['title'])}</strong>"
                + (f"<br><small>{esc(event.get('actor'))}</small>" if event.get("actor") else "")
                + (f"<p>{esc(event.get('detail'))}</p>" if event.get("detail") else "")
                + delivery
                + location_text
                + "</td></tr>"
            )
        return """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Incident {ref} after-action record</title><style>
:root{{font-family:system-ui,sans-serif;color:#18201d;background:#fff}}body{{max-width:1100px;margin:0 auto;padding:32px}}
header{{border-bottom:3px solid #183d31;padding-bottom:18px}}h1{{margin:.2rem 0}}.meta{{color:#53625d}}
.summary{{display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin:18px 0}}.summary div{{border:1px solid #ccd5d1;padding:10px}}
.summary b{{display:block;font-size:1.35rem}}table{{width:100%;border-collapse:collapse}}th,td{{padding:10px;border-bottom:1px solid #dbe2df;text-align:left;vertical-align:top}}
th{{background:#eef3f1}}time,small{{color:#5c6d66}}p{{margin:.35rem 0;white-space:pre-wrap}}.tag{{font:700 .72rem ui-monospace,monospace;text-transform:uppercase}}
.notice{{padding:10px;border:1px solid #bea75e;background:#fff9e7}}footer{{margin-top:24px;color:#5c6d66;font-size:.8rem}}
@media(max-width:760px){{body{{padding:16px}}.summary{{grid-template-columns:1fr 1fr}}table,tbody,tr,td{{display:block}}thead{{display:none}}td:first-child{{padding-bottom:2px;border:0}}}}
@media print{{body{{max-width:none;padding:0}}.summary{{break-inside:avoid}}tr{{break-inside:avoid}}}}
</style></head><body><header><small>OUTPOST · AFTER-ACTION / HANDOVER RECORD</small><h1>INC {ref} · {title}</h1>
<div class="meta">{severity} · {status} · opened {opened} · generated {generated}</div></header>
<p class="notice">{window}. Locations are coarsened to {precision} metres. Handles are preferred; unnamed mesh identifiers are suppressed.</p>
<section class="summary"><div><b>{events}</b>events</div><div><b>{alerts}</b>alerts</div><div><b>{zero}</b>zero-recipient stages</div><div><b>{acks}</b>acknowledgements</div><div><b>{airtime}</b>actual airtime (ms)</div></section>
<section><h2>Incident overview</h2><p><b>Reporter:</b> {reporter}</p><p>{narrative}</p>{location}{resolution}</section>
<h2>Ordered timeline</h2><table><thead><tr><th>Time (UTC)</th><th>Type</th><th>Record</th></tr></thead><tbody>{rows}</tbody></table>
<footer>Self-contained offline record · report schema v{version} · incident evidence retained with the incident for {retention} days.</footer></body></html>""".format(
            ref=esc(incident["local_ref"]),
            title=esc(incident["title"]),
            severity=esc(str(incident["severity"]).upper()),
            status=esc(str(incident["status"]).replace("_", " ").upper()),
            opened=esc(incident["created_at_iso"]),
            generated=esc(report["generated_at_iso"]),
            window=esc(report["change_window"]["label"]),
            precision=esc(report["privacy"]["coarse_precision_m"]),
            events=esc(summary["window_event_count"]),
            alerts=esc(summary["alert_count"]),
            zero=esc(summary["zero_recipient_stages"]),
            acks=esc(summary["acknowledged_count"]),
            airtime=esc(summary["actual_airtime_ms"]),
            reporter=esc(incident["reporter"]),
            narrative=esc(
                incident["body"] or incident["location_text"] or "No additional narrative recorded."
            ),
            location=(
                f"<p><b>Coarse location:</b> {esc(incident['location']['lat'])}, "
                f"{esc(incident['location']['lon'])}</p>"
                if incident["location"]
                else ""
            ),
            resolution=(
                f"<p><b>Resolution:</b> {esc(incident['resolution_note'])}</p>"
                if incident["resolution_note"]
                else ""
            ),
            rows="".join(rows)
            or '<tr><td colspan="3">No events in this handover window.</td></tr>',
            version=esc(report["report_version"]),
            retention=esc(report["retention"]["incident_history_days"]),
        )
