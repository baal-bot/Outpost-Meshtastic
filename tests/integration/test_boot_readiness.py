from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import threading
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from prometheus_client import generate_latest

from outpost import boot_readiness, diagnostics
from outpost.clock import VirtualClock
from outpost.config import Config
from outpost.router.intents import IntentResolver
from outpost.self_check import SelfCheckService
from outpost.store import Database
from outpost.store.backups import BackupService
from outpost.store.members import MemberRepo
from outpost.web.api import create_web_app

pytestmark = pytest.mark.production_wiring


@dataclass
class BootFixture:
    current: Path
    release: Path
    package: Path
    properties: dict[str, str]
    response: Path
    calls: Path
    command: Path

    def save(self) -> None:
        self.response.write_text(
            "\n".join(f"{key}={value}" for key, value in self.properties.items())
        )


@pytest.fixture
def boot_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> BootFixture:
    release = tmp_path / "releases" / "test-release"
    package = release / "lib" / f"python3.{sys.version_info.minor}" / "site-packages" / "outpost"
    migrations = package / "store" / "migrations"
    migrations.mkdir(parents=True)
    (migrations / "0000_core.sql").write_text("-- fixture only\n")
    (migrations / "0177_current.sql").write_text("-- fixture only\n")
    (release / "bin").mkdir()
    (release / "bin" / "python").symlink_to(sys.executable)
    current = tmp_path / "current"
    current.symlink_to(release)
    command = tmp_path / "systemctl-fixture"
    response = tmp_path / "unit-response"
    calls = tmp_path / "unit-calls"
    command.write_text(
        f"#!{sys.executable}\n"
        "import sys\nfrom pathlib import Path\n"
        f"with Path({str(calls)!r}).open('a') as output: output.write(repr(sys.argv[1:])+'\\n')\n"
        f"sys.stdout.buffer.write(Path({str(response)!r}).read_bytes())\n"
    )
    command.chmod(0o755)
    properties = dict.fromkeys(boot_readiness.UNIT_PROPERTIES, "")
    properties.update(
        LoadState="loaded",
        UnitFileState="enabled",
        ActiveState="active",
        NeedDaemonReload="no",
        ExecStart=f"{{ path={current}/bin/python ; argv[]={current}/bin/python -m outpost ; "
        "ignore_errors=no ; start_time=[n/a] ; pid=0 ; }",
        Environment="OUTPOST_CONFIG=/etc/outpost/config.yaml",
    )
    fixture = BootFixture(current, release, package, properties, response, calls, command)
    fixture.save()
    monkeypatch.setattr(boot_readiness, "CURRENT", current)
    monkeypatch.setattr(boot_readiness.shutil, "which", lambda _: str(command))
    return fixture


