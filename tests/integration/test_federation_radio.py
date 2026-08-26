from datetime import UTC, datetime

import pytest

from outpost.app import OutpostApp
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
    config = Config.model_validate({"store": {"path": str(tmp_path / "outpost.db")}})
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
    config = Config.model_validate({"store": {"path": str(tmp_path / "outpost.db")}})
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

    await app._send_federation_value("!remote", MessageType.SYNC_REQ, {"limit": 8})

    queued = app.governor.queued_items()
    assert len(queued) == 1
    assert queued[0].dest == "^all"
    assert queued[0].want_ack is False
    fragment = app.federation_codec.decode_fragment(queued[0].binary_payload, secret)
    value = app.federation_reassembler.add("!local", fragment)
    assert value == {"mesh_id": "!local", "limit": 8}
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
    assert row["drop_reason"] == "active federation secret unavailable"
    await app.database.close()
