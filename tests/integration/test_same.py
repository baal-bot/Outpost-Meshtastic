import pytest
from fastapi.testclient import TestClient

from outpost.clock import VirtualClock
from outpost.config import Config, EnvConfig, SameConfig
from outpost.env import CapAlertService, SameService
from outpost.store import Database
from outpost.transport.governor import AirtimeGovernor
from outpost.watch import AlertService
from outpost.web.api import create_web_app
from outpost.web.settings import RuntimeSettings
from tests.support.application import production_governor

pytestmark = pytest.mark.production_wiring

LIVE_HEADER = "ZCZC-WXR-TOR-042003+0130-0010000-KPBZ/NWS-"


def cap_feature(
    identifier: str = "cap-tor",
    *,
    location_code: str = "042003",
    expires: str = "2026-01-01T01:30:00Z",
) -> dict:
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
            "expires": expires,
            "geocode": {"SAME": [location_code]},
            "eventCode": {"SAME": ["TOR"]},
        },
    }


def alert_service(database: Database, clock: VirtualClock) -> tuple[AlertService, AirtimeGovernor]:
    config = Config.model_validate({"channels": {0: {"name": "public"}, 3: {"name": "watch"}}})
    governor = production_governor(database, clock)
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
    assert unrelated.matched_locations == []
    unrelated_row = (await service.list(include_expired=True))[0]
    assert unrelated_row["decision"] == "withheld"
    assert unrelated_row["review_state"] == "logged"
    relevant, _ = await service.ingest(LIVE_HEADER)
    assert relevant.relevant and not relevant.is_test
    stored = next(item for item in await service.list() if item["header"] == LIVE_HEADER)
    assert stored["matched_locations"] == [
        {
            "configured_code": "042003",
            "received_code": "042003",
            "subdivision": "0",
            "scope": "county",
        }
    ]
    assert "matched configured SAME 042003" in stored["gate_reasons"][0]
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
@pytest.mark.parametrize("location_code", ["042003", "142003"])
@pytest.mark.parametrize("cap_first", [False, True])
async def test_same_and_cap_records_share_one_reviewed_alert(
    tmp_path, monkeypatch, cap_first: bool, location_code: str
) -> None:
    database = Database(tmp_path / f"outpost-{cap_first}-{location_code}.db")
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
        await same.ingest(LIVE_HEADER.replace("042003", location_code))
        cap_item = (await cap.list())[0]
        result = await cap.approve(cap_item["id"], alerts)
        await same.reconcile_cap_duplicates()
    else:
        await same.ingest(LIVE_HEADER.replace("042003", location_code))
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


@pytest.mark.parametrize("subdivision", range(10))
def test_same_whole_county_configuration_matches_every_subdivision(subdivision: int) -> None:
    service = SameService(None, VirtualClock(), SameConfig(county_codes=["042003"]))  # type: ignore[arg-type]
    received = f"{subdivision}42003"

    message = service.parse(LIVE_HEADER.replace("042003", received))

    assert message.relevant
    assert message.matched_locations == [
        {
            "configured_code": "042003",
            "received_code": received,
            "subdivision": str(subdivision),
            "scope": "county",
        }
    ]


def test_same_narrow_subdivision_and_national_matching() -> None:
    service = SameService(None, VirtualClock(), SameConfig(county_codes=["142003"]))  # type: ignore[arg-type]

    assert service.parse(LIVE_HEADER.replace("042003", "142003")).relevant
    assert service.parse(LIVE_HEADER).relevant
    assert not service.parse(LIVE_HEADER.replace("042003", "242003")).relevant
    assert not service.parse(LIVE_HEADER.replace("042003", "142005")).relevant
    national = service.parse(LIVE_HEADER.replace("042003", "000000"))
    assert national.relevant
    assert national.matched_locations == [
        {
            "configured_code": "automatic",
            "received_code": "000000",
            "subdivision": "0",
            "scope": "national",
        }
    ]


