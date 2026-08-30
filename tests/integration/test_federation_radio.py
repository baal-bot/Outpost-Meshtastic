import asyncio
import base64
import json
import os
from datetime import UTC, datetime, timedelta

import pytest

from outpost.app import OutpostApp
from outpost.clock import VirtualClock
from outpost.config import Config
from outpost.fed import MessageType
from outpost.transport.models import InboundMessage


@pytest.mark.asyncio
async def test_radio_hello_creates_pending_peer(tmp_path) -> None:
    config = Config.model_validate(
        {"store": {"path": str(tmp_path / "outpost.db")}, "modules": {"fed": {"enabled": True}}}
    )
    app = OutpostApp(config)
    await app.database.open()
    app.radio._local_id = "!local"
    frame = app.federation_codec.encode(
        MessageType.HELLO,
        {
            "mesh_id": "!remote",
            "name": "Remote Outpost",
            "protocol": 1,
            "capabilities": {"weather": True},
        },
        12,
        None,
    )[0]
    message = InboundMessage(
        packet_id=1,
        from_id="!remote",
        to_id="^all",
        channel=0,
        portnum=config.radio.federation_portnum,
        is_direct=False,
        text=None,
        payload=frame,
        rx_time=datetime.now(UTC),
        via_mqtt=True,
    )

    await app._handle_federation_discovery(message)

    peer = await app.federation.by_mesh_id("!remote")
    assert peer.state == "pending"
    assert peer.discovery_transports == ["mqtt"]
    queued = app.governor.queued_items()
    assert len(queued) == 1
    assert queued[0].dest == "^all"
    assert queued[0].portnum == config.radio.federation_portnum
    response = app.federation_codec.decode_fragment(queued[0].binary_payload, None)
    value = app.federation_reassembler.add("!local", response)
    assert value["target_mesh_id"] == "!remote"
    await app.database.close()


@pytest.mark.asyncio
async def test_direct_hello_does_not_trigger_response_loop(tmp_path) -> None:
    config = Config.model_validate({"store": {"path": str(tmp_path / "outpost.db")}})
    app = OutpostApp(config)
    await app.database.open()
    app.radio._local_id = "!local"
    frame = app.federation_codec.encode(
        MessageType.HELLO,
        {"mesh_id": "!remote", "name": "Remote", "protocol": 1, "capabilities": {}},
        1,
        None,
    )[0]

    await app._handle_federation_discovery(
        InboundMessage(
            1,
            "!remote",
            "!local",
            0,
            config.radio.federation_portnum,
            True,
            None,
            frame,
            datetime.now(UTC),
        )
    )

    assert app.governor.queued_items() == []
    await app.database.close()


@pytest.mark.asyncio
async def test_pairing_bootstrap_uses_targeted_broadcast(tmp_path) -> None:
    config = Config.model_validate(
        {"store": {"path": str(tmp_path / "outpost.db")}, "modules": {"fed": {"enabled": True}}}
    )
    app = OutpostApp(config)
    await app.database.open()
    app.radio._local_id = "!local"
    await app.federation.discover("!remote", "Remote", 1, {}, "radio")

    await app.initiate_federation_pairing("!remote")

    queued = app.governor.queued_items()
    assert len(queued) == 1
    assert queued[0].dest == "^all"
    assert queued[0].want_ack is False
    fragment = app.federation_codec.decode_fragment(queued[0].binary_payload, None)
    value = app.federation_reassembler.add("!local", fragment)
    assert value["target_mesh_id"] == "!remote"
    await app.database.close()


@pytest.mark.asyncio
async def test_pairing_approval_uses_authenticated_targeted_broadcast(tmp_path) -> None:
    config = Config.model_validate(
        {"store": {"path": str(tmp_path / "outpost.db")}, "modules": {"fed": {"enabled": True}}}
    )
    app = OutpostApp(config)
    await app.database.open()
    app.radio._local_id = "!local"
    app.federation.local_mesh_id = "!local"
    await app.federation.discover("!remote", "Remote", 1, {}, "mqtt")
    await app.database.write(
        "UPDATE fed_peer SET state='pairing',shared_secret=? WHERE mesh_id='!remote'",
        (bytes(range(32)),),
    )
    code = app.federation.confirmation_code(bytes(range(32)), "!local", "!remote")

    await app.approve_federation_pairing("!remote", code)

    queued = app.governor.queued_items()
    assert len(queued) == 1
    assert queued[0].dest == "^all"
    assert queued[0].want_ack is False
    fragment = app.federation_codec.decode_fragment(queued[0].binary_payload, bytes(range(32)))
    value = app.federation_reassembler.add("!local", fragment)
    assert value == {"mesh_id": "!local", "target_mesh_id": "!remote", "approved": True}
    await app.database.close()


