import asyncio
import base64
from types import SimpleNamespace

import pytest
from meshtastic.protobuf import channel_pb2, config_pb2, module_config_pb2

from outpost.channel_profile import OUTPOST_CHANNEL_PROFILE
from outpost.clock import VirtualClock
from outpost.config import RadioConfig
from outpost.transport.models import LinkState
from outpost.transport.radio_link import MeshtasticRadioLink


def test_named_and_numeric_portnums_are_normalised() -> None:
    link = MeshtasticRadioLink(RadioConfig(), VirtualClock())
    assert link._portnum("TEXT_MESSAGE_APP") == 1
    assert link._portnum("TELEMETRY_APP") == 67
    assert link._portnum(260) == 260
    assert link._portnum(None) == 0


@pytest.mark.asyncio
async def test_callback_hands_message_to_event_loop_without_thread_clock_access() -> None:
    clock = VirtualClock()
    link = MeshtasticRadioLink(RadioConfig(), clock)
    link._loop = asyncio.get_running_loop()
    link._local_id = "!699c2f30"
    link._on_receive(
        {
            "id": 42,
            "fromId": "!12345678",
            "toId": "!699c2f30",
            "channel": 0,
            "viaMqtt": True,
            "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "PING"},
        }
    )
    await asyncio.sleep(0)
    message = await anext(link.inbound())
    assert message.text == "PING"
    assert message.is_direct is True
    assert message.via_mqtt is True


@pytest.mark.asyncio
async def test_callback_preserves_firmware_authenticated_pki_public_key() -> None:
    clock = VirtualClock()
    link = MeshtasticRadioLink(RadioConfig(), clock)
    link._loop = asyncio.get_running_loop()
    link._local_id = "!699c2f30"
    public_key = bytes(range(32))
    link._on_receive(
        {
            "id": 43,
            "fromId": "!12345678",
            "toId": "!699c2f30",
            "pkiEncrypted": True,
            "publicKey": base64.b64encode(public_key).decode(),
            "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "ROSTER?"},
        }
    )
    await asyncio.sleep(0)
    message = await anext(link.inbound())
    assert message.pki_encrypted is True
    assert message.pki_public_key == public_key

    link._on_receive(
        {
            "id": 44,
            "fromId": "!12345678",
            "toId": "!699c2f30",
            "pkiEncrypted": False,
            "publicKey": base64.b64encode(bytes(reversed(public_key))).decode(),
            "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "ROSTER?"},
        }
    )
    await asyncio.sleep(0)
    unverified = await anext(link.inbound())
    assert unverified.pki_public_key is None


@pytest.mark.asyncio
async def test_limited_mqtt_configuration_preserves_advanced_values() -> None:
    writes: list[object] = []
    mqtt = SimpleNamespace(
        enabled=False,
        address="",
        tls_enabled=False,
        encryption_enabled=False,
        root="",
        proxy_to_client_enabled=True,
    )
    settings = SimpleNamespace(name="LongFast", uplink_enabled=False, downlink_enabled=False)
    local = SimpleNamespace(
        moduleConfig=SimpleNamespace(mqtt=mqtt),
        channels=[SimpleNamespace(role=1, settings=settings)],
        writeConfig=lambda name: writes.append(name),
        writeChannel=lambda index: writes.append(index),
    )
    link = MeshtasticRadioLink(RadioConfig(), VirtualClock())
    link._interface = SimpleNamespace(localNode=local)

    status = await link.configure_mqtt(
        enabled=True,
        address="mqtt.meshtastic.org",
        tls_enabled=True,
        root="msh",
        channel=0,
        uplink_enabled=True,
        downlink_enabled=True,
    )

    assert status["enabled"] is True
    assert mqtt.encryption_enabled is True
    assert mqtt.proxy_to_client_enabled is True
    assert settings.uplink_enabled and settings.downlink_enabled
    assert writes == ["mqtt", 0]


