from __future__ import annotations

import asyncio
import sqlite3

import pytest

from outpost.app import OutpostApp
from outpost.clock import VirtualClock
from outpost.config import Config
from outpost.task_supervision import TaskFailureDomain


class RetryClock(VirtualClock):
    def __init__(self) -> None:
        super().__init__()
        self.sleeps: asyncio.Queue[float] = asyncio.Queue()
        self.resume: asyncio.Queue[None] = asyncio.Queue()

    async def sleep(self, seconds: float) -> None:
        self.advance(seconds)
        await self.sleeps.put(seconds)
        await self.resume.get()


@pytest.mark.asyncio
async def test_unexpected_background_task_failure_is_fatal_and_visible(tmp_path) -> None:
    app = OutpostApp(Config.model_validate({"store": {"path": str(tmp_path / "outpost.db")}}))

    async def fail() -> None:
        await asyncio.sleep(0)
        raise RuntimeError("injected send failure")

    task = app._start_background_task("airtime-governor", fail)
    reason = await asyncio.wait_for(app.wait_for_task_failure(), timeout=1)

    assert reason == "airtime-governor: RuntimeError: injected send failure"
    assert app.background_tasks_healthy() is False
    assert app.status()["task_failure"] == reason
    health = app.status()["tasks"]["airtime-governor"]
    assert health["state"] == "failed"
    assert health["failure_domain"] == "core"
    assert health["required"] is True
    assert health["failure_count"] == 1
    assert health["consecutive_failures"] == 1
    assert health["last_error"] == "RuntimeError: injected send failure"
    assert health["last_error_at"] == health["stopped_at"]
    assert health["next_retry_at"] is None
    assert health["circuit_open"] is False
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_expected_background_task_cancellation_is_not_fatal(tmp_path) -> None:
    app = OutpostApp(Config.model_validate({"store": {"path": str(tmp_path / "outpost.db")}}))

    async def wait() -> None:
        await asyncio.Event().wait()

    task = app._start_background_task("inbound-router", wait)
    app._task_progress("inbound-router")
    app._shutting_down = True
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0)

    health = app.status()["tasks"]["inbound-router"]
    assert health["state"] == "stopped"
    assert health["last_ok_at"] is not None
    assert app._task_failure.is_set() is False


@pytest.mark.asyncio
async def test_restore_quiesces_background_work_before_database_replacement(tmp_path) -> None:
    app = OutpostApp(Config.model_validate({"store": {"path": str(tmp_path / "outpost.db")}}))
    await app.database.open()
    candidate = await app.backups.create()
    await app.database.write(
        "INSERT INTO runtime_setting(key,value,updated_at) VALUES('after.backup','yes',1)"
    )
    drained = asyncio.Event()

    async def writer() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            drained.set()

    app._tasks = [app._start_background_task("test-writer", writer)]
    await asyncio.sleep(0)
    result = await app._restore_database(candidate.name)

    assert drained.is_set()
    assert app._tasks == []
    assert result["restored"] == candidate.name
    assert not await app.database.read("SELECT 1 FROM runtime_setting WHERE key='after.backup'")
    assert app._task_failure.is_set() is False
    await app.database.close()


@pytest.mark.parametrize(
    ("name", "domain", "error", "expected_delay"),
    [
        (
            "environment-poller",
            TaskFailureDomain.OPTIONAL_PROVIDER,
            sqlite3.ProgrammingError("CAP binding parameter was a list"),
            15,
        ),
        (
            "ai-keep-warm",
            TaskFailureDomain.RESTARTABLE_LOCAL,
            RuntimeError("warmup failed"),
            2,
        ),
        (
            "federation-sync",
            TaskFailureDomain.OPTIONAL_PROVIDER,
            ValueError("bad peer frame"),
            15,
        ),
        (
            "same-receiver",
            TaskFailureDomain.RESTARTABLE_LOCAL,
            OSError("SDR unavailable"),
            2,
        ),
        (
            "store-maintenance",
            TaskFailureDomain.RESTARTABLE_LOCAL,
            RuntimeError("backup failed"),
            2,
        ),
    ],
)
@pytest.mark.asyncio
async def test_optional_subsystem_failure_isolated_and_restarted(
    tmp_path, name, domain, error, expected_delay
) -> None:
    app = OutpostApp(Config.model_validate({"store": {"path": str(tmp_path / "outpost.db")}}))
    clock = RetryClock()
    app.clock = clock
    attempts = 0
    recovered = asyncio.Event()
    hold = asyncio.Event()

    async def core() -> None:
        await hold.wait()

    async def subsystem() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise error
        app._task_progress(name)
        recovered.set()
        await hold.wait()

    core_task = app._start_background_task("inbound-router", core)
    task = app._start_background_task(name, subsystem, domain)
    delay = await asyncio.wait_for(clock.sleeps.get(), timeout=1)

    health = app.status()["tasks"][name]
    assert delay == expected_delay
    assert health["state"] == "backoff"
    assert health["failure_domain"] == domain.value
    assert health["required"] is False
    assert health["failure_count"] == 1
    assert health["last_error"] == f"{type(error).__name__}: {error}"
    assert health["next_retry_at"] == int(clock.now().timestamp())
    assert app.core_tasks_healthy() is True
    assert app.background_tasks_healthy() is False
    assert app._task_failure.is_set() is False

    clock.resume.put_nowait(None)
    await asyncio.wait_for(recovered.wait(), timeout=1)
    health = app.status()["tasks"][name]
    assert health["state"] == "running"
    assert health["failure_count"] == 1
    assert health["consecutive_failures"] == 0
    assert health["restart_count"] == 1
    assert health["last_error"] == f"{type(error).__name__}: {error}"
    assert app.background_tasks_healthy() is True
    for background in (task, core_task):
        background.cancel()
    await asyncio.gather(task, core_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_repeated_optional_failure_opens_bounded_circuit(tmp_path) -> None:
    app = OutpostApp(Config.model_validate({"store": {"path": str(tmp_path / "outpost.db")}}))
    clock = RetryClock()
    app.clock = clock

    async def fail() -> None:
        raise RuntimeError("deterministic provider failure")

    task = app._start_background_task(
        "environment-poller", fail, TaskFailureDomain.OPTIONAL_PROVIDER
    )
    for expected in (15, 30, 60):
        assert await asyncio.wait_for(clock.sleeps.get(), timeout=1) == expected
        assert app.status()["tasks"]["environment-poller"]["state"] == "backoff"
        clock.resume.put_nowait(None)

    assert await asyncio.wait_for(clock.sleeps.get(), timeout=1) == 900
    health = app.status()["tasks"]["environment-poller"]
    assert health["state"] == "circuit_open"
    assert health["circuit_open"] is True
    assert health["failure_count"] == 4
    assert health["consecutive_failures"] == 4
    assert health["next_retry_at"] == int(clock.now().timestamp())
    assert app._task_failure.is_set() is False
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
