from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from prometheus_client import make_asgi_app
from pydantic import BaseModel, Field

from outpost import __version__
from outpost.bbs.admin import BBSAdmin
from outpost.env import (
    AstronomyService,
    CapAlertService,
    SeismicService,
    WaypointService,
    WeatherService,
)
from outpost.fed import FederationPeerService
from outpost.radio_operations import RadioOperations
from outpost.store import Database
from outpost.store.backups import BackupService, RestoreCoordinator
from outpost.watch import AlertService, CheckinService, IncidentService
from outpost.web.auth import WebAuthService
from outpost.web.settings import RuntimeSettings


class LoginBody(BaseModel):
    password: str


class PasswordBody(BaseModel):
    current_password: str
    new_password: str


class NodeSettingsBody(BaseModel):
    name: str | None = None
    short_name: str | None = None
    operator_contact: str | None = None
    emergency_number: str | None = None
    timezone: str | None = None
    locale: str | None = None
    units: str | None = None
    disclaimer: str | None = None
    location: dict[str, float] | None = None


class WatchSettingsBody(BaseModel):
    emergency_keywords_enabled: bool | None = None
    emergency_keywords: list[str] | None = None
    emergency_cooldown_minutes: int | None = None
    escalation: dict[str, Any] | None = None


class MemberPatchBody(BaseModel):
    trust: Literal["blocked", "guest", "member", "trusted", "responder", "operator"] | None = None
    notes: str | None = None


class PositionPurgeBody(BaseModel):
    confirmation: str


class PostPatchBody(BaseModel):
    hidden: bool
    reason: str = "operator moderation"


class BoardCreateBody(BaseModel):
    slug: str
    title: str
    description: str | None = None
    min_read_trust: str = "guest"
    min_post_trust: str = "member"
    retention_days: int | None = None
    sort_order: int = 100
    federated: bool = False


class BoardPatchBody(BaseModel):
    title: str | None = None
    description: str | None = None
    min_read_trust: str | None = None
    min_post_trust: str | None = None
    retention_days: int | None = None
    sort_order: int | None = None
    archived: bool | None = None
    federated: bool | None = None


class ThreadCreateBody(BaseModel):
    subject: str
    body: str


class ReplyCreateBody(BaseModel):
    body: str


class ThreadPatchBody(BaseModel):
    pinned: bool | None = None
    locked: bool | None = None
    hidden: bool | None = None


class MeshSendBody(BaseModel):
    text: str
    destination: str
    channel: int = 0
    traffic_class: str = "reply"


class RestoreBody(BaseModel):
    confirmation: str


class IncidentCreateBody(BaseModel):
    text: str
    force: bool = False


class IncidentPatchBody(BaseModel):
    status: Literal["open", "monitoring", "resolved", "false_alarm", "expired"] | None = None
    severity: Literal["info", "caution", "urgent", "critical"] | None = None
    resolution: str | None = None


class IncidentUpdateBody(BaseModel):
    kind: Literal["ack", "update"]
    note: str = ""


class AlertCreateBody(BaseModel):
    severity: Literal["caution", "urgent", "critical"]
    headline: str
    incident_ref: int | None = None
    channels: list[int] = [3]
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    radius_km: float = Field(default=1.0, ge=0.1, le=100)


class AlertCancelBody(BaseModel):
    resolution: str


class EventCreateBody(BaseModel):
    name: str
    roster_policy: Literal["all", "responders", "subscribed"] = "all"


class EventSolicitBody(BaseModel):
    confirmation: str


class WaypointBody(BaseModel):
    name: str
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    category: str = "general"
    notes: str = ""


class WaypointPatchBody(BaseModel):
    name: str | None = None
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    category: str | None = None
    notes: str | None = None


class FederationStateBody(BaseModel):
    state: Literal["pending", "paused", "rejected"]


class FederationApproveBody(BaseModel):
    confirmation_code: str = Field(pattern=r"^[0-9]{6}$")


class FederationSuccessorBody(BaseModel):
    old_mesh_id: str = Field(pattern=r"^![0-9a-fA-F]{8}$")
    old_node_name: str | None = Field(default=None, max_length=80)


class FederationMqttBody(BaseModel):
    enabled: bool
    address: str = Field(max_length=253)
    tls_enabled: bool = True
    root: str = Field(default="msh", min_length=1, max_length=80)
    channel: int = Field(default=0, ge=0, le=7)
    uplink_enabled: bool = True
    downlink_enabled: bool = True


class FederationServiceBody(BaseModel):
    service: Literal["weather", "alerts", "knowledge"]
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    query: str | None = Field(default=None, max_length=200)


class FederationSyncPolicyBody(BaseModel):
    boards: list[str] = Field(default_factory=list, max_length=20)
    sync_incidents: bool = False
    incident_lat: float | None = Field(default=None, ge=-90, le=90)
    incident_lon: float | None = Field(default=None, ge=-180, le=180)
    incident_radius_km: float = Field(default=25, ge=1, le=500)
    relay_alerts: bool = False
    quota_items_per_hour: int = Field(default=20, ge=1, le=500)
    relay_mail: bool = False
    quota_mail_per_hour: int = Field(default=20, ge=1, le=100)
    service_permissions: list[Literal["weather", "alerts", "knowledge"]] = Field(
        default_factory=list, max_length=3
    )
    quota_services_per_hour: int = Field(default=6, ge=1, le=60)
    service_concurrency: int = Field(default=1, ge=1, le=4)
    service_max_response_bytes: int = Field(default=1200, ge=256, le=1600)
    service_airtime_seconds_per_hour: float = Field(default=15, ge=1, le=120)
    policy_review_at: datetime | None = None
    enable_boards: list[str] | None = Field(default=None, max_length=20)
    confirm_enable_boards: bool = False


class FederationInboxBody(BaseModel):
    state: Literal["imported", "rejected"]
    reason: str = Field(default="Rejected by operator", max_length=160)


class FederationMailBody(BaseModel):
    peer_mesh_id: str
    recipient_handle: str = Field(min_length=1, max_length=40)
    subject: str = Field(default="", max_length=120)
    body: str = Field(min_length=1, max_length=800)


def _timestamp(value: int) -> str:
    return datetime.fromtimestamp(value, UTC).isoformat().replace("+00:00", "Z")


_AUDIT_SECRET_KEY = re.compile(
    r"password|passphrase|secret|token|api[_-]?key|private[_-]?key|psk|credential|"
    r"authorization|cookie",
    re.IGNORECASE,
)
_AUDIT_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(password|passphrase|secret|token|api[_-]?key|private[_-]?key|psk|credential|"
    r"authorization|cookie)(\s*[:=]\s*)([^,;\s}]+)"
)


def _redact_audit_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]"
            if _AUDIT_SECRET_KEY.search(str(key))
            else _redact_audit_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_audit_value(item) for item in value]
    return value


def _audit_detail(value: object) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    text = str(value)
    try:
        structured = _redact_audit_value(json.loads(text))
    except (json.JSONDecodeError, TypeError):
        return _AUDIT_SECRET_ASSIGNMENT.sub(r"\1\2[REDACTED]", text), "text"
    return json.dumps(structured, indent=2, sort_keys=True, ensure_ascii=False), "json"