@pytest.mark.asyncio
async def test_radio_configuration_is_guarded_and_never_reads_back_secrets() -> None:
    writes: list[object] = []
    local_config = config_pb2.Config()
    module_config = module_config_pb2.ModuleConfig()
    link = MeshtasticRadioLink(RadioConfig(), VirtualClock())

    link._set_enum(local_config.device, "role", "CLIENT")
    link._set_enum(local_config.device, "rebroadcast_mode", "ALL")
    local_config.device.node_info_broadcast_secs = 10_800
    link._set_enum(local_config.lora, "region", "US")
    link._set_enum(local_config.lora, "modem_preset", "LONG_FAST")
    local_config.lora.hop_limit = 3
    link._set_enum(local_config.position, "gps_mode", "NOT_PRESENT")
    module_config.mqtt.username = "operator"
    module_config.mqtt.password = "".join(("do", "-not-return"))

    channels = []
    for role, name in (("PRIMARY", "LongFast"), ("SECONDARY", "Outpost")):
        channel = channel_pb2.Channel()
        link._set_enum(channel, "role", role)
        channel.settings.name = name
        channel.settings.psk = b"secret-key-material-that-stays-radio"
        channels.append(channel)
    local = SimpleNamespace(
        localConfig=local_config,
        moduleConfig=module_config,
        channels=channels,
        setOwner=lambda *_: None,
        writeConfig=lambda name: writes.append(name),
        writeChannel=lambda index: writes.append(index),
    )
    link._interface = SimpleNamespace(
        localNode=local,
        getMyNodeInfo=lambda: {
            "user": {"longName": "Outpost", "shortName": "OUT"},
            "position": {"latitude": 40.44, "longitude": -79.99, "altitude": 366},
        },
    )
    link._state = LinkState.UP

    status = await link.configuration_status()

    assert status["mqtt"]["username_configured"] is True
    assert status["mqtt"]["password_configured"] is True
    assert status["lora"]["frequency_slot"] == 0
    assert status["position"]["altitude"] == 366
    assert "do-not-return" not in str(status)
    assert "secret-key-material" not in str(status)

    await link.configure(
        "lora",
        {
            "region": "US",
            "modem_preset": "LONG_FAST",
            "frequency_slot": 20,
            "hop_limit": 3,
            "tx_power": 0,
            "tx_enabled": True,
        },
    )
    assert local_config.lora.channel_num == 20
    assert writes == ["lora"]

    with pytest.raises(ValueError, match="CLIENT or CLIENT_BASE"):
        await link.configure(
            "device",
            {
                "role": "ROUTER",
                "rebroadcast_mode": "ALL",
                "node_info_broadcast_secs": 10_800,
            },
        )

    result = await link.configure(
        "channel",
        {
            "index": 1,
            "role": "SECONDARY",
            "name": "Rescue",
            "psk": None,
            "generate_psk": True,
            "uplink_enabled": True,
            "downlink_enabled": True,
            "position_precision": 16,
            "muted": False,
        },
    )
    assert len(base64.b64decode(result["generated_psk"])) == 32
    assert channels[1].settings.name == "Rescue"
    assert writes == ["lora", 1]
    assert "generated_psk" not in await link.configuration_status()
    assert (
        await link.verify_configuration_secrets(
            "channel", {"index": 1, "psk": None}, result["generated_psk"]
        )
        == []
    )
    channels[1].settings.psk = b"firmware-rejected-the-key"
    assert await link.verify_configuration_secrets(
        "channel", {"index": 1, "psk": None}, result["generated_psk"]
    ) == ["psk"]
    assert await link.verify_configuration_secrets(
        "mqtt", {"username": "operator", "password": "wrong"}
    ) == ["password"]

    link._on_lost()
    stale = await link.configuration_status()
    assert stale["available"] is False
    assert stale["stale"] is True
    assert stale["verified_at"] is not None
    assert [channel["name"] for channel in stale["channels"]] == ["LongFast", "Rescue"]
    assert "secret-key-material" not in str(stale)
    assert "last verified configuration" in stale["warnings"][-1]


@pytest.mark.asyncio
async def test_outpost_profile_only_fills_selected_disabled_slots() -> None:
    writes: list[int] = []
    link = MeshtasticRadioLink(RadioConfig(), VirtualClock())
    channels = []
    for index in range(8):
        channel = channel_pb2.Channel()
        link._set_enum(channel, "role", "PRIMARY" if index == 0 else "DISABLED")
        channel.settings.name = "Neighborhood" if index == 0 else ""
        channel.settings.psk = b"existing-user-key" if index == 0 else b""
        channels.append(channel)
    local = SimpleNamespace(
        channels=channels,
        writeChannel=lambda index: writes.append(index),
    )
    link._interface = SimpleNamespace(localNode=local)

    result = await link.configure(
        "outpost_profile",
        {"bindings": {"public": 1, "outpost": 2, "watch": 3}},
    )

    assert result["added_channel_indices"] == [1, 2, 3]
    assert writes == [1, 2, 3]
    assert channels[0].settings.name == "Neighborhood"
    assert channels[0].settings.psk == b"existing-user-key"
    for name, index in {"public": 1, "outpost": 2, "watch": 3}.items():
        assert channels[index].settings.name == name
        assert channels[index].settings.psk == OUTPOST_CHANNEL_PROFILE[name].psk

    with pytest.raises(ValueError, match="will not be overwritten"):
        await link.configure(
            "outpost_profile",
            {"bindings": {"public": 0, "outpost": 2, "watch": 3}},
        )


@pytest.mark.asyncio
async def test_routing_ack_fields_are_normalised() -> None:
    link = MeshtasticRadioLink(RadioConfig(), VirtualClock())
    link._loop = asyncio.get_running_loop()
    link._on_receive(
        {
            "id": 43,
            "fromId": "!12345678",
            "decoded": {
                "portnum": "ROUTING_APP",
                "requestId": 99,
                "routing": {"errorReason": "NONE"},
            },
        }
    )
    await asyncio.sleep(0)
    message = await anext(link.inbound())
    assert message.request_id == 99
    assert message.routing_error == "NONE"


@pytest.mark.asyncio
async def test_position_fields_are_normalised() -> None:
    link = MeshtasticRadioLink(RadioConfig(), VirtualClock())
    link._loop = asyncio.get_running_loop()
    link._local_id = "!699c2f30"
    link._on_receive(
        {
            "id": 44,
            "fromId": "!12345678",
            "toId": "!699c2f30",
            "decoded": {
                "portnum": "POSITION_APP",
                "position": {"latitudeI": 404406000, "longitudeI": -799959000},
            },
        }
    )
    await asyncio.sleep(0)
    message = await anext(link.inbound())
    assert message.latitude == pytest.approx(40.4406)
    assert message.longitude == pytest.approx(-79.9959)
    assert message.is_direct is True


@pytest.mark.asyncio
async def test_full_callback_queue_counts_drop_and_keeps_newest_frame() -> None:
    link = MeshtasticRadioLink(RadioConfig(), VirtualClock(), queue_size=1)
    link._loop = asyncio.get_running_loop()
    for packet_id in (50, 51):
        link._on_receive(
            {
                "id": packet_id,
                "fromId": "!12345678",
                "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": str(packet_id)},
            }
        )
    await asyncio.sleep(0)

    assert link.inbound_status() == {
        "depth": 1,
        "capacity": 1,
        "received": 2,
        "dropped": 1,
        "last_drop_at": 1_767_225_600,
    }
    message = await anext(link.inbound())
    assert message.packet_id == 51
    assert link.inbound_status()["depth"] == 0
