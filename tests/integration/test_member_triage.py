import time

import pytest

from outpost.store import Database
from outpost.web.member_triage import MemberTriageError, MemberTriageService


async def add_member(
    database: Database,
    mesh_num: int,
    *,
    trust: str = "guest",
    handle: str | None = None,
    first_seen: int,
    last_seen: int,
    notes: str | None = None,
) -> int:
    return await database.write(
        """INSERT INTO member(
          mesh_id,mesh_num,handle,trust,first_seen,last_seen,last_heard_snr,hops_away,notes
        ) VALUES(?,?,?,?,?,?,7.5,2,?)""",
        (f"!{mesh_num:08x}", mesh_num, handle, trust, first_seen, last_seen, notes),
    )


@pytest.mark.asyncio
async def test_member_triage_filters_review_history_and_detail(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    service = MemberTriageService(database)
    now = int(time.time())
    discovered_id = await add_member(
        database,
        1,
        first_seen=now - 300,
        last_seen=now - 60,
        notes="=spreadsheet formula",
    )
    stale_id = await add_member(
        database,
        2,
        first_seen=now - 40 * 86_400,
        last_seen=now - 31 * 86_400,
    )
    approved_id = await add_member(
        database,
        3,
        trust="member",
        handle="dana",
        first_seen=now - 86_400,
        last_seen=now - 120,
    )
    await database.write(
        "INSERT INTO member_position(member_id,lat,lon,received_at,source,expires_at) "
        "VALUES(?,40.4406,-79.9959,?,'position_app',?)",
        (discovered_id, now - 30, now + 3600),
    )
    await database.write(
        "INSERT INTO message_log(direction,peer_mesh_id,channel,portnum,is_direct,byte_len,"
        "command,outcome,rx_snr,hops,created_at) "
        "VALUES('in','!00000001',0,1,1,5,'PING','accepted',7.5,2,?)",
        (now - 30,),
    )

    listing = await service.list(view="all", saved=None, query="", cursor=0, limit=50)
    assert listing["approved_count"] == 1
    assert listing["discovered_count"] == 2
    assert listing["review_count"] == 2
    assert {item["category"] for item in listing["items"]} == {"approved", "discovered"}
    filters = {item["key"]: item["count"] for item in listing["saved_filters"]}
    assert filters["new"] == 1 and filters["stale"] == 1 and filters["review"] == 2

    detail = await service.detail(discovered_id)
    assert detail is not None
    assert detail["member"]["position_state"] == "active"
    assert detail["member"]["position_lat"] == pytest.approx(40.4406)
    assert detail["recent_activity"][0]["command"] == "PING"

    with pytest.raises(MemberTriageError, match="reason"):
        await service.update(
            discovered_id,
            trust="trusted",
            notes=None,
            notes_supplied=False,
            reason=None,
        )
    await service.update(
        discovered_id,
        trust="trusted",
        notes="Known neighbor",
        notes_supplied=True,
        reason="Identity verified in person",
    )
    reviewed = await service.detail(discovered_id)
    assert reviewed is not None
    assert reviewed["member"]["trust"] == "trusted"
    assert reviewed["member"]["notes"] == "Known neighbor"
    assert reviewed["trust_history"][0]["reason"] == "Identity verified in person"
    assert await database.read(
        "SELECT 1 FROM audit_log WHERE action='member.update' AND target='!00000001'"
    )

    with pytest.raises(MemberTriageError, match="discovered"):
        await service.set_state(approved_id, "archive", "Inactive member")
    archived = await service.set_state(stale_id, "archive", "Stale discovery cleanup")
    assert archived["state"] == "archived"
    inactive = await service.list(view="archived", saved=None, query="", cursor=0, limit=50)
    assert inactive["items"][0]["category"] == "archived"
    with pytest.raises(MemberTriageError, match="Restore"):
        await service.set_state(stale_id, "ignore", "Change inactive state")
    await service.set_state(stale_id, "restore", "")
    restored = await service.detail(stale_id)
    assert restored is not None and restored["member"]["directory_state"] == "active"
    await database.close()


@pytest.mark.asyncio
async def test_member_triage_bulk_safety_and_position_free_export(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    service = MemberTriageService(database)
    now = int(time.time())
    discovered_id = await add_member(
        database,
        10,
        first_seen=now - 60,
        last_seen=now - 30,
        notes="=unsafe cell",
    )
    approved_id = await add_member(
        database,
        11,
        trust="responder",
        handle="river",
        first_seen=now - 60,
        last_seen=now - 30,
    )
    await database.write(
        "INSERT INTO member_position(member_id,lat,lon,received_at,expires_at) VALUES(?,?,?,?,?)",
        (discovered_id, 40.44, -79.99, now, now + 3600),
    )

    result = await service.bulk(
        [discovered_id, approved_id], "ignore", "Repeated irrelevant discovery"
    )
    assert result == {"ok": True, "action": "ignore", "changed": 1, "skipped": 1}
    rows = await database.read(
        "SELECT id,directory_state FROM member WHERE id IN (?,?) ORDER BY id",
        (discovered_id, approved_id),
    )
    assert [row["directory_state"] for row in rows] == ["ignored", "active"]
    repeat = await service.bulk([discovered_id], "archive", "Already inactive")
    assert repeat["changed"] == 0 and repeat["skipped"] == 1

    content, count = await service.export([discovered_id, approved_id])
    assert count == 2
    assert "position_consent" in content
    assert "position_lat" not in content and "40.44" not in content and "-79.99" not in content
    assert "'=unsafe cell" in content
    assert await database.read("SELECT 1 FROM audit_log WHERE action='member.export'")
    assert await database.read("SELECT 1 FROM member WHERE id=?", (discovered_id,))
    await database.close()