@pytest.mark.asyncio
async def test_trusted_federation_uses_authenticated_broadcast_carrier(tmp_path) -> None:
    config = Config.model_validate(
        {
            "store": {"path": str(tmp_path / "outpost.db")},
            "airtime": {"dedupe_window_s": 0},
        }
    )
    app = OutpostApp(config)
    await app.database.open()
    app.radio._local_id = "!local"
    app.federation.local_mesh_id = "!local"
    await app.federation.discover("!remote", "Remote", 1, {}, "radio")
    secret = bytes(range(32))
    await app.database.write(
        "UPDATE fed_peer SET state='active',shared_secret=?,local_approved=1,remote_approved=1 "
        "WHERE mesh_id='!remote'",
        (secret,),
    )

    counter = await app._send_federation_value("!remote", MessageType.SYNC_REQ, {"limit": 8})

    queued = app.governor.queued_items()
    assert len(queued) == 1
    assert queued[0].dest == "^all"
    assert queued[0].want_ack is False
    fragment = app.federation_codec.decode_fragment(queued[0].binary_payload, secret)
    value = app.federation_reassembler.add("!local", fragment)
    assert value == {"mesh_id": "!local", "limit": 8}
    reused = await app._send_federation_value(
        "!remote", MessageType.SYNC_REQ, {"limit": 8}, counter=counter
    )
    assert reused == counter
    assert (
        app.federation_codec.decode_fragment(
            app.governor.queued_items()[1].binary_payload, secret
        ).counter
        == counter
    )
    assert (await app.federation.by_mesh_id("!remote")).tx_counter == 1
    await app.database.close()


@pytest.mark.asyncio
async def test_sync_retry_is_single_flight_and_recognizes_legacy_work(tmp_path) -> None:
    config = Config.model_validate(
        {
            "store": {"path": str(tmp_path / "outpost.db")},
            "modules": {"fed": {"enabled": True}},
            "airtime": {"dedupe_window_s": 0},
            "fed": {"sync_retry_minutes": 10},
        }
    )
    app = OutpostApp(config)
    await app.database.open()
    app.radio._local_id = "!local"
    secret = bytes(range(32))
    peer = await app.federation.discover("!remote", "Remote", 1, {}, "radio")
    await app.database.write(
        "UPDATE fed_peer SET state='active',shared_secret=?,boards='[\"gen\"]',"
        "local_approved=1,remote_approved=1 WHERE id=?",
        (secret, peer.id),
    )
    now = int(app.clock.now().timestamp())
    checkpoint = json.dumps({"before": None, "snapshot": now, "pending": True})
    await app.database.write(
        "INSERT INTO fed_cursor(peer_id,stream,direction,cursor,updated_at) "
        "VALUES(?,'_reconcile','recv',?,?)",
        (peer.id, checkpoint, now - 600),
    )

    # Simulate a request admitted by a release that predates durable control-frame keys.
    await app._send_federation_value("!remote", MessageType.SYNC_REQ, {"limit": 8})
    legacy = app.governor.queued_items()[0]
    assert legacy.queue_key is None

    await app._federation_sync_once()
    assert [item.item_id for item in app.governor.queued_items()] == [legacy.item_id]

    assert await app.governor.cancel_work(legacy.item_id)
    await app._federation_sync_once()
    keyed = app.governor.queued_items()[0]
    assert keyed.queue_key == "federation:!remote:sync_req"

    assert await app.governor.cancel_work(keyed.item_id)
    await app._federation_sync_once()
    assert app.governor.queued_items() == []

    await app.database.write(
        "UPDATE fed_cursor SET updated_at=? WHERE peer_id=? AND stream='_reconcile'",
        (now - 600, peer.id),
    )
    await app._federation_sync_once()
    retried = app.governor.queued_items()
    assert len(retried) == 1
    await app.database.write(
        "UPDATE fed_cursor SET updated_at=? WHERE peer_id=? AND stream='_reconcile'",
        (now - 600, peer.id),
    )
    await app._federation_sync_once()
    assert [item.item_id for item in app.governor.queued_items()] == [retried[0].item_id]
    await app.database.close()


