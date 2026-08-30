import pytest
from fastapi.testclient import TestClient

from outpost.clock import SystemClock, VirtualClock
from outpost.fed import FederationMailService, FederationPeerService
from outpost.store import Database
from outpost.web.api import create_web_app


@pytest.mark.asyncio
async def test_encrypted_mail_relay_delivers_to_local_member_once(tmp_path) -> None:
    first_db, second_db = Database(tmp_path / "a.db"), Database(tmp_path / "b.db")
    await first_db.open()
    await second_db.open()
    first_peers = FederationPeerService(first_db, VirtualClock(), "!aaaaaaaa")
    second_peers = FederationPeerService(second_db, VirtualClock(), "!bbbbbbbb")
    secret = bytes(range(32))
    for database, peers, remote in (
        (first_db, first_peers, "!bbbbbbbb"),
        (second_db, second_peers, "!aaaaaaaa"),
    ):
        await peers.discover(remote, "Peer", 1, {}, "radio")
        await database.write(
            "UPDATE fed_peer SET state='active',shared_secret=?,relay_mail=1 WHERE mesh_id=?",
            (secret, remote),
        )
    await second_db.write(
        "INSERT INTO member(mesh_id,mesh_num,handle,trust,first_seen,last_seen) "
        "VALUES('!00000001',1,'alex','member',1,1)"
    )
    sender = FederationMailService(first_db, first_peers, VirtualClock())
    receiver = FederationMailService(second_db, second_peers, VirtualClock())

    envelope = await sender.seal("!bbbbbbbb", " @alex ", "sam@Alpha", "Check in", "We are safe.")
    relay_id, state = await receiver.open("!aaaaaaaa", envelope)

    assert state == "delivered"
    assert await receiver.open("!aaaaaaaa", envelope) == (relay_id, "delivered")
    mail = await second_db.read(
        "SELECT subject,body,state FROM mail WHERE uid=?", (f"fed:{relay_id}",)
    )
    assert dict(mail[0]) == {"subject": "Check in", "body": "We are safe.", "state": "delivered"}
    await first_db.close()
    await second_db.close()


@pytest.mark.asyncio
async def test_inbound_mail_enforces_peer_and_recipient_quotas_without_spending_outbound(
    tmp_path,
) -> None:
    sender_db, receiver_db = Database(tmp_path / "sender.db"), Database(
        tmp_path / "receiver.db"
    )
    await sender_db.open()
    await receiver_db.open()
    sender_peers = FederationPeerService(sender_db, VirtualClock(), "!aaaaaaaa")
    receiver_peers = FederationPeerService(receiver_db, VirtualClock(), "!bbbbbbbb")
    secret = bytes(range(32))
    for database, peers, remote in (
        (sender_db, sender_peers, "!bbbbbbbb"),
        (receiver_db, receiver_peers, "!aaaaaaaa"),
    ):
        await peers.discover(remote, "Peer", 1, {}, "radio")
        await database.write(
            "UPDATE fed_peer SET state='active',shared_secret=?,relay_mail=1 WHERE mesh_id=?",
            (secret, remote),
        )
    await receiver_db.write(
        "UPDATE fed_peer SET quota_mail_per_hour=2,"
        "quota_mail_per_recipient_per_hour=1 WHERE mesh_id='!aaaaaaaa'"
    )
    await receiver_db.write(
        "INSERT INTO member(mesh_id,mesh_num,handle,trust,first_seen,last_seen) VALUES"
        "('!00000001',1,'alex','member',1,1),"
        "('!00000002',2,'pat','member',1,1)"
    )
    sender = FederationMailService(sender_db, sender_peers, SystemClock())
    receiver = FederationMailService(receiver_db, receiver_peers, SystemClock())

    first = await sender.seal("!bbbbbbbb", "alex", "sam@Alpha", "One", "Body")
    assert (await receiver.open("!aaaaaaaa", first))[1] == "delivered"
    repeated = await sender.seal("!bbbbbbbb", "alex", "sam@Alpha", "Two", "Body")
    with pytest.raises(ValueError, match="recipient @alex"):
        await receiver.open("!aaaaaaaa", repeated)
    boundary = await sender.seal("!bbbbbbbb", "pat", "sam@Alpha", "Three", "Body")
    assert (await receiver.open("!aaaaaaaa", boundary))[1] == "delivered"
    exceeded = await sender.seal("!bbbbbbbb", "operator", "sam@Alpha", "Four", "Body")
    with pytest.raises(ValueError, match="peer inbound mail quota"):
        await receiver.open("!aaaaaaaa", exceeded)

    usage = (await receiver_db.read("SELECT * FROM fed_mail_usage"))[0]
    assert (usage["inbound_accepted"], usage["inbound_rejected"]) == (2, 2)
    assert len(await receiver_db.read("SELECT 1 FROM mail")) == 2
    assert len(await sender_db.read("SELECT 1 FROM fed_mail_delivery WHERE direction='out'")) == 4

    async def unused_send(*args, **kwargs):
        raise AssertionError("mail send callback should not be called")

    client = TestClient(
        create_web_app(
            lambda: {"radio": "up"},
            database=receiver_db,
            federation=receiver_peers,
            federation_mail_send=unused_send,
        )
    )
    dashboard_usage = client.get("/api/v1/federation/mail").json()["usage"][0]
    assert dashboard_usage["inbound_accepted"] == 2
    assert dashboard_usage["inbound_rejected"] == 2
    assert dashboard_usage["quota_mail_per_recipient_per_hour"] == 1
    await sender_db.close()
    await receiver_db.close()


