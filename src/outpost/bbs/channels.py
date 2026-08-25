from __future__ import annotations

from dataclasses import dataclass

from outpost.router.models import TrustLevel
from outpost.store.database import Database
from outpost.store.members import Member


@dataclass(frozen=True)
class ChannelEntry:
    id: int
    name: str
    description: str | None
    slot: int | None
    psk_b64: str | None
    published: bool


class ChannelDirectory:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def list(self, member: Member) -> list[ChannelEntry]:
        rows = await self.database.read(
            """
            SELECT id,name,description,slot,NULL AS psk_b64,published,min_trust
            FROM channel_dir WHERE published=1 ORDER BY slot,name
            """
        )
        return [
            ChannelEntry(
                row["id"],
                row["name"],
                row["description"],
                row["slot"],
                None,
                bool(row["published"]),
            )
            for row in rows
            if TrustLevel.parse(member.trust) >= TrustLevel.parse(row["min_trust"])
        ]

    async def detail(self, name: str, member: Member, *, direct: bool) -> ChannelEntry | None:
        if not direct or TrustLevel.parse(member.trust) < TrustLevel.MEMBER:
            return None
        rows = await self.database.read(
            """
            SELECT id,name,description,slot,psk_b64,published,min_trust
            FROM channel_dir WHERE name=? AND published=1
            """,
            (name.lower(),),
        )
        if not rows:
            return None
        row = rows[0]
        if TrustLevel.parse(member.trust) < TrustLevel.parse(row["min_trust"]):
            return None
        return ChannelEntry(
            row["id"],
            row["name"],
            row["description"],
            row["slot"],
            row["psk_b64"],
            bool(row["published"]),
        )
