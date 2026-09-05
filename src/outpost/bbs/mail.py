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
        federated_reply: Callable[[str, str, str, str, str], Awaitable[None]] | None = None,
    ) -> None:
        self.database, self.members, self.clock = database, members, clock
        self.origin_node, self.hold_days = origin_node, hold_days
        self.federated_reply = federated_reply

    async def send(
        self,
        sender: Member,
        handle: str,
        body: str,
        *,
        conversation_key: str | None = None,
        in_reply_to: int | None = None,
        subject: str | None = None,
        participant_handle: str | None = None,
    ) -> int:
        if sender.handle is None or sender.trust == "guest":
            raise PermissionError("Claim a NAME before sending mail.")
        if not body or len(body.encode()) > 200:
            raise ValueError("Mail must be 1-200 bytes.")
        recipient = await self.members.by_handle(handle)
        now = int(self.clock.now().timestamp())
        # The placeholder must never be committed or visible to another send.
        # Keep the established node:row-id UID and reply context in the same unit.
        async with self.database.transaction() as transaction:
            mail_id = await transaction.write(
                """
                INSERT INTO mail(
                  uid,from_id,from_label,to_id,to_label,subject,body,created_at,state,expires_at,
                  in_reply_to,conversation_key,participant_handle,operator_actor
                ) VALUES('pending',?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    sender.id,
                    sender.handle,
                    recipient.id if recipient else None,
                    handle.lower(),
                    subject,
                    body,
                    now,
                    "queued",
                    now + self.hold_days * 86_400,
                    in_reply_to,
                    conversation_key,
                    (participant_handle or handle).lower(),
                    f"member:@{sender.handle}",
                ),
            )
            uid = f"{self.origin_node}:{mail_id}"
            await transaction.write(
                "UPDATE mail SET uid=?,conversation_key=COALESCE(conversation_key,?) WHERE id=?",
                (uid, f"local:{uid}", mail_id),
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
            "SELECT reply_peer_mesh_id,federation_conversation_id,uid,subject,conversation_key,"
            "participant_handle FROM mail "
            "WHERE id=? AND to_id=?",
            (mail_id, sender.id),
        )
        context = dict(rows[0]) if rows else {}
        peer_id = str(context.get("reply_peer_mesh_id") or "")
        if peer_id:
            if self.federated_reply is None:
                raise ValueError("Federated reply is unavailable.")
            conversation_id = str(
                context["federation_conversation_id"] or str(context["uid"]).removeprefix("fed:")
            )
            await self.federated_reply(
                peer_id,
                sender.handle or "member",
                conversation_id,
                str(context["subject"] or "Mesh reply"),
                body,
            )
            return
        await self.send(
            sender,
            fallback_handle,
            body,
            conversation_key=str(context.get("conversation_key") or "") or None,
            in_reply_to=mail_id,
            subject=str(context.get("subject") or "") or None,
            participant_handle=str(context.get("participant_handle") or "") or fallback_handle,
        )

    async def delete(self, member: Member, mail_id: int) -> bool:
        rows = await self.database.read(
            "SELECT id FROM mail WHERE id=? AND to_id=?",
            (mail_id, member.id),
        )
        if not rows:
            return False
        await self.database.write("DELETE FROM mail WHERE id=?", (mail_id,))
        return True