def create_web_app(
    status_provider: Callable[[], dict[str, Any]],
    database: Database | None = None,
    auth: WebAuthService | None = None,
    settings: RuntimeSettings | None = None,
    reconnect_radio: Callable[[], Awaitable[None]] | None = None,
    backups: BackupService | None = None,
    bbs_admin: BBSAdmin | None = None,
    radio_operations: RadioOperations | None = None,
    incidents: IncidentService | None = None,
    alerts: AlertService | None = None,
    checkins: CheckinService | None = None,
    weather: WeatherService | None = None,
    cap_alerts: CapAlertService | None = None,
    astronomy: AstronomyService | None = None,
    seismic: SeismicService | None = None,
    waypoints: WaypointService | None = None,
    federation: FederationPeerService | None = None,
    federation_pair: Callable[[str], Awaitable[Any]] | None = None,
    federation_approve: Callable[[str, str], Awaitable[Any]] | None = None,
    federation_mqtt_status: Callable[[], Awaitable[dict[str, Any]]] | None = None,
    federation_mqtt_configure: Callable[..., Awaitable[dict[str, Any]]] | None = None,
    federation_service_list: Callable[[], Awaitable[list[dict[str, object]]]] | None = None,
    federation_service_query: Callable[[str, dict[str, object]], Awaitable[dict[str, object]]]
    | None = None,
    federation_inbox_import: Callable[[int], Awaitable[str]] | None = None,
    federation_mail_send: (
        Callable[[str, str, str, str], Awaitable[dict[str, object]]] | None
    ) = None,
    restore_coordinator: RestoreCoordinator | None = None,
    module_provider: Callable[[], dict[str, bool]] | None = None,
) -> FastAPI:
    app = FastAPI(title="Outpost API", version=__version__, docs_url="/api/docs")

    def effective_modules() -> dict[str, bool]:
        if module_provider is not None:
            return module_provider()
        if settings is not None:
            return settings.config.modules.enabled_map()
        return {name: True for name in ("bbs", "ai", "watch", "env", "fed")}

    def api_module(path: str) -> str | None:
        routes = (
            ("/api/v1/environment", "env"),
            ("/api/v1/federation", "fed"),
            ("/api/v1/incidents", "watch"),
            ("/api/v1/alerts", "watch"),
            ("/api/v1/events", "watch"),
            ("/api/v1/watch", "watch"),
            ("/api/v1/config/watch", "watch"),
            ("/api/v1/boards", "bbs"),
            ("/api/v1/threads", "bbs"),
            ("/api/v1/posts", "bbs"),
            ("/api/v1/ai", "ai"),
        )
        return next(
            (
                module
                for prefix, module in routes
                if path == prefix or path.startswith(f"{prefix}/")
            ),
            None,
        )

    @app.middleware("http")
    async def security_headers(request: Any, call_next: Any) -> Any:
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' https://tile.openstreetmap.org; object-src 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        if request.url.path.endswith((".html", ".js", ".css")) or request.url.path == "/":
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.middleware("http")
    async def authentication(request: Request, call_next: Any) -> Response:
        path = request.url.path
        public = path in {
            "/api/v1/health",
            "/api/v1/auth/login",
            "/api/v1/auth/setup",
        } or path.startswith("/api/v1/recovery/restores/")
        if auth is not None and path.startswith("/api/v1/") and not public:
            session = await auth.session(request.cookies.get("outpost_session"))
            if session is None:
                return JSONResponse(
                    {"error": {"code": "unauthorized", "message": "Sign in required."}},
                    status_code=401,
                )
            auth_path = path.startswith("/api/v1/auth/")
            if session.must_change and not auth_path:
                return JSONResponse(
                    {
                        "error": {
                            "code": "password_change_required",
                            "message": "Complete first-run password setup before using the API.",
                        }
                    },
                    status_code=403,
                )
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                if request.headers.get("x-csrf-token") != session.csrf_token:
                    return JSONResponse(
                        {"error": {"code": "csrf", "message": "Invalid CSRF token."}},
                        status_code=403,
                    )
        module = api_module(path)
        if module is not None and not effective_modules().get(module, True):
            return JSONResponse(
                {
                    "error": {
                        "code": "module_disabled",
                        "message": (
                            f"The {module} module is disabled. Enable modules.{module}.enabled "
                            "and restart Outpost."
                        ),
                    },
                    "module": {
                        "name": module,
                        "enabled": False,
                        "restart_required_to_change": True,
                    },
                },
                status_code=409,
            )
        return await call_next(request)

    if restore_coordinator is not None:

        @app.middleware("http")
        async def recovery_maintenance(request: Request, call_next: Any) -> Response:
            path = request.url.path
            restore_request = (
                request.method == "POST"
                and path.startswith("/api/v1/backups/")
                and path.endswith("/restore")
            )
            recovery_request = path.startswith("/api/v1/recovery/restores/")
            if restore_request and restore_coordinator.maintenance_status()["active"]:
                return JSONResponse(
                    {
                        "error": {
                            "code": "maintenance",
                            "message": "Another restore is already in progress.",
                        }
                    },
                    status_code=503,
                    headers={"Retry-After": "15"},
                )
            gated_request = (
                path.startswith("/api/v1/")
                and path != "/api/v1/health"
                and not recovery_request
                and not restore_request
            )
            if gated_request and not await restore_coordinator.enter_mutation():
                status = restore_coordinator.maintenance_status()
                return JSONResponse(
                    {
                        "error": {
                            "code": "maintenance",
                            "message": "Outpost is restoring a backup; mutations are paused.",
                        },
                        "recovery": status,
                    },
                    status_code=503,
                    headers={"Retry-After": "15"},
                )
            try:
                return await call_next(request)
            finally:
                if gated_request:
                    await restore_coordinator.leave_mutation()

    tile_root = Path(".data/tiles").resolve()

    @app.get("/tiles/manifest.json", response_model=None)
    async def tile_manifest() -> Response:
        manifest = tile_root / "manifest.json"
        if not manifest.is_file():
            return Response(status_code=404)
        return FileResponse(manifest, media_type="application/json")

    @app.get("/favicon.ico", include_in_schema=False, response_model=None)
    async def favicon() -> Response:
        icon = Path(static_dir or "") / "favicon.svg"
        return FileResponse(icon, media_type="image/svg+xml")

    @app.get("/tiles/{zoom}/{x}/{y}.png", response_model=None)
    async def tile_image(zoom: int, x: int, y: int) -> Response:
        if not (0 <= zoom <= 22 and 0 <= x < 2**zoom and 0 <= y < 2**zoom):
            return Response(status_code=404)
        tile = tile_root / str(zoom) / str(x) / f"{y}.png"
        if not tile.is_file():
            return Response(status_code=404)
        return FileResponse(
            tile, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"}
        )

    if auth is not None:

        @app.get("/api/v1/auth/setup")
        async def setup_status() -> dict[str, bool | int | None]:
            return await auth.setup_status()

        @app.post("/api/v1/auth/login", response_model=None)
        async def login(
            body: LoginBody, request: Request, response: Response
        ) -> dict[str, Any] | Response:
            source = request.client.host if request.client else "unknown"
            result = await auth.login(body.password, source)
            if result is None:
                return JSONResponse(
                    {"error": {"code": "invalid_login", "message": "Invalid credentials."}},
                    status_code=401,
                )
            token, session = result
            response.set_cookie(
                "outpost_session",
                token,
                httponly=True,
                samesite="lax",
                secure=request.url.scheme == "https",
                max_age=auth.session_seconds,
            )
            return {"csrf_token": session.csrf_token, "must_change": session.must_change}

        @app.get("/api/v1/auth/session")
        async def auth_session(request: Request) -> dict[str, Any]:
            session = await auth.session(request.cookies.get("outpost_session"))
            assert session is not None
            return {"authenticated": True, **session.__dict__}

        @app.post("/api/v1/auth/logout")
        async def logout(request: Request, response: Response) -> dict[str, bool]:
            await auth.logout(request.cookies.get("outpost_session"))
            response.delete_cookie("outpost_session")
            return {"ok": True}

        @app.post("/api/v1/auth/password")
        async def password(body: PasswordBody, request: Request) -> Response:
            token = request.cookies.get("outpost_session", "")
            changed = await auth.change_password(token, body.current_password, body.new_password)
            if not changed:
                return JSONResponse(
                    {
                        "error": {
                            "code": "invalid_password",
                            "message": "Credential is invalid or replacement is too short.",
                        }
                    },
                    status_code=400,
                )
            result = JSONResponse({"ok": True, "reauthenticate": True})
            result.delete_cookie("outpost_session")
            return result

    @app.get("/api/v1/health", response_class=JSONResponse, response_model=None)
    async def health() -> dict[str, str] | Response:
        status = status_provider()
        recovery = status.get("recovery")
        if isinstance(recovery, dict) and recovery.get("active") is True:
            return JSONResponse({"status": "maintenance", "version": __version__}, status_code=503)
        radio = status.get("radio", "down")
        tasks_healthy = status.get("tasks_healthy", True) is not False
        return {
            "status": "ok" if radio == "up" and tasks_healthy else "degraded",
            "version": __version__,
        }

    if restore_coordinator is not None:

        @app.get("/api/v1/recovery/restores/{job_id}", response_model=None)
        async def restore_status(job_id: str) -> dict[str, Any] | Response:
            job = restore_coordinator.status(job_id)
            if job is None:
                return JSONResponse(
                    {"error": {"code": "not_found", "message": "Restore job not found."}},
                    status_code=404,
                )
            return job

    if federation is not None:
        if federation_mail_send is not None and database is not None:

            @app.get("/api/v1/federation/mail")
            async def federation_mail_list() -> dict[str, Any]:
                rows = await database.read(
                    "SELECT d.*,p.mesh_id,p.node_name FROM fed_mail_delivery d "
                    "JOIN fed_peer p ON p.id=d.peer_id ORDER BY d.created_at DESC LIMIT 100"
                )
                return {"items": [dict(row) for row in rows]}

            @app.post("/api/v1/federation/mail", response_model=None)
            async def federation_mail_send_route(
                body: FederationMailBody,
            ) -> dict[str, object] | Response:
                try:
                    return await federation_mail_send(
                        body.peer_mesh_id,
                        body.recipient_handle,
                        body.subject,
                        body.body,
                    )
                except ValueError as error:
                    return JSONResponse(
                        {"error": {"code": "mail_relay_failed", "message": str(error)}},
                        status_code=409,
                    )

        @app.get("/api/v1/federation/peers", response_model=None)
        async def federation_peers(state: str | None = None) -> dict[str, Any] | Response:
            allowed = {"pending", "pairing", "active", "paused", "rejected"}
            if state is not None and state not in allowed:
                return JSONResponse(
                    {"error": {"code": "invalid_state", "message": "Unknown peer state."}},
                    status_code=400,
                )
            peers = await federation.list(state)
            return {"items": [peer.__dict__ for peer in peers], "count": len(peers)}

        @app.patch("/api/v1/federation/peers/{mesh_id}", response_model=None)
        async def federation_peer_state(
            mesh_id: str, body: FederationStateBody
        ) -> dict[str, Any] | Response:
            try:
                peer = await federation.set_state(mesh_id, body.state)
            except ValueError as error:
                return JSONResponse(
                    {"error": {"code": "peer_state_failed", "message": str(error)}},
                    status_code=400,
                )
            if database is not None:
                await database.write(
                    "INSERT INTO audit_log(actor_kind,actor_ref,action,target,detail,created_at) "
                    "VALUES('web','operator','federation.peer_state',?,?,unixepoch())",
                    (f"fed_peer:{peer.id}", body.state),
                )
            return peer.__dict__

        @app.delete("/api/v1/federation/peers/{mesh_id}", response_model=None)
        async def federation_peer_forget(mesh_id: str) -> dict[str, bool] | Response:
            try:
                peer = await federation.by_mesh_id(mesh_id)
                await federation.forget(mesh_id)
            except ValueError as error:
                return JSONResponse(
                    {"error": {"code": "peer_forget_failed", "message": str(error)}},
                    status_code=409,
                )
            if database is not None:
                await database.write(
                    "INSERT INTO audit_log(actor_kind,actor_ref,action,target,detail,created_at) "
                    "VALUES('web','operator','federation.peer_forget',?,?,unixepoch())",
                    (f"fed_peer:{peer.mesh_id}", peer.node_name),
                )
            return {"ok": True}

        if database is not None:

            @app.get("/api/v1/federation/origins")
            async def federation_origins() -> dict[str, Any]:
                thread_rows = await database.read("SELECT uid FROM thread WHERE uid LIKE '!%:%'")
                post_rows = await database.read("SELECT uid FROM post WHERE uid LIKE '!%:%'")
                origins: dict[str, dict[str, Any]] = {}
                for kind, rows in (("thread_count", thread_rows), ("post_count", post_rows)):
                    for row in rows:
                        mesh_id = str(row["uid"]).split(":", 1)[0]
                        item = origins.setdefault(
                            mesh_id, {"mesh_id": mesh_id, "thread_count": 0, "post_count": 0}
                        )
                        item[kind] += 1
                peers = {
                    str(row["mesh_id"]): dict(row)
                    for row in await database.read(
                        "SELECT id,mesh_id,node_name,state,last_seen_at FROM fed_peer"
                    )
                }
                successors = {
                    str(row["old_mesh_id"]): dict(row)
                    for row in await database.read(
                        "SELECT s.old_mesh_id,s.old_node_name,s.adopted_at,"
                        "p.mesh_id successor_mesh_id,p.node_name successor_name,"
                        "p.state successor_state FROM fed_peer_successor s "
                        "JOIN fed_peer p ON p.id=s.successor_peer_id"
                    )
                }
                items = []
                for mesh_id, item in origins.items():
                    peer, successor = peers.get(mesh_id), successors.get(mesh_id)
                    item.update(
                        {
                            "node_name": (peer or {}).get("node_name")
                            or (successor or {}).get("old_node_name"),
                            "status": (peer or {}).get("state")
                            or ("successor" if successor else "former"),
                            "successor_mesh_id": (successor or {}).get("successor_mesh_id"),
                            "successor_name": (successor or {}).get("successor_name"),
                            "successor_state": (successor or {}).get("successor_state"),
                        }
                    )
                    items.append(item)
                return {"items": sorted(items, key=lambda item: item["mesh_id"])}

            @app.post("/api/v1/federation/peers/{mesh_id}/adopt-origin", response_model=None)
            async def federation_adopt_origin(
                mesh_id: str, body: FederationSuccessorBody
            ) -> dict[str, Any] | Response:
                try:
                    peer = await federation.by_mesh_id(mesh_id)
                except ValueError as error:
                    return JSONResponse(
                        {"error": {"code": "peer_not_found", "message": str(error)}},
                        status_code=404,
                    )
                if peer.state != "active":
                    return JSONResponse(
                        {
                            "error": {
                                "code": "peer_not_active",
                                "message": ("Pair the successor before adopting history."),
                            }
                        },
                        status_code=409,
                    )
                if body.old_mesh_id.lower() == mesh_id.lower():
                    return JSONResponse(
                        {
                            "error": {
                                "code": "same_identity",
                                "message": ("The predecessor and successor must differ."),
                            }
                        },
                        status_code=409,
                    )
                content = await database.read(
                    "SELECT (SELECT COUNT(*) FROM thread WHERE uid LIKE ?)+"
                    "(SELECT COUNT(*) FROM post WHERE uid LIKE ?) count",
                    (body.old_mesh_id + ":%", body.old_mesh_id + ":%"),
                )
                if not content or int(content[0]["count"]) == 0:
                    return JSONResponse(
                        {
                            "error": {
                                "code": "origin_not_found",
                                "message": ("No retained content uses that predecessor identity."),
                            }
                        },
                        status_code=404,
                    )
                await database.write(
                    "INSERT INTO fed_peer_successor(old_mesh_id,successor_peer_id,"
                    "old_node_name,adopted_at,adopted_by) VALUES(?,?,?,unixepoch(),"
                    "'web:operator') ON CONFLICT(old_mesh_id) DO UPDATE SET "
                    "successor_peer_id=excluded.successor_peer_id,old_node_name=excluded.old_node_name,"
                    "adopted_at=excluded.adopted_at,adopted_by=excluded.adopted_by",
                    (body.old_mesh_id.lower(), peer.id, body.old_node_name),
                )
                await database.write(
                    "INSERT INTO audit_log(actor_kind,actor_ref,action,target,detail,created_at) "
                    "VALUES('web','operator','federation.origin_adopt',?,?,unixepoch())",
                    (f"fed_peer:{peer.id}", body.old_mesh_id.lower()),
                )
                return {
                    "ok": True,
                    "old_mesh_id": body.old_mesh_id.lower(),
                    "successor_mesh_id": mesh_id,
                }

        @app.get("/api/v1/federation/peers/{mesh_id}/pairing-code", response_model=None)
        async def federation_pairing_code(mesh_id: str) -> dict[str, str] | Response:
            try:
                code = await federation.pairing_code(mesh_id)
            except ValueError as error:
                return JSONResponse(
                    {"error": {"code": "code_unavailable", "message": str(error)}},
                    status_code=409,
                )
            return {"confirmation_code": code}

        @app.put("/api/v1/federation/peers/{mesh_id}/sync-policy", response_model=None)
        async def federation_sync_policy(
            mesh_id: str, body: FederationSyncPolicyBody
        ) -> dict[str, Any] | Response:
            try:
                values = body.model_dump()
                review_at = values.pop("policy_review_at")
                peer = await federation.update_sync_policy(
                    mesh_id,
                    **values,
                    policy_review_at=int(review_at.timestamp()) if review_at else None,
                    applied_by="web:operator",
                )
            except ValueError as error:
                return JSONResponse(
                    {"error": {"code": "sync_policy_failed", "message": str(error)}},
                    status_code=409,
                )
            return peer.__dict__

        if database is not None:

            @app.get("/api/v1/federation/sync-status")
            async def federation_sync_status() -> dict[str, Any]:
                peers = await database.read(
                    """SELECT p.id,p.mesh_id,p.node_name,p.state,p.last_sync_at,
                       p.tx_counter,p.rx_counter,p.last_seen_at,
                       p.quota_items_per_hour,p.service_permissions,
                       p.quota_services_per_hour,p.service_concurrency,
                       p.service_max_response_bytes,p.service_airtime_seconds_per_hour,
                       COUNT(CASE WHEN i.state='pending' THEN 1 END) pending_items,
                       COUNT(CASE WHEN i.state='imported' THEN 1 END) imported_items,
                       COUNT(CASE WHEN i.state='rejected' THEN 1 END) rejected_items
                       FROM fed_peer p LEFT JOIN fed_inbox_item i ON i.peer_id=p.id
                       GROUP BY p.id ORDER BY p.last_sync_at DESC"""
                )
                cursors = await database.read(
                    "SELECT peer_id,stream,direction,cursor,updated_at FROM fed_cursor "
                    "ORDER BY updated_at DESC"
                )
                cursor_map: dict[int, list[dict[str, Any]]] = {}
                catchup_map: dict[int, dict[str, Any]] = {}
                for cursor in cursors:
                    cursor_map.setdefault(int(cursor["peer_id"]), []).append(dict(cursor))
                    if cursor["stream"] == "_reconcile" and cursor["direction"] == "recv":
                        try:
                            checkpoint = json.loads(str(cursor["cursor"]))
                        except (TypeError, ValueError):
                            checkpoint = {}
                        before = checkpoint.get("before")
                        catchup_map[int(cursor["peer_id"])] = {
                            "active": bool(checkpoint.get("pending")) or bool(before),
                            "waiting": bool(checkpoint.get("pending")),
                            "snapshot": checkpoint.get("snapshot"),
                            "updated_at": cursor["updated_at"],
                        }
                transfer_map: dict[int, dict[str, Any]] = {}
                for peer in peers:
                    peer_id = int(peer["id"])
                    paths = await database.read(
                        """SELECT transport,COUNT(*) count,MAX(created_at) last_at
                           FROM message_log WHERE airtime_class='federation'
                           AND direction='in' AND peer_mesh_id=?
                           AND outcome<>'rejected'
                           AND created_at>=unixepoch()-86400 GROUP BY transport""",
                        (peer["mesh_id"],),
                    )
                    path_map = {
                        str(path["transport"] or "unknown"): {
                            "count_24h": int(path["count"]),
                            "last_at": path["last_at"],
                        }
                        for path in paths
                    }
                    deliveries = (
                        await database.read(
                            """SELECT COUNT(*) total,
                               COALESCE(SUM(CASE WHEN state<>'delivered' THEN 1 ELSE 0 END),0)
                                 pending,
                               COALESCE(SUM(CASE WHEN state='delivered' THEN 1 ELSE 0 END),0)
                                 delivered,
                               COALESCE(SUM(CASE WHEN attempts>1 THEN attempts-1 ELSE 0 END),0)
                                 retries,
                               COALESCE(SUM(CASE WHEN state='delivered' AND attempts>1
                                 THEN 1 ELSE 0 END),0) recovered,
                               MAX(updated_at) last_attempt_at,MAX(delivered_at) last_delivered_at,
                               COALESCE(SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END),0)
                                 errors
                               FROM fed_post_delivery WHERE peer_id=?""",
                            (peer_id,),
                        )
                    )[0]
                    rejected = await database.read(
                        """SELECT drop_reason,created_at,COUNT(*) OVER() total FROM message_log
                           WHERE airtime_class='federation' AND direction='in'
                           AND peer_mesh_id=? AND outcome='rejected'
                           AND created_at>=unixepoch()-86400
                           ORDER BY created_at DESC LIMIT 5""",
                        (peer["mesh_id"],),
                    )
                    service_usage_rows = await database.read(
                        "SELECT * FROM fed_service_usage WHERE peer_id=? "
                        "ORDER BY window_start DESC LIMIT 1",
                        (peer_id,),
                    )
                    service_usage = (
                        dict(service_usage_rows[0])
                        if service_usage_rows
                        else {
                            "window_start": None,
                            "requests": 0,
                            "denied": 0,
                            "response_bytes": 0,
                            "response_airtime_seconds": 0.0,
                        }
                    )
                    service_circuits = await database.read(
                        "SELECT service,consecutive_failures,open_until,updated_at "
                        "FROM fed_service_circuit WHERE peer_id=? ORDER BY service",
                        (peer_id,),
                    )
                    transfer_map[peer_id] = {
                        "paths": {
                            "radio": path_map.get("radio", {"count_24h": 0, "last_at": None}),
                            "mqtt": path_map.get("mqtt", {"count_24h": 0, "last_at": None}),
                            "unknown": path_map.get("unknown", {"count_24h": 0, "last_at": None}),
                        },
                        "deliveries": dict(deliveries),
                        "security": {
                            "rejected_24h": int(rejected[0]["total"]) if rejected else 0,
                            "recent": [
                                {
                                    "reason": row["drop_reason"],
                                    "created_at": row["created_at"],
                                }
                                for row in rejected
                            ],
                        },
                        "catch_up": catchup_map.get(
                            peer_id,
                            {
                                "active": False,
                                "waiting": False,
                                "snapshot": None,
                                "updated_at": None,
                            },
                        ),
                        "services": {
                            "permissions": json.loads(peer["service_permissions"]),
                            "request_limit": int(peer["quota_services_per_hour"]),
                            "concurrency_limit": int(peer["service_concurrency"]),
                            "response_byte_limit": int(peer["service_max_response_bytes"]),
                            "airtime_limit_seconds": float(
                                peer["service_airtime_seconds_per_hour"]
                            ),
                            "usage": service_usage,
                            "circuits": [dict(row) for row in service_circuits],
                        },
                    }
                outbound = (
                    await database.read(
                        """SELECT COUNT(*) frames_24h,MAX(created_at) last_at
                           FROM message_log WHERE airtime_class='federation'
                           AND direction='out' AND created_at>=unixepoch()-86400"""
                    )
                )[0]
                return {
                    "items": [
                        {
                            **dict(peer),
                            "service_permissions": json.loads(peer["service_permissions"]),
                            "cursors": cursor_map.get(int(peer["id"]), []),
                            "transfers": transfer_map[int(peer["id"])],
                        }
                        for peer in peers
                    ],
                    "outbound": dict(outbound),
                }

            @app.get("/api/v1/federation/inbox")
            async def federation_inbox(state: str = "pending") -> dict[str, Any]:
                rows = await database.read(
                    "SELECT i.*,p.mesh_id,p.node_name FROM fed_inbox_item i "
                    "JOIN fed_peer p ON p.id=i.peer_id WHERE i.state=? "
                    "ORDER BY i.received_at DESC LIMIT 100",
                    (state,),
                )
                items = []
                for row in rows:
                    item = dict(row)
                    item["payload"] = json.loads(item.pop("payload_json"))
                    items.append(item)
                return {"items": items}

            @app.patch("/api/v1/federation/inbox/{item_id}", response_model=None)
            async def federation_inbox_reject(
                item_id: int, body: FederationInboxBody
            ) -> dict[str, str] | Response:
                rows = await database.read(
                    "SELECT id FROM fed_inbox_item WHERE id=? AND state='pending'", (item_id,)
                )
                if not rows:
                    return JSONResponse(
                        {"error": {"code": "not_found", "message": "Pending item not found."}},
                        status_code=404,
                    )
                if body.state == "imported":
                    if federation_inbox_import is None:
                        return JSONResponse(
                            {"error": {"code": "unavailable", "message": "Import unavailable."}},
                            status_code=409,
                        )
                    try:
                        stream = await federation_inbox_import(item_id)
                    except (KeyError, TypeError, ValueError) as error:
                        return JSONResponse(
                            {"error": {"code": "import_failed", "message": str(error)}},
                            status_code=409,
                        )
                    return {"state": "imported", "stream": stream}
                await database.write(
                    "UPDATE fed_inbox_item SET state='rejected',reviewed_at=unixepoch(),"
                    "reviewed_by='web:operator',rejection_reason=? WHERE id=?",
                    (body.reason, item_id),
                )
                return {"state": "rejected"}

        if federation_pair is not None:

            @app.post("/api/v1/federation/peers/{mesh_id}/pair", response_model=None)
            async def federation_pair_peer(mesh_id: str) -> dict[str, Any] | Response:
                try:
                    peer = await federation_pair(mesh_id)
                except ValueError as error:
                    return JSONResponse(
                        {"error": {"code": "pairing_failed", "message": str(error)}},
                        status_code=400,
                    )
                return peer.__dict__

        if federation_approve is not None:

            @app.post("/api/v1/federation/peers/{mesh_id}/approve", response_model=None)
            async def federation_approve_peer(
                mesh_id: str, body: FederationApproveBody
            ) -> dict[str, Any] | Response:
                try:
                    peer = await federation_approve(mesh_id, body.confirmation_code)
                except ValueError as error:
                    return JSONResponse(
                        {"error": {"code": "approval_failed", "message": str(error)}},
                        status_code=400,
                    )
                return peer.__dict__

        if federation_mqtt_status is not None and federation_mqtt_configure is not None:

            @app.get("/api/v1/federation/mqtt")
            async def federation_mqtt_view() -> dict[str, Any]:
                return await federation_mqtt_status()

            @app.put("/api/v1/federation/mqtt", response_model=None)
            async def federation_mqtt_update(
                body: FederationMqttBody,
            ) -> dict[str, Any] | Response:
                try:
                    return await federation_mqtt_configure(**body.model_dump())
                except (ConnectionError, ValueError) as error:
                    return JSONResponse(
                        {"error": {"code": "mqtt_config_failed", "message": str(error)}},
                        status_code=409,
                    )

        if federation_service_list is not None and federation_service_query is not None:

            @app.get("/api/v1/federation/services")
            async def federation_services() -> dict[str, Any]:
                return {"items": await federation_service_list()}

            @app.post("/api/v1/federation/services", response_model=None)
            async def federation_service_request(
                body: FederationServiceBody,
            ) -> dict[str, object] | Response:
                args = body.model_dump(exclude_none=True)
                args.pop("service", None)
                try:
                    return await federation_service_query(body.service, args)
                except ValueError as error:
                    return JSONResponse(
                        {"error": {"code": "service_unavailable", "message": str(error)}},
                        status_code=409,
                    )

    @app.get("/api/v1/status")
    async def status() -> dict[str, Any]:
        return status_provider()

    @app.get("/api/v1/modules")
    async def modules() -> dict[str, Any]:
        return {
            "items": {
                name: {"enabled": enabled, "restart_required_to_change": True}
                for name, enabled in effective_modules().items()
            },
            "change_policy": "restart_required",
        }

    if database is not None:
        if settings is not None:

            @app.get("/api/v1/config")
            async def config_view() -> dict[str, Any]:
                return settings.redacted()

            @app.patch("/api/v1/config/node", response_model=None)
            async def config_node(body: NodeSettingsBody) -> dict[str, Any] | Response:
                values = body.model_dump(exclude_none=True)
                if not values:
                    return JSONResponse(
                        {"error": {"code": "empty_update", "message": "No settings supplied."}},
                        status_code=400,
                    )
                try:
                    node = await settings.update_node(values)
                except ValueError as error:
                    return JSONResponse(
                        {"error": {"code": "invalid_config", "message": str(error)}},
                        status_code=422,
                    )
                return {"node": node}

            @app.patch("/api/v1/config/watch", response_model=None)
            async def config_watch(body: WatchSettingsBody) -> dict[str, Any] | Response:
                values = body.model_dump(exclude_none=True)
                if not values:
                    return JSONResponse(
                        {"error": {"code": "empty_update", "message": "No settings supplied."}},
                        status_code=400,
                    )
                try:
                    watch = await settings.update_watch(values)
                except ValueError as error:
                    return JSONResponse(
                        {"error": {"code": "invalid_config", "message": str(error)}},
                        status_code=422,
                    )
                return {"watch": watch, "applied": "immediately"}

            if weather is not None:

                @app.get("/api/v1/environment/weather", response_model=None)
                async def environment_weather(refresh: bool = False) -> dict[str, Any] | Response:
                    location = settings.config.node.location
                    if location is None:
                        return JSONResponse(
                            {
                                "error": {
                                    "code": "location_required",
                                    "message": "Set Outpost latitude and longitude first.",
                                }
                            },
                            status_code=409,
                        )
                    try:
                        value = await weather.current(location.lat, location.lon, refresh=refresh)
                    except RuntimeError as error:
                        return JSONResponse(
                            {"error": {"code": "weather_unavailable", "message": str(error)}},
                            status_code=503,
                        )
                    return {**value.json(), "units": settings.config.node.units}

                @app.get("/api/v1/environment/providers")
                async def environment_providers() -> dict[str, Any]:
                    return {"items": weather.provider_health()}

                if astronomy is not None:

                    @app.get("/api/v1/environment/astronomy", response_model=None)
                    async def environment_astronomy() -> dict[str, Any] | Response:
                        location = settings.config.node.location
                        if location is None:
                            return JSONResponse(
                                {
                                    "error": {
                                        "code": "location_required",
                                        "message": "Set Outpost latitude and longitude first.",
                                    }
                                },
                                status_code=409,
                            )
                        try:
                            value = astronomy.current(
                                location.lat, location.lon, settings.config.node.timezone
                            )
                        except (KeyError, ValueError):
                            return JSONResponse(
                                {
                                    "error": {
                                        "code": "timezone_invalid",
                                        "message": "The configured timezone is invalid.",
                                    }
                                },
                                status_code=422,
                            )
                        return value.json()

                @app.get("/api/v1/environment/forecast", response_model=None)
                async def environment_forecast(refresh: bool = False) -> dict[str, Any] | Response:
                    location = settings.config.node.location
                    if location is None:
                        return JSONResponse(
                            {
                                "error": {
                                    "code": "location_required",
                                    "message": "Set Outpost latitude and longitude first.",
                                }
                            },
                            status_code=409,
                        )
                    try:
                        value = await weather.forecast(location.lat, location.lon, refresh=refresh)
                    except RuntimeError as error:
                        return JSONResponse(
                            {"error": {"code": "forecast_unavailable", "message": str(error)}},
                            status_code=503,
                        )
                    return {**value.json(), "units": settings.config.node.units}

                if seismic is not None:

                    @app.get("/api/v1/environment/earthquakes")
                    async def environment_earthquakes(hours: int = 24) -> dict[str, Any]:
                        return {
                            "items": await seismic.list(max(1, min(hours, 168))),
                            "health": seismic.health(),
                            "radius_km": settings.config.env.earthquake_radius_km,
                        }

                    @app.post("/api/v1/environment/earthquakes/refresh", response_model=None)
                    async def environment_earthquake_refresh() -> dict[str, Any] | Response:
                        location = settings.config.node.location
                        if location is None:
                            return JSONResponse(
                                {
                                    "error": {
                                        "code": "location_required",
                                        "message": "Set Outpost location first.",
                                    }
                                },
                                status_code=409,
                            )
                        try:
                            return await seismic.poll(location.lat, location.lon)
                        except OSError as error:
                            return JSONResponse(
                                {"error": {"code": "provider_unavailable", "message": str(error)}},
                                status_code=503,
                            )

                    @app.post(
                        "/api/v1/environment/earthquakes/{quake_id}/approve", response_model=None
                    )
                    async def environment_earthquake_approve(
                        quake_id: int,
                    ) -> dict[str, Any] | Response:
                        try:
                            return await seismic.approve(quake_id, alerts)
                        except ValueError as error:
                            return JSONResponse(
                                {"error": {"code": "not_eligible", "message": str(error)}},
                                status_code=422,
                            )

                    @app.post(
                        "/api/v1/environment/earthquakes/{quake_id}/dismiss", response_model=None
                    )
                    async def environment_earthquake_dismiss(
                        quake_id: int,
                    ) -> dict[str, str] | Response:
                        try:
                            await seismic.dismiss(quake_id)
                        except ValueError as error:
                            return JSONResponse(
                                {"error": {"code": "not_pending", "message": str(error)}},
                                status_code=422,
                            )
                        return {"status": "dismissed"}

                if waypoints is not None:

                    @app.get("/api/v1/environment/waypoints")
                    async def environment_waypoints() -> dict[str, Any]:
                        return {"items": await waypoints.list()}

                    @app.post("/api/v1/environment/waypoints", response_model=None)
                    async def environment_waypoint_create(
                        body: WaypointBody,
                    ) -> dict[str, Any] | Response:
                        try:
                            return await waypoints.create(
                                body.name, body.latitude, body.longitude, body.category, body.notes
                            )
                        except ValueError as error:
                            return JSONResponse(
                                {"error": {"code": "invalid_waypoint", "message": str(error)}},
                                status_code=422,
                            )

                    @app.patch("/api/v1/environment/waypoints/{waypoint_id}", response_model=None)
                    async def environment_waypoint_update(
                        waypoint_id: int, body: WaypointPatchBody
                    ) -> dict[str, Any] | Response:
                        try:
                            return await waypoints.update(
                                waypoint_id, body.model_dump(exclude_none=True)
                            )
                        except ValueError as error:
                            return JSONResponse(
                                {"error": {"code": "invalid_waypoint", "message": str(error)}},
                                status_code=422,
                            )

                    @app.delete("/api/v1/environment/waypoints/{waypoint_id}", response_model=None)
                    async def environment_waypoint_delete(
                        waypoint_id: int,
                    ) -> dict[str, str] | Response:
                        try:
                            await waypoints.delete(waypoint_id)
                        except ValueError as error:
                            return JSONResponse(
                                {"error": {"code": "not_found", "message": str(error)}},
                                status_code=404,
                            )
                        return {"status": "deleted"}

        if reconnect_radio is not None:

            @app.post("/api/v1/radio/reconnect")
            async def radio_reconnect() -> dict[str, str]:
                await reconnect_radio()
                await database.write(
                    """
                    INSERT INTO audit_log(actor_kind,actor_ref,action,target,detail,created_at)
                    VALUES('web','operator','radio.reconnect','radio',NULL,unixepoch())
                    """
                )
                return {"status": "reconnecting"}

        if backups is not None:

            @app.get("/api/v1/backups")
            async def backup_list() -> dict[str, Any]:
                return {"items": backups.list()}

            @app.post("/api/v1/backups")
            async def backup_create() -> dict[str, Any]:
                path = await backups.create()
                await database.write(
                    """
                    INSERT INTO audit_log(actor_kind,actor_ref,action,target,detail,created_at)
                    VALUES('web','operator','backup.create',?,NULL,unixepoch())
                    """,
                    (path.name,),
                )
                return {"backup": backups.list()[0]}

            @app.get("/api/v1/backups/{name}", response_class=FileResponse)
            async def backup_download(name: str) -> Response:
                path = backups.resolve(name)
                if path is None:
                    return JSONResponse(
                        {"error": {"code": "not_found", "message": "Backup not found."}},
                        status_code=404,
                    )
                return FileResponse(
                    path,
                    filename=path.name,
                    media_type="application/x-sqlite3",
                    headers={"X-Outpost-Data-Classification": "sensitive-includes-location-data"},
                )

            @app.get("/api/v1/backups/{name}/validate", response_model=None)
            async def backup_validate(name: str) -> dict[str, object] | Response:
                try:
                    return await backups.validate(name)
                except (ValueError, RuntimeError) as error:
                    return JSONResponse(
                        {"error": {"code": "invalid_backup", "message": str(error)}},
                        status_code=422,
                    )

            @app.post("/api/v1/backups/{name}/restore", response_model=None)
            async def backup_restore(name: str, body: RestoreBody) -> dict[str, object] | Response:
                if restore_coordinator is None:
                    return JSONResponse(
                        {
                            "error": {
                                "code": "restore_unavailable",
                                "message": "Application recovery coordination is unavailable.",
                            }
                        },
                        status_code=503,
                    )
                try:
                    job = await restore_coordinator.schedule(name, body.confirmation)
                except (ValueError, RuntimeError) as error:
                    return JSONResponse(
                        {"error": {"code": "restore_rejected", "message": str(error)}},
                        status_code=422,
                    )
                return JSONResponse(job, status_code=202)

        if incidents is not None:

            async def incident_origins(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
                assert database is not None
                peers = {
                    str(row["mesh_id"]): str(row["node_name"] or row["mesh_id"])
                    for row in await database.read("SELECT mesh_id,node_name FROM fed_peer")
                }
                for value in values:
                    uid = str(value.get("uid", ""))
                    mesh_id = uid.split(":", 1)[0] if uid.startswith("!") and ":" in uid else None
                    value["remote"] = mesh_id is not None
                    value["origin_mesh_id"] = mesh_id
                    value["origin_name"] = peers.get(mesh_id, mesh_id) if mesh_id else None
                return values

            @app.get("/api/v1/incidents")
            async def incident_list(
                status: str | None = None,
                type: str | None = None,
                limit: int = Query(50, ge=1, le=200),
            ) -> dict[str, Any]:
                values = await incidents.list(status=status, kind=type, limit=limit)
                return {"items": await incident_origins([value.json() for value in values])}

            @app.get("/api/v1/watch/map")
            async def watch_map(hours_ago: int = Query(0, ge=0, le=24)) -> dict[str, Any]:
                assert database is not None
                cutoff = int(datetime.now(UTC).timestamp()) - hours_ago * 3600
                incident_rows = await database.read(
                    """
                    SELECT * FROM incident
                    WHERE lat IS NOT NULL AND lon IS NOT NULL AND created_at<=?
                      AND (
                        status IN ('open','monitoring')
                        OR (status='resolved' AND resolved_at>=?)
                        OR (status='expired' AND expires_at>=?)
                      )
                    ORDER BY updated_at DESC
                    """,
                    (cutoff, cutoff, cutoff),
                )
                event_rows = await database.read(
                    "SELECT id,name,opened_at FROM watch_event "
                    "WHERE opened_at<=? AND (closed_at IS NULL OR closed_at>=?) "
                    "ORDER BY id DESC LIMIT 1",
                    (cutoff, cutoff),
                )
                event = dict(event_rows[0]) if event_rows else None
                node_rows = await database.read(
                    """
                    SELECT m.id,m.mesh_id,m.handle,m.trust,p.lat,p.lon,p.received_at
                    FROM member m JOIN member_position p ON p.member_id=m.id
                    WHERE m.trust IN ('member','trusted','responder','operator')
                      AND p.received_at<=? AND p.expires_at>?
                    ORDER BY COALESCE(m.handle,m.mesh_id)
                    """,
                    (cutoff, int(datetime.now(UTC).timestamp())),
                )
                alert_rows = await database.read(
                    """SELECT a.*,i.local_ref AS incident_ref,
                              (SELECT COUNT(*) FROM alert_ack aa WHERE aa.alert_id=a.id)
                                AS ack_count
                       FROM alert a LEFT JOIN incident i ON i.id=a.incident_id
                       WHERE a.lat IS NOT NULL AND a.lon IS NOT NULL AND a.radius_m IS NOT NULL
                         AND a.raised_at<=?
                         AND (a.cancelled_at IS NULL OR a.cancelled_at>=?)
                         AND (a.expires_at IS NULL OR a.expires_at>=?)
                       ORDER BY a.raised_at DESC""",
                    (cutoff, cutoff, cutoff),
                )
                nodes: list[dict[str, Any]] = []
                for row in node_rows:
                    node = dict(row)
                    node["status"] = None
                    node["note"] = None
                    if event is not None:
                        checkin_rows = await database.read(
                            "SELECT status,note,lat,lon,created_at FROM checkin "
                            "WHERE event_id=? AND member_id=? AND created_at<=? "
                            "ORDER BY created_at DESC LIMIT 1",
                            (event["id"], node["id"], cutoff),
                        )
                        if checkin_rows:
                            checkin = dict(checkin_rows[0])
                            node.update(
                                {
                                    "status": checkin["status"],
                                    "note": checkin["note"],
                                    "lat": (
                                        checkin["lat"]
                                        if checkin["lat"] is not None
                                        else node["lat"]
                                    ),
                                    "lon": (
                                        checkin["lon"]
                                        if checkin["lon"] is not None
                                        else node["lon"]
                                    ),
                                    "position_at": checkin["created_at"],
                                }
                            )
                        else:
                            node["status"] = "unaccounted"
                    nodes.append(node)
                incident_values = await incident_origins([dict(row) for row in incident_rows])
                return {
                    "at": cutoff,
                    "hours_ago": hours_ago,
                    "event": event,
                    "incidents": incident_values,
                    "nodes": nodes,
                    "alerts": [dict(row) for row in alert_rows],
                }

            @app.post("/api/v1/incidents", response_model=None)
            async def incident_create(body: IncidentCreateBody) -> dict[str, Any] | Response:
                try:
                    created, similar = await incidents.create(
                        body.text, None, force=body.force, operator_label="web:operator"
                    )
                except ValueError as error:
                    return JSONResponse(
                        {"error": {"code": "invalid_incident", "message": str(error)}},
                        status_code=422,
                    )
                if similar:
                    return JSONResponse({"similar": similar.json()}, status_code=409)
                assert created is not None
                await database.write(
                    "INSERT INTO audit_log(actor_kind,actor_ref,action,target,detail,created_at) "
                    "VALUES('web','operator','incident.create',?,?,unixepoch())",
                    (f"incident:{created.id}", created.title),
                )
                return created.json()

            @app.get("/api/v1/incidents/{incident_id}", response_model=None)
            async def incident_detail(incident_id: int) -> dict[str, Any] | Response:
                value = await incidents.by_id(incident_id)
                if value is None:
                    return JSONResponse(
                        {"error": {"code": "not_found", "message": "Incident not found."}},
                        status_code=404,
                    )
                return {**value.json(), "updates": await incidents.updates(value.id, 100)}

            @app.patch("/api/v1/incidents/{incident_id}", response_model=None)
            async def incident_patch(
                incident_id: int, body: IncidentPatchBody
            ) -> dict[str, Any] | Response:
                value = await incidents.by_id(incident_id)
                if value is None:
                    return JSONResponse(
                        {"error": {"code": "not_found", "message": "Incident not found."}},
                        status_code=404,
                    )
                changes = body.model_dump(exclude_none=True)
                if not changes:
                    return JSONResponse(
                        {"error": {"code": "empty_update", "message": "No changes supplied."}},
                        status_code=400,
                    )
                assignments, params = [], []
                if body.status:
                    assignments.append("status=?")
                    params.append(body.status)
                    if body.status in {"resolved", "false_alarm"}:
                        assignments.extend(
                            ["resolved_at=unixepoch()", "resolved_by='web:operator'"]
                        )
                if body.severity:
                    assignments.append("severity=?")
                    params.append(body.severity)
                if body.resolution is not None:
                    assignments.append("resolution_note=?")
                    params.append(body.resolution[:500])
                assignments.append("updated_at=unixepoch()")
                params.append(incident_id)
                await database.write(
                    f"UPDATE incident SET {','.join(assignments)} WHERE id=?",  # noqa: S608
                    tuple(params),
                )
                await database.write(
                    "INSERT INTO audit_log(actor_kind,actor_ref,action,target,detail,created_at) "
                    "VALUES('web','operator','incident.update',?,?,unixepoch())",
                    (f"incident:{incident_id}", ",".join(changes)),
                )
                updated = await incidents.by_id(incident_id)
                assert updated is not None
                return updated.json()

            @app.post("/api/v1/incidents/{incident_id}/updates", response_model=None)
            async def incident_update(
                incident_id: int, body: IncidentUpdateBody
            ) -> dict[str, Any] | Response:
                try:
                    value = await incidents.operator_update(incident_id, body.kind, body.note)
                except ValueError as error:
                    return JSONResponse(
                        {"error": {"code": "invalid_update", "message": str(error)}},
                        status_code=422,
                    )
                await database.write(
                    "INSERT INTO audit_log(actor_kind,actor_ref,action,target,detail,created_at) "
                    "VALUES('web','operator',?,?,?,unixepoch())",
                    (
                        f"incident.{body.kind}",
                        f"incident:{incident_id}",
                        body.note[:500] or None,
                    ),
                )
                return value.json()

        if alerts is not None:

            @app.get("/api/v1/alerts")
            async def alert_list(active: bool = True) -> dict[str, Any]:
                return {
                    "items": [
                        await alerts.operational_json(item) for item in await alerts.list(active)
                    ]
                }

            @app.post("/api/v1/alerts", response_model=None)
            async def alert_create(body: AlertCreateBody) -> dict[str, Any] | Response:
                try:
                    value = await alerts.raise_alert(
                        body.severity,
                        body.headline,
                        "web:operator",
                        incident_ref=body.incident_ref,
                        channels=body.channels,
                        source="incident" if body.incident_ref else "operator",
                        lat=body.lat,
                        lon=body.lon,
                        radius_km=body.radius_km,
                    )
                except ValueError as error:
                    return JSONResponse(
                        {"error": {"code": "invalid_alert", "message": str(error)}},
                        status_code=422,
                    )
                await database.write(
                    "INSERT INTO audit_log(actor_kind,actor_ref,action,target,detail,created_at) "
                    "VALUES('web','operator','alert.raise',?,?,unixepoch())",
                    (f"alert:{value.id}", value.headline),
                )
                return value.json()

            @app.post("/api/v1/alerts/{alert_id}/cancel", response_model=None)
            async def alert_cancel(
                alert_id: int, body: AlertCancelBody
            ) -> dict[str, Any] | Response:
                try:
                    value = await alerts.cancel(alert_id, body.resolution, "web:operator")
                except ValueError as error:
                    return JSONResponse(
                        {"error": {"code": "invalid_alert", "message": str(error)}},
                        status_code=422,
                    )
                await database.write(
                    "INSERT INTO audit_log(actor_kind,actor_ref,action,target,detail,created_at) "
                    "VALUES('web','operator','alert.cancel',?,?,unixepoch())",
                    (f"alert:{value.id}", body.resolution[:160]),
                )
                return await alerts.operational_json(value)

            @app.post("/api/v1/alerts/{alert_id}/halt", response_model=None)
            async def alert_halt(alert_id: int) -> dict[str, Any] | Response:
                try:
                    value = await alerts.halt_escalation(alert_id)
                except ValueError as error:
                    return JSONResponse(
                        {"error": {"code": "invalid_alert", "message": str(error)}},
                        status_code=422,
                    )
                await database.write(
                    "INSERT INTO audit_log(actor_kind,actor_ref,action,target,created_at) "
                    "VALUES('web','operator','alert.escalation_halt',?,unixepoch())",
                    (f"alert:{value.id}",),
                )
                return await alerts.operational_json(value)

            @app.get("/api/v1/alerts/{alert_id}/acks", response_model=None)
            async def alert_acks(alert_id: int) -> dict[str, Any] | Response:
                if await alerts.by_id(alert_id) is None:
                    return JSONResponse(
                        {"error": {"code": "not_found", "message": "Alert not found."}},
                        status_code=404,
                    )
                rows = await database.read(
                    """SELECT m.mesh_id,m.handle,aa.acked_at,aa.note FROM alert_ack aa
                       JOIN member m ON m.id=aa.member_id WHERE aa.alert_id=?
                       ORDER BY aa.acked_at""",
                    (alert_id,),
                )
                return {"items": [dict(row) for row in rows]}

            if cap_alerts is not None:

                @app.get("/api/v1/environment/alerts")
                async def environment_alerts(include_expired: bool = False) -> dict[str, Any]:
                    return {
                        "items": await cap_alerts.list(include_expired=include_expired),
                        "health": cap_alerts.health(),
                    }

                @app.post("/api/v1/environment/alerts/refresh", response_model=None)
                async def environment_alert_refresh() -> dict[str, Any] | Response:
                    location = settings.config.node.location
                    if location is None:
                        return JSONResponse(
                            {
                                "error": {
                                    "code": "location_required",
                                    "message": "Set Outpost location first.",
                                }
                            },
                            status_code=409,
                        )
                    try:
                        return await cap_alerts.poll(location.lat, location.lon)
                    except OSError as error:
                        return JSONResponse(
                            {"error": {"code": "provider_unavailable", "message": str(error)}},
                            status_code=503,
                        )

                @app.post("/api/v1/environment/alerts/{cap_id}/approve", response_model=None)
                async def environment_alert_approve(cap_id: int) -> dict[str, Any] | Response:
                    try:
                        value = await cap_alerts.approve(cap_id, alerts)
                    except ValueError as error:
                        return JSONResponse(
                            {"error": {"code": "not_eligible", "message": str(error)}},
                            status_code=422,
                        )
                    await database.write(
                        "INSERT INTO audit_log(actor_kind,actor_ref,action,target,created_at) "
                        "VALUES('web','operator','cap.approve',?,unixepoch())",
                        (f"cap:{cap_id}",),
                    )
                    return value

                @app.post("/api/v1/environment/alerts/{cap_id}/dismiss", response_model=None)
                async def environment_alert_dismiss(cap_id: int) -> dict[str, str] | Response:
                    try:
                        await cap_alerts.dismiss(cap_id)
                    except ValueError as error:
                        return JSONResponse(
                            {"error": {"code": "not_pending", "message": str(error)}},
                            status_code=422,
                        )
                    return {"status": "dismissed"}

        if checkins is not None:

            @app.get("/api/v1/events")
            async def event_list() -> dict[str, Any]:
                values = await checkins.events()
                current = await checkins.current_event()
                return {
                    "items": [value.json() for value in values],
                    "current": current.json() if current else None,
                }

            @app.post("/api/v1/events", response_model=None)
            async def event_create(body: EventCreateBody) -> dict[str, Any] | Response:
                try:
                    value = await checkins.open_event(body.name, body.roster_policy, "web:operator")
                except ValueError as error:
                    return JSONResponse(
                        {"error": {"code": "invalid_event", "message": str(error)}},
                        status_code=422,
                    )
                await database.write(
                    "INSERT INTO audit_log(actor_kind,actor_ref,action,target,detail,created_at) "
                    "VALUES('web','operator','event.open',?,?,unixepoch())",
                    (f"event:{value.id}", value.name),
                )
                return value.json()

            @app.post("/api/v1/events/{event_id}/close", response_model=None)
            async def event_close(event_id: int) -> dict[str, Any] | Response:
                try:
                    value = await checkins.close_event(event_id)
                except ValueError as error:
                    return JSONResponse(
                        {"error": {"code": "invalid_event", "message": str(error)}},
                        status_code=422,
                    )
                await database.write(
                    "INSERT INTO audit_log(actor_kind,actor_ref,action,target,detail,created_at) "
                    "VALUES('web','operator','event.close',?,?,unixepoch())",
                    (f"event:{value.id}", value.name),
                )
                return value.json()

            @app.get("/api/v1/events/{event_id}/roster", response_model=None)
            async def event_roster(event_id: int) -> dict[str, Any] | Response:
                try:
                    return await checkins.summary(event_id)
                except ValueError as error:
                    return JSONResponse(
                        {"error": {"code": "not_found", "message": str(error)}},
                        status_code=404,
                    )

            @app.get("/api/v1/events/{event_id}/roster.csv", response_model=None)
            async def event_roster_csv(event_id: int) -> Response:
                try:
                    content = await checkins.csv_export(event_id)
                except ValueError as error:
                    return JSONResponse(
                        {"error": {"code": "not_found", "message": str(error)}},
                        status_code=404,
                    )
                return Response(
                    content,
                    media_type="text/csv",
                    headers={
                        "Content-Disposition": (
                            f'attachment; filename="outpost-roster-{event_id}.csv"'
                        ),
                        "X-Outpost-Data-Classification": (
                            "sensitive-may-include-member-location-data"
                        ),
                    },
                )

            @app.get("/api/v1/events/{event_id}/solicitation-preview", response_model=None)
            async def event_solicitation_preview(
                event_id: int,
            ) -> dict[str, Any] | Response:
                try:
                    recipients = await checkins.solicitation_preview(event_id)
                except ValueError as error:
                    return JSONResponse(
                        {"error": {"code": "not_found", "message": str(error)}},
                        status_code=404,
                    )
                return {
                    "send_enabled": bool(recipients),
                    "recipient_count": len(recipients),
                    "recipients": recipients,
                    "message": checkins.solicitation_message(event)
                    if (event := await checkins.by_id(event_id))
                    else "",
                }

            @app.post("/api/v1/events/{event_id}/solicit", response_model=None)
            async def event_solicit(
                event_id: int, body: EventSolicitBody
            ) -> dict[str, Any] | Response:
                if body.confirmation != f"QUEUE {event_id}":
                    return JSONResponse(
                        {
                            "error": {
                                "code": "confirmation_required",
                                "message": f"Confirmation must be QUEUE {event_id}.",
                            }
                        },
                        status_code=422,
                    )
                try:
                    result = await checkins.solicit(event_id)
                except ValueError as error:
                    return JSONResponse(
                        {"error": {"code": "solicitation_rejected", "message": str(error)}},
                        status_code=422,
                    )
                await database.write(
                    "INSERT INTO audit_log(actor_kind,actor_ref,action,target,detail,created_at) "
                    "VALUES('web','operator','event.solicit',?,?,unixepoch())",
                    (f"event:{event_id}", f"recipients:{result['recipient_count']}"),
                )
                return result

        @app.get("/api/v1/boards")
        async def boards(
            cursor: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200)
        ) -> dict[str, Any]:
            rows = await database.read(
                """
                SELECT b.id,b.slug,b.title,b.description,b.min_read_trust,b.min_post_trust,
                       b.retention_days,b.sort_order,b.archived,b.federated,
                       COUNT(t.id) AS thread_count
                FROM board b LEFT JOIN thread t ON t.board_id=b.id AND t.hidden=0
                WHERE b.archived=0 GROUP BY b.id ORDER BY b.sort_order,b.id LIMIT ? OFFSET ?
                """,
                (limit + 1, cursor),
            )
            return {
                "items": [dict(row) for row in rows[:limit]],
                "next_cursor": cursor + limit if len(rows) > limit else None,
            }

        if bbs_admin is not None:

            async def apply_board_federation_policy(board_id: int, enabled: bool) -> None:
                if federation is None:
                    return
                board_rows = await database.read("SELECT slug FROM board WHERE id=?", (board_id,))
                if not board_rows:
                    return
                slug = str(board_rows[0]["slug"])
                for peer in await federation.list("active"):
                    boards = list(peer.boards)
                    if enabled and slug not in boards:
                        boards.append(slug)
                    elif not enabled and slug in boards:
                        boards.remove(slug)
                    else:
                        continue
                    await federation.update_sync_policy(
                        peer.mesh_id,
                        boards=boards,
                        sync_incidents=peer.sync_incidents,
                        incident_lat=peer.incident_lat,
                        incident_lon=peer.incident_lon,
                        incident_radius_km=peer.incident_radius_km,
                        relay_alerts=peer.relay_alerts,
                        quota_items_per_hour=peer.quota_items_per_hour,
                        relay_mail=peer.relay_mail,
                        quota_mail_per_hour=peer.quota_mail_per_hour,
                        service_permissions=peer.service_permissions,
                        quota_services_per_hour=peer.quota_services_per_hour,
                        service_concurrency=peer.service_concurrency,
                        service_max_response_bytes=peer.service_max_response_bytes,
                        service_airtime_seconds_per_hour=peer.service_airtime_seconds_per_hour,
                    )
                await database.write(
                    "INSERT INTO audit_log(actor_kind,actor_ref,action,target,detail,created_at) "
                    "VALUES('web','operator','federation.board_policy',?,?,unixepoch())",
                    (f"board:{board_id}", json.dumps({"slug": slug, "enabled": enabled})),
                )

            @app.post("/api/v1/boards", response_model=None)
            async def board_create(body: BoardCreateBody) -> dict[str, int] | Response:
                try:
                    board_id = await bbs_admin.create_board(body.model_dump())
                except ValueError as error:
                    return JSONResponse(
                        {"error": {"code": "invalid_board", "message": str(error)}},
                        status_code=422,
                    )
                if body.federated:
                    await apply_board_federation_policy(board_id, True)
                return {"id": board_id}

            @app.patch("/api/v1/boards/{board_id}", response_model=None)
            async def board_patch(
                board_id: int, body: BoardPatchBody
            ) -> dict[str, bool] | Response:
                try:
                    found = await bbs_admin.update_board(
                        board_id, body.model_dump(exclude_none=True)
                    )
                except ValueError as error:
                    return JSONResponse(
                        {"error": {"code": "invalid_board", "message": str(error)}},
                        status_code=422,
                    )
                if not found:
                    return JSONResponse(
                        {"error": {"code": "not_found", "message": "Board not found."}},
                        status_code=404,
                    )
                if body.federated is not None:
                    await apply_board_federation_policy(board_id, body.federated)
                return {"ok": True}

            @app.post("/api/v1/boards/{board_id}/threads", response_model=None)
            async def thread_create(
                board_id: int, body: ThreadCreateBody
            ) -> dict[str, int] | Response:
                try:
                    thread_id = await bbs_admin.create_thread(board_id, body.subject, body.body)
                except ValueError as error:
                    return JSONResponse(
                        {"error": {"code": "invalid_thread", "message": str(error)}},
                        status_code=422,
                    )
                return {"id": thread_id}

            @app.post("/api/v1/threads/{thread_id}/posts", response_model=None)
            async def reply_create(
                thread_id: int, body: ReplyCreateBody
            ) -> dict[str, int] | Response:
                try:
                    post_id = await bbs_admin.reply(thread_id, body.body)
                except ValueError as error:
                    return JSONResponse(
                        {"error": {"code": "invalid_post", "message": str(error)}},
                        status_code=422,
                    )
                return {"id": post_id}

            @app.patch("/api/v1/threads/{thread_id}", response_model=None)
            async def thread_patch(
                thread_id: int, body: ThreadPatchBody
            ) -> dict[str, bool] | Response:
                try:
                    found = await bbs_admin.update_thread(
                        thread_id, body.model_dump(exclude_none=True)
                    )
                except ValueError as error:
                    return JSONResponse(
                        {"error": {"code": "invalid_thread", "message": str(error)}},
                        status_code=422,
                    )
                if not found:
                    return JSONResponse(
                        {"error": {"code": "not_found", "message": "Thread not found."}},
                        status_code=404,
                    )
                return {"ok": True}

        @app.get("/api/v1/boards/{board_id}/threads")
        async def threads(
            board_id: int,
            cursor: int = Query(0, ge=0),
            limit: int = Query(50, ge=1, le=200),
        ) -> dict[str, Any]:
            rows = await database.read(
                """
                SELECT t.id,t.subject,t.author_id,COALESCE(m.handle,'anon') AS author,
                       t.post_count,t.created_at,t.last_post_at,t.pinned,t.locked,t.origin_node,
                       t.uid LIKE '!%:%' AS remote,
                       COALESCE(
                         (SELECT node_name FROM fed_peer p
                          WHERE t.uid LIKE p.mesh_id || ':%' LIMIT 1),
                         (SELECT COALESCE(s.old_node_name,p.node_name) ||
                           ' · successor paired' FROM fed_peer_successor s
                          JOIN fed_peer p ON p.id=s.successor_peer_id
                          WHERE t.uid LIKE s.old_mesh_id || ':%' LIMIT 1),
                         substr(t.uid,1,instr(t.uid,':')-1) || ' · former peer'
                       ) AS origin_name,
                       COALESCE(
                         (SELECT state FROM fed_peer p WHERE t.uid LIKE p.mesh_id || ':%' LIMIT 1),
                         (SELECT 'successor' FROM fed_peer_successor s
                          WHERE t.uid LIKE s.old_mesh_id || ':%' LIMIT 1),'former'
                       ) AS origin_status,
                       (SELECT p.mesh_id FROM fed_peer_successor s JOIN fed_peer p
                        ON p.id=s.successor_peer_id WHERE t.uid LIKE s.old_mesh_id || ':%'
                        LIMIT 1) AS successor_mesh_id
                FROM thread t LEFT JOIN member m ON m.id=t.author_id
                WHERE t.board_id=? AND t.hidden=0
                ORDER BY t.pinned DESC,t.last_post_at DESC LIMIT ? OFFSET ?
                """,
                (board_id, limit + 1, cursor),
            )
            items = [dict(row) for row in rows[:limit]]
            for item in items:
                item["created_at"] = _timestamp(item["created_at"])
                item["last_post_at"] = _timestamp(item["last_post_at"])
            return {
                "items": items,
                "next_cursor": cursor + limit if len(rows) > limit else None,
            }

        @app.get("/api/v1/threads/{thread_id}")
        async def thread(thread_id: int) -> dict[str, Any]:
            thread_rows = await database.read(
                """
                SELECT t.id,t.subject,t.pinned,t.locked,t.hidden,t.origin_node,
                       t.uid LIKE '!%:%' AS remote,
                       COALESCE(
                         (SELECT node_name FROM fed_peer p
                          WHERE t.uid LIKE p.mesh_id || ':%' LIMIT 1),
                         (SELECT COALESCE(s.old_node_name,p.node_name) ||
                           ' · successor paired' FROM fed_peer_successor s
                          JOIN fed_peer p ON p.id=s.successor_peer_id
                          WHERE t.uid LIKE s.old_mesh_id || ':%' LIMIT 1),
                         substr(t.uid,1,instr(t.uid,':')-1) || ' · former peer'
                       ) AS origin_name,
                       COALESCE(
                         (SELECT state FROM fed_peer p WHERE t.uid LIKE p.mesh_id || ':%' LIMIT 1),
                         (SELECT 'successor' FROM fed_peer_successor s
                          WHERE t.uid LIKE s.old_mesh_id || ':%' LIMIT 1),'former'
                       ) AS origin_status,
                       (SELECT p.mesh_id FROM fed_peer_successor s JOIN fed_peer p
                        ON p.id=s.successor_peer_id WHERE t.uid LIKE s.old_mesh_id || ':%'
                        LIMIT 1) AS successor_mesh_id,b.id AS board_id,b.slug
                FROM thread t JOIN board b ON b.id=t.board_id WHERE t.id=?
                """,
                (thread_id,),
            )
            if not thread_rows:
                return {"id": thread_id, "posts": []}
            rows = await database.read(
                """
                SELECT p.id,p.seq,p.author_label,p.body,p.created_at,p.hidden,p.hidden_reason,
                       p.origin_node,p.uid LIKE '!%:%' AS remote,
                       COALESCE(
                         (SELECT node_name FROM fed_peer peer
                          WHERE p.uid LIKE peer.mesh_id || ':%' LIMIT 1),
                         (SELECT COALESCE(s.old_node_name,peer.node_name) ||
                           ' · successor paired' FROM fed_peer_successor s
                          JOIN fed_peer peer ON peer.id=s.successor_peer_id
                          WHERE p.uid LIKE s.old_mesh_id || ':%' LIMIT 1),
                         substr(p.uid,1,instr(p.uid,':')-1) || ' · former peer'
                       ) AS origin_name,
                       COALESCE(
                         (SELECT state FROM fed_peer peer
                          WHERE p.uid LIKE peer.mesh_id || ':%' LIMIT 1),
                         (SELECT 'successor' FROM fed_peer_successor s
                          WHERE p.uid LIKE s.old_mesh_id || ':%' LIMIT 1),'former'
                       ) AS origin_status
                FROM post p WHERE p.thread_id=? ORDER BY p.seq
                """,
                (thread_id,),
            )
            items = [dict(row) for row in rows]
            for item in items:
                item["created_at"] = _timestamp(item["created_at"])
            return {**dict(thread_rows[0]), "posts": items}

        @app.get("/api/v1/channels")
        async def channels() -> dict[str, Any]:
            rows = await database.read(
                """
                SELECT id,name,description,slot FROM channel_dir
                WHERE published=1 ORDER BY slot,name
                """
            )
            return {"items": [dict(row) for row in rows], "next_cursor": None}

        @app.get("/api/v1/dashboard/overview")
        async def dashboard_overview() -> dict[str, Any]:
            counts = await database.read(
                """
                SELECT
                  SUM(CASE WHEN handle IS NOT NULL
                             OR trust IN ('member','trusted','responder','operator')
                           THEN 1 ELSE 0 END) AS members_total,
                  SUM(CASE WHEN last_seen >= unixepoch()-86400 THEN 1 ELSE 0 END) AS heard_24h,
                  SUM(CASE WHEN last_seen >= unixepoch()-604800 THEN 1 ELSE 0 END) AS heard_7d
                FROM member
                """
            )
            traffic = await database.read(
                """
                SELECT direction,COUNT(*) AS count,COALESCE(SUM(byte_len),0) AS bytes
                FROM message_log WHERE created_at >= unixepoch()-86400 GROUP BY direction
                """
            )
            activity = await database.read(
                """
                SELECT ml.id,ml.direction,ml.peer_mesh_id,ml.channel,ml.command,ml.outcome,
                       ml.created_at,COALESCE(m.handle,'') AS handle
                FROM message_log ml LEFT JOIN member m ON m.id=ml.member_id
                ORDER BY ml.created_at DESC LIMIT 12
                """
            )
            activity_items = [dict(row) for row in activity]
            for item in activity_items:
                item["direction"] = "outbound" if item["direction"] == "out" else "inbound"
                item["created_at"] = _timestamp(item["created_at"])
            member_counts = dict(counts[0])
            traffic_items = {
                ("outbound" if row["direction"] == "out" else "inbound"): {
                    "count": row["count"],
                    "bytes": row["bytes"],
                }
                for row in traffic
            }
            return {
                "members": {key: int(value or 0) for key, value in member_counts.items()},
                "traffic_24h": traffic_items,
                "activity": activity_items,
            }

        @app.get("/api/v1/members/map")
        async def member_map() -> dict[str, Any]:
            now = int(datetime.now(UTC).timestamp())
            rows = await database.read(
                """SELECT m.id,m.mesh_id,m.handle,m.trust,m.last_seen,m.last_heard_snr,
                          m.hops_away,json_extract(m.prefs,'$.position') AS privacy,
                          p.lat,p.lon,p.received_at,p.source,p.expires_at
                   FROM member m JOIN member_position p ON p.member_id=m.id
                   WHERE (m.handle IS NOT NULL
                      OR m.trust IN ('member','trusted','responder','operator'))
                     AND p.expires_at>?
                   ORDER BY p.received_at DESC""",
                (now,),
            )
            items = [dict(row) for row in rows]
            for item in items:
                received_at, expires_at = int(item["received_at"]), int(item["expires_at"])
                item["age_seconds"] = max(0, now - received_at)
                item["deletes_in_seconds"] = max(0, expires_at - now)
                item["retention_hours"] = max(1, (expires_at - received_at) // 3_600)
                item["privacy"] = item["privacy"] or "coarse"
                item["visibility"] = f"operator exact; member {item['privacy']}"
                item["last_seen"] = _timestamp(item["last_seen"])
                item["received_at"] = _timestamp(received_at)
                item["expires_at"] = _timestamp(expires_at)
            return {"items": items}

        @app.delete("/api/v1/members/{member_id}/position", response_model=None)
        async def member_position_delete(member_id: int) -> dict[str, bool] | Response:
            async with database.transaction() as transaction:
                rows = await transaction.read(
                    """SELECT m.mesh_id,p.source,p.received_at,p.expires_at
                       FROM member m JOIN member_position p ON p.member_id=m.id WHERE m.id=?""",
                    (member_id,),
                )
                if not rows:
                    return JSONResponse(
                        {
                            "error": {
                                "code": "not_found",
                                "message": "Member position not found.",
                            }
                        },
                        status_code=404,
                    )
                row = rows[0]
                detail = (
                    f"source={row['source']};received_at={row['received_at']};"
                    f"scheduled_expiry={row['expires_at']}"
                )
                await transaction.write(
                    "DELETE FROM pending_incident_location WHERE member_id=?", (member_id,)
                )
                await transaction.write(
                    "DELETE FROM member_position WHERE member_id=?", (member_id,)
                )
                await transaction.write(
                    "INSERT INTO audit_log(actor_kind,actor_ref,action,target,detail,created_at) "
                    "VALUES('web','operator','member.position_delete',?,?,unixepoch())",
                    (row["mesh_id"], detail),
                )
            return {"deleted": True}

        @app.post("/api/v1/members/positions/purge-expired", response_model=None)
        async def member_positions_purge(
            body: PositionPurgeBody,
        ) -> dict[str, int] | Response:
            if body.confirmation != "PURGE EXPIRED POSITIONS":
                return JSONResponse(
                    {
                        "error": {
                            "code": "confirmation_required",
                            "message": "Confirmation must be PURGE EXPIRED POSITIONS.",
                        }
                    },
                    status_code=422,
                )
            now = int(datetime.now(UTC).timestamp())
            async with database.transaction() as transaction:
                rows = await transaction.read(
                    "SELECT COUNT(*) AS count FROM member_position WHERE expires_at<=?", (now,)
                )
                count = int(rows[0]["count"])
                pending_rows = await transaction.read(
                    "SELECT COUNT(*) AS count FROM pending_incident_location WHERE expires_at<=?",
                    (now,),
                )
                pending_count = int(pending_rows[0]["count"])
                await transaction.write(
                    "DELETE FROM pending_incident_location WHERE expires_at<=?", (now,)
                )
                await transaction.write("DELETE FROM member_position WHERE expires_at<=?", (now,))
                await transaction.write(
                    "INSERT INTO audit_log(actor_kind,actor_ref,action,target,detail,created_at) "
                    "VALUES('web','operator','member.position_purge','member_position',?,?)",
                    (
                        f"deleted={count};pending_deleted={pending_count};"
                        f"expired_at_or_before={now}",
                        now,
                    ),
                )
            return {"deleted": count, "pending_deleted": pending_count}

        @app.get("/api/v1/members")
        async def members(
            cursor: int = Query(0, ge=0),
            limit: int = Query(50, ge=1, le=200),
            view: Literal["approved", "discovered", "all"] = "approved",
        ) -> dict[str, Any]:
            conditions = {
                "approved": (
                    "handle IS NOT NULL OR trust IN ('member','trusted','responder','operator')"
                ),
                "discovered": "handle IS NULL AND trust IN ('guest','blocked')",
                "all": "1=1",
            }
            rows = await database.read(
                f"""
                SELECT id,mesh_id,handle,trust,first_seen,last_seen,last_heard_snr,hops_away,notes
                FROM member WHERE {conditions[view]}
                ORDER BY last_seen DESC,id LIMIT ? OFFSET ?
                """,  # noqa: S608 - condition is selected from fixed literals.
                (limit + 1, cursor),
            )
            counts = await database.read(
                """SELECT
                   SUM(handle IS NOT NULL OR trust IN ('member','trusted','responder','operator'))
                     AS approved_count,
                   SUM(handle IS NULL AND trust IN ('guest','blocked')) AS discovered_count
                   ,SUM(trust IN ('trusted','responder','operator')) AS trusted_count
                   FROM member"""
            )
            items = [dict(row) for row in rows[:limit]]
            for item in items:
                item["first_seen"] = _timestamp(item["first_seen"])
                item["last_seen"] = _timestamp(item["last_seen"])
            return {
                "items": items,
                "approved_count": int(counts[0]["approved_count"] or 0),
                "discovered_count": int(counts[0]["discovered_count"] or 0),
                "trusted_count": int(counts[0]["trusted_count"] or 0),
                "next_cursor": cursor + limit if len(rows) > limit else None,
            }

        @app.get("/api/v1/mesh/messages")
        async def mesh_messages(
            cursor: int = Query(0, ge=0),
            limit: int = Query(50, ge=1, le=200),
            direction: str | None = None,
            channel: int | None = Query(None, ge=0, le=7),
            outcome: str | None = None,
        ) -> dict[str, Any]:
            conditions, params = ["1=1"], []
            if direction is not None:
                conditions.append("direction=?")
                params.append(direction)
            if channel is not None:
                conditions.append("channel=?")
                params.append(channel)
            if outcome is not None:
                conditions.append("outcome=?")
                params.append(outcome)
            params.extend((limit + 1, cursor))
            rows = await database.read(
                f"""
                SELECT id,direction,peer_mesh_id,channel,portnum,is_direct,packet_id,text,
                       byte_len,toa_ms,airtime_class,command,outcome,drop_reason,latency_ms,
                       rx_snr,rx_rssi,hops,created_at
                FROM message_log WHERE {" AND ".join(conditions)}
                ORDER BY id DESC LIMIT ? OFFSET ?
                """,  # noqa: S608
                tuple(params),
            )
            items = [dict(row) for row in rows[:limit]]
            for item in items:
                item["created_at"] = _timestamp(item["created_at"])
            return {"items": items, "next_cursor": cursor + limit if len(rows) > limit else None}

        @app.get("/api/v1/mail")
        async def mail_list(
            cursor: int = Query(0, ge=0),
            limit: int = Query(50, ge=1, le=200),
            state: str | None = None,
            member: int | None = None,
        ) -> dict[str, Any]:
            conditions, params = ["1=1"], []
            if state is not None:
                conditions.append("state=?")
                params.append(state)
            if member is not None:
                conditions.append("(from_id=? OR to_id=?)")
                params.extend((member, member))
            params.extend((limit + 1, cursor))
            rows = await database.read(
                f"""
                SELECT id,uid,from_id,from_label,to_id,to_label,subject,created_at,delivered_at,
                       read_at,state,expires_at,LENGTH(body) AS body_length
                FROM mail WHERE {" AND ".join(conditions)}
                ORDER BY id DESC LIMIT ? OFFSET ?
                """,  # noqa: S608
                tuple(params),
            )
            items = [dict(row) for row in rows[:limit]]
            for item in items:
                for key in ("created_at", "delivered_at", "read_at", "expires_at"):
                    if item[key] is not None:
                        item[key] = _timestamp(item[key])
            return {"items": items, "next_cursor": cursor + limit if len(rows) > limit else None}

        @app.get("/api/v1/mail/{mail_id}")
        async def mail_detail(mail_id: int) -> Response:
            rows = await database.read(
                """
                SELECT id,uid,from_label,to_label,subject,body,created_at,delivered_at,
                       read_at,state,expires_at FROM mail WHERE id=?
                """,
                (mail_id,),
            )
            if not rows:
                return JSONResponse(
                    {"error": {"code": "not_found", "message": "Mail not found."}},
                    status_code=404,
                )
            item = dict(rows[0])
            for key in ("created_at", "delivered_at", "read_at", "expires_at"):
                if item[key] is not None:
                    item[key] = _timestamp(item[key])
            await database.write(
                """
                INSERT INTO audit_log(actor_kind,actor_ref,action,target,detail,created_at)
                VALUES('web','operator','mail.view',?,NULL,unixepoch())
                """,
                (f"mail:{mail_id}",),
            )
            return JSONResponse(item)

        if radio_operations is not None:

            @app.get("/api/v1/mesh/queue")
            async def mesh_queue() -> dict[str, Any]:
                return {"items": await radio_operations.queue()}

            @app.delete("/api/v1/mesh/queue/{item_id}")
            async def mesh_queue_cancel(item_id: int) -> Response:
                if not await radio_operations.cancel(item_id):
                    return JSONResponse(
                        {"error": {"code": "not_found", "message": "Queue item not found."}},
                        status_code=404,
                    )
                return JSONResponse({"ok": True})

            @app.get("/api/v1/mesh/airtime")
            async def mesh_airtime() -> dict[str, Any]:
                return radio_operations.airtime()

            @app.post("/api/v1/mesh/send", response_model=None)
            async def mesh_send(body: MeshSendBody) -> dict[str, int] | Response:
                try:
                    item_id = await radio_operations.send(
                        body.text, body.destination, body.channel, body.traffic_class
                    )
                except ValueError as error:
                    return JSONResponse(
                        {"error": {"code": "send_rejected", "message": str(error)}},
                        status_code=422,
                    )
                return {"queue_id": item_id}

        @app.patch("/api/v1/members/{member_id}")
        async def member_patch(member_id: int, body: MemberPatchBody) -> Response:
            values = body.model_dump(exclude_none=True)
            if not values:
                return JSONResponse(
                    {"error": {"code": "empty_update", "message": "No changes supplied."}},
                    status_code=400,
                )
            assignments, params = [], []
            for key, value in values.items():
                assignments.append(f"{key}=?")
                params.append(value)
            params.append(member_id)
            rows = await database.read("SELECT mesh_id FROM member WHERE id=?", (member_id,))
            if not rows:
                return JSONResponse(
                    {"error": {"code": "not_found", "message": "Member not found."}},
                    status_code=404,
                )
            await database.write(
                f"UPDATE member SET {','.join(assignments)} WHERE id=?",  # noqa: S608
                tuple(params),
            )
            await database.write(
                """
                INSERT INTO audit_log(actor_kind,actor_ref,action,target,detail,created_at)
                VALUES('web','operator','member.update',?,?,unixepoch())
                """,
                (rows[0]["mesh_id"], ",".join(sorted(values))),
            )
            return JSONResponse({"ok": True})

        @app.patch("/api/v1/posts/{post_id}")
        async def post_patch(post_id: int, body: PostPatchBody) -> Response:
            rows = await database.read(
                "SELECT thread_id,seq,hidden FROM post WHERE id=?", (post_id,)
            )
            if not rows:
                return JSONResponse(
                    {"error": {"code": "not_found", "message": "Post not found."}},
                    status_code=404,
                )
            row = rows[0]
            await database.write(
                "UPDATE post SET hidden=?,hidden_by=?,hidden_reason=? WHERE id=?",
                (int(body.hidden), "web:operator", body.reason[:160], post_id),
            )
            if row["seq"] == 1:
                await database.write(
                    "UPDATE thread SET hidden=? WHERE id=?",
                    (int(body.hidden), row["thread_id"]),
                )
            await database.write(
                """
                INSERT INTO audit_log(actor_kind,actor_ref,action,target,detail,created_at)
                VALUES('web','operator',?,?,?,unixepoch())
                """,
                (
                    "bbs.hide" if body.hidden else "bbs.unhide",
                    f"post:{post_id}",
                    body.reason[:160],
                ),
            )
            return JSONResponse({"ok": True})

        @app.get("/api/v1/audit")
        async def audit(
            cursor: int = Query(0, ge=0),
            limit: int = Query(50, ge=1, le=200),
            from_time: datetime | None = None,
            until: datetime | None = None,
            actor: str = Query(default="", max_length=100),
            action: str = Query(default="", max_length=100),
            target: str = Query(default="", max_length=160),
            outcome: Literal["success", "denied", "failure"] | None = None,
        ) -> dict[str, Any]:
            clauses: list[str] = []
            params: list[object] = []
            if from_time is not None:
                clauses.append("created_at>=?")
                params.append(int(from_time.timestamp()))
            if until is not None:
                clauses.append("created_at<=?")
                params.append(int(until.timestamp()))
            for column, value in (
                ("actor_kind || ':' || actor_ref", actor),
                ("action", action),
                ("COALESCE(target,'')", target),
            ):
                if value.strip():
                    clauses.append(f"instr(lower({column}),lower(?))>0")  # noqa: S608
                    params.append(value.strip())
            if outcome is not None:
                clauses.append("outcome=?")
                params.append(outcome)
            where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
            audit_sql = (
                "SELECT id,actor_kind,actor_ref,action,target,detail,outcome,created_at "  # noqa: S608
                f"FROM audit_log{where} ORDER BY id DESC LIMIT ? OFFSET ?"
            )
            rows = await database.read(
                audit_sql,
                (*params, limit + 1, cursor),
            )
            total = int(
                (
                    await database.read(
                        f"SELECT COUNT(*) count FROM audit_log{where}",  # noqa: S608
                        params,
                    )
                )[0]["count"]
            )
            items = [dict(row) for row in rows[:limit]]
            for item in items:
                item["created_at"] = _timestamp(item["created_at"])
                item["detail"], item["detail_format"] = _audit_detail(item["detail"])
            return {
                "items": items,
                "total": total,
                "next_cursor": cursor + limit if len(rows) > limit else None,
            }

        @app.get("/api/v1/security/safety-floor")
        async def safety_floor_activity() -> dict[str, Any]:
            summary = (
                await database.read(
                    "SELECT COALESCE(SUM(attempt_count),0) attempts,"
                    "COALESCE(SUM(accepted_count),0) accepted,"
                    "COALESCE(SUM(coalesced_count),0) coalesced,"
                    "COUNT(DISTINCT member_mesh_id) members,MAX(last_seen_at) last_seen_at "
                    "FROM safety_floor_attempt"
                )
            )[0]
            rows = await database.read(
                "SELECT member_mesh_id,command,attempt_count,accepted_count,coalesced_count,"
                "last_seen_at FROM safety_floor_attempt WHERE coalesced_count>0 "
                "ORDER BY last_seen_at DESC LIMIT 20"
            )
            items = [dict(row) for row in rows]
            for item in items:
                item["last_seen_at"] = _timestamp(item["last_seen_at"])
            value = dict(summary)
            value["last_seen_at"] = (
                _timestamp(value["last_seen_at"]) if value["last_seen_at"] is not None else None
            )
            return {"summary": value, "items": items}

    app.mount("/metrics", make_asgi_app())
    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="dashboard")
    return app
