import asyncio

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


@pytest.mark.asyncio
async def test_database_reuses_a_bounded_read_connection_pool(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()

    results = await asyncio.gather(
        *(database.read("SELECT COUNT(*) count FROM member") for _ in range(24))
    )
    first = database.read_pool_status()
    assert all(result[0]["count"] == 0 for result in results)
    assert 1 <= first["opened"] <= first["capacity"] == 2
    assert first["active"] == first["opened"]
    assert first["queries"] == 24

    for _ in range(10):
        await database.read("SELECT 1")
    reused = database.read_pool_status()
    assert reused["opened"] == first["opened"]
    assert reused["queries"] == 34

    await database.close()
    assert database.read_pool_status()["active"] == 0
