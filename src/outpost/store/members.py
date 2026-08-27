from __future__ import annotations

from dataclasses import dataclass

from outpost.clock import Clock

from .database import Database


@dataclass(frozen=True)
class Member:
    id: int
    mesh_id: str
    mesh_num: int
    handle: str | None
    trust: str
    first_seen: int
    last_seen: int


class MemberRepo:
    def __init__(self, database: Database, clock: Clock) -> None:
        self.database, self.clock = database, clock

    async def resolve(
        self,
        mesh_id: str,
        *,
        last_heard_snr: float | None = None,
        hops_away: int | None = None,
    ) -> Member:
        select_member = (
            "SELECT id,mesh_id,mesh_num,handle,trust,first_seen,last_seen "
            "FROM member WHERE mesh_id=?"
        )
        rows = await self.database.read(
            select_member,
            (mesh_id,),
        )
        now = int(self.clock.now().timestamp())
        if not rows:
            mesh_num = int(mesh_id.removeprefix("!"), 16)
            await self.database.write(
                "INSERT INTO member(mesh_id,mesh_num,first_seen,last_seen,"
                "last_heard_snr,hops_away) "
                "VALUES(?,?,?,?,?,?)",
                (mesh_id, mesh_num, now, now, last_heard_snr, hops_away),
            )
            rows = await self.database.read(
                select_member,
                (mesh_id,),
            )
        else:
            await self.database.write(
                "UPDATE member SET last_seen=?,last_heard_snr=COALESCE(?,last_heard_snr),"
                "hops_away=COALESCE(?,hops_away) WHERE mesh_id=?",
                (now, last_heard_snr, hops_away, mesh_id),
            )
        row = rows[0]
        return Member(**dict(row))

    async def claim_handle(self, mesh_id: str, handle: str, *, approve: bool = True) -> Member:
        normalized = handle.lower()
        existing = await self.database.read(
            "SELECT mesh_id FROM member WHERE handle=? AND mesh_id<>?",
            (normalized, mesh_id),
        )
        if existing:
            raise ValueError("handle is already claimed")
        now = int(self.clock.now().timestamp())
        trust = "member" if approve else "guest"
        await self.database.write(
            """
            UPDATE member SET handle=?,trust=?,handle_changed_at=?,last_seen=?,
              directory_state='active',directory_state_at=?,directory_state_by='mesh:enrollment'
            WHERE mesh_id=?
            """,
            (normalized, trust, now, now, now, mesh_id),
        )
        return await self.resolve(mesh_id)

    async def by_handle(self, handle: str) -> Member | None:
        rows = await self.database.read(
            """
            SELECT id,mesh_id,mesh_num,handle,trust,first_seen,last_seen
            FROM member WHERE handle=?
            """,
            (handle.lower(),),
        )
        return Member(**dict(rows[0])) if rows else None

    async def recent(self, limit: int = 8) -> list[Member]:
        rows = await self.database.read(
            """
            SELECT id,mesh_id,mesh_num,handle,trust,first_seen,last_seen
            FROM member ORDER BY last_seen DESC LIMIT ?
            """,
            (limit,),
        )
        return [Member(**dict(row)) for row in rows]
