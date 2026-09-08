from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from prometheus_client import generate_latest

from outpost.clock import VirtualClock
from outpost.config import Config
from outpost.radio_operations import RadioOperations
from outpost.radio_power import RadioPowerMonitor, normalize_battery_level
from outpost.router.intents import IntentResolver
from outpost.self_check import SelfCheckService
from outpost.situation import BriefingCapability, SituationBriefingService
from outpost.store import Database
from outpost.store.backups import BackupService
from outpost.transport.models import LinkState
from outpost.transport.radio_link import MeshtasticRadioLink
from outpost.web.api import create_web_app
from tests.support.application import production_governor

pytestmark = pytest.mark.production_wiring


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        (101, None),
        (-1, None),
        ("42", None),
        (True, None),
        (float("nan"), None),
        (float("inf"), None),
        (0, 0),
        (100, 100),
    ],
)
def test_battery_normalization_distinguishes_external_power(
    raw: object, expected: int | None
) -> None:
    assert normalize_battery_level(raw) == expected


@pytest.mark.asyncio
async def test_no_battery_is_recorded_and_reported_without_a_false_alarm(tmp_path: Path) -> None:
    config = Config.model_validate({"store": {"path": str(tmp_path / "outpost.db")}})
    database = Database(config.store.path)
    await database.open()
    clock = VirtualClock()
    monitor = RadioPowerMonitor(database, clock, config.radio.power)
    await monitor.restore()
    await monitor.observe(None)

    power = await monitor.history()
    assert power["reported"] is False
    assert power["condition"] == "not_reported"
    assert power["battery_level"] is None
    assert power["samples"] == [
        {
            "captured_at": int(clock.now().timestamp()),
            "battery_level": None,
            "external_power": False,
        }
    ]

    intents = tmp_path / "intents.yaml"
    intents.write_text("[]\n", encoding="utf-8")
    self_check = SelfCheckService(
        database,
        config,
        clock,
        BackupService(database),
        IntentResolver(str(intents)),
    )
    check = await self_check._radio_power()
    assert not check.passed and check.state == "unknown"
    assert check.evidence["condition"] == "not_reported"

    situation = SituationBriefingService(
        database,
        clock,
        lambda: {
            "radio": "up",
            "queues": {},
            "inbound": {},
            "radio_power": monitor.snapshot(),
        },
        modules=lambda: {"bbs": False, "watch": False, "env": False, "fed": False},
    )
    briefing = await situation.snapshot(BriefingCapability.OPERATOR)
    power_item = next(item for item in briefing["items"] if item["id"] == "network:power")
    assert power_item["title"] == "Radio power not reported"
    assert "external power" in power_item["detail"]
    await database.close()


