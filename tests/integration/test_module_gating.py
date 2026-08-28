from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from outpost.app import OutpostApp
from outpost.config import Config
from outpost.web.api import create_web_app

MODULE_ENDPOINTS = {
    "bbs": "/api/v1/boards",
    "watch": "/api/v1/incidents",
    "env": "/api/v1/environment/weather",
    "fed": "/api/v1/federation/peers",
    "ai": "/api/v1/ai/status",
}


@pytest.mark.parametrize("disabled_module", MODULE_ENDPOINTS)
def test_each_disabled_module_returns_the_same_api_contract(disabled_module: str) -> None:
    states = {name: True for name in MODULE_ENDPOINTS}
    states[disabled_module] = False
    client = TestClient(create_web_app(lambda: {"radio": "up"}, module_provider=lambda: states))

    response = client.get(MODULE_ENDPOINTS[disabled_module])

    assert response.status_code == 409
    assert response.json() == {
        "error": {
            "code": "module_disabled",
            "message": (
                f"The {disabled_module} module is disabled. Enable "
                f"modules.{disabled_module}.enabled and restart Outpost."
            ),
        },
        "module": {
            "name": disabled_module,
            "enabled": False,
            "restart_required_to_change": True,
        },
    }
    for enabled_module, endpoint in MODULE_ENDPOINTS.items():
        if enabled_module != disabled_module:
            assert client.get(endpoint).status_code != 409


@pytest.mark.parametrize(
    "disabled_modules",
    (("bbs", "watch"), ("env", "fed", "ai"), tuple(MODULE_ENDPOINTS)),
)
def test_disabled_module_combinations_gate_reads_and_mutations(
    disabled_modules: tuple[str, ...],
) -> None:
    states = {name: name not in disabled_modules for name in MODULE_ENDPOINTS}
    client = TestClient(create_web_app(lambda: {"radio": "up"}, module_provider=lambda: states))

    for module in disabled_modules:
        assert client.get(MODULE_ENDPOINTS[module]).status_code == 409
        assert client.post(MODULE_ENDPOINTS[module], json={}).status_code == 409
    modules = client.get("/api/v1/modules").json()
    assert modules["change_policy"] == "restart_required"
    assert {name: value["enabled"] for name, value in modules["items"].items()} == states


@pytest.mark.asyncio
async def test_disabled_modules_remove_commands_and_background_workers(tmp_path) -> None:
    config = Config.model_validate(
        {
            "store": {"path": str(tmp_path / "outpost.db")},
            "modules": {name: {"enabled": False} for name in ("bbs", "ai", "watch", "env", "fed")},
        }
    )
    app = OutpostApp(config)

    assert config.modules.enabled_map() == {
        "bbs": False,
        "ai": False,
        "watch": False,
        "env": False,
        "fed": False,
    }
    assert app.router.registry.resolve("BOARDS") is None
    assert app.router.registry.resolve("OP") is None
    assert app.router.registry.resolve("REPORT") is None
    assert app.router.registry.resolve("WX") is None
    assert app.router.registry.resolve("MAIL") is not None
    assert app.router.registry.known("REPORT") is not None
    assert app.router.registry.known("REPORT").module == "watch"
    assert app.router.registry.known("WX") is not None
    assert app.router.registry.known("BOARDS") is not None

    enabled_modules = config.modules.enabled_map()
    for spec in app.router.registry.known_commands():
        expected_active = (
            enabled_modules.get(spec.module, True)
            if spec.module != "operator"
            else enabled_modules["bbs"]
        )
        for token in (spec.name, *spec.aliases):
            assert app.router.registry.known(token) is spec
            assert (app.router.registry.resolve(token) is spec) is expected_active

    await app.startup()
    try:
        task_names = {task.get_name() for task in app._tasks}
        assert task_names.isdisjoint(
            {
                "bbs-digests",
                "watch-scheduler",
                "environment-poller",
                "federation-discovery",
                "federation-services",
                "federation-sync",
                "federation-delivery",
            }
        )
        assert all(not value["enabled"] for value in app.status()["modules"].values())
    finally:
        await app.shutdown()


@pytest.mark.asyncio
async def test_enabled_module_combination_registers_matching_work(tmp_path) -> None:
    config = Config.model_validate(
        {
            "store": {"path": str(tmp_path / "outpost.db")},
            "modules": {
                "bbs": {"enabled": True},
                "watch": {"enabled": True},
                "env": {"enabled": True},
                "fed": {"enabled": True},
            },
            "env": {"user_agent": "Outpost tests (operator: test@example.org)"},
        }
    )
    app = OutpostApp(config)

    assert app.router.registry.resolve("BOARDS") is not None
    assert app.router.registry.resolve("OP") is not None
    assert app.router.registry.resolve("REPORT") is not None
    assert app.router.registry.resolve("WX") is not None

    await app.startup()
    try:
        task_names = {task.get_name() for task in app._tasks}
        assert {
            "bbs-digests",
            "watch-scheduler",
            "environment-poller",
            "federation-discovery",
            "federation-services",
            "federation-sync",
            "federation-delivery",
        } <= task_names
    finally:
        await app.shutdown()
