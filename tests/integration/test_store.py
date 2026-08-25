import pytest

from outpost.clock import VirtualClock
from outpost.store import Database
from outpost.store.members import MemberRepo


@pytest.mark.asyncio
async def test_database_migrates_and_resolves_member(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    member = await MemberRepo(database, VirtualClock()).resolve("!a1b2c3d4")
    assert member.trust == "guest"
    assert member.mesh_num == 0xA1B2C3D4
    await database.close()
