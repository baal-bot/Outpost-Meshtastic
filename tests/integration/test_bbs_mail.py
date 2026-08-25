import pytest

from outpost.bbs.digests import DigestService
from outpost.bbs.mail import MailService
from outpost.bbs.service import BBSService, derive_subject
from outpost.clock import VirtualClock
from outpost.config import Config
from outpost.store import Database
from outpost.store.members import MemberRepo


@pytest.mark.asyncio
async def test_thread_reply_search_and_mail_lifecycle(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock = VirtualClock()
    members = MemberRepo(database, clock)
    dana = await members.resolve("!00000001")
    dana = await members.claim_handle(dana.mesh_id, "dana")
    ray = await members.resolve("!00000002")
    ray = await members.claim_handle(ray.mesh_id, "ray")
    bbs = BBSService(database, clock, "!699c2f30")
    mail = MailService(database, members, clock, "!699c2f30")

    assert len(await bbs.boards(dana)) == 7
    created = await bbs.create_thread("roads", "Mill Road bridge is open.", dana)
    assert created.subject == "Mill Road bridge is open"
    reply = await bbs.reply(created.id, "Confirmed one lane.", ray)
    assert reply.seq == 2
    await bbs.reply(created.id, "Town notified.", dana)
    await bbs.reply(created.id, "Cones are up.", ray)
    replies = await bbs.replies(created.id, dana, after_seq=1, limit=2)
    assert [value.seq for value in replies] == [2, 3]
    opening = await bbs.thread(created.id, dana)
    assert opening is not None and opening.post_count == 4
    results = await bbs.search("bridge", dana)
    assert results and results[0].thread_id == created.id
    assert await bbs.new_counts(dana) == {"roads": 1}
    assert await bbs.new_counts(dana) == {}
    await bbs.subscribe("roads", dana)
    assert await bbs.unsubscribe("roads", dana) is True
    assert await bbs.unsubscribe("roads", dana) is False

    mail_id = await mail.send(dana, "ray", "Can you check the culvert?")
    inbox = await mail.inbox(ray)
    assert inbox[0].id == mail_id
    read = await mail.read(ray, mail_id)
    assert read is not None and read.body == "Can you check the culvert?"
    assert await mail.delete(ray, mail_id) is True
    assert await mail.delete(ray, mail_id) is False
    assert await bbs.remove_own_post(created.id, 1, dana, 30) is True
    assert await bbs.thread(created.id, dana) is None
    await database.close()


@pytest.mark.asyncio
async def test_mail_to_unknown_handle_binds_on_claim(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock = VirtualClock()
    members = MemberRepo(database, clock)
    sender = await members.resolve("!00000001")
    sender = await members.claim_handle(sender.mesh_id, "dana")
    mail = MailService(database, members, clock, "!699c2f30")
    await mail.send(sender, "newperson", "Welcome.")
    recipient = await members.resolve("!00000003")
    recipient = await members.claim_handle(recipient.mesh_id, "newperson")
    assert await mail.bind_handle(recipient) == 1
    assert len(await mail.inbox(recipient)) == 1
    await database.close()


@pytest.mark.asyncio
async def test_reply_uses_preserved_federation_peer_route(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock = VirtualClock()
    members = MemberRepo(database, clock)
    operator = await members.resolve("!00000001")
    operator = await members.claim_handle(operator.mesh_id, "lead")
    replies: list[tuple[str, str]] = []

    async def relay(peer_id: str, body: str) -> None:
        replies.append((peer_id, body))

    service = MailService(database, members, clock, "local", federated_reply=relay)
    mail_id = await database.write(
        "INSERT INTO mail(uid,from_label,to_id,to_label,body,created_at,state,expires_at,"
        "reply_peer_mesh_id) VALUES('fed:test','operator@ALPHA',?,'lead','Check in',1,"
        "'delivered',999999,'!aaaaaaaa')",
        (operator.id,),
    )

    await service.reply(operator, mail_id, "operator@ALPHA", "All clear.")

    assert replies == [("!aaaaaaaa", "All clear.")]
    await database.close()


@pytest.mark.asyncio
async def test_operator_catch_all_mail_is_not_exposed_to_radio_members(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock = VirtualClock()
    members = MemberRepo(database, clock)
    operator = await members.resolve("!00000001")
    operator = await members.claim_handle(operator.mesh_id, "lead")
    await database.write("UPDATE member SET trust='operator' WHERE id=?", (operator.id,))
    operator = await members.resolve(operator.mesh_id)
    service = MailService(database, members, clock, "local")
    mail_id = await database.write(
        "INSERT INTO mail(uid,from_label,to_label,body,created_at,state,expires_at,"
        "reply_peer_mesh_id) VALUES('fed:operator','operator@ALPHA','operator','Check in',1,"
        "'delivered',999999,'!aaaaaaaa')"
    )

    assert await service.inbox(operator) == []
    assert await service.read(operator, mail_id) is None
    assert await service.delete(operator, mail_id) is False
    await database.close()


def test_subject_uses_word_boundary() -> None:
    subject = derive_subject(
        "This is a deliberately long subject that must stop at a word boundary here"
    )
    assert len(subject) <= 48
    assert not subject.endswith("subj")


@pytest.mark.asyncio
async def test_immediate_and_daily_digests_are_coalesced_and_checkpointed(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock = VirtualClock()
    config = Config.model_validate({"store": {"path": str(tmp_path / "outpost.db")}})
    members = MemberRepo(database, clock)
    author = await members.resolve("!00000001")
    author = await members.claim_handle(author.mesh_id, "author")
    reader = await members.resolve("!00000002")
    reader = await members.claim_handle(reader.mesh_id, "reader")
    bbs = BBSService(database, clock, "local")
    digests = DigestService(database, clock, config)

    await bbs.subscribe("roads", reader, "immediate")
    await bbs.create_thread("roads", "Bridge is open", author)
    due = await digests.due()
    assert len(due) == 1 and due[0].cadence == "immediate"
    assert "roads: Bridge is open" in due[0].text
    await digests.mark_scheduled(due[0])
    assert await digests.due() == []

    await bbs.subscribe("events", reader, "daily")
    await bbs.create_thread("events", "Market on Saturday", author)
    assert await digests.due() == []
    clock.advance(13 * 3_600)
    daily = await digests.due()
    assert len(daily) == 1 and daily[0].cadence == "daily"
    await database.close()