async def ready_service(tmp_path: Path) -> SelfCheckService:
    intents = tmp_path / "intents.yaml"
    intents.write_text("[]\n")
    config = Config.model_validate(
        {"store": {"path": str(tmp_path / "outpost.db")}, "router": {"intents_file": str(intents)}}
    )
    database = Database(config.store.path)
    await database.open()
    clock = VirtualClock()
    member = await MemberRepo(database, clock).resolve("!00000001")
    await database.write("UPDATE member SET trust='responder' WHERE id=?", (member.id,))
    await database.write(
        "INSERT INTO runtime_setting(key,value,updated_at) VALUES('maintenance.last_date',?,?)",
        (json.dumps(clock.now().date().isoformat()), int(clock.now().timestamp())),
    )
    return SelfCheckService(
        database, config, clock, BackupService(database), IntentResolver(str(intents))
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("same_source", [False, True])
async def test_boot_schema_uses_real_readiness_and_persists_scoped_evidence(
    tmp_path: Path, boot_fixture: BootFixture, monkeypatch: pytest.MonkeyPatch, same_source: bool
) -> None:
    if same_source:
        monkeypatch.setattr(boot_readiness, "PACKAGE", boot_fixture.package)
    service = await ready_service(tmp_path)
    try:
        report = await service.run("startup")
        assert report["status"] == "ready"
        check = next(item for item in report["checks"] if item["name"] == "boot_schema")
        assert check["passed"] is True
        assert check["evidence"]["state"] == "compatible"
        assert check["evidence"]["source_relation"] == (
            "same_location" if same_source else "different_location"
        )
        assert "not proof of reboot recovery" in check["detail"]
        assert str(tmp_path) not in json.dumps(check)
        stored = await service.database.read(
            "SELECT value FROM runtime_setting WHERE key='readiness.self_check'"
        )
        assert json.loads(stored[0]["value"]) == report
        assert 'outpost_self_check_state{check="boot_schema",severity="operations"} 1.0' in (
            generate_latest().decode()
        )
        calls = boot_fixture.calls.read_text()
        assert len(calls.splitlines()) == 2
        assert all("'show', 'outpost.service', '--all'" in line for line in calls.splitlines())
        assert "restart" not in calls and "-m" not in calls
        assert not await service.database.read("SELECT id FROM outbound_work")
    finally:
        await service.database.close()


@pytest.mark.asyncio
async def test_healthy_developer_http_cannot_hide_incompatible_boot_schema(
    tmp_path: Path, boot_fixture: BootFixture
) -> None:
    service = await ready_service(tmp_path)
    migration = boot_fixture.package / "store" / "migrations" / "0177_current.sql"
    migration.rename(migration.with_name("0153_old.sql"))
    properties_before = boot_fixture.response.read_bytes()
    schema_before = await service.database.read("SELECT version,applied_at FROM schema_version")
    app = create_web_app(lambda: {"radio": "up"}, database=service.database, self_check=service)
    try:
        local = TestClient(app, client=("127.0.0.1", 50000))
        assert local.get("/api/v1/health").json()["status"] == "ok"
        report = local.post("/api/v1/diagnostics/readiness").json()
        assert report["status"] == "degraded"
        assert report["failed_checks"] == ["boot_schema"]
        check = next(item for item in report["checks"] if item["name"] == "boot_schema")
        assert check["evidence"]["state"] == "incompatible"
        assert check["evidence"]["boot_schema_cap"] == 153
        assert "177 exceeds selected boot capacity 153" in check["detail"]
        assert "Do not bypass schema guards" in check["remediation"]
        for path in ("/api/v1/readiness", "/api/v1/dashboard/poll", "/api/v1/diagnostics/status"):
            response = local.get(path).json()
            assert (response if path.endswith("/readiness") else response["readiness"]) == report
        # Reading cached HTTP status must not spawn more probes.
        assert len(boot_fixture.calls.read_text().splitlines()) == 2
        assert TestClient(app).post("/api/v1/diagnostics/readiness").status_code == 403
        assert not await service.database.read("SELECT id FROM outbound_work")
        assert not await service.database.read("SELECT id FROM mail")
        assert schema_before == await service.database.read(
            "SELECT version,applied_at FROM schema_version"
        )
        assert boot_fixture.response.read_bytes() == properties_before
        assert 'outpost_self_check_state{check="boot_schema",severity="operations"} 0.0' in (
            generate_latest().decode()
        )
        # A later explicit run recovers the warning without restoring any data.
        migration.with_name("0153_old.sql").rename(migration)
        assert (await service.run("maintenance"))["status"] == "ready"
    finally:
        await service.database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key", "value", "reason"),
    [
        ("LoadState", "not-found", "unit_not_loaded"),
        ("UnitFileState", "disabled", "unit_not_enabled"),
        ("UnitFileState", "masked", "unit_not_enabled"),
        ("UnitFileState", "enabled-runtime", "unit_not_enabled"),
        ("NeedDaemonReload", "yes", "unit_reload_pending"),
        ("ExecStart", "private-wrapper secret-command", "unsupported_launch"),
        (
            "Environment",
            "OUTPOST_CONFIG=/private/config PASSWORD=secret-value",
            "custom_unit_environment",
        ),
        ("EnvironmentFiles", "/private/secret-file", "custom_unit_environment"),
        ("PassEnvironment", "PYTHONPATH", "custom_unit_environment"),
        ("UnsetEnvironment", "OUTPOST_CONFIG", "custom_unit_environment"),
        ("DropInPaths", "/private/custom-unit", "custom_unit_environment"),
        ("ActiveState", "failed", "selected_service_failed"),
        ("ActiveState", "inactive", "selected_service_not_active"),
        ("ActiveState", "activating", "selected_service_not_active"),
    ],
)
async def test_unknown_or_failed_boot_selection_never_reports_all_ready(
    tmp_path: Path, boot_fixture: BootFixture, key: str, value: str, reason: str
) -> None:
    boot_fixture.properties[key] = value
    boot_fixture.save()
    service = await ready_service(tmp_path)
    try:
        report = await service.run("manual")
        assert report["status"] == "degraded"
        assert report["failed_checks"] == ["boot_schema"]
        check = next(item for item in report["checks"] if item["name"] == "boot_schema")
        assert check["passed"] is False
        assert check["evidence"]["reason"] == reason
        assert "Recovery readiness is not established" in check["detail"]
        assert "secret-" not in json.dumps(report)
        assert "/private/" not in json.dumps(report)
        assert str(tmp_path) not in json.dumps(check)
    finally:
        await service.database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("same_source", [False, True])