@pytest.mark.asyncio
async def test_offline_peer_pauses_sync_until_authenticated_activity_returns(tmp_path) -> None:
    config = Config.model_validate(
        {
            "store": {"path": str(tmp_path / "outpost.db")},
            "modules": {"fed": {"enabled": True}},
            "fed": {"peer_stale_hours": 1},
        }
    )
    app = OutpostApp(config)
    await app.database.open()
    app.radio._local_id = "!local"
    secret = bytes(range(32))
    peer = await app.federation.discover("!remote", "Remote", 1, {}, "radio")
    now = int(app.clock.now().timestamp())
    await app.database.write(
        "UPDATE fed_peer SET state='active',shared_secret=?,boards='[\"gen\"]',"
        "local_approved=1,remote_approved=1,last_seen_at=? WHERE id=?",
        (secret, now - 3_601, peer.id),
    )

    await app._federation_sync_once()
    assert app.governor.queued_items() == []
    assert not app.federation.is_online(await app.federation.by_mesh_id("!remote"))

    frame = app.federation_codec.encode(
        MessageType.SYNC_REQ,
        {
            "mesh_id": "!remote",
            "target_mesh_id": "!local",
            "limit": 8,
            "budget": 20,
            "snapshot": now,
            "before": None,
        },
        1,
        secret,
    )[0]
    await app._handle_federation_discovery(
        InboundMessage(
            79,
            "!remote",
            "^all",
            0,
            config.radio.federation_portnum,
            False,
            None,
            frame,
            datetime.now(UTC),
        )
    )

    recovered = await app.federation.by_mesh_id("!remote")
    assert recovered.state == "active"
    assert app.federation.is_online(recovered)
    assert app.governor.queued_items()[0].queue_key == "federation:!remote:sync_manifest"
    await app.database.close()


@pytest.mark.asyncio
async def test_repeated_sync_requests_enqueue_one_manifest_response(tmp_path) -> None:
    config = Config.model_validate(
        {
            "store": {"path": str(tmp_path / "outpost.db")},
            "airtime": {"dedupe_window_s": 0},
        }
    )
    app = OutpostApp(config)
    await app.database.open()
    app.radio._local_id = "!local"
    secret = bytes(range(32))
    peer = await app.federation.discover("!remote", "Remote", 1, {}, "radio")
    await app.database.write(
        "UPDATE fed_peer SET state='active',shared_secret=?,boards='[\"gen\"]',"
        "local_approved=1,remote_approved=1 WHERE id=?",
        (secret, peer.id),
    )
    request = {
        "mesh_id": "!remote",
        "limit": 8,
        "budget": 20,
        "snapshot": int(app.clock.now().timestamp()),
        "before": None,
    }

    for packet_id, counter in enumerate((1, 2), start=80):
        frame = app.federation_codec.encode(MessageType.SYNC_REQ, request, counter, secret)[0]
        await app._handle_federation_discovery(
            InboundMessage(
                packet_id,
                "!remote",
                "^all",
                0,
                config.radio.federation_portnum,
                False,
                None,
                frame,
                datetime.now(UTC),
            )
        )

    queued = app.governor.queued_items()
    assert len(queued) == 1
    assert {item.queue_key for item in queued} == {"federation:!remote:sync_manifest"}
    assert {
        app.federation_codec.decode_fragment(item.binary_payload, secret).msg_type
        for item in queued
    } == {MessageType.SYNC_MANIFEST}

    results = await asyncio.gather(
        *(
            app._queue_federation_control("!remote", MessageType.SYNC_DONE, {"received": 0})
            for _ in range(4)
        )
    )
    assert results.count(True) == 1
    assert results.count(False) == 3
    assert (
        len([item for item in app.governor.queued_items() if item.queue_key.endswith("sync_done")])
        == 1
    )
    await app.database.close()


