"""Outage assessment through the real service/store, with synthetic local evidence."""

import asyncio
import json
import os
from datetime import timedelta
from types import SimpleNamespace

import pytest
from prometheus_client import generate_latest

from outpost import readiness_probes, self_check
from outpost.app import OutpostApp
from outpost.clock import SystemClock, VirtualClock
from outpost.config import Config
from outpost.maps.regions import Region
from outpost.router.intents import IntentResolver
from outpost.self_check import ATTESTABLE, MAX_REPORT_AGE, OBSERVATION_PREFIX, SelfCheckService
from outpost.store.backups import BackupService
from outpost.store.members import MemberRepo
from outpost.transport.simulated import SimulatedRadioLink
from tests.support.maps import source_factory
from tests.unit.test_regional_maps import install

pytestmark = pytest.mark.production_wiring


@pytest.fixture
async def appliance(tmp_path, monkeypatch):
    intents = tmp_path / "intents.yaml"
    intents.write_text("[]\n")
    tiles = tmp_path / "tiles"
    tiles.mkdir()
    (tiles / "manifest.json").write_text(json.dumps({"tile_count": 1}))
    monkeypatch.setattr(
        self_check,
        "inspect_boot_schema",
        lambda schema: {
            "state": "compatible",
            "reason": "schema_capacity_sufficient",
            "database_schema": schema,
            "boot_schema_cap": schema,
            "source_relation": "same_location",
        },
    )
    config = Config.model_validate(
        {
            "store": {
                "path": str(tmp_path / "node.db"),
                "tiles_path": str(tiles),
                "releases_path": str(tmp_path / "releases"),
            },
            "router": {"intents_file": str(intents)},
            "modules": {"watch": {"enabled": True}, "fed": {"enabled": True}},
        }
    )
    clock = VirtualClock()
    app = OutpostApp(config, clock=clock, radio=SimulatedRadioLink(clock, node_id="!00000001"))
    await app.database.open()
    try:
        member = await MemberRepo(app.database, clock).resolve("!00000002")
        await app.database.write("UPDATE member SET trust='responder' WHERE id=?", (member.id,))
        now = int(clock.now().timestamp())
        await app.database.write(
            "INSERT INTO runtime_setting(key,value,updated_at) VALUES('maintenance.last_date',?,?)",
            (json.dumps(clock.now().date().isoformat()), now),
        )
        await app.database.write(
            "INSERT INTO radio_power_sample(captured_at,battery_level) VALUES(?,80)", (now,)
        )
        yield app
    finally:
        await app.ai_service.close()
        await app.radio.close()
        await app.database.close()


def checks(report):
    return {row["name"]: row for row in report["checks"]}


async def observe(app, name="station_power", outcome="pass", observed_at=None, token=None):
    report = await app.self_check.run("test")
    return await app.self_check.record_observation(
        name,
        outcome,
        int(app.clock.now().timestamp()) if observed_at is None else observed_at,
        token or checks(report)[name]["review_token"],
        "web:synthetic-operator",
    )


@pytest.fixture
async def verified_maps(appliance, monkeypatch):
    factory = source_factory()
    monkeypatch.setattr("outpost.maps.setup.RangeSource", factory)
    monkeypatch.setattr(appliance.map_setup, "_source", factory)
    install(appliance.map_setup, Region(-1.2864, 36.8172, 2, 12))
    try:
        yield appliance
    finally:
        appliance.map_setup.close()


@pytest.mark.parametrize("change", ["none", "restart", "expiry", "invalid"])
async def test_verified_map_pass_is_independent_of_a_successful_observation(verified_maps, change):
    app = verified_maps
    report = await observe(app, "offline_maps")
    row = checks(report)["offline_maps"]
    assert row["state"] == "pass" and row["passed"]
    assert row["evidence"]["observation_state"] == "attested"
    if change == "restart":
        app.self_check = SelfCheckService(
            app.database, app.config, app.clock, app.backups, app.router.intents
        )
    elif change == "expiry":
        app.clock.advance(86_400)
    elif change == "invalid":
        await app.database.write(
            "UPDATE runtime_setting SET value='broken' WHERE key=?",
            (OBSERVATION_PREFIX + "offline_maps",),
        )
    report = await app.self_check.run("test")
    row = checks(report)["offline_maps"]
    assert row["state"] == "pass" and row["passed"]
    assert row["evidence"]["observation_state"] == (
        "attested" if change == "none" else "unknown" if change == "invalid" else "stale"
    )
    assert "offline_maps" not in report["failed_checks"]
    assert row["evidence"]["wan_disconnection_tested"] is False
    assert not app.radio.sent


