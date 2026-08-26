import asyncio
from types import SimpleNamespace

import pytest

from outpost.clock import VirtualClock
from outpost.config import RadioConfig
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
async def test_mqtt_configuration_is_written_to_radio_and_forces_encryption() -> None:
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
    assert mqtt.proxy_to_client_enabled is False
    assert settings.uplink_enabled and settings.downlink_enabled
    assert writes == ["mqtt", 0]


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
