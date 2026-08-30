import sqlite3
import urllib.error
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from outpost.ai.retrieval import RetrievalEngine
from outpost.clock import VirtualClock
from outpost.config import Config, EnvConfig
from outpost.env import CapAlertService
from outpost.store import Database
from outpost.watch import AlertService
from tests.support.application import production_governor

pytestmark = pytest.mark.production_wiring


def feature(identifier: str, *, severity: str = "Severe", status: str = "Actual") -> dict:
    return {
        "id": identifier,
        "properties": {
            "id": identifier,
            "sender": "w-nws.webmaster@noaa.gov",
            "sent": "2026-01-01T00:00:00Z",
            "messageType": "Alert",
            "status": status,
            "event": "Tornado Warning",
            "headline": "Tornado Warning issued for the local area",
            "description": "Take shelter now.",
            "areaDesc": "Allegheny County",
            "severity": severity,
            "urgency": "Immediate",
            "certainty": "Observed",
            "effective": "2026-01-01T00:00:00Z",
            "expires": "2026-01-01T01:00:00Z",
        },
    }


@pytest.mark.asyncio
async def test_cap_gate_dedupe_and_review_inbox(tmp_path, monkeypatch) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    payload = {"features": [feature("cap-1"), feature("cap-2", severity="Moderate")]}

    async def request(*args, **kwargs):
        return payload

    monkeypatch.setattr("outpost.env.cap._request_json", request)
    service = CapAlertService(database, VirtualClock(), EnvConfig())

    result = await service.poll(40.4406, -79.9959)
    assert result == {"seen": 2, "accepted": 1, "withheld": 1}
    await service.poll(40.4406, -79.9959)
    items = await service.list()
    assert len(items) == 2
    assert items[0]["decision"] in {"accepted", "withheld"}
    withheld = next(item for item in items if item["decision"] == "withheld")
    assert "severity is below Severe" in withheld["gate_reasons"]

    await service.dismiss(withheld["id"])
    assert (
        next(item for item in await service.list() if item["id"] == withheld["id"])["review_state"]
        == "dismissed"
    )
    await database.close()


@pytest.mark.asyncio
async def test_cap_expiry_uses_instants_for_every_supported_iso_representation(
    tmp_path, monkeypatch
) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock = VirtualClock()
    representations = {
        "negative": "2025-12-31T20:30:00-04:00",
        "positive": "2026-01-01T05:30:00+05:00",
        "zulu": "2026-01-01T00:30:00Z",
        "naive": "2026-01-01T00:30:00",
    }
    payload = {"features": []}
    for name, expiry in representations.items():
        item = feature(f"cap-{name}")
        item["properties"]["expires"] = expiry
        payload["features"].append(item)

    async def request(*args, **kwargs):
        return payload

    monkeypatch.setattr("outpost.env.cap._request_json", request)
    service = CapAlertService(database, clock, EnvConfig())

    await service.poll(40.4406, -79.9959)
    rows = await database.read(
        "SELECT identifier,expires_epoch,review_state FROM cap_alert ORDER BY identifier"
    )
    assert {int(row["expires_epoch"]) for row in rows} == {1_767_227_400}
    assert {row["review_state"] for row in rows} == {"pending"}
    evidence = await RetrievalEngine(database, now=lambda: int(clock.now().timestamp()))._weather()
    assert {item.ref for item in evidence} == {
        "wx:alert@cap-negative",
        "wx:alert@cap-positive",
        "wx:alert@cap-zulu",
    }

    clock.advance(1_799)
    await service.poll(40.4406, -79.9959)
    assert {item["review_state"] for item in await service.list()} == {"pending"}

    clock.advance(1)
    await service.poll(40.4406, -79.9959)
    expired = await service.list(include_expired=True)
    assert {item["review_state"] for item in expired} == {"expired"}
    assert await service.list() == []
    await database.close()