async def test_own_notify_startup_is_a_static_schema_check_not_a_boot_failure(
    tmp_path: Path, boot_fixture: BootFixture, monkeypatch: pytest.MonkeyPatch, same_source: bool
) -> None:
    boot_fixture.properties.update(ActiveState="activating", MainPID=str(os.getpid()))
    boot_fixture.save()
    if same_source:
        monkeypatch.setattr(boot_readiness, "PACKAGE", boot_fixture.package)
    service = await ready_service(tmp_path)
    try:
        report = await service.run("startup")
        assert report["status"] == ("ready" if same_source else "degraded")
        check = next(item for item in report["checks"] if item["name"] == "boot_schema")
        assert check["passed"] is same_source
        if same_source:
            assert "not proof of reboot recovery" in check["detail"]
        else:
            assert check["evidence"]["reason"] == "selected_service_not_active"
    finally:
        await service.database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("schema_changes", [False, True])
async def test_boot_probe_does_not_block_radio_event_loop(
    tmp_path: Path,
    boot_fixture: BootFixture,
    monkeypatch: pytest.MonkeyPatch,
    schema_changes: bool,
) -> None:
    service = await ready_service(tmp_path)
    started, release = threading.Event(), threading.Event()
    original = boot_readiness._unit_properties

    def slow_unit() -> dict[str, str]:
        started.set()
        assert release.wait(3)
        return original()

    monkeypatch.setattr(boot_readiness, "_unit_properties", slow_unit)
    pending = asyncio.create_task(service.run("startup"))
    try:
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set()
        assert not pending.done()
        # The heartbeat and ordinary database reader remain available during the
        # system bus wait; no second writer or readiness poll loop is introduced.
        assert await service.database.read("SELECT 1")
        if schema_changes:
            await service.database.write("UPDATE schema_version SET version=178 WHERE version=177")
        release.set()
        report = await pending
        assert report["status"] == ("degraded" if schema_changes else "ready")
        if schema_changes:
            check = next(item for item in report["checks"] if item["name"] == "boot_schema")
            assert check["evidence"]["reason"] == "database_schema_changed"
    finally:
        release.set()
        await pending
        await service.database.close()


@pytest.mark.parametrize("backend", ["absent", "old"])
def test_diagnostic_bundle_checks_boot_independently_of_live_backend(
    tmp_path: Path, boot_fixture: BootFixture, monkeypatch: pytest.MonkeyPatch, backend: str
) -> None:
    database = tmp_path / "diagnostic.db"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("CREATE TABLE schema_version(version INTEGER)")
        connection.execute("INSERT INTO schema_version VALUES(178)")
        connection.commit()
    before = database.read_bytes()
    config = Config.model_validate({"store": {"path": str(database)}})
    monkeypatch.setattr(diagnostics, "_run_live_self_check", lambda _: {"reachable": False})
    monkeypatch.setattr(diagnostics, "_live_status", lambda _: {"reachable": backend == "old"})
    monkeypatch.setattr(diagnostics, "_service_status", lambda: {"available": False})
    evidence = diagnostics.runtime_evidence(config)
    assert evidence["self_check"]["status"] == "unavailable"
    assert evidence["boot_schema"]["state"] == "incompatible"
    assert evidence["boot_schema"]["database_schema"] == 178
    assert database.read_bytes() == before
    assert str(tmp_path) not in json.dumps(evidence["boot_schema"])


