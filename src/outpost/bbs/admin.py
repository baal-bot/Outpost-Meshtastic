from __future__ import annotations

import json
import re
from typing import Any

from outpost.clock import Clock
from outpost.store import Database

TRUST_LEVELS = {"guest", "member", "trusted", "responder", "operator"}
SLUG = re.compile(r"^[a-z0-9-]+$")
BOARD_FIELDS = {
    "title",
    "description",
    "min_read_trust",
    "min_post_trust",
    "retention_days",
    "sort_order",
    "archived",
    "federated",
}
THREAD_FIELDS = {"pinned", "locked", "hidden"}


class BBSAdmin:
    def __init__(
        self, database: Database, clock: Clock, reserved_slugs: set[str], origin: str = "local"
    ) -> None:
        self.database, self.clock, self.reserved_slugs, self.origin = (
            database,
            clock,
            reserved_slugs,
            origin,
        )

    async def _audit(self, action: str, target: str, detail: object) -> None:
        await self.database.write(
            """
            INSERT INTO audit_log(actor_kind,actor_ref,action,target,detail,created_at)
            VALUES('web','operator',?,?,?,?)
            """,
            (action, target, json.dumps(detail), int(self.clock.now().timestamp())),
        )

    async def create_board(self, values: dict[str, Any]) -> int:
        slug = str(values["slug"]).lower()
        if len(slug.encode()) > 16 or not SLUG.fullmatch(slug):
            raise ValueError("Slug must be 1-16 bytes using a-z, 0-9, and hyphen.")
        if slug in self.reserved_slugs:
            raise ValueError("Slug collides with a radio command or alias.")
        if await self.database.read("SELECT 1 FROM board WHERE slug=?", (slug,)):
            raise ValueError("Board slug already exists.")
        self._validate_trust(values)
        board_id = await self.database.write(
            """
            INSERT INTO board(
              slug,title,description,min_read_trust,min_post_trust,retention_days,sort_order,
              federated,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                slug,
                str(values["title"])[:80],
                values.get("description"),
                values.get("min_read_trust", "guest"),
                values.get("min_post_trust", "member"),
                values.get("retention_days"),
                values.get("sort_order", 100),
                int(bool(values.get("federated", False))),
                int(self.clock.now().timestamp()),
            ),
        )
        await self._audit("board.create", f"board:{board_id}", {"slug": slug})
        return board_id

    async def update_board(self, board_id: int, values: dict[str, Any]) -> bool:
        if not values or set(values) - BOARD_FIELDS:
            raise ValueError("No supported board changes supplied.")
        self._validate_trust(values)
        if not await self.database.read("SELECT 1 FROM board WHERE id=?", (board_id,)):
            return False
        assignments = ",".join(f"{key}=?" for key in values)
        await self.database.write(
            f"UPDATE board SET {assignments} WHERE id=?",  # noqa: S608
            (*values.values(), board_id),
        )
        await self._audit("board.update", f"board:{board_id}", sorted(values))
        return True

    @staticmethod
    def _validate_trust(values: dict[str, Any]) -> None:
        for key in ("min_read_trust", "min_post_trust"):
            if key in values and values[key] not in TRUST_LEVELS:
                raise ValueError(f"Invalid {key}.")

    async def create_thread(self, board_id: int, subject: str, body: str) -> int:
        if not await self.database.read("SELECT 1 FROM board WHERE id=?", (board_id,)):
            raise ValueError("Board not found.")
        if not subject.strip() or len(subject) > 64 or not body.strip() or len(body) > 1_000:
            raise ValueError("Subject is 1-64 characters; body is 1-1000 characters.")
        now = int(self.clock.now().timestamp())
        thread_id = await self.database.write(
            """
            INSERT INTO thread(
              uid,board_id,subject,origin_node,created_at,last_post_at,post_count
            ) VALUES('pending',?,?,?,?,?,1)
            """,
            (board_id, subject.strip(), self.origin, now, now),
        )
        await self.database.write(
            "UPDATE thread SET uid=? WHERE id=?", (f"{self.origin}:{thread_id}", thread_id)
        )
        post_id = await self.database.write(
            """
            INSERT INTO post(uid,thread_id,seq,author_label,origin_node,body,created_at)
            VALUES('pending',?,1,'operator',?,?,?)
            """,
            (thread_id, self.origin, body.strip(), now),
        )
        await self.database.write(
            "UPDATE post SET uid=? WHERE id=?", (f"{self.origin}:{post_id}", post_id)
        )
        await self._audit("thread.create", f"thread:{thread_id}", {"board_id": board_id})
        return thread_id

    async def reply(self, thread_id: int, body: str) -> int:
        if not body.strip() or len(body) > 1_000:
            raise ValueError("Reply is 1-1000 characters.")
        rows = await self.database.read("SELECT locked,hidden FROM thread WHERE id=?", (thread_id,))
        if not rows or rows[0]["hidden"]:
            raise ValueError("Thread not found.")
        if rows[0]["locked"]:
            raise ValueError("Thread is locked.")
        sequence = await self.database.read(
            "SELECT COALESCE(MAX(seq),0)+1 value FROM post WHERE thread_id=?", (thread_id,)
        )
        now, seq = int(self.clock.now().timestamp()), int(sequence[0]["value"])
        post_id = await self.database.write(
            """
            INSERT INTO post(uid,thread_id,seq,author_label,origin_node,body,created_at)
            VALUES('pending',?,?,'operator',?,?,?)
            """,
            (thread_id, seq, self.origin, body.strip(), now),
        )
        await self.database.write(
            "UPDATE post SET uid=? WHERE id=?", (f"{self.origin}:{post_id}", post_id)
        )
        await self.database.write(
            "UPDATE thread SET post_count=?,last_post_at=? WHERE id=?", (seq, now, thread_id)
        )
        await self._audit("post.create", f"post:{post_id}", {"thread_id": thread_id})
        return post_id

    async def update_thread(self, thread_id: int, values: dict[str, Any]) -> bool:
        if not values or set(values) - THREAD_FIELDS:
            raise ValueError("No supported thread changes supplied.")
        if not await self.database.read("SELECT 1 FROM thread WHERE id=?", (thread_id,)):
            return False
        assignments = ",".join(f"{key}=?" for key in values)
        await self.database.write(
            f"UPDATE thread SET {assignments} WHERE id=?",  # noqa: S608
            (*(int(value) for value in values.values()), thread_id),
        )
        await self._audit("thread.update", f"thread:{thread_id}", values)
        return True
