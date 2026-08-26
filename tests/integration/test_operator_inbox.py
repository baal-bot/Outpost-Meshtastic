import pytest
from fastapi.testclient import TestClient

from outpost.clock import VirtualClock
from outpost.fed import FederationMailService, FederationPeerService
from outpost.store import Database
from outpost.web.api import create_web_app


async def paired_databases(tmp_path):
    first_db, second_db = Database(tmp_path / "first.db"), Database(tmp_path / "second.db")
    await first_db.open()
    await second_db.open()
    first_peers = FederationPeerService(first_db, VirtualClock(), "!aaaaaaaa")
    second_peers = FederationPeerService(second_db, VirtualClock(), "!bbbbbbbb")
    secret = bytes(range(32))
    for database, peers, remote, name in (
        (first_db, first_peers, "!bbbbbbbb", "Denver Outpost"),
        (second_db, second_peers, "!aaaaaaaa", "Pittsburgh Outpost"),
    ):
        await peers.discover(remote, name, 1, {}, "mqtt")
        await database.write(
            "UPDATE fed_peer SET state='active',shared_secret=?,relay_mail=1 WHERE mesh_id=?",
            (secret, remote),
        )
    return first_db, second_db, first_peers, second_peers


@pytest.mark.asyncio
async def test_operations_inbox_groups_member_conversation_and_preserves_reply_route(
    tmp_path,
) -> None:
    first_db, second_db, first_peers, second_peers = await paired_databases(tmp_path)
    member_id = await second_db.write(
        "INSERT INTO member(mesh_id,mesh_num,handle,trust,first_seen,last_seen) "
        "VALUES('!00000666',1638,'666','operator',1,1)"
    )
    first_mail = FederationMailService(first_db, first_peers, VirtualClock())
    second_mail = FederationMailService(second_db, second_peers, VirtualClock())

    opening = await first_mail.seal(
        "!bbbbbbbb",
        "666",
        "operator@PIT",
        "Moderation review",
        "Please review this post.",
    )
    await second_mail.open("!aaaaaaaa", opening)
    response = await second_mail.seal(
        "!aaaaaaaa",
        "operator",
        "666@DEN",
        "Moderation review",
        "I have reviewed it.",
        conversation_id=opening["conversation_id"],
        message_kind="member",
        participant_handle="666",
        reply_to="666",
        operator_actor="member:@666",
    )
    await first_mail.open("!bbbbbbbb", response)

    remote_inbound = (await second_db.read("SELECT * FROM mail WHERE mail_direction='in' LIMIT 1"))[
        0
    ]
    assert remote_inbound["to_id"] == member_id
    assert remote_inbound["participant_handle"] == "666"
    assert remote_inbound["operator_actor"] == "web:operator"
    local_response = (await first_db.read("SELECT * FROM mail WHERE mail_direction='in' LIMIT 1"))[
        0
    ]
    assert local_response["to_id"] is None
    assert local_response["from_label"] == "666@DEN"
    assert local_response["reply_recipient_handle"] == "666"
    assert local_response["message_kind"] == "member"

    replies: list[tuple[str, ...]] = []

    async def reply(*values: str):
        replies.append(values)
        return {"relay_id": "reply-1", "state": "sent"}

    client = TestClient(
        create_web_app(
            lambda: {"radio": "up"},
            database=first_db,
            federation_mail_reply=reply,
        )
    )
    listed = client.get("/api/v1/mail/conversations?route=federated&q=reviewed").json()
    assert listed["total"] == 1 and listed["counts"]["unread"] == 1
    conversation = listed["items"][0]
    assert conversation["message_count"] == 2
    assert conversation["message_kind"] == "member"
    assert conversation["participant_handle"] == "666"
    assert conversation["peer_name"] == "Denver Outpost"
    assert conversation["transports"] == ["mqtt"]
    assert conversation["reply_address"] == "666"

    key = conversation["conversation_key"]
    detail = client.get(f"/api/v1/mail/conversations/{key}").json()
    assert [message["mail_direction"] for message in detail["messages"]] == ["out", "in"]
    assert detail["conversation"]["unread_count"] == 0
    sent = client.post(f"/api/v1/mail/conversations/{key}/reply", json={"body": "Thank you."})
    assert sent.status_code == 200
    assert replies == [
        (
            "!bbbbbbbb",
            "666",
            "Moderation review",
            "Thank you.",
            opening["conversation_id"],
            "member",
            "666",
        )
    ]

    assert (
        client.patch(f"/api/v1/mail/conversations/{key}", json={"state": "unread"}).status_code
        == 200
    )
    assert client.get("/api/v1/mail/conversations?status=unread").json()["total"] == 1
    assert (
        client.patch(f"/api/v1/mail/conversations/{key}", json={"state": "archive"}).status_code
        == 200
    )
    assert client.get("/api/v1/mail/conversations").json()["total"] == 0
    assert client.get("/api/v1/mail/conversations?archive=archived").json()["total"] == 1
    await first_db.close()
    await second_db.close()


@pytest.mark.asyncio
async def test_operator_system_mail_stays_web_only_even_with_named_operator(tmp_path) -> None:
    first_db, second_db, first_peers, second_peers = await paired_databases(tmp_path)
    await second_db.write(
        "INSERT INTO member(mesh_id,mesh_num,handle,trust,first_seen,last_seen) "
        "VALUES('!00000001',1,'lead','operator',1,1)"
    )
    sender = FederationMailService(first_db, first_peers, VirtualClock())
    receiver = FederationMailService(second_db, second_peers, VirtualClock())

    envelope = await sender.seal(
        "!bbbbbbbb", "operator", "operator@PIT", "System status", "Can you check in?"
    )
    await receiver.open("!aaaaaaaa", envelope)

    message = (await second_db.read("SELECT * FROM mail WHERE mail_direction='in'"))[0]
    assert message["to_id"] is None
    assert message["to_label"] == "operator"
    assert message["message_kind"] == "system"
    assert message["participant_handle"] == "operator"
    await first_db.close()
    await second_db.close()