@pytest.mark.parametrize("schema", [None, True, "177", -1, 10_000])
def test_invalid_database_schema_is_unknown(boot_fixture: BootFixture, schema: object) -> None:
    evidence = boot_readiness.inspect_boot_schema(schema)
    assert evidence["state"] == "unknown"
    assert evidence["reason"] == "database_schema_unavailable"


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("missing_current", "boot_selection_unavailable"),
        ("broken_current", "inspection_unavailable"),
        ("missing_python", "boot_interpreter_unavailable"),
        ("custom_python", "boot_interpreter_ambiguous"),
        ("mismatched_python", "boot_interpreter_package_mismatch"),
        ("missing_package", "boot_package_ambiguous"),
        ("multiple_packages", "boot_package_ambiguous"),
        ("empty_migrations", "package_metadata_invalid"),
        ("duplicate_migration", "package_metadata_invalid"),
        ("symlink_migration", "package_unreadable"),
        ("entries_limit", "package_entry_limit"),
    ],
)
def test_unreadable_or_ambiguous_packages_are_unknown(
    boot_fixture: BootFixture, monkeypatch: pytest.MonkeyPatch, mutation: str, reason: str
) -> None:
    migration = boot_fixture.package / "store" / "migrations" / "0177_current.sql"
    if mutation in {"missing_current", "broken_current"}:
        boot_fixture.current.unlink()
        if mutation == "broken_current":
            boot_fixture.current.symlink_to(boot_fixture.release / "absent")
    elif mutation == "missing_python":
        (boot_fixture.release / "bin" / "python").unlink()
    elif mutation == "custom_python":
        executable = boot_fixture.release / "bin" / "python"
        executable.unlink()
        executable.write_text("#!/bin/sh\nexit 99\n")
        executable.chmod(0o755)
    elif mutation == "mismatched_python":
        minor = 12 if sys.version_info.minor == 13 else 13
        directory = boot_fixture.package.parents[1]
        directory.rename(directory.with_name(f"python3.{minor}"))
    elif mutation == "missing_package":
        boot_fixture.package.rename(boot_fixture.package.with_name("not-outpost"))
    elif mutation == "multiple_packages":
        minor = 12 if sys.version_info.minor == 13 else 13
        (boot_fixture.release / "lib" / f"python3.{minor}" / "site-packages" / "outpost").mkdir(
            parents=True
        )
    elif mutation == "empty_migrations":
        migration.unlink()
        migration.with_name("0000_core.sql").unlink()
    elif mutation == "duplicate_migration":
        migration.with_name("0177_duplicate.sql").write_text("-- duplicate\n")
    elif mutation == "symlink_migration":
        migration.unlink()
        migration.symlink_to(migration.with_name("0000_core.sql"))
    elif mutation == "entries_limit":
        monkeypatch.setattr(boot_readiness, "MAX_ENTRIES", 1)
    evidence = boot_readiness.inspect_boot_schema(177)
    assert evidence["state"] == "unknown"
    assert evidence["reason"] == reason