@pytest.mark.asyncio
async def test_same_duplicate_reactivates_after_cap_dismissal(tmp_path, monkeypatch) -> None:
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
    cap = CapAlertService(database, clock, config.env)
    alerts, _governor = alert_service(database, clock)

    async def request(*args, **kwargs):
        return {"features": [cap_feature()]}

    monkeypatch.setattr("outpost.env.cap._request_json", request)
    await cap.poll(40.4406, -79.9959)
    await same.ingest(LIVE_HEADER)
    duplicate = (await same.list())[0]
    assert duplicate["review_state"] == "duplicate"
    assert duplicate["cap_correlation"]["review_state"] == "pending"

    app = create_web_app(
        lambda: {"radio": "up"},
        database=database,
        settings=RuntimeSettings(database, config),
        alerts=alerts,
        cap_alerts=cap,
        same_events=same,
    )
    cap_item = (await cap.list())[0]
    client = TestClient(app)
    assert client.get("/api/v1/dashboard/poll").json()["environment"] == {"same_pending": 1}
    response = client.post(f"/api/v1/environment/alerts/{cap_item['id']}/dismiss")
    assert response.status_code == 200, response.text
    actionable = (await same.list())[0]
    assert actionable["decision"] == "accepted"
    assert actionable["review_state"] == "pending"
    assert actionable["cap_correlation"]["review_state"] == "dismissed"

    await same.approve(actionable["id"], alerts)
    assert len(await alerts.list()) == 1
    assert client.get("/api/v1/dashboard/poll").json()["environment"] == {"same_pending": 0}
    await database.close()


@pytest.mark.asyncio
async def test_same_duplicate_reactivates_after_cap_expiry(tmp_path, monkeypatch) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock = VirtualClock()
    same = SameService(database, clock, SameConfig(county_codes=["042003"]))
    cap = CapAlertService(database, clock, EnvConfig())
    alerts, _governor = alert_service(database, clock)

    async def request(*args, **kwargs):
        return {"features": [cap_feature(expires="2026-01-01T01:00:00Z")]}

    monkeypatch.setattr("outpost.env.cap._request_json", request)
    await cap.poll(40.4406, -79.9959)
    await same.ingest(LIVE_HEADER)
    assert (await same.list())[0]["review_state"] == "duplicate"

    clock.advance(3601)
    await cap.poll(40.4406, -79.9959)
    assert await same.reconcile_cap_duplicates() == 1
    actionable = (await same.list())[0]
    assert actionable["review_state"] == "pending"
    assert actionable["cap_correlation"]["review_state"] == "expired"

    await same.approve(actionable["id"], alerts)
    assert len(await alerts.list()) == 1
    await database.close()


@pytest.mark.asyncio
async def test_same_duplicate_can_be_overridden_by_operator(tmp_path, monkeypatch) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock = VirtualClock()
    same = SameService(database, clock, SameConfig(county_codes=["042003"]))
    cap = CapAlertService(database, clock, EnvConfig())
    alerts, _governor = alert_service(database, clock)

    async def request(*args, **kwargs):
        return {"features": [cap_feature()]}

    monkeypatch.setattr("outpost.env.cap._request_json", request)
    await cap.poll(40.4406, -79.9959)
    await same.ingest(LIVE_HEADER)
    duplicate = (await same.list())[0]

    await same.approve(duplicate["id"], alerts)
    assert len(await alerts.list()) == 1
    assert (await cap.list())[0]["review_state"] == "approved"
    assert (await same.list())[0]["review_state"] == "approved"

    await database.close()


@pytest.mark.asyncio
async def test_same_duplicate_expires_if_untouched(tmp_path, monkeypatch) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock = VirtualClock()
    same = SameService(database, clock, SameConfig(county_codes=["042003"]))
    cap = CapAlertService(database, clock, EnvConfig())

    async def request(*args, **kwargs):
        return {"features": [cap_feature()]}

    monkeypatch.setattr("outpost.env.cap._request_json", request)
    await cap.poll(40.4406, -79.9959)
    await same.ingest(LIVE_HEADER)
    assert (await same.list())[0]["review_state"] == "duplicate"

    clock.advance(5401)
    expired = (await same.list(include_expired=True))[0]
    assert expired["review_state"] == "expired"

    await database.close()


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