@pytest.mark.asyncio
async def test_cap_repoll_revives_an_alert_whose_expiry_was_extended(tmp_path, monkeypatch) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock = VirtualClock()
    item = feature("cap-extended")
    item["properties"]["expires"] = "2025-12-31T23:59:00Z"
    payload = {"features": [item]}

    async def request(*args, **kwargs):
        return payload

    monkeypatch.setattr("outpost.env.cap._request_json", request)
    service = CapAlertService(database, clock, EnvConfig())

    await service.poll(40.4406, -79.9959)
    assert (await service.list(include_expired=True))[0]["review_state"] == "expired"
    item["properties"]["expires"] = "2026-01-01T00:30:00-04:00"
    await service.poll(40.4406, -79.9959)
    revived = (await service.list())[0]
    assert revived["review_state"] == "pending"
    assert revived["expires_epoch"] == 1_767_241_800
    await database.close()


def test_cap_expiry_migration_backfills_epochs_and_repairs_live_rows(tmp_path) -> None:
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE cap_alert (
          id INTEGER PRIMARY KEY,
          expires_at TEXT NOT NULL,
          review_state TEXT NOT NULL,
          decision TEXT NOT NULL
        );
        CREATE INDEX idx_cap_review ON cap_alert(review_state,decision,expires_at);
        """
    )
    live = (datetime.now(UTC) + timedelta(hours=1)).astimezone(timezone(timedelta(hours=-4)))
    expired = datetime.now(UTC) - timedelta(hours=1)
    connection.executemany(
        "INSERT INTO cap_alert(expires_at,review_state,decision) VALUES(?, 'expired','accepted')",
        ((live.isoformat(),), (expired.isoformat().replace("+00:00", "Z"),)),
    )
    migration = (
        Path(__file__).parents[2] / "src/outpost/store/migrations/0156_cap_expiry_epoch.sql"
    ).read_text()

    connection.executescript(migration)
    rows = connection.execute(
        "SELECT expires_epoch,review_state FROM cap_alert ORDER BY id"
    ).fetchall()

    assert rows[0][0] == int(live.timestamp()) and rows[0][1] == "pending"
    assert rows[1][0] == int(expired.timestamp()) and rows[1][1] == "expired"
    connection.close()


def test_cap_gate_rejects_expired_test_and_unlikely() -> None:
    clock = VirtualClock()
    value = feature("cap-test", status="Test")["properties"]
    value["certainty"] = "Unlikely"
    value["expires"] = "2025-12-31T23:00:00Z"

    decision, reasons = CapAlertService._gate(value, clock.now())

    assert decision == "withheld"
    assert "status is not Actual" in reasons
    assert "certainty is Unlikely" in reasons
    assert "alert is expired" in reasons


@pytest.mark.asyncio
async def test_cap_update_supersedes_and_cancel_issues_all_clear(tmp_path, monkeypatch) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock = VirtualClock()
    payload = {"features": [feature("cap-original")]}

    async def request(*args, **kwargs):
        return payload

    monkeypatch.setattr("outpost.env.cap._request_json", request)
    cap = CapAlertService(database, clock, EnvConfig())
    config = Config.model_validate({"channels": {0: {"name": "public"}, 3: {"name": "watch"}}})
    governor = production_governor(database, clock)
    alerts = AlertService(database, governor, clock, config)

    await cap.poll(40.4406, -79.9959)
    original_cap = (await cap.list())[0]
    original = await cap.approve(original_cap["id"], alerts)
    assert original["expires_at"] == 1_767_229_200

    update = feature("cap-update")
    update["properties"]["messageType"] = "Update"
    update["properties"]["references"] = [
        "w-nws.webmaster@noaa.gov,cap-original,2026-01-01T00:00:00Z"
    ]
    update["properties"]["headline"] = "Updated Tornado Warning"
    payload["features"] = [update]
    await cap.poll(40.4406, -79.9959)
    update_cap = next(item for item in await cap.list() if item["identifier"] == "cap-update")
    replacement = await cap.approve(update_cap["id"], alerts)
    assert replacement["expires_at"] == 1_767_229_200
    assert (await alerts.by_id(original["id"])).cancelled_at is not None
    assert replacement["id"] != original["id"]
    assert all(
        item.queue_key != f"alert:{original['id']}:repeat" for item in governor.queued_items()
    )

    cancel = feature("cap-cancel")
    cancel["properties"]["messageType"] = "Cancel"
    cancel["properties"]["references"] = "w-nws.webmaster@noaa.gov,cap-update,2026-01-01T00:00:00Z"
    payload["features"] = [cancel]
    await cap.poll(40.4406, -79.9959)
    cancel_cap = next(item for item in await cap.list() if item["identifier"] == "cap-cancel")
    await cap.approve(cancel_cap["id"], alerts)
    assert (await alerts.by_id(replacement["id"])).cancelled_at is not None
    assert any(item.text.startswith("ALL CLEAR") for item in governor.queued_items())
    await database.close()


@pytest.mark.asyncio
async def test_cap_approval_preserves_short_and_long_warning_expiry(tmp_path, monkeypatch) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock = VirtualClock()
    short = feature("cap-short")
    short["properties"]["expires"] = "2026-01-01T00:30:00Z"
    long = feature("cap-long")
    long["properties"]["expires"] = "2026-01-04T00:00:00Z"
    payload = {"features": [short, long]}

    async def request(*args, **kwargs):
        return payload

    monkeypatch.setattr("outpost.env.cap._request_json", request)
    config = Config.model_validate({"channels": {0: {"name": "public"}, 3: {"name": "watch"}}})
    alerts = AlertService(
        database, production_governor(database, clock, airtime=config.airtime), clock, config
    )
    cap = CapAlertService(database, clock, EnvConfig())

    await cap.poll(40.4406, -79.9959)
    inbox = {item["identifier"]: item for item in await cap.list()}
    short_alert = await cap.approve(inbox["cap-short"]["id"], alerts)
    long_alert = await cap.approve(inbox["cap-long"]["id"], alerts)

    assert short_alert["expires_at"] == 1_767_227_400
    assert long_alert["expires_at"] == 1_767_484_800
    assert "until 00:30" in short_alert["headline"]
    assert "until 00:00" in long_alert["headline"]
    await database.close()


@pytest.mark.asyncio
async def test_cap_missing_expiry_uses_visible_six_hour_fallback(tmp_path, monkeypatch) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock = VirtualClock()
    item = feature("cap-missing")
    item["properties"].pop("expires")

    async def request(*args, **kwargs):
        return {"features": [item]}

    monkeypatch.setattr("outpost.env.cap._request_json", request)
    config = Config.model_validate({"channels": {0: {"name": "public"}, 3: {"name": "watch"}}})
    alerts = AlertService(
        database, production_governor(database, clock, airtime=config.airtime), clock, config
    )
    cap = CapAlertService(database, clock, EnvConfig())

    await cap.poll(40.4406, -79.9959)
    stored = (await cap.list())[0]
    assert stored["decision"] == "accepted" and stored["review_state"] == "pending"
    assert stored["expires_epoch"] == int(clock.now().timestamp()) + 6 * 3_600
    assert stored["gate_reasons"] == ["expiry missing; using the documented 6-hour fallback"]
    alert = await cap.approve(stored["id"], alerts)
    assert alert["expires_at"] == stored["expires_epoch"]
    assert "until 06:00" in alert["headline"]
    await database.close()


@pytest.mark.asyncio
async def test_cap_approval_clearly_refuses_an_expired_warning(tmp_path, monkeypatch) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock = VirtualClock()
    item = feature("cap-past")
    item["properties"]["expires"] = "2025-12-31T23:59:00Z"

    async def request(*args, **kwargs):
        return {"features": [item]}

    monkeypatch.setattr("outpost.env.cap._request_json", request)
    config = Config.model_validate({"channels": {0: {"name": "public"}, 3: {"name": "watch"}}})
    alerts = AlertService(
        database, production_governor(database, clock, airtime=config.airtime), clock, config
    )
    cap = CapAlertService(database, clock, EnvConfig())

    await cap.poll(40.4406, -79.9959)
    stored = (await cap.list(include_expired=True))[0]
    with pytest.raises(ValueError, match="expired at .* cannot be approved"):
        await cap.approve(stored["id"], alerts)
    assert await alerts.list() == []
    await database.close()


def test_cap_polygon_must_contain_outpost() -> None:
    properties = feature("cap-polygon")["properties"]
    geometry = {
        "type": "Polygon",
        "coordinates": [[[-80.1, 40.3], [-80.0, 40.3], [-80.0, 40.4], [-80.1, 40.3]]],
    }
    decision, reasons = CapAlertService._gate(
        properties, VirtualClock().now(), geometry, (40.4406, -79.9959)
    )
    assert decision == "withheld"
    assert "alert polygon does not contain the Outpost" in reasons


@pytest.mark.asyncio
async def test_point_queries_use_location_scoped_cache(tmp_path, monkeypatch) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    calls: list[str] = []

    async def request(url, *args, **kwargs):
        calls.append(url)
        if "/points/" in url:
            city, state = ("Denver", "CO") if "39.7392" in url else ("Pittsburgh", "PA")
            return {
                "properties": {"relativeLocation": {"properties": {"city": city, "state": state}}}
            }
        item = feature("denver-alert" if "39.7392" in url else "pittsburgh-alert")
        item["properties"]["areaDesc"] = "Denver County" if "39.7392" in url else "Allegheny"
        return {"updated": "2026-01-01T00:05:00Z", "features": [item]}

    monkeypatch.setattr("outpost.env.cap._request_json", request)
    service = CapAlertService(database, VirtualClock(), EnvConfig())

    denver, denver_source = await service.query_point(39.7392, -104.9903)
    pittsburgh, pittsburgh_source = await service.query_point(40.4406, -79.9959)
    repeated, repeated_source = await service.query_point(39.7392, -104.9903)

    assert denver["status"] == pittsburgh["status"] == "ok"
    assert denver["items"][0]["area_desc"] == "Denver County"
    assert pittsburgh["items"][0]["area_desc"] == "Allegheny"
    assert repeated == denver
    assert repeated_source == denver_source
    assert denver_source["query_lat"] == 39.7392
    assert denver_source["query_lon"] == -104.9903
    assert denver_source["service_area"] == "Denver, CO"
    assert pittsburgh_source["service_area"] == "Pittsburgh, PA"
    assert len(calls) == 4
    assert len(await database.read("SELECT 1 FROM cap_point_cache")) == 2
    await database.close()


@pytest.mark.asyncio
async def test_point_query_states_distinguish_empty_stale_unsupported_and_failure(
    tmp_path, monkeypatch
) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock = VirtualClock()
    mode = "ok"

    async def request(url, *args, **kwargs):
        if mode == "unsupported" and "/points/" in url:
            raise urllib.error.HTTPError(url, 404, "outside NWS service area", {}, None)
        if mode == "failure":
            raise OSError("provider offline")
        if "/points/" in url:
            return {"properties": {"forecastZone": "https://api.weather.gov/zones/forecast/PAZ021"}}
        if mode == "empty":
            return {"updated": "2026-01-01T00:05:00Z", "features": []}
        return {"updated": "2026-01-01T00:05:00Z", "features": [feature("cached-alert")]}

    monkeypatch.setattr("outpost.env.cap._request_json", request)
    service = CapAlertService(database, clock, EnvConfig(refresh_minutes=5, max_age_hours=1))

    current, _ = await service.query_point(40.4406, -79.9959)
    assert current["status"] == "ok"
    clock.advance(301)
    mode = "failure"
    stale, stale_source = await service.query_point(40.4406, -79.9959)
    assert stale["status"] == "stale"
    assert stale["items"] == current["items"]
    assert stale["error"] == "provider offline"
    assert stale_source["cache_age_seconds"] == 301
    clock.advance(1_500)
    expired_cache, _ = await service.query_point(40.4406, -79.9959)
    assert expired_cache["status"] == "provider_failure"
    assert expired_cache["items"] == []

    mode = "empty"
    empty, _ = await service.query_point(41.0000, -80.0000)
    assert empty == {"status": "empty", "items": []}

    mode = "unsupported"
    unsupported, _ = await service.query_point(51.5072, -0.1276)
    assert unsupported == {"status": "unsupported_region", "items": []}

    mode = "failure"
    failed, failed_source = await service.query_point(48.8566, 2.3522)
    assert failed["status"] == "provider_failure"
    assert failed["items"] == []
    assert failed["error"] == "provider offline"
    assert failed_source["fetched_at"] is None
    await database.close()