def test_changed_release_selection_is_unknown(
    boot_fixture: BootFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = boot_readiness._schema_cap

    def switch_after_inspection(package: Path) -> int:
        value = original(package)
        if package == boot_fixture.package:
            other = boot_fixture.release.parent / "other-release"
            other.mkdir()
            boot_fixture.current.unlink()
            boot_fixture.current.symlink_to(other)
        return value

    monkeypatch.setattr(boot_readiness, "_schema_cap", switch_after_inspection)
    assert boot_readiness.inspect_boot_schema(177)["reason"] == "selection_changed"


def test_changed_unit_and_elided_empty_array_metadata(
    boot_fixture: BootFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in ("EnvironmentFiles", "PassEnvironment", "UnsetEnvironment"):
        del boot_fixture.properties[key]
    boot_fixture.save()
    assert boot_readiness.inspect_boot_schema(177)["state"] == "compatible"
    original = boot_readiness._schema_cap

    def change_unit(package: Path) -> int:
        cap = original(package)
        if package == boot_fixture.package:
            boot_fixture.properties["ActiveState"] = "inactive"
            boot_fixture.save()
        return cap

    monkeypatch.setattr(boot_readiness, "_schema_cap", change_unit)
    assert boot_readiness.inspect_boot_schema(177)["reason"] == "selection_changed"


@pytest.mark.asyncio
async def test_unreadable_database_schema_is_not_a_pass(
    tmp_path: Path, boot_fixture: BootFixture
) -> None:
    service = await ready_service(tmp_path)
    try:
        await service.database.write("DROP TABLE schema_version")
        report = await service.run("manual")
        assert report["status"] == "degraded"
        assert report["failed_checks"] == ["boot_schema"]
        check = next(item for item in report["checks"] if item["name"] == "boot_schema")
        assert check["evidence"]["reason"] == "database_schema_unavailable"
    finally:
        await service.database.close()


@pytest.mark.asyncio
async def test_cancelled_readiness_keeps_single_flight_until_probe_exits(
    tmp_path: Path, boot_fixture: BootFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = await ready_service(tmp_path)
    started, release = threading.Event(), threading.Event()
    original = boot_readiness._unit_properties
    calls = 0

    def slow_unit() -> dict[str, str]:
        nonlocal calls
        calls += 1
        started.set()
        assert release.wait(3)
        return original()

    monkeypatch.setattr(boot_readiness, "_unit_properties", slow_unit)
    first = asyncio.create_task(service.run("cancelled-client"))
    second = None
    try:
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set()
        first.cancel()
        second = asyncio.create_task(service.run("next-client"))
        await asyncio.sleep(0.05)
        assert not first.done() and not second.done()
        assert calls == 1
        first.cancel()
        await asyncio.sleep(0.05)
        assert not first.done() and not second.done()
        assert calls == 1
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert (await second)["status"] == "ready"
        assert calls == 4
    finally:
        release.set()
        await asyncio.gather(first, *([second] if second else []), return_exceptions=True)
        await service.database.close()


@pytest.mark.parametrize(
    "mode",
    [
        "missing",
        "failure",
        "timeout",
        "exit_timeout",
        "oversize",
        "invalid",
        "duplicate",
        "incomplete",
        "encoding",
    ],
)
def test_systemctl_failures_are_bounded_and_redacted(
    boot_fixture: BootFixture, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    expected = "unit_unavailable"
    if mode == "missing":
        monkeypatch.setattr(boot_readiness.shutil, "which", lambda _: None)
    elif mode == "failure":
        boot_fixture.command.write_text(f"#!{sys.executable}\nraise SystemExit(1)\n")
    elif mode == "timeout":
        boot_fixture.command.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(30)\n")
        monkeypatch.setattr(boot_readiness, "PROBE_SECONDS", 0.05)
        expected = "unit_timeout"
    elif mode == "exit_timeout":
        boot_fixture.command.write_text(
            f"#!{sys.executable}\nimport os, time\nos.close(1)\ntime.sleep(30)\n"
        )
        monkeypatch.setattr(boot_readiness, "PROBE_SECONDS", 0.15)
        expected = "unit_timeout"
    elif mode == "oversize":
        boot_fixture.response.write_text("private-secret" * boot_readiness.MAX_OUTPUT)
        expected = "unit_output_limit"
    elif mode == "encoding":
        boot_fixture.response.write_bytes(b"\xffprivate-secret")
        expected = "inspection_unavailable"
    else:
        boot_fixture.response.write_text(
            {
                "invalid": "private-secret",
                "duplicate": "LoadState=x\nLoadState=y",
                "incomplete": "LoadState=x",
            }[mode]
        )
        expected = "unit_metadata_invalid"
    evidence = boot_readiness.inspect_boot_schema(177)
    assert evidence["state"] == "unknown"
    assert evidence["reason"] == expected
    assert "private-secret" not in json.dumps(evidence)
