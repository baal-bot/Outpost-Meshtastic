import pytest

from outpost.bbs.admin import BBSAdmin
from outpost.clock import VirtualClock
from outpost.store import Database


@pytest.mark.asyncio
async def test_bbs_admin_lifecycle_validates_and_audits(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    admin = BBSAdmin(database, VirtualClock(), {"help", "ping"})

    with pytest.raises(ValueError, match="collides"):
        await admin.create_board({"slug": "help", "title": "Collision"})
    board_id = await admin.create_board(
        {
            "slug": "garden",
            "title": "Garden",
            "description": "Plants and produce",
            "min_post_trust": "member",
            "federated": True,
        }
    )
    assert (
        await admin.update_board(board_id, {"min_post_trust": "trusted", "federated": False})
        is True
    )
    board = await database.read("SELECT federated FROM board WHERE id=?", (board_id,))
    assert board[0]["federated"] == 0
    thread_id = await admin.create_thread(board_id, "Seed exchange", "Bring labeled seeds.")
    post_id = await admin.reply(thread_id, "Operator will provide envelopes.")
    await database.write("UPDATE thread SET post_count=0 WHERE id=?", (thread_id,))
    second_post_id = await admin.reply(thread_id, "Cached counts do not allocate sequences.")
    assert await admin.update_thread(thread_id, {"pinned": True, "locked": True}) is True
    rows = await database.read(
        "SELECT pinned,locked,post_count FROM thread WHERE id=?", (thread_id,)
    )
    assert dict(rows[0]) == {"pinned": 1, "locked": 1, "post_count": 3}
    assert await database.read("SELECT 1 FROM post WHERE id=?", (post_id,))
    assert await database.read("SELECT seq FROM post WHERE id=? AND seq=3", (second_post_id,))
    actions = [
        row["action"] for row in await database.read("SELECT action FROM audit_log ORDER BY id")
    ]
    assert actions == [
        "board.create",
        "board.update",
        "thread.create",
        "post.create",
        "post.create",
        "thread.update",
    ]
    await database.close()
