from __future__ import annotations

from types import SimpleNamespace

import pytest

from outpost.ai.budget import conservative_tokens
from outpost.ai.retrieval import RetrievalEngine
from outpost.ai.tools import ReadOnlyToolCatalogue, ToolValidationError
from outpost.clock import VirtualClock
from outpost.store import Database
from outpost.store.members import MemberRepo


@pytest.mark.asyncio
async def test_tool_catalogue_is_strict_bounded_and_filters_board_permissions(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock = VirtualClock()
    members = MemberRepo(database, clock)
    member = await members.resolve("!00000001")
    member = await members.claim_handle(member.mesh_id, "relay")
    await database.write("UPDATE board SET min_read_trust='trusted' WHERE slug='roads'")

    async def add_post(board: str, uid: str) -> None:
        rows = await database.read("SELECT id FROM board WHERE slug=?", (board,))
        thread_id = await database.write(
            """
            INSERT INTO thread(uid,board_id,subject,author_id,origin_node,created_at,last_post_at)
            VALUES(?,?,?,?,?,unixepoch(),unixepoch())
            """,
            (f"thread:{uid}", rows[0]["id"], "Generator", member.id, "local"),
        )
        await database.write(
            """
            INSERT INTO post(uid,thread_id,seq,author_id,author_label,origin_node,body,created_at)
            VALUES(?,?,1,?,?,?,'Generator available today',unixepoch())
            """,
            (f"post:{uid}", thread_id, member.id, "relay", "local"),
        )

    await add_post("gen", "public")
    await add_post("roads", "private")
    retrieval = RetrievalEngine(database, now=lambda: int(clock.now().timestamp()))
    catalogue = ReadOnlyToolCatalogue(retrieval)

    assert len(catalogue.definitions()) == 12
    result = await catalogue.invoke(
        "search_boards", {"query": "generator available", "limit": 8}, member, None
    )
    assert result.chunks
    assert all(chunk.ref.startswith("board:gen#") for chunk in result.chunks)
    tool = next(item for item in catalogue.TOOLS if item.name == "search_boards")
    assert conservative_tokens(result.content) <= tool.max_result_tokens

    with pytest.raises(ToolValidationError, match="invalid"):
        await catalogue.invoke(
            "search_boards",
            {"query": "generator", "limit": 5, "unexpected": True},
            member,
            None,
        )
    await database.close()


@pytest.mark.asyncio
async def test_find_member_tool_never_returns_position(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock = VirtualClock()
    members = MemberRepo(database, clock)
    asker = await members.resolve("!00000001")
    asker = await members.claim_handle(asker.mesh_id, "relay")
    target = await members.resolve("!00000002")
    await members.claim_handle(target.mesh_id, "dana")
    await database.write(
        """
        INSERT INTO member_position(member_id,lat,lon,source,received_at,expires_at)
        VALUES(?,40.1,-75.2,'radio',unixepoch(),unixepoch()+3600)
        """,
        (target.id,),
    )
    catalogue = ReadOnlyToolCatalogue(
        RetrievalEngine(database, now=lambda: int(clock.now().timestamp()))
    )

    result = await catalogue.invoke(
        "find_member", {"handle_or_partial": "dana"}, asker, SimpleNamespace()
    )

    assert "dana" in result.content
    assert all(value not in result.content.casefold() for value in ("40.1", "-75.2", "lat", "lon"))
    await database.close()