@pytest.mark.asyncio
async def test_mail_relay_requires_peer_permission(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    peers = FederationPeerService(database, VirtualClock(), "!local")
    await peers.discover("!remote", "Remote", 1, {}, "radio")
    await database.write("UPDATE fed_peer SET state='active',shared_secret=?", (bytes(32),))

    with pytest.raises(ValueError, match="not enabled"):
        await FederationMailService(database, peers, VirtualClock()).seal(
            "!remote", "alex", "operator", "Hi", "Body"
        )
    await database.close()


@pytest.mark.asyncio
async def test_operator_mailbox_does_not_require_radio_member(tmp_path) -> None:
    first_db, second_db = Database(tmp_path / "first.db"), Database(tmp_path / "second.db")
    await first_db.open()
    await second_db.open()
    first_peers = FederationPeerService(first_db, VirtualClock(), "!aaaaaaaa")
    second_peers = FederationPeerService(second_db, VirtualClock(), "!bbbbbbbb")
    secret = bytes(range(32))
    for database, peers, remote in (
        (first_db, first_peers, "!bbbbbbbb"),
        (second_db, second_peers, "!aaaaaaaa"),
    ):
        await peers.discover(remote, "Peer", 1, {}, "radio")
        await database.write(
            "UPDATE fed_peer SET state='active',shared_secret=?,relay_mail=1 WHERE mesh_id=?",
            (secret, remote),
        )
    sender = FederationMailService(first_db, first_peers, VirtualClock())
    receiver = FederationMailService(second_db, second_peers, VirtualClock())

    envelope = await sender.seal(
        "!bbbbbbbb", "@operator", "operator@ALPHA", "Status", "Reply requested."
    )
    relay_id, state = await receiver.open("!aaaaaaaa", envelope)

    assert state == "delivered"
    mail = (
        await second_db.read(
            "SELECT to_id,to_label,reply_peer_mesh_id FROM mail WHERE uid=?", (f"fed:{relay_id}",)
        )
    )[0]
    assert dict(mail) == {
        "to_id": None,
        "to_label": "operator",
        "reply_peer_mesh_id": "!aaaaaaaa",
    }
    await first_db.close()
    await second_db.close()


@pytest.mark.asyncio
async def test_named_operator_member_mail_remains_member_mail(tmp_path) -> None:
    first_db, second_db = Database(tmp_path / "sender.db"), Database(tmp_path / "receiver.db")
    await first_db.open()
    await second_db.open()
    first_peers = FederationPeerService(first_db, VirtualClock(), "!aaaaaaaa")
    second_peers = FederationPeerService(second_db, VirtualClock(), "!bbbbbbbb")
    secret = bytes(range(32))
    for database, peers, remote in (
        (first_db, first_peers, "!bbbbbbbb"),
        (second_db, second_peers, "!aaaaaaaa"),
    ):
        await peers.discover(remote, "Peer", 1, {}, "radio")
        await database.write(
            "UPDATE fed_peer SET state='active',shared_secret=?,relay_mail=1 WHERE mesh_id=?",
            (secret, remote),
        )
    operator_id = await second_db.write(
        "INSERT INTO member(mesh_id,mesh_num,handle,trust,first_seen,last_seen) "
        "VALUES('!00000666',1638,'666','operator',1,1)"
    )
    sender = FederationMailService(first_db, first_peers, VirtualClock())
    receiver = FederationMailService(second_db, second_peers, VirtualClock())

    envelope = await sender.seal(
        "!bbbbbbbb", "@666", "operator@ALPHA", "Moderation", "Please review this issue."
    )
    relay_id, state = await receiver.open("!aaaaaaaa", envelope)

    assert state == "delivered"
    mail = (
        await second_db.read(
            "SELECT to_id,to_label,reply_peer_mesh_id FROM mail WHERE uid=?",
            (f"fed:{relay_id}",),
        )
    )[0]
    assert dict(mail) == {
        "to_id": operator_id,
        "to_label": "666",
        "reply_peer_mesh_id": "!aaaaaaaa",
    }
    await first_db.close()
    await second_db.close()


@pytest.mark.asyncio
async def test_federated_mail_rejects_member_system_identity_confusion(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    peers = FederationPeerService(database, VirtualClock(), "!local")
    await peers.discover("!remote", "Remote", 1, {}, "radio")
    await database.write(
        "UPDATE fed_peer SET state='active',shared_secret=?,relay_mail=1 WHERE mesh_id='!remote'",
        (bytes(32),),
    )
    mail = FederationMailService(database, peers, VirtualClock())

    with pytest.raises(ValueError, match="operator catch-all"):
        await mail.seal(
            "!remote",
            "666",
            "operator@LOCAL",
            "Invalid",
            "Body",
            message_kind="system",
            participant_handle="666",
        )
    with pytest.raises(ValueError, match="named member"):
        await mail.seal(
            "!remote",
            "operator",
            "operator@LOCAL",
            "Invalid",
            "Body",
            message_kind="member",
            participant_handle="operator",
        )
    assert await database.read("SELECT 1 FROM mail") == []
    await database.close()
