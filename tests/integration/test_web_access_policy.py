from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import outpost.web.api as api_module
from outpost.config import Config, WebConfig
from outpost.store import Database
from outpost.web.api import (
    API_MODULE_PREFIXES,
    STEP_UP_PREFIXES,
    VIEWER_READ_PATHS,
    VIEWER_SELF_SERVICE,
    create_web_app,
    route_access_policy,
    step_up_path,
)
from outpost.web.auth import WebSession
from outpost.web.settings import RuntimeSettings


class StubAuth:
    def __init__(self, session: WebSession | None) -> None:
        self.value = session

    async def session(self, token: str | None) -> WebSession | None:
        del token
        return self.value


def _session(*, role: str = "operator", step_up_until: int | None = 2_000_000_000) -> WebSession:
    return WebSession(
        csrf_token="csrf-token",  # noqa: S106 - isolated middleware fixture
        must_change=False,
        account_id=1,
        username="test-operator",
        display_name="Test Operator",
        role=role,
        mfa_enabled=False,
        step_up_until=step_up_until,
    )


def _client(
    *,
    config: WebConfig | None = None,
    session: WebSession | None = None,
    modules: dict[str, bool] | None = None,
    address: str = "192.0.2.20",
) -> TestClient:
    auth = cast(Any, StubAuth(session))
    app = create_web_app(
        lambda: {"radio": "up"},
        auth=auth,
        web_config=config,
        module_provider=(lambda: modules) if modules is not None else None,
    )
    return TestClient(app, client=(address, 50000))


@pytest.mark.parametrize("path", ["/metrics", "/metrics/"])
def test_metrics_policy_is_authenticated_loopback_or_disabled(path: str) -> None:
    unauthenticated = _client()
    assert unauthenticated.get(path).status_code == 401
    assert unauthenticated.head(path).status_code == 401

    operator = _client(session=_session())
    assert operator.get(path).status_code == 200
    assert operator.head(path).status_code == 200

    viewer = _client(session=_session(role="viewer"))
    assert viewer.get(path).status_code == 403

    loopback_config = WebConfig(metrics_access="loopback")
    assert _client(config=loopback_config, address="127.0.0.1").get(path).status_code == 200
    assert _client(config=loopback_config).get(path).status_code == 403

    disabled_config = WebConfig(metrics_access="disabled")
    assert _client(config=disabled_config, session=_session()).get(path).status_code == 404


def test_openapi_routes_are_disabled_and_every_http_route_declares_access() -> None:
    app = create_web_app(lambda: {"radio": "up"})
    paths = {route.path for route in app.routes if isinstance(route, APIRoute)}

    assert {"/openapi.json", "/api/docs", "/redoc"}.isdisjoint(paths)
    assert [path for path in paths if route_access_policy(path) is None] == []


def _source_route_methods() -> set[tuple[str, str]]:
    source = Path(api_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    methods = {"get", "head", "post", "put", "patch", "delete", "options"}
    routes: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        for decorator in node.decorator_list:
            if (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr in methods
                and decorator.args
                and isinstance(decorator.args[0], ast.Constant)
                and isinstance(decorator.args[0].value, str)
            ):
                routes.add((decorator.func.attr.upper(), decorator.args[0].value))
    return routes


def test_route_guards_match_declared_routes_and_all_broadcast_actions_require_step_up() -> None:
    routes = _source_route_methods()
    paths = {path for _, path in routes}

    for prefix in STEP_UP_PREFIXES:
        assert any(path == prefix or path.startswith(f"{prefix}/") for path in paths), prefix
    for prefix, _ in API_MODULE_PREFIXES:
        assert any(path == prefix or path.startswith(f"{prefix}/") for path in paths), prefix
    for method, path in VIEWER_SELF_SERVICE:
        assert (method, path) in routes
    for path in VIEWER_READ_PATHS:
        assert ("GET", path) in routes

    protected_broadcasts = {
        ("POST", "/api/v1/alerts"),
        ("POST", "/api/v1/environment/alerts/refresh"),
        ("POST", "/api/v1/environment/alerts/{cap_id}/approve"),
        ("POST", "/api/v1/environment/alerts/{cap_id}/dismiss"),
        ("POST", "/api/v1/environment/earthquakes/{quake_id}/approve"),
        ("POST", "/api/v1/environment/earthquakes/{quake_id}/dismiss"),
        ("POST", "/api/v1/environment/same/{same_id}/approve"),
        ("POST", "/api/v1/environment/same/{same_id}/dismiss"),
    }
    assert protected_broadcasts <= routes
    assert all(step_up_path(method, path) for method, path in protected_broadcasts)


def test_registered_cap_review_routes_require_step_up(tmp_path: Path) -> None:
    database = Database(tmp_path / "outpost.db")
    settings = RuntimeSettings(database, Config())
    app = create_web_app(
        lambda: {"radio": "up"},
        database=database,
        auth=cast(Any, StubAuth(_session(step_up_until=0))),
        settings=settings,
        alerts=cast(Any, object()),
        cap_alerts=cast(Any, object()),
    )
    registered = {route.path for route in app.routes if isinstance(route, APIRoute)}
    paths = {
        "/api/v1/environment/alerts/refresh",
        "/api/v1/environment/alerts/{cap_id}/approve",
        "/api/v1/environment/alerts/{cap_id}/dismiss",
    }
    assert paths <= registered

    client = TestClient(app)
    for path in paths:
        response = client.post(
            path.replace("{cap_id}", "1"),
            headers={"x-csrf-token": "csrf-token"},
        )
        assert response.status_code == 428
        assert response.json()["error"]["code"] == "step_up_required"


def _assert_security_headers(response: Any) -> None:
    assert response.headers["content-security-policy"].startswith("default-src 'self'")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"


def test_security_headers_cover_success_and_every_middleware_denial_class() -> None:
    success = _client().get("/api/v1/health")
    unauthorized = _client().get("/api/v1/auth/session")
    csrf = _client(session=_session()).post("/api/v1/auth/logout")
    step_up = _client(session=_session(step_up_until=0)).post(
        "/api/v1/environment/alerts/1/approve",
        headers={"x-csrf-token": "csrf-token"},
    )
    disabled = _client(session=_session(), modules={"env": False}).get(
        "/api/v1/environment/weather"
    )

    assert [value.status_code for value in (success, unauthorized, csrf, step_up, disabled)] == [
        200,
        401,
        403,
        428,
        409,
    ]
    for response in (success, unauthorized, csrf, step_up, disabled):
        _assert_security_headers(response)
    assert unauthorized.headers["cache-control"] == "no-store"
    assert csrf.headers["cache-control"] == "no-store"
