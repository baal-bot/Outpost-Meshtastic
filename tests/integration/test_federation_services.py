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


@pytest.mark.asyncio
async def test_inbound_peer_services_default_to_denied_and_account_usage(tmp_path) -> None:
    config = Config.model_validate({"store": {"path": str(tmp_path / "outpost.db")}})
    app = OutpostApp(config)
    await app.database.open()
    app.radio._local_id = "!serving"
    app.federation.local_mesh_id = "!serving"
    secret = bytes(range(32))
    await app.federation.discover("!requester", "Requester", 1, {"weather": True}, "radio")
    await app.database.write(
        "UPDATE fed_peer SET state='active',shared_secret=?,local_approved=1,remote_approved=1 "
        "WHERE mesh_id='!requester'",
        (secret,),
    )
    frames = app.federation_codec.encode(
        MessageType.SERVICE_QUERY,
        {
            "request_id": "denied-weather",
            "mesh_id": "!requester",
            "service": "weather",
            "args": {"lat": 40.4406, "lon": -79.9959},
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

    denied = (
        await app.database.read(
            "SELECT status,error,response_count FROM fed_service_request "
            "WHERE request_id='denied-weather'"
        )
    )[0]
    assert denied["status"] == "failed"
    assert denied["error"] == "peer service is not permitted by operator policy"
    assert denied["response_count"] == 1
    usage = (await app.database.read("SELECT * FROM fed_service_usage"))[0]
    assert usage["requests"] == 0
    assert usage["denied"] == 1
    assert usage["response_airtime_seconds"] > 0
    await app.database.close()


@pytest.mark.asyncio
async def test_peer_service_request_and_concurrency_quotas_cannot_be_bypassed_by_ids(
    tmp_path,
) -> None:
    config = Config.model_validate({"store": {"path": str(tmp_path / "outpost.db")}})
    app = OutpostApp(config)
    await app.database.open()
    await app.federation.discover("!requester", "Requester", 1, {}, "radio")
    await app.database.write("UPDATE fed_peer SET state='active' WHERE mesh_id='!requester'")
    await app.federation.update_sync_policy(
        "!requester",
        boards=[],
        sync_incidents=False,
        relay_alerts=False,
        quota_items_per_hour=20,
        service_permissions=["weather"],
        quota_services_per_hour=2,
        service_concurrency=1,
    )
    now = int(app.clock.now().timestamp())
    args = '{"lat":40.4406,"lon":-79.9959}'

    _, first, _ = await app.federation.admit_service_request(
        "!requester", "request-1", "weather", args, "same", now, now + 180
    )
    _, concurrent, _ = await app.federation.admit_service_request(
        "!requester", "request-2", "weather", args, "same", now, now + 180
    )
    await app.database.write(
        "UPDATE fed_service_request SET status='complete',completed_at=? "
        "WHERE request_id='request-1'",
        (now,),
    )
    _, second, _ = await app.federation.admit_service_request(
        "!requester", "request-3", "weather", args, "same", now, now + 180
    )
    await app.database.write(
        "UPDATE fed_service_request SET status='complete',completed_at=? "
        "WHERE request_id='request-3'",
        (now,),
    )
    _, throttled, _ = await app.federation.admit_service_request(
        "!requester", "request-4", "weather", args, "same", now, now + 180
    )

    assert (first, concurrent, second, throttled) == (
        "admitted",
        "concurrency_quota",
        "admitted",
        "request_quota",
    )
    usage = (await app.database.read("SELECT * FROM fed_service_usage"))[0]
    assert usage["requests"] == 2
    assert usage["denied"] == 2
    peer = await app.federation.by_mesh_id("!requester")
    assert await app.federation.reserve_service_response(peer, 1201, 1.0, now) == (
        "response_byte_quota"
    )
    assert await app.federation.reserve_service_response(peer, 100, 16.0, now) == ("airtime_quota")
    for _ in range(3):
        await app.federation.record_service_provider_outcome(peer, "weather", True, now)
    _, circuit, _ = await app.federation.admit_service_request(
        "!requester", "request-5", "weather", args, "same", now, now + 180
    )
    assert circuit == "circuit_open"
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
        "UPDATE fed_peer SET state='active',shared_secret=?,local_approved=1,remote_approved=1,"
        "service_permissions='[\"alerts\"]' WHERE mesh_id='!requester'",
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
    retry_frames = app.federation_codec.encode(
        MessageType.SERVICE_QUERY,
        {
            "request_id": "alert-point-test",
            "mesh_id": "!requester",
            "service": "alerts",
            "args": {"lat": lat, "lon": lon},
            "expires_at": int(app.clock.now().timestamp()) + 180,
            "ttl": 1,
        },
        2,
        secret,
    )
    for index, frame in enumerate(retry_frames):
        await app._handle_federation_discovery(
            InboundMessage(
                packet_id=100 + index,
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
    delivery = (
        await app.database.read(
            "SELECT response_count FROM fed_service_request WHERE request_id='alert-point-test'"
        )
    )[0]
    assert delivery["response_count"] == 2
    assert len(calls) == 2
    await app.database.close()