async def test_expiring_map_observation_does_not_expire_a_fresh_measurement(verified_maps):
    app = verified_maps
    await observe(app, "offline_maps", observed_at=int(app.clock.now().timestamp()) - 86_340)
    app.clock.advance(61)
    report = await app.self_check.latest()
    row = checks(report)["offline_maps"]
    assert not report["cached_evidence_stale"]
    assert row["state"] == "pass" and row["passed"]
    assert row["evidence"]["observation_state"] == "stale"


async def test_current_failed_map_observation_still_requires_attention(verified_maps):
    report = await observe(verified_maps, "offline_maps", outcome="fail")
    row = checks(report)["offline_maps"]
    assert row["state"] == "fail" and not row["passed"]
    assert row["evidence"]["state"] == "pass"
    assert row["evidence"]["observation_state"] == "fail"
    assert "offline_maps" in report["failed_checks"]


async def test_healthy_local_checks_do_not_certify_unknown_outage_prerequisites(appliance):
    report = await appliance.self_check.run("startup")
    rows = checks(report)
    assert report["status"] == "degraded" and report["safety_failures"] == 0
    assert set(report["failed_checks"]) == ATTESTABLE
    assert all(rows[name]["state"] == "unknown" and not rows[name]["passed"] for name in ATTESTABLE)
    assert rows["boot_schema"]["passed"] and rows["radio_power"]["passed"]
    assert rows["storage_reserve"]["passed"] and rows["responder_audience"]["passed"]
    assert report["state_counts"]["unknown"] == 7
    assert not appliance.radio.sent
    assert not await appliance.database.read("SELECT * FROM outbound_work")
    assert not await appliance.database.read("SELECT * FROM alert")
    assert not await appliance.database.read("SELECT * FROM mail")


async def test_cli_construction_defers_the_monotonic_baseline_until_the_loop(appliance):
    service = await asyncio.to_thread(
        SelfCheckService,
        appliance.database,
        appliance.config,
        SystemClock(),
        appliance.backups,
        appliance.router.intents,
    )
    assert service._initial_mono is None
    assert not service._clock_changed()
    assert service._initial_mono is not None
    assert not service._clock_changed()


