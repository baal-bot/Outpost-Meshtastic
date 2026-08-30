from __future__ import annotations

import asyncio
import csv
import io
from dataclasses import asdict, dataclass
from typing import Any

from outpost.clock import Clock
from outpost.csv_safety import csv_safe_row
from outpost.store import Database
from outpost.store.members import Member
from outpost.transport.chunker import truncate_utf8
from outpost.transport.governor import AirtimeGovernor, OutboundItem
from outpost.transport.models import Severity, TrafficClass
from outpost.transport.toa import MAX_PAYLOAD_BYTES

from .delivery import AudienceDelivery, AudienceNotifier


@dataclass(frozen=True)
class WatchEvent:
    id: int
    name: str
    opened_at: int
    closed_at: int | None
    opened_by: str
    roster_policy: str

    def json(self) -> dict[str, Any]:
        return asdict(self)


class CheckinService:
    def __init__(self, database: Database, governor: AirtimeGovernor, clock: Clock) -> None:
        self.database, self.governor, self.clock = database, governor, clock
        self.notifier = AudienceNotifier(database, governor, clock)
        self._solicitation_lock = asyncio.Lock()

    @staticmethod
    def solicitation_message(event: WatchEvent) -> str:
        prefix = 'Outpost welfare check: "'
        suffix = '". Reply OK [note] or HELPME [note].'
        name_budget = MAX_PAYLOAD_BYTES - len((prefix + suffix).encode())
        name = truncate_utf8(event.name, name_budget)
        return f"{prefix}{name}{suffix}"

    async def open_event(self, name: str, policy: str, actor: str) -> WatchEvent:
        name = name.strip()
        if not name or len(name) > 80:
            raise ValueError("Event name must be 1-80 characters.")
        if policy not in {"all", "responders", "subscribed"}:
            raise ValueError("Roster policy must be all, responders, or subscribed.")
        if await self.current_event() is not None:
            raise ValueError("Close the current watch event first.")
        event_id = await self.database.write(
            "INSERT INTO watch_event(name,opened_at,opened_by,roster_policy) VALUES(?,?,?,?)",
            (name, int(self.clock.now().timestamp()), actor, policy),
        )
        value = await self.by_id(event_id)
        assert value is not None
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
        text = f"⚠ HELP @{label} · {event_name} · {note[:80] or 'needs assistance'}"
        return await self.notifier.deliver(
            purpose="checkin_help",
            target=f"checkin:{checkin_id}",
            audience="responders",
            text=text,
            channels=[0],
            traffic_class=TrafficClass.ALERT,
            severity=Severity.URGENT,
            exclude_mesh_ids=(member.mesh_id,),
            dedupe_token=f"checkin:{checkin_id}:help-notification",
        )

    async def _members_for(self, event: WatchEvent) -> list[Any]:
        if event.roster_policy == "responders":
            where = "WHERE trust IN ('responder','operator')"
        elif event.roster_policy == "subscribed":
            where = (
                "WHERE trust IN ('member','trusted','responder','operator') "
                "AND json_extract(prefs,'$.roster')=1"
            )
        else:
            where = "WHERE trust IN ('member','trusted','responder','operator')"
        return await self.database.read(
            f"SELECT id,mesh_id,handle,trust FROM member {where} "  # noqa: S608
            "ORDER BY COALESCE(handle,mesh_id)"
        )

    async def roster(self, event_id: int) -> list[dict[str, Any]]:
        event = await self.by_id(event_id)
        if event is None:
            raise ValueError("Event not found.")
        members = await self._members_for(event)
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
            result.append({**dict(member), **latest})
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
        estimate.update({"recipient_count": len(selected), "channel_count": 1, "channels": [0]})
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
                                channel=0,
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

    async def csv_export(self, event_id: int) -> str:
        summary = await self.summary(event_id)
        output = io.StringIO()
        writer = csv.DictWriter(
            output,
            fieldnames=["mesh_id", "handle", "trust", "status", "note", "lat", "lon", "created_at"],
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(csv_safe_row(row) for row in summary["items"])
        return output.getvalue()