@pytest.mark.asyncio
async def test_external_power_survives_adapter_governor_storage_and_readiness(
    tmp_path: Path,
) -> None:
    config = Config.model_validate(
        {
            "store": {"path": str(tmp_path / "outpost.db")},
            "radio": {"power": {"shed_discretionary": True}},
        }
    )
    database = Database(config.store.path)
    await database.open()
    try:
        clock = VirtualClock()
        monitor = RadioPowerMonitor(database, clock, config.radio.power)
        intents = tmp_path / "intents.yaml"
        intents.write_text("[]\n", encoding="utf-8")
        checker = SelfCheckService(
            database, config, clock, BackupService(database), IntentResolver(str(intents))
        )
        link = MeshtasticRadioLink(config.radio, clock)
        link._state = LinkState.UP
        link._local_id = "!00000001"
        device_metrics = {"batteryLevel": 101}
        link._interface = SimpleNamespace(nodes={link._local_id: {"deviceMetrics": device_metrics}})
        governor = production_governor(database, clock, link=link)
        governor.power_config = config.radio.power
        governor.power_observer = monitor.observe
        monitor.on_condition_change = lambda condition: checker.run("radio-power")

        await governor.tick()
        power = await monitor.history()
        assert power["condition"] == "external" and power["external_power"] is True
        assert power["battery_level"] is None and power["reported"] is False
        assert power["shedding"]["active"] is False
        assert power["trend"]["direction"] == "unavailable"
        assert power["samples"][-1]["external_power"] is True
        metrics = generate_latest().decode()
        assert "outpost_radio_battery_level_percent NaN" in metrics
        assert "outpost_radio_battery_reported 0.0" in metrics
        report = checker.snapshot()
        checks = {row["name"]: row for row in report["checks"]}
        assert checks["radio_power"]["state"] == "pass"
        assert "radio_power" not in report["failed_checks"]
        assert checks["station_power"]["state"] == "unknown"
        assert report["status"] != "ready"

        restored = RadioPowerMonitor(database, clock, config.radio.power)
        await restored.restore()
        assert restored.snapshot() == monitor.snapshot()
        operations = RadioOperations(database, governor, clock, power=restored)
        client = TestClient(
            create_web_app(lambda: {"radio": "up"}, database=database, radio_operations=operations)
        )
        assert client.get("/api/v1/mesh/power").json()["condition"] == "external"
        situation = SituationBriefingService(
            database,
            clock,
            lambda: {"radio": "up", "radio_power": restored.snapshot()},
            modules=lambda: {"bbs": False, "watch": False, "env": False, "fed": False},
        )
        briefing = await situation.snapshot(BriefingCapability.OPERATOR)
        power_item = next(item for item in briefing["items"] if item["id"] == "network:power")
        assert power_item["title"] == "Radio on external power"
        assert power_item["severity"] == "info"

        # External power expires under the same policy as a battery reading.
        clock.advance(2 * config.radio.power.sample_interval_s + 61)
        assert (await checker._radio_power()).state == "stale"
        assert (
            next(row for row in checker.snapshot()["checks"] if row["name"] == "radio_power")[
                "state"
            ]
            == "stale"
        )

        # New telemetry changes conditions immediately, even inside the sampling interval.
        for raw, condition, state, shedding in (
            (None, "not_reported", "unknown", False),
            (5, "critical", "fail", True),
            (101, "external", "pass", False),
        ):
            device_metrics["batteryLevel"] = raw
            await governor.tick()
            assert monitor.snapshot()["condition"] == condition
            assert monitor.snapshot()["shedding"]["active"] is shedding
            assert (await checker._radio_power()).state == state
            assert governor.battery_level == (5 if raw == 5 else None)
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_external_power_migration_preserves_legacy_unknown_samples(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "outpost.db"
    database = Database(path)
    await database.open()
    await database.write(
        "INSERT INTO radio_power_sample(captured_at,battery_level) VALUES(10,NULL),(20,80)"
    )
    await database.close()
    with sqlite3.connect(path) as legacy:
        legacy.execute("ALTER TABLE radio_power_sample DROP COLUMN external_power")
        legacy.execute("DELETE FROM schema_version WHERE version=186")
    database = Database(path)
    await database.open()
    try:
        rows = await database.read(
            "SELECT id,captured_at,battery_level,external_power FROM radio_power_sample ORDER BY id"
        )
        assert [tuple(row) for row in rows] == [(1, 10, None, 0), (2, 20, 80, 0)]
        assert (await database.read("PRAGMA integrity_check"))[0][0] == "ok"
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_falling_power_trend_crosses_warning_and_critical_conditions(
    tmp_path: Path,
) -> None:
    config = Config.model_validate(
        {
            "store": {"path": str(tmp_path / "outpost.db")},
            "radio": {
                "power": {
                    "warning_percent": 30,
                    "critical_percent": 15,
                    "sample_interval_s": 300,
                    "trend_hours": 24,
                    "shed_discretionary": True,
                    "shed_below_percent": 15,
                }
            },
        }
    )
    database = Database(config.store.path)
    await database.open()
    clock = VirtualClock()
    conditions: list[str] = []

    async def condition_changed(condition: str) -> None:
        conditions.append(condition)

    monitor = RadioPowerMonitor(
        database, clock, config.radio.power, on_condition_change=condition_changed
    )
    await monitor.restore()
    await monitor.observe(80)

    clock.advance(6 * 3_600)
    await monitor.observe(28)
    warning = monitor.snapshot()
    assert warning["condition"] == "warning"
    assert warning["trend"] == {
        "direction": "falling",
        "delta_percent": -52,
        "elapsed_hours": 6.0,
        "window_hours": 24,
        "sample_count": 2,
    }

    clock.advance(6 * 3_600)
    await monitor.observe(12)
    critical = await monitor.history()
    assert critical["condition"] == "critical"
    assert critical["trend"]["delta_percent"] == -68
    assert critical["shedding"]["active"] is True
    assert [sample["battery_level"] for sample in critical["samples"]] == [80, 28, 12]
    assert conditions == ["normal", "warning", "critical"]

    intents = tmp_path / "intents.yaml"
    intents.write_text("[]\n", encoding="utf-8")
    self_check = SelfCheckService(
        database,
        config,
        clock,
        BackupService(database),
        IntentResolver(str(intents)),
    )
    report = await self_check.run("test")
    power_check = next(check for check in report["checks"] if check["name"] == "radio_power")
    assert power_check["passed"] is False
    assert power_check["evidence"]["condition"] == "critical"

    governor = production_governor(database, clock)
    operations = RadioOperations(database, governor, clock, power=monitor)
    client = TestClient(
        create_web_app(
            lambda: {"radio": "up", "radio_power": monitor.snapshot()},
            database=database,
            radio_operations=operations,
        )
    )
    response = client.get("/api/v1/mesh/power")
    assert response.status_code == 200
    assert response.json()["trend"]["direction"] == "falling"
    await database.close()
