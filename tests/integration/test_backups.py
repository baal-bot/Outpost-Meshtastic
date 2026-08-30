import asyncio
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from outpost.clock import VirtualClock
from outpost.config import BackupConfig
from outpost.store import Database
from outpost.store.backups import (
    BackupService,
    RestoreCoordinator,
    RestoreRecoveredError,
    plan_release_pruning,
    prune_release_directories,
    release_inventory,
)
from outpost.watch import CheckinService
from outpost.web.api import create_web_app
from outpost.web.auth import WebAuthService
from tests.support.application import production_governor

pytestmark = pytest.mark.production_wiring


def test_backup_routes_are_independent_from_optional_checkins(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    backups = BackupService(database)
    with_backups = create_web_app(lambda: {"radio": "up"}, database=database, backups=backups)
    backup_paths = {route.path for route in with_backups.routes}
    assert {
        "/api/v1/backups",
        "/api/v1/backups/{name}",
        "/api/v1/backups/{name}/validate",
        "/api/v1/backups/{name}/restore",
    } <= backup_paths

    clock = VirtualClock()
    checkins = CheckinService(
        database,
        production_governor(database, clock),
        clock,
    )
    without_backups = create_web_app(lambda: {"radio": "up"}, database=database, checkins=checkins)
    assert not any(route.path.startswith("/api/v1/backups") for route in without_backups.routes)


@pytest.mark.asyncio
async def test_recovery_snapshots_are_classified_rotated_and_removable(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    policy = BackupConfig(
        keep=2,
        pre_upgrade_keep=2,
        pre_rollback_days=30,
        superseded_release_keep=0,
    )
    service = BackupService(database, policy)
    service.directory.mkdir()
    now = datetime(2026, 8, 30, tzinfo=UTC)

    def artifact(name: str, age_days: int, content: bytes = b"snapshot") -> Path:
        path = service.directory / name
        path.write_bytes(content)
        stamp = (now - timedelta(days=age_days)).timestamp()
        os.utime(path, (stamp, stamp))
        return path

    for index in range(3):
        artifact(f"outpost-{index}.db", index)
    for index in range(4):
        artifact(f"pre-upgrade-release-{index}.db", index)
    artifact("pre-manual-rollback-newest.db", 1)
    artifact("pre-manual-rollback-recent.db", 10)
    artifact("pre-manual-rollback-old.db", 40)
    artifact("operator-note.txt", 2, b"unmanaged")
    artifact("pre-upgrade-release-3.db-wal", 3, b"sidecar")

    kinds = {str(item["kind"]) for item in service.list()}
    assert {"scheduled", "pre_upgrade", "pre_rollback", "unmanaged", "auxiliary"} <= kinds
    assert service.rotate(now=now) == 5
    assert len(service._snapshot_paths("outpost-")) == 2
    assert len(service._snapshot_paths("pre-upgrade-")) == 2
    assert {path.name for path in service._snapshot_paths("pre-manual-rollback-")} == {
        "pre-manual-rollback-newest.db",
        "pre-manual-rollback-recent.db",
    }

    recovery = [item for item in service.list() if item["kind"] == "pre_rollback"]
    removable = next(item for item in recovery if item["removable"])
    protected = next(item for item in recovery if item["protected"])
    client = TestClient(create_web_app(lambda: {"radio": "up"}, database=database, backups=service))
    rejected = client.request(
        "DELETE",
        f"/api/v1/backups/{protected['name']}",
        json={"confirmation": f"DELETE {protected['name']}"},
    )
    assert rejected.status_code == 422
    removed = client.request(
        "DELETE",
        f"/api/v1/backups/{removable['name']}",
        json={"confirmation": f"DELETE {removable['name']}"},
    )
    assert removed.status_code == 200
    assert not (service.directory / str(removable["name"])).exists()
    assert await database.read("SELECT 1 FROM audit_log WHERE action='backup.recovery_delete'")
    await database.close()


def test_release_pruning_preserves_current_previous_and_configured_prior(tmp_path) -> None:
    releases = tmp_path / "releases"
    releases.mkdir()
    now = datetime.now(UTC).timestamp()
    for index in range(5):
        release = releases / f"release-{index}"
        release.mkdir()
        (release / "payload.bin").write_bytes(bytes([index]) * (index + 1))
        os.utime(release, (now + index, now + index))
    current = tmp_path / "current"
    previous = tmp_path / "previous"
    current.symlink_to(releases / "release-4")
    previous.symlink_to(releases / "release-3")

    planned = plan_release_pruning(releases, current, previous, keep_prior=1)
    assert [path.name for path in planned] == ["release-1", "release-0"]

    removed = prune_release_directories(releases, current, previous, keep_prior=1)

    assert removed == ["release-1", "release-0"]
    assert {path.name for path in releases.iterdir()} == {
        "release-2",
        "release-3",
        "release-4",
    }
    assert release_inventory(releases) == {"count": 3, "size_bytes": 3 + 4 + 5}


@pytest.mark.asyncio
async def test_backup_is_verified_listed_and_safely_resolved(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    service = BackupService(database)
    path = await service.create()

    assert service.list()[0]["name"] == path.name
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        connection.close()
    assert service.resolve(path.name) == path
    assert service.resolve("../outpost.db") is None
    validation = await service.validate(path.name)
    assert validation["integrity"] == "ok"
    await database.write(
        "INSERT INTO runtime_setting(key,value,updated_at) VALUES('test.restore','true',1)"
    )
    with pytest.raises(ValueError, match="Confirmation"):
        await service.prepare_restore(path.name, "wrong")
    await service.prepare_restore(path.name, f"RESTORE {path.name}")
    restored = await service.restore_quiesced(path.name)
    assert restored["restored"] == path.name
    assert restored["candidate"]["integrity"] == "ok"
    assert restored["safety"]["integrity"] == "ok"
    assert restored["database"]["integrity"] == "ok"
    assert not await database.read("SELECT 1 FROM runtime_setting WHERE key='test.restore'")
    assert await database.read("SELECT 1 FROM audit_log WHERE action='backup.restore'")
    await database.close()


@pytest.mark.asyncio
async def test_failed_restore_automatically_recovers_safety_snapshot(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    service = BackupService(database)
    candidate = await service.create()
    await database.write(
        "INSERT INTO runtime_setting(key,value,updated_at) VALUES('restore.guard','present',1)"
    )
    original_restore = database.restore_from
    failed = False

    async def fail_after_candidate(source: str | Path) -> None:
        nonlocal failed
        await original_restore(source)
        if Path(source) == candidate and not failed:
            failed = True
            raise RuntimeError("injected restore failure")

    database.restore_from = fail_after_candidate  # type: ignore[method-assign]
    with pytest.raises(RestoreRecoveredError, match="injected restore failure") as raised:
        await service.restore_quiesced(candidate.name)
    assert raised.value.safety_backup.startswith("outpost-")
    rows = await database.read("SELECT value FROM runtime_setting WHERE key='restore.guard'")
    assert rows[0]["value"] == "present"
    assert (await database.validate_current())["integrity"] == "ok"
    await database.close()


@pytest.mark.asyncio
async def test_restore_coordinator_drains_and_persists_progress(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    service = BackupService(database)
    candidate = await service.create()
    restore_started, release_restore, restart = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )

    async def controlled_restore(name: str) -> dict[str, object]:
        restore_started.set()
        await release_restore.wait()
        return {"restored": name, "safety_backup": "safety.db"}

    coordinator = RestoreCoordinator(service, controlled_restore, restart.set)
    assert await coordinator.enter_mutation() is True
    job = await coordinator.schedule(candidate.name, f"RESTORE {candidate.name}")
    assert await coordinator.enter_mutation() is False
    await asyncio.sleep(0.15)
    assert not restore_started.is_set()
    await coordinator.leave_mutation()
    await asyncio.wait_for(restore_started.wait(), timeout=1)
    release_restore.set()
    await asyncio.wait_for(restart.wait(), timeout=1)

    status = coordinator.status(str(job["job_id"]))
    assert status is not None
    assert status["state"] == "completed"
    assert status["safety_backup"] == "safety.db"
    # A new process can report the terminal job without access to the old web session.
    reloaded = RestoreCoordinator(service, controlled_restore, restart.set)
    assert reloaded.status(str(job["job_id"]))["state"] == "completed"
    await database.close()


@pytest.mark.asyncio
async def test_restore_progress_survives_session_and_maintenance_blocks_api(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    service = BackupService(database)
    candidate = await service.create()
    restart = asyncio.Event()

    async def restored(name: str) -> dict[str, object]:
        return {"restored": name, "safety_backup": "safety.db"}

    coordinator = RestoreCoordinator(service, restored, restart.set)
    job = await coordinator.schedule(candidate.name, f"RESTORE {candidate.name}")
    await asyncio.wait_for(restart.wait(), timeout=1)
    auth = WebAuthService(database, 12)
    await auth.ensure_credential()
    client = TestClient(
        create_web_app(
            lambda: {"radio": "up", "recovery": coordinator.maintenance_status()},
            database=database,
            auth=auth,
            backups=service,
            restore_coordinator=coordinator,
        )
    )

    # The unguessable job URL is intentionally available without the replaced session.
    status = client.get(str(job["status_url"]))
    assert status.status_code == 200 and status.json()["state"] == "completed"
    assert client.get("/api/v1/health").status_code == 503
    blocked = client.post("/api/v1/auth/login", json={"password": "anything"})
    assert blocked.status_code == 503 and blocked.json()["error"]["code"] == "maintenance"
    await database.close()
