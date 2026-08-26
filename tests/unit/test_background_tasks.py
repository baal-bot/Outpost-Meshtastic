from __future__ import annotations

import asyncio

import pytest

from outpost.app import OutpostApp
from outpost.config import Config


@pytest.mark.asyncio
async def test_unexpected_background_task_failure_is_fatal_and_visible(tmp_path) -> None:
    app = OutpostApp(Config.model_validate({"store": {"path": str(tmp_path / "outpost.db")}}))

    async def fail() -> None:
        await asyncio.sleep(0)
        raise RuntimeError("injected send failure")

    task = app._start_background_task("airtime-governor", fail())
    reason = await asyncio.wait_for(app.wait_for_task_failure(), timeout=1)

    assert reason == "airtime-governor: RuntimeError: injected send failure"
    assert app.background_tasks_healthy() is False
    assert app.status()["task_failure"] == reason
    assert app.status()["tasks"]["airtime-governor"] == {
        "state": "failed",
        "started_at": app.status()["tasks"]["airtime-governor"]["started_at"],
        "last_ok_at": None,
        "stopped_at": app.status()["tasks"]["airtime-governor"]["stopped_at"],
        "error": "RuntimeError: injected send failure",
    }
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_expected_background_task_cancellation_is_not_fatal(tmp_path) -> None:
    app = OutpostApp(Config.model_validate({"store": {"path": str(tmp_path / "outpost.db")}}))

    async def wait() -> None:
        await asyncio.Event().wait()

    task = app._start_background_task("inbound-router", wait())
    app._task_progress("inbound-router")
    app._shutting_down = True
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0)

    health = app.status()["tasks"]["inbound-router"]
    assert health["state"] == "stopped"
    assert health["last_ok_at"] is not None
    assert app._task_failure.is_set() is False
