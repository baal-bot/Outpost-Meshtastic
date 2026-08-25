from __future__ import annotations

import asyncio
import csv
import io
from dataclasses import asdict, dataclass
from typing import Any

from outpost.clock import Clock
from outpost.store import Database
from outpost.store.members import Member
from outpost.transport.governor import AirtimeGovernor, OutboundItem
from outpost.transport.models import Severity, TrafficClass


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
        self._solicitation_lock = asyncio.Lock()

    @staticmethod
    def solicitation_message(event: WatchEvent) -> str:
        name = event.name[:60]
        return f'Outpost welfare check: "{name}". Reply OK [note] or HELPME [note].'

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
        position = await self.database.read(
            "SELECT lat,lon FROM member_position WHERE member_id=?", (member.id,)
        )
        lat = float(position[0]["lat"]) if position else None
        lon = float(position[0]["lon"]) if position else None
        await self.database.write(
            "INSERT INTO checkin(member_id,event_id,status,note,lat,lon,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                member.id,
                event.id if event else None,
                status,
                note[:280] or None,
                lat,
                lon,
                int(self.clock.now().timestamp()),
            ),
        )
        if status == "need_help":
            await self._notify_responders(member, event, note)
        roster = await self.roster(event.id) if event else []
        return {
            "event": event.json() if event else None,
            "checked_in": sum(row["status"] != "unaccounted" for row in roster),
            "total": len(roster),
        }

    async def _notify_responders(self, member: Member, event: WatchEvent | None, note: str) -> None:
        responders = await self.database.read(
            "SELECT mesh_id FROM member WHERE trust IN ('responder','operator') AND id<>?",
            (member.id,),
        )
        label = member.handle or member.mesh_id
        event_name = event.name if event else "community"
        text = f"⚠ HELP @{label} · {event_name} · {note[:80] or 'needs assistance'}"
        for responder in responders:
            self.governor.enqueue(
                OutboundItem(
                    text=text,
                    dest=responder["mesh_id"],
                    channel=0,
                    traffic_class=TrafficClass.ALERT,
                    severity=Severity.URGENT,
                    want_ack=True,
                )
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
                """SELECT status,note,lat,lon,created_at FROM checkin
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

    async def solicit(self, event_id: int) -> dict[str, Any]:
        async with self._solicitation_lock:
            event = await self.by_id(event_id)
            if event is None or event.closed_at is not None:
                raise ValueError("No open event.")
            recipients = await self.solicitation_preview(event_id)
            if not recipients:
                raise ValueError("No unsolicited, unaccounted members remain.")
            message = self.solicitation_message(event)
            queue_ids = self.governor.enqueue_many(
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
                ]
            )
            if queue_ids is None:
                raise ValueError("The complete batch could not be admitted by queue policy.")
            queued_at = int(self.clock.now().timestamp())
            for recipient, queue_id in zip(recipients, queue_ids, strict=True):
                await self.database.write(
                    "INSERT INTO checkin_solicitation(event_id,member_id,queue_item_id,queued_at) "
                    "VALUES(?,?,?,?)",
                    (event.id, recipient["member_id"], queue_id, queued_at),
                )
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
        writer.writerows(summary["items"])
        return output.getvalue()