@pytest.mark.asyncio
async def test_authenticated_topology_frame_updates_only_its_target_peer(tmp_path) -> None:
    config = Config.model_validate({"store": {"path": str(tmp_path / "outpost.db")}})
    app = OutpostApp(config)
    await app.database.open()
    app.radio._local_id = "!local"
    secret = bytes(range(32))
    await app.federation.discover("!remote", "Remote", 1, {}, "mqtt")
    await app.database.write(
        "UPDATE fed_peer SET state='active',shared_secret=?,local_approved=1,remote_approved=1 "
        "WHERE mesh_id='!remote'",
        (secret,),
    )
    frame = app.federation_codec.encode(
        MessageType.TOPOLOGY_UPDATE,
        {
            "mesh_id": "!remote",
            "target_mesh_id": "!local",
            "topology": {
                "generated_at": int(app.clock.now().timestamp()),
                "location": {"lat": 40.4, "lon": -80.0, "precision_km": 25},
            },
        },
        1,
        secret,
    )[0]

    await app._handle_federation_discovery(
        InboundMessage(
            90,
            "!remote",
            "^all",
            0,
            config.radio.federation_portnum,
            False,
            None,
            frame,
            datetime.now(UTC),
            via_mqtt=True,
        )
    )

    peer = (await app.federation_topology.overview())["items"][0]
    assert peer["location"] == {
        "lat": 40.4,
        "lon": -80.0,
        "precision_km": 25.0,
        "received_at": int(app.clock.now().timestamp()),
    }
    await app.database.close()


@pytest.mark.asyncio
async def test_allowed_remote_thread_root_imports_without_manual_inbox_approval(tmp_path) -> None:
    config = Config.model_validate({"store": {"path": str(tmp_path / "outpost.db")}})
    app = OutpostApp(config)
    await app.database.open()
    app.radio._local_id = "!local"
    secret = bytes(range(32))
    await app.federation.discover("!remote", "Remote", 1, {}, "radio")
    await app.database.write(
        "UPDATE fed_peer SET state='active',shared_secret=?,boards='[\"gen\"]',"
        "local_approved=1,remote_approved=1 WHERE mesh_id='!remote'",
        (secret,),
    )
    await app.database.write("UPDATE board SET federated=1 WHERE slug='gen'")
    item = {
        "stream": "board:gen",
        "uid": "!remote:local:20",
        "digest": "test",
        "payload": {
            "uid": "!remote:local:20",
            "thread_uid": "!remote:local:7",
            "seq": 1,
            "subject": "Remote thread",
            "author_label": "operator@Remote",
            "origin_node": "!remote",
            "body": "First post",
            "created_at": 100,
            "edited_at": None,
        },
    }
    frames = app.federation_codec.encode(
        MessageType.ITEM, {"mesh_id": "!remote", "item": item}, 1, secret
    )

    for packet_id, frame in enumerate(frames, start=2):
        await app._handle_federation_discovery(
            InboundMessage(
                packet_id,
                "!remote",
                "^all",
                0,
                config.radio.federation_portnum,
                False,
                None,
                frame,
                datetime.now(UTC),
            )
        )

    threads = await app.database.read("SELECT uid,subject FROM thread WHERE uid='!remote:local:7'")
    assert dict(threads[0]) == {"uid": "!remote:local:7", "subject": "Remote thread"}
    inbox = await app.database.read("SELECT state FROM fed_inbox_item WHERE uid='!remote:local:20'")
    assert inbox[0]["state"] == "imported"
    await app.database.close()


@pytest.mark.asyncio
async def test_radio_hello_cannot_claim_another_sender(tmp_path) -> None:
    config = Config.model_validate({"store": {"path": str(tmp_path / "outpost.db")}})
    app = OutpostApp(config)
    await app.database.open()
    frame = app.federation_codec.encode(
        MessageType.HELLO,
        {"mesh_id": "!impersonated", "name": "Fake", "protocol": 1},
        13,
        None,
    )[0]
    message = InboundMessage(
        1,
        "!sender",
        "^all",
        0,
        config.radio.federation_portnum,
        False,
        None,
        frame,
        datetime.now(UTC),
    )

    await app._handle_federation_discovery(message)

    assert await app.federation.list() == []
    await app.database.close()


