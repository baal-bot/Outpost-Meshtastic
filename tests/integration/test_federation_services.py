from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import pytest

from outpost.app import OutpostApp
from outpost.config import Config
from outpost.fed import FrameCodec, MessageType
from outpost.transport.models import InboundMessage


def public_alert(area: str) -> dict:
    return {
        "properties": {
            "status": "Actual",
            "event": "Flood Warning",
            "headline": f"Flood Warning for {area}",
            "severity": "Severe",
            "areaDesc": area,
            "expires": "2030-01-01T01:00:00Z",
        }
    }


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


@pytest.mark.parametrize(
    ("lat", "lon", "area"),
    ((40.4406, -79.9959, "Allegheny County"), (39.7392, -104.9903, "Denver County")),
)
@pytest.mark.asyncio
async def test_serving_outpost_queries_alerts_for_requesting_nodes_exact_point(
    tmp_path, monkeypatch, lat: float, lon: float, area: str
) -> None:
    config = Config.model_validate(
        {
            "store": {"path": str(tmp_path / "outpost.db")},
            "node": {"location": {"lat": 40.4406, "lon": -79.9959}},
        }
    )
    app = OutpostApp(config)
    await app.database.open()
    app.radio._local_id = "!serving"
    app.federation.local_mesh_id = "!serving"
    secret = bytes(range(32))
    await app.federation.discover("!requester", "Requester", 1, {"alerts": True}, "radio")
    await app.database.write(
        "UPDATE fed_peer SET state='active',shared_secret=?,local_approved=1,remote_approved=1 "
        "WHERE mesh_id='!requester'",
        (secret,),
    )
    requested_point = f"{lat:.4f},{lon:.4f}"
    calls: list[str] = []

    async def request(url, *args, **kwargs):
        calls.append(url)
        if "/points/" in url:
            return {"properties": {"forecastZone": "https://api.weather.gov/zones/PAZ021"}}
        return {"updated": "2026-08-26T12:00:00Z", "features": [public_alert(area)]}

    monkeypatch.setattr("outpost.env.cap._request_json", request)
    frames = app.federation_codec.encode(
        MessageType.SERVICE_QUERY,
        {
            "request_id": "alert-point-test",
            "mesh_id": "!requester",
            "service": "alerts",
            "args": {"lat": lat, "lon": lon},
            "expires_at": int(app.clock.now().timestamp()) + 180,
            "ttl": 1,
        },
        1,
        secret,
    )
    for index, frame in enumerate(frames):
        await app._handle_federation_discovery(
            InboundMessage(
                packet_id=index + 1,
                from_id="!requester",
                to_id="!serving",
                channel=0,
                portnum=config.radio.federation_portnum,
                is_direct=True,
                text=None,
                payload=frame,
                rx_time=datetime.now(UTC),
            )
        )

    served = next(
        value for value in await app.federation_service_requests() if value["direction"] == "in"
    )
    assert served["status"] == "complete"
    assert served["result"]["status"] == "ok"
    assert served["result"]["items"][0]["area_desc"] == area
    assert served["provenance"]["query_lat"] == lat
    assert served["provenance"]["query_lon"] == lon
    assert served["provenance"]["provider_timestamp"] == "2026-08-26T12:00:00Z"
    assert served["provenance"]["serving_outpost"] == "!serving"
    assert requested_point in calls[0]
    assert parse_qs(urlparse(calls[1]).query)["point"] == [requested_point]
    await app.database.close()
