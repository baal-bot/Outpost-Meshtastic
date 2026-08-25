import pytest

from outpost.bbs.channels import ChannelDirectory
from outpost.bbs.service import BBSService
from outpost.clock import VirtualClock
from outpost.store import Database
from outpost.store.members import MemberRepo


@pytest.mark.asyncio
async def test_channel_keys_only_appear_in_member_dm_detail(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock = VirtualClock()
    members = MemberRepo(database, clock)
    guest = await members.resolve("!00000001")
    member = await members.resolve("!00000002")
    member = await members.claim_handle(member.mesh_id, "dana")
    await database.write("UPDATE channel_dir SET psk_b64='secret' WHERE name='public'")
    directory = ChannelDirectory(database)

    listed = await directory.list(guest)
    assert listed and all(entry.psk_b64 is None for entry in listed)
    assert await directory.detail("public", guest, direct=True) is None
    assert await directory.detail("public", member, direct=False) is None
    detail = await directory.detail("public", member, direct=True)
    assert detail is not None and detail.psk_b64 == "secret"
    await database.close()


@pytest.mark.asyncio
async def test_operator_removal_hides_post_and_records_audit(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock = VirtualClock()
    members = MemberRepo(database, clock)
    author = await members.resolve("!00000001")
    author = await members.claim_handle(author.mesh_id, "dana")
    operator = await members.resolve("!00000002")
    await database.write("UPDATE member SET trust='operator' WHERE id=?", (operator.id,))
    operator = await members.resolve(operator.mesh_id)
    bbs = BBSService(database, clock, "local")
    thread = await bbs.create_thread("roads", "Bridge report", author)
    reply = await bbs.reply(thread.id, "This is spam", author)

    assert await bbs.moderate_remove(thread.id, reply.seq, operator, "spam") is True
    assert await bbs.replies(thread.id, author) == []
    audit = await database.read("SELECT actor_ref,action,target,detail FROM audit_log")
    assert dict(audit[0]) == {
        "actor_ref": operator.mesh_id,
        "action": "bbs.remove",
        "target": f"thread:{thread.id}:post:{reply.seq}",
        "detail": "spam",
    }
    assert await bbs.moderation_status() == (1, 1)
    await database.close()
