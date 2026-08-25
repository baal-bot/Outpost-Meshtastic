import pytest

from outpost.clock import VirtualClock
from outpost.fed import FederationMailService, FederationPeerService
from outpost.store import Database


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

    envelope = await sender.seal("!bbbbbbbb", "alex", "sam@Alpha", "Check in", "We are safe.")
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
