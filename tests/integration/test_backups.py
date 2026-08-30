import asyncio
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from outpost.clock import VirtualClock
from outpost.store import Database
from outpost.store.backups import (
    BackupService,
    RestoreCoordinator,
    RestoreRecoveredError,
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
