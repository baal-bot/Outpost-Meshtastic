from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from outpost.clock import Clock
from outpost.store.database import Database
from outpost.store.members import Member, MemberRepo


@dataclass(frozen=True)
class MailView:
    id: int
    from_label: str
    to_label: str
    body: str
    created_at: int
    read_at: int | None
    reply_peer_mesh_id: str | None


class MailService:
    def __init__(
        self,
        database: Database,
        members: MemberRepo,
        clock: Clock,
        origin_node: str,
        hold_days: int = 14,
        federated_reply: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> None:
        self.database, self.members, self.clock = database, members, clock
        self.origin_node, self.hold_days = origin_node, hold_days
        self.federated_reply = federated_reply

    async def send(self, sender: Member, handle: str, body: str) -> int:
        if sender.handle is None or sender.trust == "guest":
            raise PermissionError("Claim a NAME before sending mail.")
        if not body or len(body.encode()) > 200:
            raise ValueError("Mail must be 1-200 bytes.")
        recipient = await self.members.by_handle(handle)
        now = int(self.clock.now().timestamp())
        mail_id = await self.database.write(
            """
            INSERT INTO mail(
              uid,from_id,from_label,to_id,to_label,body,created_at,state,expires_at
            ) VALUES('pending',?,?,?,?,?,?,?,?)
            """,
            (
                sender.id,
                sender.handle,
                recipient.id if recipient else None,
                handle.lower(),
                body,
                now,
                "queued",
                now + self.hold_days * 86_400,
            ),
        )
        await self.database.write(
            "UPDATE mail SET uid=? WHERE id=?", (f"{self.origin_node}:{mail_id}", mail_id)
        )
        return mail_id

    async def bind_handle(self, member: Member) -> int:
        if member.handle is None:
            return 0
        rows = await self.database.read(
            "SELECT id FROM mail WHERE to_id IS NULL AND to_label=? AND expires_at>?",
            (member.handle, int(self.clock.now().timestamp())),
        )
        for row in rows:
            await self.database.write("UPDATE mail SET to_id=? WHERE id=?", (member.id, row["id"]))
        return len(rows)

    async def inbox(self, member: Member, limit: int = 5) -> list[MailView]:
        rows = await self.database.read(
            """
            SELECT id,from_label,to_label,body,created_at,read_at,reply_peer_mesh_id FROM mail
            WHERE to_id=? AND state NOT IN ('expired','undeliverable')
            ORDER BY created_at DESC LIMIT ?
            """,
            (member.id, limit),
        )
        return [MailView(**dict(row)) for row in rows]

    async def read(self, member: Member, mail_id: int) -> MailView | None:
        rows = await self.database.read(
            """
            SELECT id,from_label,to_label,body,created_at,read_at,reply_peer_mesh_id
            FROM mail WHERE id=? AND to_id=?
            """,
            (mail_id, member.id),
        )
        if not rows:
            return None
        now = int(self.clock.now().timestamp())
        await self.database.write(
            "UPDATE mail SET read_at=?,state='read' WHERE id=?", (now, mail_id)
        )
        row = dict(rows[0])
        row["read_at"] = now
        return MailView(**row)

    async def reply(self, sender: Member, mail_id: int, fallback_handle: str, body: str) -> None:
        rows = await self.database.read(
            "SELECT reply_peer_mesh_id FROM mail WHERE id=? AND to_id=?", (mail_id, sender.id)
        )
        peer_id = str(rows[0]["reply_peer_mesh_id"] or "") if rows else ""
        if peer_id:
            if self.federated_reply is None:
                raise ValueError("Federated reply is unavailable.")
            await self.federated_reply(peer_id, body)
            return
        await self.send(sender, fallback_handle, body)

    async def delete(self, member: Member, mail_id: int) -> bool:
        rows = await self.database.read(
            "SELECT id FROM mail WHERE id=? AND to_id=?",
            (mail_id, member.id),
        )
        if not rows:
            return False
        await self.database.write("DELETE FROM mail WHERE id=?", (mail_id,))
        return True
