from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, replace
from typing import Any, cast

from outpost.clock import Clock
from outpost.config import Config, EscalationPolicy, EscalationStage
from outpost.store import Database
from outpost.store.members import Member
from outpost.transport.governor import AirtimeGovernor, OutboundItem
from outpost.transport.models import Severity, TrafficClass

from .delivery import AudienceDelivery, AudienceNotifier


@dataclass(frozen=True)
class Alert:
    id: int
    incident_id: int | None
    incident_ref: int | None
    severity: str
    headline: str
    source: str
    channels: str
    raised_by: str
    raised_at: int
    expires_at: int | None
    cancelled_at: int | None
    escalation_stage: int
    next_escalation_at: int | None
    ack_required: int
    broadcast_count: int
    repeat_count: int
    all_clear_at: int | None
    all_clear_queued: int
    ack_count: int
    lat: float | None
    lon: float | None
    radius_m: int | None
    delivery_state: str
    last_delivery_count: int
    delivery_error_at: int | None
    coalesced: bool = False

    def json(self) -> dict[str, Any]:
        value = asdict(self)
        value["channels"] = json.loads(self.channels)
        return value


class AlertService:
    def __init__(
        self, database: Database, governor: AirtimeGovernor, clock: Clock, config: Config
    ) -> None:
        self.database, self.governor, self.clock, self.config = database, governor, clock, config
        self.notifier = AudienceNotifier(database, governor, clock)

    def _row(self, row: Any) -> Alert:
        return Alert(**{key: row[key] for key in Alert.__dataclass_fields__ if key != "coalesced"})

    def render(self, severity: str, headline: str) -> str:
        marker = {"caution": "!", "urgent": "⚠", "critical": "⚠⚠"}[severity]
        return f"{marker}{severity.upper()} {headline} {self.config.node.short_name}"

    def _policy(self, severity: str) -> EscalationPolicy:
        return cast(EscalationPolicy, getattr(self.config.watch.escalation, severity))

    async def operational_json(self, alert: Alert) -> dict[str, Any]:
        value = alert.json()
        policy = self._policy(alert.severity)
        next_stage = (
            policy.stages[alert.escalation_stage]
            if alert.escalation_stage < len(policy.stages)
            else None
        )
        rows = await self.database.read(
            """SELECT m.mesh_id,m.handle,aa.acked_at,aa.note FROM alert_ack aa
               JOIN member m ON m.id=aa.member_id WHERE aa.alert_id=? ORDER BY aa.acked_at""",
            (alert.id,),
        )
        audiences = await self.database.read(
            "SELECT destination,channel,first_admitted_at,last_admitted_at,admissions "
            "FROM alert_audience WHERE alert_id=? ORDER BY destination,channel",
            (alert.id,),
        )
        value.update(
            {
                "stage_total": len(policy.stages),
                "next_action": next_stage.model_dump() if next_stage else None,
                "repeat_max": self.config.watch.alert_repeat_max,
                "repeat_remaining": (
                    max(0, self.config.watch.alert_repeat_max - alert.repeat_count)
                    if next_stage is not None and next_stage.repeat
                    else 0
                ),
                "acknowledgements": [dict(row) for row in rows],
                "audiences": [dict(row) for row in audiences],
            }
        )
        return value

    async def raise_alert(
        self,
        severity: str,
        headline: str,
        raised_by: str,
        *,
        incident_ref: int | None = None,
        channels: list[int] | None = None,
        source: str = "operator",
        lat: float | None = None,
        lon: float | None = None,
        radius_km: float = 1.0,
        supersedes_alert_id: int | None = None,
        expires_at: int | None = None,
    ) -> Alert:
        if severity not in {"caution", "urgent", "critical"}:
            raise ValueError("Alert severity must be caution, urgent, or critical.")
        if source not in {"operator", "incident", "cap", "same"}:
            raise ValueError("Alert source is not permitted.")
        headline = headline.strip()
        if not headline or len(headline.encode()) > 140:
            raise ValueError("Alert headline must be 1-140 UTF-8 bytes.")
        incident_id = None
        if incident_ref is not None:
            rows = await self.database.read(
                """SELECT id,lat,lon FROM incident
                   WHERE local_ref=? AND status IN ('open','monitoring')""",
                (incident_ref,),
            )
            if not rows:
                raise ValueError("No active incident at that reference.")
            incident_id = int(rows[0]["id"])
            if lat is None and rows[0]["lat"] is not None:
                lat = float(rows[0]["lat"])
            if lon is None and rows[0]["lon"] is not None:
                lon = float(rows[0]["lon"])
        if (lat is None) != (lon is None):
            raise ValueError("Alert center requires both latitude and longitude.")
        if lat is not None:
            assert lon is not None
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError("Alert center is outside valid coordinate bounds.")
        if not 0.1 <= radius_km <= 100:
            raise ValueError("Alert radius must be between 0.1 and 100 km.")
        radius_m = round(radius_km * 1000) if lat is not None else None
        policy = self._policy(severity)
        selected = channels or sorted(
            {channel for stage in policy.stages for channel in stage.channels}
        )
        selected = sorted(set(selected))
        if any(channel not in self.config.channels for channel in selected):
            raise ValueError("Alert channel is not configured.")
        if not policy.stages:
            raise ValueError("Alert escalation policy must contain at least one stage.")
        previous = None
        if supersedes_alert_id is not None:
            previous = await self.by_id(supersedes_alert_id)
            if previous is None or previous.cancelled_at is not None:
                raise ValueError("Superseded alert is not active.")
        now = int(self.clock.now().timestamp())
        alert_expires_at = expires_at if expires_at is not None else now + 6 * 3600
        if alert_expires_at <= now:
            raise ValueError("Alert expiry must be in the future.")
        ack_required = policy.ack_threshold
        fingerprint_value = json.dumps(
            {
                "severity": severity,
                "headline": headline,
                "raised_by": raised_by,
                "source": source,
                "incident_id": incident_id,
                "channels": selected,
                "lat": lat,
                "lon": lon,
                "radius_m": radius_m,
                "expires_at": expires_at,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        request_fingerprint = hashlib.sha256(fingerprint_value).hexdigest()
        coalesced = False
        async with self.database.transaction() as transaction:
            existing = []
            if supersedes_alert_id is None:
                existing = await transaction.read(
                    "SELECT id FROM alert WHERE request_fingerprint=? AND raised_at>=? "
                    "AND cancelled_at IS NULL AND all_clear_at IS NULL "
                    "ORDER BY id DESC LIMIT 1",
                    (
                        request_fingerprint,
                        now - self.config.watch.alert_submission_dedupe_seconds,
                    ),
                )
            if existing:
                alert_id = int(existing[0]["id"])
                coalesced = True
            else:
                alert_id = await transaction.write(
                    """INSERT INTO alert(
                       uid,incident_id,severity,headline,source,channels,raised_by,raised_at,
                       effective_at,expires_at,escalation_stage,next_escalation_at,ack_required,
                       lat,lon,radius_m,request_fingerprint
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        str(uuid.uuid4()),
                        incident_id,
                        severity,
                        headline,
                        source,
                        json.dumps(selected, separators=(",", ":")),
                        raised_by,
                        now,
                        now,
                        alert_expires_at,
                        0,
                        now,
                        ack_required,
                        lat,
                        lon,
                        radius_m,
                        request_fingerprint,
                    ),
                )
                if supersedes_alert_id is not None:
                    await transaction.write(
                        "UPDATE alert SET cancelled_at=?,next_escalation_at=NULL WHERE id=?",
                        (now, supersedes_alert_id),
                    )
        if coalesced:
            alert = await self.by_id(alert_id)
            assert alert is not None
            return replace(alert, coalesced=True)
        await self._advance_alert(
            alert_id,
            override_channels=channels,
            supersedes=(
                f"alert:{supersedes_alert_id}:repeat" if supersedes_alert_id is not None else None
            ),
        )
        alert = await self.by_id(alert_id)
        assert alert is not None
        return await self.by_id(alert_id) or alert

    async def _broadcast(
        self,
        alert: Alert,
        stage: EscalationStage,
        channels: list[int] | None = None,
        supersedes: str | None = None,
        dedupe_token: str | None = None,
    ) -> AudienceDelivery:
        text = self.render(alert.severity, alert.headline)
        severity = Severity(alert.severity)
        now = int(self.clock.now().timestamp())
        selected_channels = channels or stage.channels
        delivery = await self.notifier.deliver(
            purpose="alert_escalation",
            target=f"alert:{alert.id}:stage:{alert.escalation_stage}",
            audience=stage.notify,
            text=text,
            channels=selected_channels,
            traffic_class=TrafficClass.ALERT,
            severity=severity,
            queue_key=f"alert:{alert.id}:repeat",
            supersedes=supersedes,
            dedupe_token=dedupe_token,
        )
        if delivery.admitted:
            pairs = [
                (destination, int(channel))
                for destination in delivery.destinations
                for channel in selected_channels
            ]
            for destination, channel in pairs:
                await self.database.write(
                    "INSERT INTO alert_audience(alert_id,destination,channel,"
                    "first_admitted_at,last_admitted_at,admissions) VALUES(?,?,?,?,?,1) "
                    "ON CONFLICT(alert_id,destination,channel) DO UPDATE SET "
                    "last_admitted_at=excluded.last_admitted_at,admissions=admissions+1",
                    (alert.id, destination, channel, now, now),
                )
            await self.database.write(
                "UPDATE alert SET broadcast_count=broadcast_count+?,delivery_state='delivered',"
                "last_delivery_count=?,delivery_error_at=NULL WHERE id=?",
                (delivery.admitted, delivery.admitted, alert.id),
            )
        return delivery

    async def _advance_alert(
        self,
        alert_id: int,
        *,
        override_channels: list[int] | None = None,
        supersedes: str | None = None,
    ) -> bool:
        alert = await self.by_id(alert_id)
        if alert is None or alert.cancelled_at is not None:
            return False
        policy = self._policy(alert.severity)
        if alert.ack_required and alert.ack_count >= alert.ack_required:
            await self.database.write(
                "UPDATE alert SET next_escalation_at=NULL WHERE id=?", (alert.id,)
            )
            return False
        stage_index = alert.escalation_stage
        if stage_index >= len(policy.stages):
            await self.database.write(
                "UPDATE alert SET next_escalation_at=NULL WHERE id=?", (alert.id,)
            )
            return False
        stage = policy.stages[stage_index]
        repeat_count = alert.repeat_count
        next_at: int | None
        delivery_key = f"alert:{alert.id}:stage:{stage_index}:repeat:{repeat_count}"
        delivery = await self._broadcast(
            alert,
            stage,
            override_channels,
            supersedes or (f"alert:{alert.id}:repeat" if stage.repeat else None),
            delivery_key,
        )
        now = int(self.clock.now().timestamp())
        if not delivery.admitted:
            retry_seconds = 300 if delivery.state == "empty_audience" else 60
            await self.database.write(
                "UPDATE alert SET delivery_state=?,last_delivery_count=0,delivery_error_at=?,"
                "next_escalation_at=? WHERE id=?",
                (delivery.state, now, now + retry_seconds, alert.id),
            )
            return False
        if stage.repeat:
            repeat_count += 1
            if repeat_count < self.config.watch.alert_repeat_max:
                next_stage = stage_index
                next_at = now + self.config.watch.alert_repeat_interval_minutes * 60
            else:
                next_stage = stage_index + 1
                repeat_count = 0
                next_at = (
                    max(now, alert.raised_at + policy.stages[next_stage].after_minutes * 60)
                    if next_stage < len(policy.stages)
                    else None
                )
        else:
            next_stage = stage_index + 1
            repeat_count = 0
            next_at = (
                alert.raised_at + policy.stages[next_stage].after_minutes * 60
                if next_stage < len(policy.stages)
                else None
            )
        await self.database.write(
            "UPDATE alert SET escalation_stage=?,next_escalation_at=?,repeat_count=? WHERE id=?",
            (next_stage, next_at, repeat_count, alert.id),
        )
        return True

    async def by_id(self, alert_id: int) -> Alert | None:
        rows = await self.database.read(
            """SELECT a.*,i.local_ref AS incident_ref,COUNT(aa.member_id) AS ack_count
               FROM alert a LEFT JOIN incident i ON i.id=a.incident_id
               LEFT JOIN alert_ack aa ON aa.alert_id=a.id WHERE a.id=? GROUP BY a.id""",
            (alert_id,),
        )
        return self._row(rows[0]) if rows else None

    async def list(self, active_only: bool = True) -> list[Alert]:
        where = (
            "WHERE a.cancelled_at IS NULL AND (a.expires_at IS NULL OR a.expires_at>?)"
            if active_only
            else ""
        )
        params = (int(self.clock.now().timestamp()),) if active_only else ()
        rows = await self.database.read(
            f"""SELECT a.*,i.local_ref AS incident_ref,COUNT(aa.member_id) AS ack_count
                FROM alert a LEFT JOIN incident i ON i.id=a.incident_id
                LEFT JOIN alert_ack aa ON aa.alert_id=a.id {where}
                GROUP BY a.id ORDER BY a.raised_at DESC""",  # noqa: S608
            params,
        )
        return [self._row(row) for row in rows]

    async def acknowledge(self, incident_ref: int, member: Member, note: str = "") -> Alert:
        rows = await self.database.read(
            """SELECT a.id FROM alert a JOIN incident i ON i.id=a.incident_id
               WHERE i.local_ref=? AND a.cancelled_at IS NULL
               AND (a.expires_at IS NULL OR a.expires_at>?) ORDER BY a.id DESC LIMIT 1""",
            (incident_ref, int(self.clock.now().timestamp())),
        )
        if not rows:
            raise ValueError("No active alert for that incident.")
        alert_id = int(rows[0]["id"])
        await self.database.write(
            "INSERT OR IGNORE INTO alert_ack(alert_id,member_id,acked_at,note) VALUES(?,?,?,?)",
            (alert_id, member.id, int(self.clock.now().timestamp()), note or None),
        )
        alert = await self.by_id(alert_id)
        assert alert is not None
        if alert.ack_required and alert.ack_count >= alert.ack_required:
            await self.database.write(
                "UPDATE alert SET next_escalation_at=NULL WHERE id=?", (alert.id,)
            )
        return await self.by_id(alert_id) or alert

    async def cancel(self, alert_id: int, resolution: str, actor: str) -> Alert:
        alert = await self.by_id(alert_id)
        if alert is None or alert.cancelled_at is not None:
            raise ValueError("No active alert.")
        now = int(self.clock.now().timestamp())
        audience_rows = await self.database.read(
            "SELECT destination,channel FROM alert_audience WHERE alert_id=? "
            "ORDER BY destination,channel",
            (alert.id,),
        )
        audiences: list[dict[str, str | int]] = [
            {"destination": str(row["destination"]), "channel": int(row["channel"])}
            for row in audience_rows
        ]
        if not audiences:
            audiences = [
                {"destination": "^all", "channel": channel}
                for channel in cast(list[int], json.loads(alert.channels))
            ]
        text = f"ALL CLEAR {resolution[:120]} {self.config.node.short_name}"
        items = [
            OutboundItem(
                text=text,
                dest=str(audience["destination"]),
                channel=int(audience["channel"]),
                traffic_class=TrafficClass.ALERT,
                severity=Severity(alert.severity),
                want_ack=str(audience["destination"]) != "^all",
                supersedes=f"alert:{alert.id}:repeat" if index == 0 else None,
                dedupe_token=f"alert:{alert.id}:all-clear",
            )
            for index, audience in enumerate(audiences)
        ]
        queued = await self.governor.admit_many(items)
        await self.database.write(
            "UPDATE alert SET cancelled_at=?,next_escalation_at=NULL,all_clear_at=?,"
            "all_clear_queued=? WHERE id=?",
            (now, now, len(queued) if queued is not None else 0, alert.id),
        )
        if alert.incident_id:
            await self.database.write(
                """UPDATE incident SET status='resolved',resolved_at=?,resolved_by=?,
                   resolution_note=?,updated_at=? WHERE id=?""",
                (now, actor, resolution[:500], now, alert.incident_id),
            )
        return await self.by_id(alert.id) or alert

    async def halt_escalation(self, alert_id: int) -> Alert:
        alert = await self.by_id(alert_id)
        if alert is None or alert.cancelled_at is not None:
            raise ValueError("No active alert.")
        await self.database.write(
            "UPDATE alert SET next_escalation_at=NULL WHERE id=?", (alert.id,)
        )
        return await self.by_id(alert.id) or alert

    async def advance_due(self) -> int:
        rows = await self.database.read(
            """SELECT id FROM alert WHERE cancelled_at IS NULL AND next_escalation_at<=?
               AND (expires_at IS NULL OR expires_at>?) ORDER BY next_escalation_at,id""",
            (int(self.clock.now().timestamp()), int(self.clock.now().timestamp())),
        )
        advanced = 0
        for row in rows:
            advanced += int(await self._advance_alert(int(row["id"])))
        return advanced
