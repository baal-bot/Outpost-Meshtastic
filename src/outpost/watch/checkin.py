from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import sqlite3
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from outpost.clock import Clock
from outpost.csv_safety import csv_safe_row
from outpost.store import Database
from outpost.store.members import Member
from outpost.transport.chunker import truncate_utf8
from outpost.transport.governor import AirtimeGovernor, OutboundItem
from outpost.transport.models import Severity, TrafficClass
from outpost.transport.toa import MAX_PAYLOAD_BYTES

from .delivery import AudienceDelivery, AudienceNotifier

RESPONDER_GROUP_TYPES = frozenset(
    {
        "general",
        "medical",
        "fire",
        "search",
        "logistics",
        "communications",
        "public_safety",
    }
)


@dataclass(frozen=True)
class WatchEvent:
    id: int
    name: str
    opened_at: int
    closed_at: int | None
    opened_by: str
    roster_policy: str
    event_kind: str = "real"
    responder_group_id: int | None = None
    schedule_id: int | None = None
    scheduled_for: int | None = None
    auto_close_at: int | None = None

    def json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class WelfareSchedule:
    id: int
    name: str
    cadence: str
    day_of_period: int
    local_time: str
    roster_policy: str
    responder_group_id: int | None
    window_minutes: int
    suppress_if_real_event: int
    recipient_limit: int
    airtime_limit_ms: int
    enabled: int
    next_run_at: int
    last_run_at: int | None
    last_outcome: str | None
    created_at: int
    updated_at: int
    created_by: str
    archived_at: int | None

    def json(self) -> dict[str, Any]:
        value = asdict(self)
        value["suppress_if_real_event"] = bool(self.suppress_if_real_event)
        value["enabled"] = bool(self.enabled)
        value["airtime_limit_seconds"] = self.airtime_limit_ms / 1000
        return value


