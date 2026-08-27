import asyncio

import pytest

from outpost.clock import VirtualClock
from outpost.store import Database
from outpost.store.members import MemberRepo


@pytest.mark.asyncio
async def test_database_migrates_and_resolves_member(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    repository = MemberRepo(database, VirtualClock())
    member = await repository.resolve("!a1b2c3d4", last_heard_snr=8.5, hops_away=2)
    assert member.trust == "guest"
    assert member.mesh_num == 0xA1B2C3D4
    heard = await database.read(
        "SELECT last_heard_snr,hops_away FROM member WHERE id=?", (member.id,)
    )
    assert dict(heard[0]) == {"last_heard_snr": 8.5, "hops_away": 2}
    await database.write("UPDATE member SET directory_state='ignored' WHERE id=?", (member.id,))
    await repository.claim_handle(member.mesh_id, "relay")
    state = await database.read("SELECT directory_state FROM member WHERE id=?", (member.id,))
    assert state[0]["directory_state"] == "active"
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