@pytest.mark.asyncio
async def test_rejected_federation_frame_records_safe_reason(tmp_path) -> None:
    config = Config.model_validate({"store": {"path": str(tmp_path / "outpost.db")}})
    app = OutpostApp(config)
    await app.database.open()
    message = InboundMessage(
        42,
        "!remote",
        "!local",
        0,
        config.radio.federation_portnum,
        True,
        None,
        bytes((0x4F, 1, int(MessageType.SERVICE_RESPONSE))) + bytes(20),
        datetime.now(UTC),
    )
    await app.message_log.record_inbound(message)

    await app._handle_federation_discovery(message)

    row = (await app.database.read("SELECT outcome,drop_reason FROM message_log"))[0]
    assert row["outcome"] == "rejected"
    assert row["drop_reason"] == "unauthenticated peer"
    await app.database.close()


@pytest.mark.asyncio
async def test_pairing_frame_rejects_integer_key_material_without_raising(tmp_path) -> None:
    config = Config.model_validate({"store": {"path": str(tmp_path / "outpost.db")}})
    app = OutpostApp(config)
    await app.database.open()
    app.radio._local_id = "!local"
    frame = app.federation_codec.encode(
        MessageType.PAIR_REQ,
        {
            "mesh_id": "!remote",
            "target_mesh_id": "!local",
            "public_key": 2**63,
            "nonce": 2**63,
        },
        0,
        None,
    )[0]
    message = InboundMessage(
        43,
        "!remote",
        "^all",
        0,
        config.radio.federation_portnum,
        False,
        None,
        frame,
        datetime.now(UTC),
    )
    await app.message_log.record_inbound(message)

    await app._handle_federation_discovery(message)

    row = (await app.database.read("SELECT outcome,drop_reason FROM message_log"))[0]
    assert tuple(row) == ("rejected", "invalid federation frame")
    assert await app.federation.list() == []
    await app.database.close()


@pytest.mark.asyncio
async def test_sync_frame_rejects_non_integer_bounds_without_raising(tmp_path) -> None:
    config = Config.model_validate({"store": {"path": str(tmp_path / "outpost.db")}})
    app = OutpostApp(config)
    await app.database.open()
    app.radio._local_id = "!local"
    secret = bytes(range(32))
    await app.federation.discover("!remote", "Remote", 1, {}, "radio")
    await app.database.write(
        "UPDATE fed_peer SET state='active',shared_secret=?,local_approved=1,remote_approved=1 "
        "WHERE mesh_id='!remote'",
        (secret,),
    )
    frame = app.federation_codec.encode(
        MessageType.SYNC_REQ,
        {"mesh_id": "!remote", "target_mesh_id": "!local", "limit": float("inf")},
        1,
        secret,
    )[0]
    message = InboundMessage(
        44,
        "!remote",
        "^all",
        0,
        config.radio.federation_portnum,
        False,
        None,
        frame,
        datetime.now(UTC),
    )
    await app.message_log.record_inbound(message)

    await app._handle_federation_discovery(message)

    row = (await app.database.read("SELECT outcome,drop_reason FROM message_log"))[0]
    assert tuple(row) == ("rejected", "invalid federation frame")
    assert app.governor.queued_items() == []
    await app.database.close()


