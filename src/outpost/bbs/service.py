from __future__ import annotations

import re
from dataclasses import dataclass

from outpost.clock import Clock
from outpost.router.models import TrustLevel
from outpost.store.database import Database, Transaction
from outpost.store.members import Member


@dataclass(frozen=True)
class BoardSummary:
    id: int
    slug: str
    title: str
    thread_count: int


@dataclass(frozen=True)
class ThreadSummary:
    id: int
    board_slug: str
    subject: str
    author_label: str
    post_count: int
    created_at: int
    last_post_at: int


@dataclass(frozen=True)
class PostView:
    id: int
    thread_id: int
    board_slug: str
    seq: int
    author_label: str
    subject: str
    body: str
    created_at: int
    post_count: int


def _trust_at_least(actual: str, required: str) -> bool:
    return TrustLevel.parse(actual) >= TrustLevel.parse(required)


def derive_subject(body: str) -> str:
    sentence = re.split(r"(?<=[.!?])\s", body.strip(), maxsplit=1)[0]
    candidate = sentence[:48]
    if len(sentence) > 48 and " " in candidate:
        candidate = candidate.rsplit(" ", 1)[0]
    return candidate.rstrip(".,;: ") or "Post"


class BBSService:
    def __init__(
        self,
        database: Database,
        clock: Clock,
        origin_node: str,
        page_ttl_minutes: int = 15,
    ) -> None:
        self.database, self.clock, self.origin_node = database, clock, origin_node
        self.page_ttl_seconds = page_ttl_minutes * 60

    async def boards(self, member: Member) -> list[BoardSummary]:
        rows = await self.database.read(
            """
            SELECT b.id,b.slug,b.title,COUNT(t.id) AS thread_count,b.min_read_trust
            FROM board b LEFT JOIN thread t ON t.board_id=b.id AND t.hidden=0
            WHERE b.archived=0 GROUP BY b.id ORDER BY b.sort_order,b.slug
            """
        )
        return [
            BoardSummary(row["id"], row["slug"], row["title"], row["thread_count"])
            for row in rows
            if _trust_at_least(member.trust, row["min_read_trust"])
        ]

    async def board(self, slug: str, member: Member) -> tuple[int, str, str] | None:
        rows = await self.database.read(
            "SELECT id,slug,min_read_trust FROM board WHERE slug=? AND archived=0",
            (slug.lower(),),
        )
        if not rows or not _trust_at_least(member.trust, rows[0]["min_read_trust"]):
            return None
        row = rows[0]
        return row["id"], row["slug"], row["min_read_trust"]

    async def threads(
        self, slug: str, member: Member, limit: int = 5, offset: int = 0
    ) -> list[ThreadSummary]:
        board = await self.board(slug, member)
        if board is None:
            return []
        rows = await self.database.read(
            """
            SELECT t.id,b.slug AS board_slug,t.subject,
                   COALESCE(m.handle,'anon') AS author_label,
                   t.post_count,t.created_at,t.last_post_at
            FROM thread t JOIN board b ON b.id=t.board_id
            LEFT JOIN member m ON m.id=t.author_id
            WHERE t.board_id=? AND t.hidden=0
            ORDER BY t.pinned DESC,t.last_post_at DESC LIMIT ? OFFSET ?
            """,
            (board[0], limit, offset),
        )
        return [ThreadSummary(**dict(row)) for row in rows]

    async def create_thread(self, slug: str, body: str, member: Member) -> ThreadSummary:
        if not body or len(body.encode()) > 200:
            raise ValueError("Post must be 1-200 bytes.")
        subject = derive_subject(body)
        now = int(self.clock.now().timestamp())
        async with self.database.transaction() as transaction:
            board_rows = await transaction.read(
                "SELECT id,min_post_trust FROM board WHERE slug=? AND archived=0",
                (slug.lower(),),
            )
            if not board_rows:
                raise ValueError(f'No board "{slug}".')
            board = board_rows[0]
            if not _trust_at_least(member.trust, board["min_post_trust"]):
                raise PermissionError("Claim a NAME before posting.")
            thread_id = await transaction.write(
                """
                INSERT INTO thread(
                  uid,board_id,subject,author_id,origin_node,created_at,last_post_at,post_count
                )
                VALUES('pending',?,?,?,?,?,?,1)
                """,
                (board["id"], subject, member.id, self.origin_node, now, now),
            )
            thread_uid = f"{self.origin_node}:{thread_id}"
            await transaction.write("UPDATE thread SET uid=? WHERE id=?", (thread_uid, thread_id))
            post_id = await transaction.write(
                """
                INSERT INTO post(
                  uid,thread_id,seq,author_id,author_label,origin_node,body,created_at
                ) VALUES('pending',?,1,?,?,?,?,?)
                """,
                (thread_id, member.id, member.handle or "anon", self.origin_node, body, now),
            )
            await transaction.write(
                "UPDATE post SET uid=? WHERE id=?", (f"{self.origin_node}:{post_id}", post_id)
            )
        return ThreadSummary(thread_id, slug.lower(), subject, member.handle or "anon", 1, now, now)

    async def _thread(
        self, thread_id: int, member: Member, transaction: Transaction | None = None
    ) -> PostView | None:
        store = transaction or self.database
        rows = await store.read(
            """
            SELECT p.id,p.thread_id,b.slug AS board_slug,p.seq,p.author_label,t.subject,
                   p.body,p.created_at,t.post_count,b.min_read_trust
            FROM thread t JOIN board b ON b.id=t.board_id
            JOIN post p ON p.thread_id=t.id AND p.seq=1
            WHERE t.id=? AND t.hidden=0 AND p.hidden=0
            """,
            (thread_id,),
        )
        if not rows or not _trust_at_least(member.trust, rows[0]["min_read_trust"]):
            return None
        row = dict(rows[0])
        row.pop("min_read_trust")
        return PostView(**row)

    async def thread(self, thread_id: int, member: Member) -> PostView | None:
        return await self._thread(thread_id, member)

    async def reply(self, thread_id: int, body: str, member: Member) -> PostView:
        if not _trust_at_least(member.trust, "member"):
            raise PermissionError("Claim a NAME before replying.")
        if not body or len(body.encode()) > 200:
            raise ValueError("Reply must be 1-200 bytes.")
        now = int(self.clock.now().timestamp())
        async with self.database.transaction() as transaction:
            opening = await self._thread(thread_id, member, transaction)
            if opening is None:
                raise ValueError("No thread.")
            sequence = await transaction.read(
                "SELECT COALESCE(MAX(seq),0)+1 value FROM post WHERE thread_id=?", (thread_id,)
            )
            seq = int(sequence[0]["value"])
            post_id = await transaction.write(
                """
                INSERT INTO post(
                  uid,thread_id,seq,author_id,author_label,origin_node,body,created_at
                ) VALUES('pending',?,?,?,?,?,?,?)
                """,
                (thread_id, seq, member.id, member.handle or "anon", self.origin_node, body, now),
            )
            await transaction.write(
                "UPDATE post SET uid=? WHERE id=?", (f"{self.origin_node}:{post_id}", post_id)
            )
            await transaction.write(
                "UPDATE thread SET post_count=?,last_post_at=? WHERE id=?", (seq, now, thread_id)
            )
        return PostView(
            post_id,
            thread_id,
            opening.board_slug,
            seq,
            member.handle or "anon",
            opening.subject,
            body,
            now,
            seq,
        )

    async def replies(
        self, thread_id: int, member: Member, *, after_seq: int = 1, limit: int = 3
    ) -> list[PostView]:
        opening = await self.thread(thread_id, member)
        if opening is None:
            return []
        rows = await self.database.read(
            """
            SELECT p.id,p.thread_id,b.slug AS board_slug,p.seq,p.author_label,t.subject,
                   p.body,p.created_at,t.post_count
            FROM post p JOIN thread t ON t.id=p.thread_id
            JOIN board b ON b.id=t.board_id
            WHERE p.thread_id=? AND p.seq>? AND p.hidden=0 AND t.hidden=0
            ORDER BY p.seq LIMIT ?
            """,
            (thread_id, after_seq, limit),
        )
        return [PostView(**dict(row)) for row in rows]

    async def search(self, terms: str, member: Member, limit: int = 3) -> list[PostView]:
        rows = await self.database.read(
            """
            SELECT p.id,p.thread_id,b.slug AS board_slug,p.seq,p.author_label,t.subject,
                   p.body,p.created_at,t.post_count,b.min_read_trust
            FROM post_fts f JOIN post p ON p.id=f.rowid
            JOIN thread t ON t.id=p.thread_id JOIN board b ON b.id=t.board_id
            WHERE post_fts MATCH ? AND p.hidden=0 AND t.hidden=0
            ORDER BY bm25(post_fts),p.created_at DESC LIMIT ?
            """,
            (terms, limit),
        )
        results = []
        for raw in rows:
            row = dict(raw)
            required = row.pop("min_read_trust")
            if _trust_at_least(member.trust, required):
                results.append(PostView(**row))
        return results

    async def new_counts(self, member: Member) -> dict[str, int]:
        marker_rows = await self.database.read(
            "SELECT last_seen_at FROM read_marker WHERE member_id=? AND scope='new'",
            (member.id,),
        )
        marker = int(marker_rows[0]["last_seen_at"]) if marker_rows else 0
        rows = await self.database.read(
            """
            SELECT b.slug,b.min_read_trust,COUNT(t.id) AS count
            FROM board b JOIN thread t ON t.board_id=b.id
            WHERE t.hidden=0 AND t.last_post_at>?
            GROUP BY b.id ORDER BY b.sort_order
            """,
            (marker,),
        )
        counts = {
            row["slug"]: int(row["count"])
            for row in rows
            if _trust_at_least(member.trust, row["min_read_trust"])
        }
        now = int(self.clock.now().timestamp())
        await self.database.write(
            """
            INSERT INTO read_marker(member_id,scope,last_seen_at)
            VALUES(?,'new',?)
            ON CONFLICT(member_id,scope) DO UPDATE SET last_seen_at=excluded.last_seen_at
            """,
            (member.id, now),
        )
        return counts

    async def subscribe(self, slug: str, member: Member, cadence: str = "on_request") -> None:
        if not _trust_at_least(member.trust, "member"):
            raise PermissionError("Claim a NAME before subscribing.")
        board = await self.board(slug, member)
        if board is None:
            raise ValueError(f'No board "{slug}".')
        if cadence not in {"on_request", "daily", "immediate"}:
            raise ValueError("Cadence must be on_request, daily, or immediate.")
        await self.database.write(
            """
            INSERT INTO subscription(member_id,board_id,cadence,created_at)
            VALUES(?,?,?,?)
            ON CONFLICT(member_id,board_id) DO UPDATE SET cadence=excluded.cadence
            """,
            (member.id, board[0], cadence, int(self.clock.now().timestamp())),
        )
        if cadence in {"daily", "immediate"}:
            latest = await self.database.read("SELECT COALESCE(MAX(id),0) AS id FROM thread")
            await self.database.write(
                """
                INSERT INTO digest_state(member_id,cadence,last_thread_id,last_sent_at)
                VALUES(?,?,?,?) ON CONFLICT(member_id,cadence) DO NOTHING
                """,
                (
                    member.id,
                    cadence,
                    latest[0]["id"],
                    int(self.clock.now().timestamp()) if cadence == "daily" else None,
                ),
            )

    async def unsubscribe(self, slug: str, member: Member) -> bool:
        board = await self.board(slug, member)
        if board is None:
            raise ValueError(f'No board "{slug}".')
        rows = await self.database.read(
            "SELECT 1 FROM subscription WHERE member_id=? AND board_id=?",
            (member.id, board[0]),
        )
        if rows:
            await self.database.write(
                "DELETE FROM subscription WHERE member_id=? AND board_id=?",
                (member.id, board[0]),
            )
        return bool(rows)

    async def remove_own_post(
        self, thread_id: int, seq: int, member: Member, window_minutes: int
    ) -> bool:
        rows = await self.database.read(
            """
            SELECT id,created_at FROM post
            WHERE thread_id=? AND seq=? AND author_id=? AND hidden=0
            """,
            (thread_id, seq, member.id),
        )
        if not rows:
            return False
        age = int(self.clock.now().timestamp()) - int(rows[0]["created_at"])
        if age > window_minutes * 60:
            raise PermissionError("Delete window expired. Ask operator.")
        await self.database.write(
            "UPDATE post SET hidden=1,hidden_by=?,hidden_reason='self-delete' WHERE id=?",
            (member.mesh_id, rows[0]["id"]),
        )
        if seq == 1:
            await self.database.write("UPDATE thread SET hidden=1 WHERE id=?", (thread_id,))
        return True

    async def moderate_remove(self, thread_id: int, seq: int, actor: Member, reason: str) -> bool:
        if TrustLevel.parse(actor.trust) < TrustLevel.OPERATOR:
            raise PermissionError("Operator access required.")
        rows = await self.database.read(
            "SELECT id FROM post WHERE thread_id=? AND seq=? AND hidden=0",
            (thread_id, seq),
        )
        if not rows:
            return False
        reason = reason.strip()[:160] or "operator removal"
        await self.database.write(
            "UPDATE post SET hidden=1,hidden_by=?,hidden_reason=? WHERE id=?",
            (actor.mesh_id, reason, rows[0]["id"]),
        )
        if seq == 1:
            await self.database.write("UPDATE thread SET hidden=1 WHERE id=?", (thread_id,))
        await self.database.write(
            """
            INSERT INTO audit_log(actor_kind,actor_ref,action,target,detail,created_at)
            VALUES('member',?,'bbs.remove',?,?,?)
            """,
            (
                actor.mesh_id,
                f"thread:{thread_id}:post:{seq}",
                reason,
                int(self.clock.now().timestamp()),
            ),
        )
        return True

    async def moderation_status(self) -> tuple[int, int]:
        hidden = await self.database.read("SELECT COUNT(*) AS count FROM post WHERE hidden=1")
        audited = await self.database.read(
            "SELECT COUNT(*) AS count FROM audit_log WHERE action LIKE 'bbs.%'"
        )
        return int(hidden[0]["count"]), int(audited[0]["count"])
