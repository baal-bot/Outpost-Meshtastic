import pytest
from fastapi.testclient import TestClient

from outpost.clock import VirtualClock
from outpost.config import AirtimeConfig, Config, EnvConfig, SameConfig
from outpost.env import CapAlertService, SameService
from outpost.store import Database
from outpost.transport.governor import AirtimeGovernor
from outpost.transport.simulated import SimulatedRadioLink
from outpost.watch import AlertService
from outpost.web.api import create_web_app
from outpost.web.settings import RuntimeSettings

LIVE_HEADER = "ZCZC-WXR-TOR-042003+0130-0010000-KPBZ/NWS-"


def cap_feature(identifier: str = "cap-tor") -> dict:
    return {
        "id": identifier,
        "properties": {
            "id": identifier,
            "sender": "w-nws.webmaster@noaa.gov",
            "sent": "2026-01-01T00:00:00Z",
            "messageType": "Alert",
            "status": "Actual",
            "event": "Tornado Warning",
            "headline": "Tornado Warning issued for Allegheny County",
            "description": "Take shelter now.",
            "areaDesc": "Allegheny County",
            "severity": "Extreme",
            "urgency": "Immediate",
            "certainty": "Observed",
            "effective": "2026-01-01T00:00:00Z",
            "expires": "2026-01-01T01:30:00Z",
            "geocode": {"SAME": ["042003"]},
            "eventCode": {"SAME": ["TOR"]},
        },
    }


def alert_service(database: Database, clock: VirtualClock) -> tuple[AlertService, AirtimeGovernor]:
    config = Config.model_validate({"channels": {0: {"name": "public"}, 3: {"name": "watch"}}})
    governor = AirtimeGovernor(SimulatedRadioLink(), AirtimeConfig(), clock)
    return AlertService(database, governor, clock, config), governor