class CheckinService:
    def __init__(
        self,
        database: Database,
        governor: AirtimeGovernor,
        clock: Clock,
        timezone: str = "UTC",
        public_channel: Callable[[], int] | None = None,
    ) -> None:
        self.database, self.governor, self.clock = database, governor, clock
        self.timezone = ZoneInfo(timezone)
        self.public_channel = public_channel or (lambda: 0)
        self.notifier = AudienceNotifier(database, governor, clock)
        self._solicitation_lock = asyncio.Lock()
        self._schedule_lock = asyncio.Lock()

    @staticmethod
    def solicitation_message(event: WatchEvent) -> str:
        prefix = (
            'DRILL — Outpost welfare check: "'
            if event.event_kind == "drill"
            else 'Outpost welfare check: "'
        )
        suffix = (
            '". Reply OK [note]. HELPME always reports real need.'
            if event.event_kind == "drill"
            else '". Reply OK [note] or HELPME [note].'
        )
        name_budget = MAX_PAYLOAD_BYTES - len((prefix + suffix).encode())
        name = truncate_utf8(event.name, name_budget)
        return f"{prefix}{name}{suffix}"

    async def open_event(
        self,
        name: str,
        policy: str,
        actor: str,
        *,
        event_kind: str = "real",
        responder_group_id: int | None = None,
        schedule_id: int | None = None,
        scheduled_for: int | None = None,
        auto_close_at: int | None = None,
    ) -> WatchEvent:
        name = name.strip()
        if not name or len(name) > 80:
            raise ValueError("Event name must be 1-80 characters.")
        if policy not in {"all", "responders", "subscribed"}:
            raise ValueError("Roster policy must be all, responders, or subscribed.")
        if event_kind not in {"real", "drill"}:
            raise ValueError("Event kind must be real or drill.")
        if responder_group_id is not None:
            if policy != "responders":
                raise ValueError("A responder group requires the responders roster policy.")
            await self._require_group(responder_group_id)
        current = await self.current_event()
        if current is not None:
            if event_kind == "real" and current.event_kind == "drill":
                await self.close_event(current.id)
            else:
                raise ValueError("Close the current watch event first.")
        try:
            event_id = await self.database.write(
                "INSERT INTO watch_event(name,opened_at,opened_by,roster_policy,event_kind,"
                "responder_group_id,schedule_id,scheduled_for,auto_close_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    name,
                    int(self.clock.now().timestamp()),
                    actor,
                    policy,
                    event_kind,
                    responder_group_id,
                    schedule_id,
                    scheduled_for,
                    auto_close_at,
                ),
            )
        except sqlite3.IntegrityError as error:
            if "idx_watch_event_one_open" in str(error) or "watch_event" in str(error):
                raise ValueError("Close the current watch event first.") from error
            raise
        value = await self.by_id(event_id)
        assert value is not None
        if event_kind == "drill":
            await self._snapshot_roster(value)
        return value

    async def by_id(self, event_id: int) -> WatchEvent | None:
        rows = await self.database.read("SELECT * FROM watch_event WHERE id=?", (event_id,))
        return WatchEvent(**dict(rows[0])) if rows else None

    async def current_event(self) -> WatchEvent | None:
        rows = await self.database.read(
            "SELECT * FROM watch_event WHERE closed_at IS NULL ORDER BY id DESC LIMIT 1"
        )
        return WatchEvent(**dict(rows[0])) if rows else None

    async def events(self, limit: int = 50) -> list[WatchEvent]:
        rows = await self.database.read(
            "SELECT * FROM watch_event ORDER BY opened_at DESC LIMIT ?", (limit,)
        )
        return [WatchEvent(**dict(row)) for row in rows]

    async def close_event(self, event_id: int) -> WatchEvent:
        value = await self.by_id(event_id)
        if value is None or value.closed_at is not None:
            raise ValueError("No open event.")
        await self.database.write(
            "UPDATE watch_event SET closed_at=? WHERE id=?",
            (int(self.clock.now().timestamp()), event_id),
        )
        updated = await self.by_id(event_id)
        assert updated is not None
        return updated

    async def checkin(self, member: Member, status: str, note: str = "") -> dict[str, Any]:
        if status not in {"ok", "need_help", "evacuated"}:
            raise ValueError("Invalid check-in status.")
        event = await self.current_event()
        now = int(self.clock.now().timestamp())
        position = await self.database.read(
            "SELECT lat,lon FROM member_position WHERE member_id=? AND expires_at>?",
            (member.id, now),
        )
        lat = float(position[0]["lat"]) if position else None
        lon = float(position[0]["lon"]) if position else None
        checkin_id = await self.database.write(
            "INSERT INTO checkin(member_id,event_id,status,note,lat,lon,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                member.id,
                event.id if event else None,
                status,
                note[:280] or None,
                lat,
                lon,
                now,
            ),
        )
        notification: AudienceDelivery | None = None
        if status == "need_help":
            notification = await self._notify_responders(member, event, note, checkin_id)
            await self.database.write(
                "UPDATE checkin SET notification_state=?,notification_count=? WHERE id=?",
                (notification.state, notification.admitted, checkin_id),
            )
        roster = await self.roster(event.id) if event else []
        return {
            "event": event.json() if event else None,
            "checked_in": sum(row["status"] != "unaccounted" for row in roster),
            "total": len(roster),
            "notification": (
                {
                    "state": notification.state,
                    "admitted": notification.admitted,
                    "reason": notification.failure_reason,
                }
                if notification is not None
                else None
            ),
        }

    async def _notify_responders(
        self, member: Member, event: WatchEvent | None, note: str, checkin_id: int
    ) -> AudienceDelivery:
        label = member.handle or member.mesh_id
        event_name = event.name if event else "community"
        help_label = "REAL HELP during DRILL" if event and event.event_kind == "drill" else "HELP"
        text = f"⚠ {help_label} @{label} · {event_name} · {note[:80] or 'needs assistance'}"
        return await self.notifier.deliver(
            purpose="checkin_help",
            target=f"checkin:{checkin_id}",
            audience="responders",
            text=text,
            channels=[self.public_channel()],
            traffic_class=TrafficClass.ALERT,
            severity=Severity.URGENT,
            exclude_mesh_ids=(member.mesh_id,),
            dedupe_token=f"checkin:{checkin_id}:help-notification",
        )

    async def groups(self) -> list[dict[str, Any]]:
        rows = await self.database.read(
            "SELECT g.*,COUNT(gm.member_id) member_count FROM responder_group g "
            "LEFT JOIN responder_group_member gm ON gm.group_id=g.id "
            "GROUP BY g.id ORDER BY g.name COLLATE NOCASE"
        )
        result: list[dict[str, Any]] = []
        for row in rows:
            members = await self.database.read(
                "SELECT m.id,m.mesh_id,m.handle,m.long_name,m.short_name,m.trust,m.last_seen "
                "FROM responder_group_member gm "
                "JOIN member m ON m.id=gm.member_id WHERE gm.group_id=? "
                "ORDER BY COALESCE(m.handle,m.mesh_id)",
                (row["id"],),
            )
            result.append({**dict(row), "members": [dict(member) for member in members]})
        return result

    async def _require_group(self, group_id: int) -> dict[str, Any]:
        rows = await self.database.read("SELECT * FROM responder_group WHERE id=?", (group_id,))
        if not rows:
            raise ValueError("Responder group not found.")
        return dict(rows[0])

    @staticmethod
    def _validate_group(name: str, response_type: str) -> str:
        normalized_name = name.strip()
        if not 1 <= len(normalized_name) <= 50:
            raise ValueError("Group name must be 1-50 characters.")
        if response_type not in RESPONDER_GROUP_TYPES:
            raise ValueError("Unknown responder group type.")
        return normalized_name

    async def create_group(self, name: str, response_type: str, actor: str) -> dict[str, Any]:
        name = self._validate_group(name, response_type)
        try:
            group_id = await self.database.write(
                "INSERT INTO responder_group(name,response_type,created_at,created_by) "
                "VALUES(?,?,?,?)",
                (name, response_type, int(self.clock.now().timestamp()), actor),
            )
        except sqlite3.IntegrityError as error:
            if "UNIQUE constraint failed" in str(error):
                raise ValueError("A responder group with that name already exists.") from error
            raise
        return next(group for group in await self.groups() if group["id"] == group_id)

    async def update_group(self, group_id: int, name: str, response_type: str) -> dict[str, Any]:
        await self._require_group(group_id)
        name = self._validate_group(name, response_type)
        try:
            await self.database.write(
                "UPDATE responder_group SET name=?,response_type=? WHERE id=?",
                (name, response_type, group_id),
            )
        except sqlite3.IntegrityError as error:
            if "UNIQUE constraint failed" in str(error):
                raise ValueError("A responder group with that name already exists.") from error
            raise
        return next(group for group in await self.groups() if group["id"] == group_id)

    async def set_group_members(
        self, group_id: int, member_ids: list[int], actor: str
    ) -> dict[str, Any]:
        await self._require_group(group_id)
        unique_ids = sorted(set(member_ids))
        if len(unique_ids) > 200:
            raise ValueError("A responder group may contain at most 200 members.")
        if unique_ids:
            placeholders = ",".join("?" for _ in unique_ids)
            eligible = await self.database.read(
                f"SELECT id FROM member WHERE id IN ({placeholders}) "  # noqa: S608
                "AND trust IN ('responder','operator')",
                tuple(unique_ids),
            )
            if {int(row["id"]) for row in eligible} != set(unique_ids):
                raise ValueError("Groups may contain only responder or operator identities.")
        now = int(self.clock.now().timestamp())
        async with self.database.transaction() as transaction:
            await transaction.write(
                "DELETE FROM responder_group_member WHERE group_id=?", (group_id,)
            )
            for member_id in unique_ids:
                await transaction.write(
                    "INSERT INTO responder_group_member(group_id,member_id,added_at,added_by) "
                    "VALUES(?,?,?,?)",
                    (group_id, member_id, now, actor),
                )
        return next(group for group in await self.groups() if group["id"] == group_id)

    async def delete_group(self, group_id: int) -> None:
        await self._require_group(group_id)
        now = int(self.clock.now().timestamp())
        async with self.database.transaction() as transaction:
            await transaction.write(
                "UPDATE welfare_schedule SET enabled=0,last_outcome='group_removed',updated_at=? "
                "WHERE responder_group_id=? AND archived_at IS NULL",
                (now, group_id),
            )
            await transaction.write("DELETE FROM responder_group WHERE id=?", (group_id,))

    async def responder_candidates(self) -> list[dict[str, Any]]:
        rows = await self.database.read(
            "SELECT id,mesh_id,handle,long_name,short_name,trust,last_seen FROM member "
            "WHERE trust IN ('responder','operator') ORDER BY COALESCE(handle,mesh_id)"
        )
        return [dict(row) for row in rows]

    async def set_drill_participation(self, member_id: int, enabled: bool) -> bool:
        rows = await self.database.read("SELECT trust FROM member WHERE id=?", (member_id,))
        if not rows or rows[0]["trust"] not in {"member", "trusted", "responder", "operator"}:
            raise ValueError("Only admitted members can change drill participation.")
        await self.database.write(
            "UPDATE member SET prefs=json_set(prefs,'$.drills',?) WHERE id=?",
            (1 if enabled else 0, member_id),
        )
        return enabled

    async def drill_participation(self, member_id: int) -> bool:
        rows = await self.database.read(
            "SELECT COALESCE(json_extract(prefs,'$.drills'),1) enabled FROM member WHERE id=?",
            (member_id,),
        )
        if not rows:
            raise ValueError("Member not found.")
        return bool(rows[0]["enabled"])

    async def _members_for(self, event: WatchEvent) -> list[Any]:
        params: tuple[Any, ...] = ()
        if event.responder_group_id is not None:
            joins = "JOIN responder_group_member gm ON gm.member_id=member.id"
            where = "WHERE gm.group_id=? AND trust IN ('responder','operator')"
            params = (event.responder_group_id,)
        elif event.roster_policy == "responders":
            joins = ""
            where = "WHERE trust IN ('responder','operator')"
        elif event.roster_policy == "subscribed":
            joins = ""
            where = (
                "WHERE trust IN ('member','trusted','responder','operator') "
                "AND json_extract(prefs,'$.roster')=1"
            )
        else:
            joins = ""
            where = "WHERE trust IN ('member','trusted','responder','operator')"
        if event.event_kind == "drill":
            where += " AND COALESCE(json_extract(prefs,'$.drills'),1)=1"
        return await self.database.read(
            f"SELECT member.id,mesh_id,handle,trust,last_seen FROM member {joins} {where} "  # noqa: S608
            "ORDER BY COALESCE(handle,mesh_id)",
            params,
        )

    async def _snapshot_roster(self, event: WatchEvent) -> None:
        members = await self._members_for(event)
        async with self.database.transaction() as transaction:
            for member in members:
                await transaction.write(
                    "INSERT OR IGNORE INTO welfare_event_roster"
                    "(event_id,member_id,last_seen_at_open) VALUES(?,?,?)",
                    (event.id, member["id"], member["last_seen"]),
                )

    async def _roster_members(self, event: WatchEvent) -> list[Any]:
        if event.event_kind == "drill":
            return await self.database.read(
                "SELECT m.id,m.mesh_id,m.handle,m.trust,m.last_seen,r.last_seen_at_open "
                "FROM welfare_event_roster r JOIN member m ON m.id=r.member_id "
                "WHERE r.event_id=? ORDER BY COALESCE(m.handle,m.mesh_id)",
                (event.id,),
            )
        return await self._members_for(event)

    async def roster(self, event_id: int) -> list[dict[str, Any]]:
        event = await self.by_id(event_id)
        if event is None:
            raise ValueError("Event not found.")
        members = await self._roster_members(event)
        result = []
        for member in members:
            rows = await self.database.read(
                """SELECT status,note,lat,lon,created_at,notification_state,notification_count
                   FROM checkin
                   WHERE event_id=? AND member_id=? ORDER BY created_at DESC LIMIT 1""",
                (event.id, member["id"]),
            )
            latest = (
                dict(rows[0])
                if rows
                else {
                    "status": "unaccounted",
                    "note": None,
                    "lat": None,
                    "lon": None,
                    "created_at": None,
                    "notification_state": None,
                    "notification_count": 0,
                }
            )
            result.append({**dict(member), **latest, "event_kind": event.event_kind})
        return result

    async def summary(self, event_id: int) -> dict[str, Any]:
        event = await self.by_id(event_id)
        if event is None:
            raise ValueError("Event not found.")
        roster = await self.roster(event_id)
        counts = {status: 0 for status in ("ok", "need_help", "evacuated", "unaccounted")}
        for row in roster:
            counts[row["status"]] += 1
        return {"event": event.json(), "counts": counts, "items": roster}

    async def solicitation_preview(self, event_id: int) -> list[dict[str, Any]]:
        event = await self.by_id(event_id)
        if event is None or event.closed_at is not None:
            raise ValueError("No open event.")
        roster = await self.roster(event_id)
        sent = await self.database.read(
            "SELECT member_id FROM checkin_solicitation WHERE event_id=?", (event_id,)
        )
        sent_ids = {row["member_id"] for row in sent}
        return [
            {
                "member_id": row["id"],
                "mesh_id": row["mesh_id"],
                "handle": row["handle"],
                "trust": row["trust"],
            }
            for row in roster
            if row["status"] == "unaccounted" and row["id"] not in sent_ids
        ]

    async def solicitation_airtime(
        self, event_id: int, recipients: list[dict[str, Any]] | None = None
    ) -> dict[str, object]:
        event = await self.by_id(event_id)
        if event is None or event.closed_at is not None:
            raise ValueError("No open event.")
        selected = (
            recipients if recipients is not None else await self.solicitation_preview(event_id)
        )
        estimate = self.governor.estimate_text(
            self.solicitation_message(event),
            traffic_class=TrafficClass.DIGEST,
            copies=len(selected),
        )
        public_channel = self.public_channel()
        estimate.update(
            {
                "recipient_count": len(selected),
                "channel_count": 1,
                "channels": [public_channel],
            }
        )
        return estimate

    async def solicit(self, event_id: int) -> dict[str, Any]:
        async with self._solicitation_lock:
            event = await self.by_id(event_id)
            if event is None or event.closed_at is not None:
                raise ValueError("No open event.")
            recipients = await self.solicitation_preview(event_id)
            if not recipients:
                raise ValueError("No unsolicited, unaccounted members remain.")
            message = self.solicitation_message(event)
            queued_at = int(self.clock.now().timestamp())
            queue_ids: list[int] | None = None
            try:
                async with self.database.transaction() as transaction:
                    queue_ids = await self.governor.admit_many(
                        [
                            OutboundItem(
                                text=message,
                                dest=row["mesh_id"],
                                channel=self.public_channel(),
                                traffic_class=TrafficClass.DIGEST,
                                want_ack=True,
                                queue_key=f"checkin:{event.id}:{row['member_id']}",
                            )
                            for row in recipients
                        ],
                        hold=True,
                        transaction=transaction,
                    )
                    if queue_ids is None:
                        raise ValueError(
                            "The complete batch could not be admitted by queue policy."
                        )
                    for recipient, queue_id in zip(recipients, queue_ids, strict=True):
                        await transaction.write(
                            "INSERT INTO checkin_solicitation(event_id,member_id,queue_item_id,"
                            "queued_at) VALUES(?,?,?,?)",
                            (event.id, recipient["member_id"], queue_id, queued_at),
                        )
            except BaseException:
                if queue_ids is not None:
                    await self.governor.retract_work(queue_ids, persisted=False)
                raise
            await self.governor.release_work(queue_ids)
            return {
                "event_id": event.id,
                "recipient_count": len(recipients),
                "queue_ids": queue_ids,
                "message": message,
            }

    def _quiet_at(self, local_time: time) -> bool:
        quiet = self.governor.config.quiet_hours
        if "digest" not in quiet.classes:
            return False
        start = time.fromisoformat(quiet.start)
        end = time.fromisoformat(quiet.end)
        return start <= local_time < end if start < end else local_time >= start or local_time < end

    @staticmethod
    def _airtime_milliseconds(value: object) -> int:
        if not isinstance(value, (int, float)):
            raise ValueError("Airtime estimate did not contain a numeric duration.")
        return round(value * 1000)

    async def schedule_preview(
        self,
        name: str,
        roster_policy: str,
        responder_group_id: int | None = None,
        *,
        cadence: str | None = None,
        day_of_period: int | None = None,
        local_time: str | None = None,
        window_minutes: int = 120,
    ) -> dict[str, Any]:
        if cadence is not None:
            if day_of_period is None or local_time is None:
                raise ValueError("Cadence previews require a day and local time.")
            self._validate_schedule_fields(cadence, day_of_period, local_time, window_minutes)
        name = name.strip()
        if not 1 <= len(name) <= 80:
            raise ValueError("Schedule name must be 1-80 characters.")
        if roster_policy not in {"all", "responders", "subscribed"}:
            raise ValueError("Roster policy must be all, responders, or subscribed.")
        if responder_group_id is not None:
            if roster_policy != "responders":
                raise ValueError("A responder group requires the responders roster policy.")
            await self._require_group(responder_group_id)
        event = WatchEvent(
            id=0,
            name=name,
            opened_at=0,
            closed_at=None,
            opened_by="schedule-preview",
            roster_policy=roster_policy,
            event_kind="drill",
            responder_group_id=responder_group_id,
        )
        members = await self._members_for(event)
        recipients = [
            {
                "member_id": row["id"],
                "mesh_id": row["mesh_id"],
                "handle": row["handle"],
                "trust": row["trust"],
            }
            for row in members
        ]
        estimate = self.governor.estimate_text(
            self.solicitation_message(event),
            traffic_class=TrafficClass.DIGEST,
            copies=len(recipients),
        )
        estimate.update({"recipient_count": len(recipients), "channel_count": 1, "channels": [0]})
        preview_token = hashlib.sha256(
            "\0".join(
                [
                    self.solicitation_message(event),
                    roster_policy,
                    str(responder_group_id or ""),
                    *(str(row["mesh_id"]) for row in recipients),
                    str(estimate["total_seconds"]),
                    str(estimate["costing_preset"]),
                ]
            ).encode()
        ).hexdigest()
        return {
            "recipient_count": len(recipients),
            "recipients": recipients,
            "message": self.solicitation_message(event),
            "airtime": estimate,
            "preview_token": preview_token,
        }

    def _validate_schedule_fields(
        self, cadence: str, day_of_period: int, local_time: str, window_minutes: int
    ) -> time:
        if cadence not in {"weekly", "biweekly", "monthly"}:
            raise ValueError("Cadence must be weekly, biweekly, or monthly.")
        if cadence in {"weekly", "biweekly"} and day_of_period not in range(7):
            raise ValueError("Weekly schedules require a weekday from 0 through 6.")
        if cadence == "monthly" and day_of_period not in range(1, 29):
            raise ValueError("Monthly schedules require a day from 1 through 28.")
        try:
            parsed = time.fromisoformat(local_time)
        except ValueError as error:
            raise ValueError("Local time must use 24-hour HH:MM form.") from error
        if parsed.tzinfo is not None or len(local_time) != 5:
            raise ValueError("Local time must use 24-hour HH:MM form.")
        if not 30 <= window_minutes <= 1440:
            raise ValueError("Response window must be 30-1440 minutes.")
        if self._quiet_at(parsed):
            raise ValueError("Drill time falls inside configured digest quiet hours.")
        return parsed

    def _first_schedule_time(
        self, cadence: str, day_of_period: int, local_at: time, after: datetime
    ) -> int:
        local_after = after.astimezone(self.timezone)
        if cadence == "monthly":
            year, month = local_after.year, local_after.month
            for _ in range(14):
                candidate = datetime(
                    year, month, day_of_period, local_at.hour, local_at.minute, tzinfo=self.timezone
                )
                if candidate > local_after:
                    return int(candidate.timestamp())
                month += 1
                if month == 13:
                    year += 1
                    month = 1
            raise AssertionError("monthly schedule search exhausted")
        days = (day_of_period - local_after.weekday()) % 7
        candidate_date = local_after.date() + timedelta(days=days)
        candidate = datetime.combine(candidate_date, local_at, self.timezone)
        if candidate <= local_after:
            candidate += timedelta(days=7)
        return int(candidate.timestamp())

    def _next_schedule_time(self, schedule: WelfareSchedule, due_at: int, now: int) -> int:
        local_due = datetime.fromtimestamp(due_at, self.timezone)
        parsed = time.fromisoformat(schedule.local_time)
        if schedule.cadence == "monthly":
            year, month = local_due.year, local_due.month + 1
            if month == 13:
                year += 1
                month = 1
            candidate = datetime(
                year,
                month,
                schedule.day_of_period,
                parsed.hour,
                parsed.minute,
                tzinfo=self.timezone,
            )
        else:
            days = 14 if schedule.cadence == "biweekly" else 7
            candidate = datetime.combine(
                local_due.date() + timedelta(days=days), parsed, self.timezone
            )
        while int(candidate.timestamp()) <= now:
            if schedule.cadence == "monthly":
                year, month = candidate.year, candidate.month + 1
                if month == 13:
                    year += 1
                    month = 1
                candidate = datetime(
                    year,
                    month,
                    schedule.day_of_period,
                    parsed.hour,
                    parsed.minute,
                    tzinfo=self.timezone,
                )
            else:
                candidate = datetime.combine(
                    candidate.date() + timedelta(days=14 if schedule.cadence == "biweekly" else 7),
                    parsed,
                    self.timezone,
                )
        return int(candidate.timestamp())

    async def schedules(self) -> list[WelfareSchedule]:
        rows = await self.database.read(
            "SELECT * FROM welfare_schedule WHERE archived_at IS NULL "
            "ORDER BY enabled DESC,next_run_at,name"
        )
        return [WelfareSchedule(**dict(row)) for row in rows]

    def schedule_json(self, schedule: WelfareSchedule) -> dict[str, Any]:
        value = schedule.json()
        local = datetime.fromtimestamp(schedule.next_run_at, self.timezone)
        value["timezone"] = self.timezone.key
        value["next_run_local"] = local.strftime("%Y-%m-%d %H:%M %Z")
        return value

    async def create_schedule(
        self,
        name: str,
        cadence: str,
        day_of_period: int,
        local_time: str,
        roster_policy: str,
        actor: str,
        *,
        preview_token: str,
        responder_group_id: int | None = None,
        window_minutes: int = 120,
        suppress_if_real_event: bool = True,
        airtime_confirmation: bool = False,
    ) -> WelfareSchedule:
        parsed = self._validate_schedule_fields(cadence, day_of_period, local_time, window_minutes)
        preview = await self.schedule_preview(name, roster_policy, responder_group_id)
        if preview_token != preview["preview_token"]:
            raise ValueError(
                "The drill audience or airtime changed; preview it again before saving."
            )
        airtime = preview["airtime"]
        if airtime["requires_confirmation"] and not airtime_confirmation:
            raise ValueError("The preview crosses an airtime constraint and requires confirmation.")
        now_dt = self.clock.now()
        now = int(now_dt.timestamp())
        next_run = self._first_schedule_time(cadence, day_of_period, parsed, now_dt)
        schedule_id = await self.database.write(
            "INSERT INTO welfare_schedule(name,cadence,day_of_period,local_time,roster_policy,"
            "responder_group_id,window_minutes,suppress_if_real_event,recipient_limit,"
            "airtime_limit_ms,enabled,next_run_at,created_at,updated_at,created_by) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,1,?,?,?,?)",
            (
                name.strip(),
                cadence,
                day_of_period,
                local_time,
                roster_policy,
                responder_group_id,
                window_minutes,
                1 if suppress_if_real_event else 0,
                preview["recipient_count"],
                self._airtime_milliseconds(airtime["total_seconds"]),
                next_run,
                now,
                now,
                actor,
            ),
        )
        return next(schedule for schedule in await self.schedules() if schedule.id == schedule_id)

    async def set_schedule_enabled(self, schedule_id: int, enabled: bool) -> WelfareSchedule:
        schedules = {schedule.id: schedule for schedule in await self.schedules()}
        if schedule_id not in schedules:
            raise ValueError("Welfare schedule not found.")
        schedule = schedules[schedule_id]
        if enabled and schedule.last_outcome == "group_removed":
            raise ValueError("This schedule lost its responder group; remove and recreate it.")
        now_dt = self.clock.now()
        next_run = schedule.next_run_at
        if enabled and next_run <= int(now_dt.timestamp()):
            next_run = self._first_schedule_time(
                schedule.cadence,
                schedule.day_of_period,
                time.fromisoformat(schedule.local_time),
                now_dt,
            )
        await self.database.write(
            "UPDATE welfare_schedule SET enabled=?,next_run_at=?,updated_at=? WHERE id=?",
            (1 if enabled else 0, next_run, int(now_dt.timestamp()), schedule_id),
        )
        return next(value for value in await self.schedules() if value.id == schedule_id)

    async def delete_schedule(self, schedule_id: int) -> None:
        rows = await self.database.read("SELECT 1 FROM welfare_schedule WHERE id=?", (schedule_id,))
        if not rows:
            raise ValueError("Welfare schedule not found.")
        now = int(self.clock.now().timestamp())
        await self.database.write(
            "UPDATE welfare_schedule SET enabled=0,archived_at=?,updated_at=? WHERE id=?",
            (now, now, schedule_id),
        )

    async def _record_schedule_run(
        self,
        schedule: WelfareSchedule,
        due_at: int,
        outcome: str,
        *,
        event_id: int | None = None,
        recipient_count: int = 0,
        detail: str | None = None,
    ) -> None:
        now = int(self.clock.now().timestamp())
        await self.database.write(
            "INSERT OR IGNORE INTO welfare_schedule_run(schedule_id,event_id,due_at,processed_at,"
            "outcome,recipient_count,detail) VALUES(?,?,?,?,?,?,?)",
            (schedule.id, event_id, due_at, now, outcome, recipient_count, detail),
        )
        next_run = self._next_schedule_time(schedule, due_at, now)
        await self.database.write(
            "UPDATE welfare_schedule SET next_run_at=?,last_run_at=?,last_outcome=?,updated_at=? "
            "WHERE id=?",
            (next_run, now, outcome, now, schedule.id),
        )

    async def close_due_drills(self) -> int:
        now = int(self.clock.now().timestamp())
        rows = await self.database.read(
            "SELECT id FROM watch_event WHERE event_kind='drill' AND closed_at IS NULL "
            "AND auto_close_at IS NOT NULL AND auto_close_at<=?",
            (now,),
        )
        for row in rows:
            await self.close_event(int(row["id"]))
        return len(rows)

    async def run_due_schedules(self) -> list[dict[str, Any]]:
        async with self._schedule_lock:
            await self.close_due_drills()
            now = int(self.clock.now().timestamp())
            due = [
                schedule
                for schedule in await self.schedules()
                if schedule.enabled and schedule.next_run_at <= now
            ]
            outcomes: list[dict[str, Any]] = []
            for schedule in due:
                due_at = schedule.next_run_at
                current = await self.current_event()
                if current is not None and current.event_kind == "real":
                    if not schedule.suppress_if_real_event:
                        continue
                    outcome = "suppressed_real_event"
                    await self._record_schedule_run(
                        schedule, due_at, outcome, detail=f"watch_event:{current.id}"
                    )
                    outcomes.append({"schedule_id": schedule.id, "outcome": outcome})
                    continue
                if current is not None:
                    outcome = "suppressed_open_event"
                    await self._record_schedule_run(
                        schedule, due_at, outcome, detail=f"watch_event:{current.id}"
                    )
                    outcomes.append({"schedule_id": schedule.id, "outcome": outcome})
                    continue
                local_due = datetime.fromtimestamp(due_at, self.timezone).time()
                if self._quiet_at(local_due):
                    outcome = "suppressed_quiet_hours"
                    await self._record_schedule_run(schedule, due_at, outcome)
                    outcomes.append({"schedule_id": schedule.id, "outcome": outcome})
                    continue
                event: WatchEvent | None = None
                try:
                    event = await self.open_event(
                        schedule.name,
                        schedule.roster_policy,
                        f"schedule:{schedule.id}",
                        event_kind="drill",
                        responder_group_id=schedule.responder_group_id,
                        schedule_id=schedule.id,
                        scheduled_for=due_at,
                        auto_close_at=now + schedule.window_minutes * 60,
                    )
                    recipients = await self.solicitation_preview(event.id)
                    estimate = await self.solicitation_airtime(event.id, recipients)
                    recipient_count = len(recipients)
                    actual_ms = self._airtime_milliseconds(estimate["total_seconds"])
                    if (
                        recipient_count > schedule.recipient_limit
                        or actual_ms > schedule.airtime_limit_ms
                        or bool(estimate["requires_confirmation"])
                    ):
                        outcome = "suppressed_airtime_growth"
                        await self.close_event(event.id)
                        await self._record_schedule_run(
                            schedule,
                            due_at,
                            outcome,
                            event_id=event.id,
                            recipient_count=recipient_count,
                            detail=(
                                f"approved:{schedule.recipient_limit}/{schedule.airtime_limit_ms}ms;"
                                f"actual:{recipient_count}/{actual_ms}ms"
                            ),
                        )
                    elif not recipients:
                        outcome = "no_recipients"
                        await self.close_event(event.id)
                        await self._record_schedule_run(
                            schedule, due_at, outcome, event_id=event.id
                        )
                    else:
                        await self.solicit(event.id)
                        outcome = "started"
                        await self._record_schedule_run(
                            schedule,
                            due_at,
                            outcome,
                            event_id=event.id,
                            recipient_count=recipient_count,
                        )
                except Exception as error:
                    if event is not None and event.closed_at is None:
                        current_event = await self.by_id(event.id)
                        if current_event is not None and current_event.closed_at is None:
                            await self.close_event(event.id)
                    outcome = "failed"
                    await self._record_schedule_run(
                        schedule,
                        due_at,
                        outcome,
                        event_id=event.id if event else None,
                        detail=f"{type(error).__name__}: {str(error)[:180]}",
                    )
                outcomes.append({"schedule_id": schedule.id, "outcome": outcome})
            return outcomes

    async def participation_report(self) -> dict[str, Any]:
        events = await self.database.read(
            "SELECT e.id,e.name,e.opened_at,e.closed_at,e.roster_policy,e.responder_group_id,"
            "e.schedule_id,COUNT(DISTINCT r.member_id) roster_count,"
            "COUNT(DISTINCT c.member_id) response_count "
            "FROM watch_event e LEFT JOIN welfare_event_roster r ON r.event_id=e.id "
            "LEFT JOIN checkin c ON c.event_id=e.id AND c.member_id=r.member_id "
            "WHERE e.event_kind='drill' AND (e.schedule_id IS NULL OR EXISTS ("
            "SELECT 1 FROM welfare_schedule_run sr WHERE sr.event_id=e.id "
            "AND sr.outcome='started')) GROUP BY e.id ORDER BY e.opened_at DESC LIMIT 50"
        )
        history = []
        for row in events:
            value = dict(row)
            total = int(value["roster_count"])
            responses = int(value["response_count"])
            value["response_rate"] = round(responses / total * 100, 1) if total else 0.0
            history.append(value)
        never = await self.database.read(
            "SELECT m.id,m.mesh_id,m.handle,m.trust,COUNT(c.id) response_count "
            "FROM welfare_event_roster r JOIN watch_event e ON e.id=r.event_id "
            "JOIN member m ON m.id=r.member_id "
            "LEFT JOIN checkin c ON c.event_id=e.id AND c.member_id=m.id "
            "WHERE e.event_kind='drill' AND (e.schedule_id IS NULL OR EXISTS ("
            "SELECT 1 FROM welfare_schedule_run sr WHERE sr.event_id=e.id "
            "AND sr.outcome='started')) GROUP BY m.id HAVING COUNT(c.id)=0 "
            "ORDER BY COALESCE(m.handle,m.mesh_id)"
        )
        unheard: list[dict[str, Any]] = []
        if history:
            latest = history[0]
            rows = await self.database.read(
                "SELECT m.id,m.mesh_id,m.handle,m.trust,m.last_seen,e.opened_at last_net_at "
                "FROM welfare_event_roster r JOIN member m ON m.id=r.member_id "
                "JOIN watch_event e ON e.id=r.event_id WHERE r.event_id=? "
                "AND m.last_seen<e.opened_at AND NOT EXISTS (SELECT 1 FROM checkin c "
                "WHERE c.event_id=e.id AND c.member_id=m.id) "
                "ORDER BY COALESCE(m.handle,m.mesh_id)",
                (latest["id"],),
            )
            unheard = [dict(row) for row in rows]
        runs = await self.database.read(
            "SELECT r.*,s.name schedule_name FROM welfare_schedule_run r "
            "JOIN welfare_schedule s ON s.id=r.schedule_id "
            "ORDER BY r.processed_at DESC LIMIT 50"
        )
        return {
            "generated_at": int(self.clock.now().timestamp()),
            "nets": history,
            "never_responded": [dict(row) for row in never],
            "not_heard_since_last_net": unheard,
            "runs": [dict(row) for row in runs],
        }

    async def csv_export(self, event_id: int) -> str:
        summary = await self.summary(event_id)
        output = io.StringIO()
        writer = csv.DictWriter(
            output,
            fieldnames=[
                "event_kind",
                "mesh_id",
                "handle",
                "trust",
                "status",
                "note",
                "lat",
                "lon",
                "created_at",
            ],
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(csv_safe_row(row) for row in summary["items"])
        return output.getvalue()
