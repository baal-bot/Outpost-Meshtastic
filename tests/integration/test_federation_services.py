from datetime import UTC, datetime

import pytest

from outpost.app import OutpostApp
from outpost.config import Config
from outpost.fed import FrameCodec, MessageType
from outpost.transport.models import InboundMessage


@pytest.mark.asyncio
async def test_service_request_selects_capable_peer_and_records_response(tmp_path) -> None:
    config = Config.model_validate({"store": {"path": str(tmp_path / "outpost.db")}})
    app = OutpostApp(config)
    await app.database.open()
    app.federation.local_mesh_id = "!local"
    await app.federation.discover("!remote", "Remote", 1, {"weather": True}, "radio")
    secret = bytes(range(32))
    await app.database.write(
        "UPDATE fed_peer SET state='active',shared_secret=?,local_approved=1,remote_approved=1 "
        "WHERE mesh_id='!remote'",
        (secret,),
    )

    request = await app.request_federation_service("weather", {"lat": 40.4, "lon": -80.0})

    assert request["status"] == "pending"
    assert request["peer_mesh_id"] == "!remote"
    assert app.governor.queue_depths()["federation"] > 0
    request_id = str(request["request_id"])
    frames = app.federation_codec.encode(
        MessageType.SERVICE_RESPONSE,
        {
            "request_id": request_id,
            "mesh_id": "!remote",
            "ok": True,
            "result": {"temperature_c": 21.0},
            "provenance": {
                "provider": "nws",
                "fetched_at": 123,
                "cache_age_seconds": 4,
                "serving_outpost": "!remote",
            },
            "error": None,
        },
        1,
        secret,
    )
    for index, frame in enumerate(frames):
        await app._handle_federation_discovery(
            InboundMessage(
                packet_id=index + 1,
                from_id="!remote",
                to_id="!local",
                channel=0,
                portnum=config.radio.federation_portnum,
                is_direct=True,
                text=None,
                payload=frame,
                rx_time=datetime.now(UTC),
            )
        )

    completed = (await app.federation_service_requests())[0]
    assert completed["status"] == "complete"
    assert completed["result"] == {"temperature_c": 21.0}
    assert completed["provenance"]["serving_outpost"] == "!remote"
    await app.database.close()


@pytest.mark.asyncio
async def test_service_request_requires_active_capable_peer(tmp_path) -> None:
    config = Config.model_validate({"store": {"path": str(tmp_path / "outpost.db")}})
    app = OutpostApp(config)
    await app.database.open()

    with pytest.raises(ValueError, match="no active peer"):
        await app.request_federation_service("alerts", {})

    await app.database.close()


def test_weather_service_wire_result_stays_below_reliable_radio_size() -> None:
    result = {
        "temperature_c": 25.555555555555557,
        "precipitation_mm": 0.0,
        "wind_kph": 11.265408,
        "wind_direction": 270,
        "weather_code": 0,
    }
    wire = OutpostApp._service_result_to_wire("weather", result)
    codec = FrameCodec()
    frames = codec.encode(
        MessageType.SERVICE_RESPONSE,
        {
            "request_id": "03ba13dafebbe599be8017b5",
            "mesh_id": "!699c2f30",
            "ok": True,
            "result": wire,
            "provenance": {
                "provider": "nws",
                "fetched_at": 1787680919,
                "serving_outpost": "!699c2f30",
            },
            "error": None,
        },
        1,
        bytes(range(32)),
    )

    assert len(frames) == 1
    assert len(frames[0]) < 200
    assert OutpostApp._service_result_from_wire("weather", wire) == result