@pytest.mark.parametrize(
    "kind",
    [
        "missing",
        "oversized",
        "broken",
        "nested",
        "not_map",
        "empty",
        "pipe",
        "symlink",
        "single_tile",
    ],
)
async def test_map_metadata_is_bounded_and_one_tile_is_not_coverage(appliance, tmp_path, kind):
    manifest = tmp_path / "tiles" / "manifest.json"
    if kind in {"missing", "pipe", "symlink"}:
        manifest.unlink()
        if kind == "pipe":
            os.mkfifo(manifest)
        elif kind == "symlink":
            target = tmp_path / "private-fixture"
            target.write_text('"synthetic-secret-never-export"')
            manifest.symlink_to(target)
    elif kind == "oversized":
        manifest.write_bytes(b"x" * (readiness_probes.MAX_MANIFEST_BYTES + 1))
    elif kind == "broken":
        manifest.write_bytes(b"\xff{")
    elif kind == "nested":
        manifest.write_text("[" * 5000 + "]" * 5000)
    elif kind == "not_map":
        manifest.write_text("[]")
    elif kind == "empty":
        manifest.write_text('{"tile_count":0}')
    else:
        tile = tmp_path / "tiles" / "1" / "0"
        tile.mkdir(parents=True)
        (tile / "0.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    report = await asyncio.wait_for(appliance.self_check.run("test"), timeout=3)
    result = checks(report)["offline_maps"]
    assert result["state"] == ("unknown" if kind == "single_tile" else "fail")
    assert not result["passed"] and report["status"] != "ready"
    assert str(tmp_path) not in json.dumps(result)
    assert "synthetic-secret" not in json.dumps(result)


@pytest.mark.parametrize(
    "kind,state",
    [
        ("normal", "pass"),
        ("bytes", "fail"),
        ("inodes", "fail"),
        ("missing", "unknown"),
        ("invalid", "unknown"),
        ("impossible_bytes", "unknown"),
        ("impossible_inodes", "unknown"),
    ],
)
async def test_storage_reports_only_immediate_headroom(appliance, monkeypatch, kind, state):
    values = dict(
        f_blocks=100_000, f_frsize=65_536, f_bavail=50_000, f_files=10_000, f_favail=5_000
    )
    if kind == "bytes":
        values["f_bavail"] = 1
    elif kind == "inodes":
        values["f_favail"] = 1
    elif kind == "invalid":
        values["f_files"] = 0
    elif kind == "impossible_bytes":
        values["f_bavail"] = values["f_blocks"] + 1
    elif kind == "impossible_inodes":
        values["f_favail"] = values["f_files"] + 1

    def counters(_):
        if kind == "missing":
            raise OSError("synthetic-private-path")
        return SimpleNamespace(**values)

    monkeypatch.setattr(readiness_probes.os, "statvfs", counters)
    result = checks(await appliance.self_check.run("test"))["storage_reserve"]
    assert result["state"] == state and result["passed"] == (state == "pass")
    assert "synthetic-private-path" not in json.dumps(result)
    assert result["evidence"].get("outage_duration_verified") is not True


@pytest.mark.parametrize(
    "age,level,state",
    [
        (0, 80, "pass"),
        (0, 1, "fail"),
        (1000, 80, "stale"),
        (-1, 80, "unknown"),
        (0, None, "unknown"),
    ],
)
async def test_radio_battery_freshness_is_separate_from_station_power(appliance, age, level, state):
    await appliance.database.write(
        "UPDATE radio_power_sample SET captured_at=?,battery_level=?",
        (int(appliance.clock.now().timestamp()) - age, level),
    )
    report = checks(await appliance.self_check.run("test"))
    assert report["radio_power"]["state"] == state
    assert report["station_power"]["state"] == "unknown"


@pytest.mark.parametrize("age,state", [(0, "pass"), (1000, "stale"), (-1, "unknown")])
async def test_external_radio_power_keeps_freshness_and_station_boundaries(appliance, age, state):
    await appliance.database.write(
        "UPDATE radio_power_sample SET captured_at=?,battery_level=NULL,external_power=1",
        (int(appliance.clock.now().timestamp()) - age,),
    )
    report = checks(await appliance.self_check.run("test"))
    assert report["radio_power"]["state"] == state
    assert report["radio_power"]["passed"] is (state == "pass")
    assert report["radio_power"]["evidence"]["external_power"] is True
    assert report["station_power"]["state"] == "unknown"


@pytest.mark.parametrize("name", sorted(ATTESTABLE))
async def test_operator_observation_is_audited_but_never_measured_certification(appliance, name):
    report = await observe(appliance, name)
    row = checks(report)[name]
    assert row["state"] == "attested" and row["passed"] is False
    assert report["status"] == "degraded"
    records = await appliance.database.read(
        "SELECT * FROM audit_log WHERE action='readiness.observation'"
    )
    assert len(records) == 1 and records[0]["actor_ref"] == "web:synthetic-operator"
    assert records[0]["target"] == name
    assert json.loads(records[0]["detail"])["outcome"] == "pass"
    assert not appliance.radio.sent and not await appliance.database.read(
        "SELECT * FROM outbound_work"
    )
    assert not await appliance.database.read("SELECT * FROM alert")


async def test_observation_cannot_override_measured_failure_and_can_report_failure(
    appliance, tmp_path
):
    (tmp_path / "tiles" / "manifest.json").unlink()
    assert checks(await observe(appliance, "offline_maps"))["offline_maps"]["state"] == "fail"
    assert checks(await observe(appliance, outcome="fail"))["station_power"]["state"] == "fail"


async def test_cached_passes_and_metrics_age_without_rerunning_or_mutating_the_saved_report(
    appliance,
):
    fresh = await appliance.self_check.run("test")
    appliance.clock.advance(MAX_REPORT_AGE + 1)
    stale = await appliance.self_check.latest()
    assert stale["status"] == "degraded" and stale["cached_evidence_stale"]
    assert checks(stale)["storage_reserve"]["state"] == "stale"
    assert checks(fresh)["storage_reserve"]["state"] == "pass"
    metrics = generate_latest().decode()
    assert 'outpost_self_check_state{check="storage_reserve",severity="operations"} 0.0' in metrics
    assert 'outpost_self_check_evidence_state{check="storage_reserve",state="stale"} 1.0' in metrics
    saved = json.loads(
        (
            await appliance.database.read(
                "SELECT value FROM runtime_setting WHERE key='readiness.self_check'"
            )
        )[0][0]
    )
    assert checks(saved)["storage_reserve"]["state"] == "pass"


@pytest.mark.parametrize("change", ["expired", "restart", "policy", "clock"])
async def test_old_observations_cannot_pass_after_expiry_restart_policy_or_clock_change(
    appliance, change
):
    app = appliance
    await observe(app)
    if change == "expired":
        app.clock.advance(86_400)
    elif change == "policy":
        app.config.radio.federation_portnum += 1
    elif change == "clock":
        app.clock.epoch -= timedelta(hours=2)
    else:
        app.self_check = SelfCheckService(
            app.database,
            app.config,
            app.clock,
            BackupService(app.database),
            IntentResolver(app.config.router.intents_file),
        )
        assert checks(await app.self_check.latest())["station_power"]["state"] == "stale"
    report = await app.self_check.run("test")
    assert checks(report)["station_power"]["state"] == ("unknown" if change == "clock" else "stale")
    assert report["status"] != "ready"
    if change == "clock":
        assert checks(report)["time_confidence"]["state"] == "fail"
        with pytest.raises(ValueError, match="stable local clock"):
            await observe(app)


async def test_two_operators_cannot_overwrite_an_unseen_observation(appliance):
    report = await appliance.self_check.run("test")
    token = checks(report)["station_power"]["review_token"]
    results = await asyncio.gather(
        *(
            appliance.self_check.record_observation(
                "station_power", outcome, int(appliance.clock.now().timestamp()), token, "web:test"
            )
            for outcome in ("pass", "fail")
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, ValueError) for result in results) == 1
    assert (
        len(
            await appliance.database.read(
                "SELECT * FROM audit_log WHERE action='readiness.observation'"
            )
        )
        == 1
    )


async def test_observation_and_audit_roll_back_together(appliance, monkeypatch):
    async def fail(*args, **kwargs):
        raise RuntimeError("synthetic audit failure")

    monkeypatch.setattr(self_check, "write_audit", fail)
    with pytest.raises(RuntimeError, match="audit failure"):
        await observe(appliance)
    assert not await appliance.database.read(
        "SELECT * FROM runtime_setting WHERE key=?", (OBSERVATION_PREFIX + "station_power",)
    )
    assert not await appliance.database.read(
        "SELECT * FROM audit_log WHERE action='readiness.observation'"
    )


@pytest.mark.parametrize("raw", ["broken", "[]", '{"status":"ready","checks":[]}'])
async def test_legacy_or_malformed_cached_report_is_never_all_ready(appliance, raw):
    await appliance.database.write(
        "INSERT INTO runtime_setting(key,value,updated_at) VALUES('readiness.self_check',?,0)",
        (raw,),
    )
    assert (await appliance.self_check.latest())["status"] == "never_run"


async def test_cached_radio_measurement_expires_before_the_report_itself(appliance):
    report = await appliance.self_check.run("test")
    age_limit = checks(report)["radio_power"]["evidence"]["max_age_seconds"]
    appliance.clock.advance(age_limit + 1)
    report = await appliance.self_check.latest()
    assert not report["cached_evidence_stale"]
    assert checks(report)["radio_power"]["state"] == "stale"
    assert checks(report)["boot_schema"]["state"] == "pass"


async def test_partial_modern_cache_is_rejected_before_aging(appliance):
    report = await appliance.self_check.run("test")
    del report["checks"][0]["detail"]
    await appliance.database.write(
        "UPDATE runtime_setting SET value=? WHERE key='readiness.self_check'",
        (json.dumps(report),),
    )
    appliance.self_check._cached = appliance.self_check._empty_report()
    appliance.clock.advance(MAX_REPORT_AGE + 1)
    assert (await appliance.self_check.latest())["status"] == "never_run"


async def test_policy_change_while_probing_invalidates_the_collected_passes(appliance, monkeypatch):
    original = appliance.self_check._boot_schema

    async def change_policy():
        result = await original()
        appliance.config.radio.federation_portnum += 1
        return result

    monkeypatch.setattr(appliance.self_check, "_boot_schema", change_policy)
    report = await appliance.self_check.run("test")
    assert report["cached_evidence_stale"] and report["status"] != "ready"
    assert checks(report)["boot_schema"]["state"] == "stale"
    assert checks(report)["storage_reserve"]["state"] == "stale"


@pytest.mark.parametrize(
    "raw",
    [
        "broken",
        "[]",
        '{"schema":true}',
        '{"schema":1,"observed_at":1,"valid_until":2,"outcome":"pass"}',
    ],
)
async def test_invalid_operator_observation_stays_unknown(appliance, raw):
    await appliance.database.write(
        "INSERT INTO runtime_setting(key,value,updated_at) VALUES(?,?,0)",
        (OBSERVATION_PREFIX + "station_power", raw),
    )
    result = checks(await appliance.self_check.run("test"))["station_power"]
    assert result["state"] == "unknown" and not result["passed"]


@pytest.mark.parametrize("change", ["future", "too_old", "boolean", "name", "outcome", "token"])
async def test_invalid_observation_input_cannot_create_a_record(appliance, change):
    report = await appliance.self_check.run("test")
    name, outcome = "station_power", "pass"
    stamp = int(appliance.clock.now().timestamp())
    token = checks(report)[name]["review_token"]
    if change == "future":
        stamp += 1
    elif change == "too_old":
        stamp -= 86_400
    elif change == "boolean":
        stamp = True
    elif change == "name":
        name = "boot_schema"
    elif change == "outcome":
        outcome = "certified"
    else:
        token = token[:3]
    with pytest.raises(ValueError):
        await appliance.self_check.record_observation(name, outcome, stamp, token, "web:test")
    assert not await appliance.database.read(
        "SELECT * FROM audit_log WHERE action='readiness.observation'"
    )


async def test_post_commit_cancellation_invalidates_the_old_in_memory_report(
    appliance, monkeypatch
):
    original = self_check.write_audit

    async def cancel_after_commit(tx, **kwargs):
        result = await original(tx, **kwargs)
        requester = asyncio.current_task()

        def cancel():
            requester.cancel()

        tx.after_commit(cancel)
        return result

    monkeypatch.setattr(self_check, "write_audit", cancel_after_commit)
    pending = asyncio.create_task(observe(appliance))
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert (
        len(
            await appliance.database.read(
                "SELECT * FROM audit_log WHERE action='readiness.observation'"
            )
        )
        == 1
    )
    assert (await appliance.self_check.latest())["status"] == "never_run"
    assert (
        checks(await appliance.self_check.run("recovery"))["station_power"]["state"] == "attested"
    )


async def test_outpost_map_setup_uses_configured_store_and_location(appliance, monkeypatch):
    from outpost.config import Location
    from tests.support.maps import source_factory
    from tests.unit.test_regional_maps import install

    factory = source_factory()
    monkeypatch.setattr(appliance.map_setup, "_source", factory)
    monkeypatch.setattr("outpost.maps.setup.RangeSource", factory)
    appliance.config.node.location = Location(lat=-1.2864, lon=36.8172)
    suggestion = appliance.map_position()
    assert suggestion["source"] == "configured" and suggestion["latitude"] == -1.2864
    await asyncio.to_thread(install, appliance.map_setup)
    report = await appliance.self_check.run("regional-map-test")
    result = checks(report)["offline_maps"]
    assert result["state"] == "unknown"
    assert result["evidence"]["reason"] == "overview_only"
    appliance.map_setup.close()
