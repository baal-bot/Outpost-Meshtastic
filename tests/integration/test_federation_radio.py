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
    assert queued[0].dest == "!remote"
    assert queued[0].portnum == config.radio.federation_portnum
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