@pytest.mark.asyncio
async def test_same_test_message_is_relevant_logged_once_and_never_broadcastable(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    service = SameService(database, VirtualClock(), SameConfig(county_codes=["042003"]))
    header = "ZCZC-WXR-RWT-042003+0030-2361200-KPBZ/NWS-"
    message, created = await service.ingest(header)
    duplicate, created_again = await service.ingest(header)
    assert created and not created_again
    assert message.is_test and message.relevant
    assert duplicate.header == message.header
    assert not (message.relevant and not message.is_test and created)
    rows = await database.read("SELECT * FROM same_event")
    assert len(rows) == 1
    assert rows[0]["is_test"] == 1
    assert rows[0]["decision"] == "log_only"
    assert rows[0]["review_state"] == "logged"
    alerts, governor = alert_service(database, VirtualClock())
    with pytest.raises(ValueError, match="not eligible"):
        await service.approve(rows[0]["id"], alerts)
    assert governor.queued_items() == []
    await database.close()


@pytest.mark.asyncio
async def test_same_live_warning_filters_county_and_exposes_silence_health(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock = VirtualClock()
    service = SameService(
        database,
        clock,
        SameConfig(enabled=True, county_codes=["042003"], silence_alarm_minutes=10),
    )
    assert service.health()["status"] == "monitoring"
    service.start_monitoring()
    clock.advance(601)
    assert service.health()["status"] == "no_signal"
    unrelated, _ = await service.ingest("ZCZC-WXR-TOR-039001+0015-2361200-KCLE/NWS-")
    assert not unrelated.relevant and not unrelated.is_test
    unrelated_row = (await service.list(include_expired=True))[0]
    assert unrelated_row["decision"] == "withheld"
    assert unrelated_row["review_state"] == "logged"
    relevant, _ = await service.ingest(LIVE_HEADER)
    assert relevant.relevant and not relevant.is_test
    assert service.health()["status"] == "up"
    clock.advance(601)
    assert service.health()["status"] == "no_signal"
    await database.close()


@pytest.mark.asyncio
async def test_same_live_warning_requires_approval_before_alert_delivery(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock = VirtualClock()
    service = SameService(database, clock, SameConfig(county_codes=["042003"]))
    alerts, governor = alert_service(database, clock)

    await service.ingest(LIVE_HEADER)
    item = (await service.list())[0]
    assert item["decision"] == "accepted"
    assert item["review_state"] == "pending"
    assert governor.queued_items() == []

    approved = await service.approve(item["id"], alerts)
    assert approved["source"] == "same"
    assert approved["expires_at"] == 1767231000
    assert approved["expires_at"] > int(clock.now().timestamp())
    assert {item.channel for item in governor.queued_items()} == {0, 3}
    assert (await service.list())[0]["review_state"] == "approved"
    await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cap_first", [False, True])
async def test_same_and_cap_records_share_one_reviewed_alert(
    tmp_path, monkeypatch, cap_first: bool
) -> None:
    database = Database(tmp_path / f"outpost-{cap_first}.db")
    await database.open()
    clock = VirtualClock()
    same = SameService(database, clock, SameConfig(county_codes=["042003"]))
    cap = CapAlertService(database, clock, EnvConfig())
    alerts, governor = alert_service(database, clock)

    async def request(*args, **kwargs):
        feature = cap_feature()
        feature["properties"]["event"] = "Localized Tornado Alert"
        return {"features": [feature]}

    monkeypatch.setattr("outpost.env.cap._request_json", request)
    if cap_first:
        await cap.poll(40.4406, -79.9959)
        await same.ingest(LIVE_HEADER)
        cap_item = (await cap.list())[0]
        result = await cap.approve(cap_item["id"], alerts)
        await same.reconcile_cap_duplicates()
    else:
        await same.ingest(LIVE_HEADER)
        same_item = (await same.list())[0]
        result = await same.approve(same_item["id"], alerts)
        await cap.poll(40.4406, -79.9959)
        assert await same.reconcile_cap_duplicates() == 1

    same_item = (await same.list())[0]
    cap_item = (await cap.list())[0]
    assert same_item["linked_alert_id"] == result["id"]
    assert cap_item["linked_alert_id"] == result["id"]
    assert len(await alerts.list()) == 1
    assert {item.channel for item in governor.queued_items()} == {0, 3}
    await database.close()


def test_same_rejects_malformed_header() -> None:
    service = SameService(None, VirtualClock(), SameConfig())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="invalid SAME"):
        service.parse("not a SAME message")


@pytest.mark.asyncio
async def test_same_review_api_surfaces_health_badge_and_operator_actions(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock = VirtualClock()
    config = Config.model_validate(
        {
            "store": {"path": str(tmp_path / "outpost.db")},
            "modules": {"env": {"enabled": True}, "watch": {"enabled": True}},
            "channels": {0: {"name": "public"}, 3: {"name": "watch"}},
            "env": {
                "user_agent": "(outpost.example, operator@example.org)",
                "same": {"county_codes": ["042003"]},
            },
        }
    )
    same = SameService(database, clock, config.env.same)
    await same.ingest(LIVE_HEADER)
    alerts, _governor = alert_service(database, clock)
    app = create_web_app(
        lambda: {"radio": "up"},
        database=database,
        settings=RuntimeSettings(database, config),
        alerts=alerts,
        same_events=same,
        same_receiver_health=lambda: {
            **same.health(),
            "status": "listening",
            "frequency_mhz": 162.55,
            "restart_count": 0,
        },
    )
    client = TestClient(app)

    inbox = client.get("/api/v1/environment/same")
    assert inbox.status_code == 200
    assert inbox.json()["health"]["status"] == "listening"
    assert inbox.json()["items"][0]["review_state"] == "pending"
    assert client.get("/api/v1/dashboard/poll").json()["environment"] == {"same_pending": 1}

    same_id = inbox.json()["items"][0]["id"]
    approved = client.post(f"/api/v1/environment/same/{same_id}/approve")
    assert approved.status_code == 200
    assert approved.json()["source"] == "same"
    assert client.get("/api/v1/dashboard/poll").json()["environment"] == {"same_pending": 0}
    assert await database.read("SELECT 1 FROM audit_log WHERE action='same.approve'")
    await database.close()