@pytest.mark.asyncio
async def test_tampered_replayed_and_clock_skewed_frames_fail_without_side_effects(
    tmp_path,
) -> None:
    config = Config.model_validate({"store": {"path": str(tmp_path / "outpost.db")}})
    app = OutpostApp(config)
    await app.database.open()
    app.radio._local_id = "!local"
    secret = bytes(range(32))
    await app.federation.discover("!remote", "Remote", 1, {}, "radio")
    await app.database.write(
        "UPDATE fed_peer SET state='active',shared_secret=?,local_approved=1,remote_approved=1 "
        "WHERE mesh_id='!remote'",
        (secret,),
    )

    async def receive(packet_id: int, frame: bytes) -> None:
        message = InboundMessage(
            packet_id,
            "!remote",
            "^all",
            0,
            config.radio.federation_portnum,
            False,
            None,
            frame,
            datetime.now(UTC),
        )
        await app.message_log.record_inbound(message)
        await app._handle_federation_discovery(message)

    tampered = bytearray(
        app.federation_codec.encode(
            MessageType.SYNC_DONE, {"mesh_id": "!remote", "sent": 1}, 1, secret
        )[0]
    )
    tampered[-1] ^= 1
    await receive(50, bytes(tampered))

    valid = app.federation_codec.encode(
        MessageType.SYNC_DONE, {"mesh_id": "!remote", "sent": 1}, 1, secret
    )[0]
    await receive(51, valid)
    await receive(52, valid)

    # Simulate the receiving Outpost clock running ten minutes ahead. A peer
    # request with a nominal three-minute lifetime must fail closed instead of
    # being executed after the receiver believes it expired.
    peer_now = datetime.now(UTC)
    app.clock = VirtualClock(epoch=peer_now + timedelta(minutes=10))
    expired = app.federation_codec.encode(
        MessageType.SERVICE_QUERY,
        {
            "mesh_id": "!remote",
            "request_id": "expired-test",
            "service": "weather",
            "args": {},
            "expires_at": int(peer_now.timestamp()) + 180,
            "ttl": 180,
        },
        2,
        secret,
    )[0]
    await receive(53, expired)

    rows = await app.database.read(
        "SELECT packet_id,outcome,drop_reason FROM message_log ORDER BY packet_id"
    )
    assert [dict(row) for row in rows] == [
        {"packet_id": 50, "outcome": "rejected", "drop_reason": "authentication failed"},
        {"packet_id": 51, "outcome": "received", "drop_reason": None},
        {"packet_id": 52, "outcome": "rejected", "drop_reason": "replay detected"},
        {"packet_id": 53, "outcome": "rejected", "drop_reason": "expired message"},
    ]
    assert await app.database.read("SELECT 1 FROM fed_service_request") == []
    assert await app.database.read("SELECT 1 FROM fed_inbox_item") == []
    assert await app.database.read("SELECT 1 FROM fed_mail_delivery") == []
    await app.database.close()


@pytest.mark.asyncio
async def test_multipart_item_completes_across_retries_and_replays_only_receipt(tmp_path) -> None:
    config = Config.model_validate({"store": {"path": str(tmp_path / "outpost.db")}})
    app = OutpostApp(config)
    await app.database.open()
    app.radio._local_id = "!local"
    secret = bytes(range(32))
    await app.federation.discover("!remote", "Remote", 1, {}, "radio")
    await app.database.write(
        "UPDATE fed_peer SET state='active',shared_secret=?,boards='[\"gen\"]',"
        "local_approved=1,remote_approved=1 WHERE mesh_id='!remote'",
        (secret,),
    )
    await app.database.write("UPDATE board SET federated=1 WHERE slug='gen'")
    body = base64.b64encode(os.urandom(450)).decode()
    item = {
        "stream": "board:gen",
        "uid": "!remote:local:multipart-post",
        "digest": "multipart",
        "payload": {
            "uid": "!remote:local:multipart-post",
            "thread_uid": "!remote:local:multipart-thread",
            "seq": 1,
            "subject": "Multipart recovery",
            "author_label": "operator@Remote",
            "origin_node": "!remote",
            "body": body,
            "created_at": 100,
            "edited_at": None,
        },
    }
    frames = app.federation_codec.encode(
        MessageType.ITEM, {"mesh_id": "!remote", "item": item}, 1, secret
    )
    assert len(frames) > 1

    async def receive(packet_id: int, frame: bytes) -> None:
        message = InboundMessage(
            packet_id,
            "!remote",
            "^all",
            0,
            config.radio.federation_portnum,
            False,
            None,
            frame,
            datetime.now(UTC),
        )
        await app.message_log.record_inbound(message)
        await app._handle_federation_discovery(message)

    await receive(70, frames[0])
    assert await app.database.read("SELECT 1 FROM fed_inbox_item") == []
    for packet_id, frame in enumerate(frames[1:], start=71):
        await receive(packet_id, frame)
    inbox = await app.database.read("SELECT state FROM fed_inbox_item")
    assert inbox[0]["state"] == "imported"
    receipt_frames = len(app.governor.queued_items())
    assert receipt_frames > 0

    for packet_id, frame in enumerate(frames, start=80):
        await receive(packet_id, frame)
    assert len(await app.database.read("SELECT 1 FROM fed_inbox_item")) == 1
    assert len(app.governor.queued_items()) > receipt_frames
    assert (await app.federation.by_mesh_id("!remote")).rx_counter == 1
    await app.database.close()
