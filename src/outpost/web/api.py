from __future__ import annotations

import ipaddress
import json
import re
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal, cast

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field, model_validator

from outpost import __version__
from outpost.ai import AIService
from outpost.ai.store import AIStore
from outpost.audit import display_audit_detail, write_audit
from outpost.bbs.admin import BBSAdmin
from outpost.channel_profile import channel_slot
from outpost.clock import SystemClock
from outpost.config import DEFAULT_TILES_PATH, RetentionConfig, WebConfig
from outpost.env import (
    AstronomyService,
    CapAlertService,
    SameService,
    SeismicService,
    WaypointService,
    WeatherService,
)
from outpost.fed import (
    FederationPeerService,
    FederationRelayService,
    FederationTopologyService,
)
from outpost.member_data import MemberDataService, retention_statement
from outpost.operator_context import (
    current_actor,
    current_actor_ref,
    reset_current_actor,
    set_current_actor,
)
from outpost.radio_operations import RadioOperations
from outpost.self_check import SelfCheckService
from outpost.situation import BriefingCapability, BriefingViewer, SituationBriefingService
from outpost.store import Database
from outpost.store.backups import BackupService, RestoreCoordinator
from outpost.store.maintenance import MaintenanceService
from outpost.watch import AlertService, CheckinService, IncidentReportService, IncidentService
from outpost.web.auth import MfaChallenge, WebAuthService
from outpost.web.member_triage import NEEDS_REVIEW_SQL, MemberTriageError, MemberTriageService
from outpost.web.operator_inbox import OperatorInboxService
from outpost.web.settings import RuntimeSettings
from outpost.web.tiles import absolute_tile_root, find_tile, inspect_tile_pack
from outpost.web.transport import WebTransportMiddleware, transport_status

PUBLIC_API_PATHS = frozenset(
    {
        "/api/v1/health",
        "/api/v1/privacy/retention",
        "/api/v1/runtime",
        "/api/v1/diagnostics/readiness",
        "/api/v1/diagnostics/status",
        "/api/v1/auth/login",
        "/api/v1/auth/setup",
        "/api/v1/recovery/restores/{job_id}",
    }
)
PUBLIC_API_PREFIXES = ("/api/v1/recovery/restores/",)
PUBLIC_NON_API_ROUTE_PATHS = frozenset(
    {
        "/tiles/manifest.json",
        "/tiles/{zoom}/{x}/{y}.{extension}",
        "/favicon.ico",
        "/connecttest.txt",
        "/ncsi.txt",
        "/hotspot-detect.html",
        "/generate_204",
    }
)
AUTHENTICATED_NON_API_PATHS = frozenset({"/metrics", "/metrics/"})
API_MODULE_PREFIXES = (
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
STEP_UP_PREFIXES = (
    "/api/v1/auth/accounts",
    "/api/v1/auth/mfa",
    "/api/v1/radio/config",
    "/api/v1/federation/peers",
    "/api/v1/federation/mqtt",
    "/api/v1/federation/origins",
    "/api/v1/federation/relay/origins",
    "/api/v1/federation/relay/identity",
    "/api/v1/config/watch",
    "/api/v1/ai",
    "/api/v1/alerts",
    "/api/v1/member-data-requests",
    "/api/v1/environment/alerts",
    "/api/v1/environment/earthquakes",
    "/api/v1/environment/same",
)
VIEWER_SELF_SERVICE = frozenset(
    {
        ("GET", "/api/v1/auth/session"),
        ("GET", "/api/v1/auth/sessions"),
        ("POST", "/api/v1/auth/logout"),
        ("POST", "/api/v1/auth/password"),
        ("POST", "/api/v1/auth/step-up"),
        ("POST", "/api/v1/auth/mfa/begin"),
        ("POST", "/api/v1/auth/mfa/confirm"),
        ("DELETE", "/api/v1/auth/mfa"),
        ("DELETE", "/api/v1/auth/sessions"),
    }
)
VIEWER_READ_PATHS = frozenset({"/api/v1/wallboard/summary", "/api/v1/sitrep"})


def route_access_policy(path: str) -> Literal["public", "authenticated"] | None:
    """Return the declared access policy for a registered HTTP route or request path."""
    if (
        path in PUBLIC_API_PATHS
        or path in PUBLIC_NON_API_ROUTE_PATHS
        or any(path.startswith(prefix) for prefix in PUBLIC_API_PREFIXES)
    ):
        return "public"
    if path.startswith("/api/v1/") or path in AUTHENTICATED_NON_API_PATHS:
        return "authenticated"
    return None


def api_module(path: str) -> str | None:
    return next(
        (
            module
            for prefix, module in API_MODULE_PREFIXES
            if path == prefix or path.startswith(f"{prefix}/")
        ),
        None,
    )


def step_up_path(method: str, path: str) -> bool:
    if method in {"GET", "HEAD", "OPTIONS"}:
        return False
    return (
        any(path.startswith(prefix) for prefix in STEP_UP_PREFIXES)
        or (path.startswith("/api/v1/backups/") and path.endswith("/restore"))
        or (path.startswith("/api/v1/backups/") and method == "DELETE")
        or (path.startswith("/api/v1/members/") and method in {"PATCH", "DELETE"})
        or (path.startswith("/api/v1/members/") and path.endswith("/pki"))
    )


class LoginBody(BaseModel):
    username: str = Field(default="operator", min_length=1, max_length=32)
    password: str
    code: str | None = Field(default=None, max_length=32)


class PasswordBody(BaseModel):
    current_password: str
    new_password: str


class StepUpBody(BaseModel):
    password: str
    code: str | None = Field(default=None, max_length=32)


class AccountCreateBody(BaseModel):
    username: str = Field(min_length=2, max_length=32)
    display_name: str = Field(min_length=1, max_length=80)
    role: Literal["administrator", "operator", "viewer"]
    initial_password: str = Field(min_length=12, max_length=512)


class AccountPatchBody(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=80)
    role: Literal["administrator", "operator", "viewer"] | None = None
    enabled: bool | None = None


class AccountPasswordBody(BaseModel):
    temporary_password: str = Field(min_length=12, max_length=512)


class AccountRadioBody(BaseModel):
    member_id: int | None = Field(default=None, ge=1)


class MfaConfirmBody(BaseModel):
    code: str = Field(min_length=6, max_length=32)


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


class AITestBody(BaseModel):
    question: str = Field(min_length=1, max_length=1000)


class KBDocumentBody(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=1, max_length=12_000)
    slug: str | None = Field(default=None, max_length=64)


class AIRatingBody(BaseModel):
    rating: Literal[-1, 0, 1]


class AIPromoteBody(BaseModel):
    title: str = Field(min_length=1, max_length=120)


class AIRefusalBody(BaseModel):
    phrase: str = Field(min_length=3, max_length=120)
    reason: str = Field(min_length=1, max_length=120)


class MemberPatchBody(BaseModel):
    trust: Literal["blocked", "guest", "member", "trusted", "responder", "operator"] | None = None
    notes: str | None = Field(default=None, max_length=2000)
    reason: str | None = Field(default=None, max_length=240)


class MemberStateBody(BaseModel):
    action: Literal["archive", "ignore", "restore"]
    reason: str = Field(default="", max_length=240)


class MemberPkiReviewBody(BaseModel):
    action: Literal["approve", "reject"]
    reason: str = Field(min_length=3, max_length=240)


class MemberBulkBody(BaseModel):
    member_ids: list[int] = Field(min_length=1, max_length=200)
    action: Literal["archive", "ignore", "restore"]
    reason: str = Field(default="", max_length=240)


class PositionPurgeBody(BaseModel):
    confirmation: str


class MaintenanceRunBody(BaseModel):
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
    airtime_confirmation: bool = False


class RestoreBody(BaseModel):
    confirmation: str


class BackupDeleteBody(BaseModel):
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


class IncidentLocationBody(BaseModel):
    location: str = Field(min_length=1, max_length=200)


class IncidentMergeBody(BaseModel):
    target_id: int = Field(gt=0)


class AlertCreateBody(BaseModel):
    severity: Literal["caution", "urgent", "critical"]
    headline: str
    incident_ref: int | None = None
    channels: list[int] | None = None
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    radius_km: float = Field(default=1.0, ge=0.1, le=100)
    airtime_confirmation: bool = False


class AlertCancelBody(BaseModel):
    resolution: str = Field(min_length=1, max_length=140)


class EventCreateBody(BaseModel):
    name: str
    roster_policy: Literal["all", "responders", "subscribed"] = "all"
    responder_group_id: int | None = Field(default=None, gt=0)


class EventSolicitBody(BaseModel):
    confirmation: str
    airtime_confirmation: bool = False


class ResponderGroupCreateBody(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    response_type: Literal[
        "general", "medical", "fire", "search", "logistics", "communications", "public_safety"
    ] = "general"


class ResponderGroupUpdateBody(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    response_type: Literal[
        "general", "medical", "fire", "search", "logistics", "communications", "public_safety"
    ]


class ResponderGroupMembersBody(BaseModel):
    member_ids: list[int] = Field(default_factory=list, max_length=200)


class WelfareSchedulePreviewBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    cadence: Literal["weekly", "biweekly", "monthly"]
    day_of_period: int = Field(ge=0, le=28)
    local_time: str = Field(min_length=5, max_length=5)
    roster_policy: Literal["all", "responders", "subscribed"] = "all"
    responder_group_id: int | None = Field(default=None, gt=0)
    window_minutes: int = Field(default=120, ge=30, le=1440)


class WelfareScheduleCreateBody(WelfareSchedulePreviewBody):
    suppress_if_real_event: bool = True
    airtime_confirmation: bool = False
    preview_token: str = Field(pattern=r"^[0-9a-f]{64}$")


class WelfareScheduleStateBody(BaseModel):
    enabled: bool


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


class RadioIdentityConfigBody(BaseModel):
    long_name: str = Field(min_length=1, max_length=40)
    short_name: str = Field(min_length=1, max_length=4)


class RadioDeviceConfigBody(BaseModel):
    role: Literal["CLIENT", "CLIENT_BASE"]
    rebroadcast_mode: Literal["ALL", "LOCAL_ONLY", "CORE_PORTNUMS_ONLY"] = "ALL"
    node_info_broadcast_secs: int = Field(default=10_800, ge=900, le=86_400)


class RadioLoraConfigBody(BaseModel):
    region: str = Field(min_length=2, max_length=24, pattern=r"^[A-Z0-9_]+$")
    modem_preset: str = Field(min_length=2, max_length=40, pattern=r"^[A-Z0-9_]+$")
    frequency_slot: int = Field(ge=0, le=65_535)
    hop_limit: int = Field(default=3, ge=1, le=7)
    tx_power: int = Field(default=0, ge=0, le=30)
    tx_enabled: bool = True


class RadioPositionConfigBody(BaseModel):
    fixed_position: bool = False
    gps_mode: str = Field(min_length=2, max_length=24, pattern=r"^[A-Z0-9_]+$")
    smart_broadcast: bool = True
    broadcast_secs: int = Field(default=0, ge=0, le=86_400)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    altitude: int = Field(default=0, ge=-500, le=10_000)

    @model_validator(mode="after")
    def fixed_coordinates_are_complete(self) -> RadioPositionConfigBody:
        if self.fixed_position and (self.latitude is None or self.longitude is None):
            raise ValueError("fixed position requires latitude and longitude")
        return self


class RadioChannelConfigBody(BaseModel):
    index: int = Field(ge=0, le=7)
    role: Literal["PRIMARY", "SECONDARY", "DISABLED"]
    name: str = Field(default="", max_length=12)
    psk: str | None = Field(default=None, max_length=44)
    generate_psk: bool = False
    uplink_enabled: bool = False
    downlink_enabled: bool = False
    position_precision: int = Field(default=0, ge=0, le=32)
    muted: bool = False

    @model_validator(mode="after")
    def key_source_is_unambiguous(self) -> RadioChannelConfigBody:
        if self.psk and self.generate_psk:
            raise ValueError("provide a channel key or generate one, not both")
        return self


class RadioOutpostProfileBody(BaseModel):
    bindings: dict[str, int]

    @model_validator(mode="after")
    def all_channels_have_distinct_slots(self) -> RadioOutpostProfileBody:
        normalized = {str(name).lower(): int(index) for name, index in self.bindings.items()}
        if set(normalized) != {"public", "outpost", "watch"}:
            raise ValueError("select slots for public, outpost, and watch")
        if len(set(normalized.values())) != 3:
            raise ValueError("each Outpost channel must use a different slot")
        if any(index < 0 or index > 7 for index in normalized.values()):
            raise ValueError("Outpost channels must use slots 0 through 7")
        self.bindings = normalized
        return self


class RadioMqttConfigBody(FederationMqttBody):
    username: str | None = Field(default=None, max_length=128)
    password: str | None = Field(default=None, max_length=256)
    json_enabled: bool | None = None
    proxy_to_client_enabled: bool | None = None
    map_reporting_enabled: bool | None = None


class RadioConfigurationBody(BaseModel):
    preflight_id: str | None = Field(default=None, min_length=8, max_length=64)
    identity: RadioIdentityConfigBody | None = None
    device: RadioDeviceConfigBody | None = None
    lora: RadioLoraConfigBody | None = None
    position: RadioPositionConfigBody | None = None
    channel: RadioChannelConfigBody | None = None
    outpost_profile: RadioOutpostProfileBody | None = None
    mqtt: RadioMqttConfigBody | None = None

    @model_validator(mode="after")
    def exactly_one_section(self) -> RadioConfigurationBody:
        populated = [
            name
            for name in (
                "identity",
                "device",
                "lora",
                "position",
                "channel",
                "outpost_profile",
                "mqtt",
            )
            if getattr(self, name) is not None
        ]
        if len(populated) != 1:
            raise ValueError("provide exactly one radio configuration section")
        return self

    def change(self) -> tuple[str, dict[str, Any]]:
        section = next(
            name
            for name in (
                "identity",
                "device",
                "lora",
                "position",
                "channel",
                "outpost_profile",
                "mqtt",
            )
            if getattr(self, name) is not None
        )
        value = getattr(self, section)
        return section, value.model_dump()


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
    quota_mail_per_recipient_per_hour: int = Field(default=5, ge=1, le=100)
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


def _default_relay_scopes() -> list[Literal["incident", "request", "receipt", "opaque"]]:
    return ["incident", "request", "receipt"]


class FederationRelayPolicyBody(BaseModel):
    enabled: bool = False
    paused: bool = False
    scopes: list[Literal["incident", "request", "receipt", "opaque"]] = Field(
        default_factory=_default_relay_scopes, max_length=4
    )
    max_stored_items: int = Field(default=50, ge=1, le=500)
    max_stored_bytes: int = Field(default=65_536, ge=1_024, le=1_048_576)
    rate_per_hour: int = Field(default=20, ge=1, le=200)
    airtime_seconds_per_hour: float = Field(default=30, ge=1, le=300)


class FederationRelayCreateBody(BaseModel):
    destination: str = Field(pattern=r"^![0-9a-fA-F]{8}$")
    scope: Literal["incident", "request", "receipt"]
    payload: dict[str, Any]
    expires_in: int = Field(default=86_400, ge=60, le=604_800)
    hop_limit: int = Field(default=3, ge=1, le=4)
    idempotency_key: str | None = Field(default=None, max_length=64)


class FederationRelayActionBody(BaseModel):
    action: Literal["pause", "resume", "purge", "retry"]


class FederationRelayOriginBody(BaseModel):
    state: Literal["trusted", "rejected", "forget", "replace", "reject_candidate"]
    fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class FederationTopologyPolicyBody(BaseModel):
    share_location: bool = False
    location_lat: float | None = Field(default=None, ge=-90, le=90)
    location_lon: float | None = Field(default=None, ge=-180, le=180)
    precision_km: float = Field(default=10, ge=1, le=100)


class MailConversationStateBody(BaseModel):
    state: Literal["read", "unread", "archive", "active"]


class MailConversationReplyBody(BaseModel):
    body: str = Field(min_length=1, max_length=800)


class MemberDataReviewBody(BaseModel):
    action: Literal["approve", "reject"]
    reason: str = Field(min_length=3, max_length=240)


def _timestamp(value: int) -> str:
    return datetime.fromtimestamp(value, UTC).isoformat().replace("+00:00", "Z")


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
    maintenance: MaintenanceService | None = None,
    module_provider: Callable[[], dict[str, bool]] | None = None,
    federation_mail_reply: (
        Callable[[str, str, str, str, str, str, str], Awaitable[dict[str, object]]] | None
    ) = None,
    same_events: SameService | None = None,
    same_receiver_health: Callable[[], dict[str, Any]] | None = None,
    ai_service: AIService | None = None,
    ai_store: AIStore | None = None,
    ai_test: Callable[[str], Awaitable[dict[str, object]]] | None = None,
    federation_relay: FederationRelayService | None = None,
    federation_topology: FederationTopologyService | None = None,
    radio_configuration_status: Callable[[], Awaitable[dict[str, Any]]] | None = None,
    radio_configuration_configure: (
        Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]] | None
    ) = None,
    radio_configuration_preflight: (
        Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]] | None
    ) = None,
    radio_configuration_apply: (
        Callable[[str, str, dict[str, Any]], Awaitable[dict[str, Any]]] | None
    ) = None,
    situation: SituationBriefingService | None = None,
    web_config: WebConfig | None = None,
    self_check: SelfCheckService | None = None,
    tile_path: str | Path = DEFAULT_TILES_PATH,
    incident_reports: IncidentReportService | None = None,
    member_data: MemberDataService | None = None,
) -> FastAPI:
    app = FastAPI(
        title="Outpost API",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    effective_web_config = web_config or WebConfig()
    effective_incident_reports = incident_reports or (
        IncidentReportService(database, incidents.clock)
        if database is not None and incidents is not None
        else None
    )
    effective_member_data = member_data or (
        MemberDataService(database, SystemClock()) if database is not None else None
    )

    @app.get("/api/v1/privacy/retention")
    async def privacy_retention() -> dict[str, Any]:
        retention = (
            effective_member_data.retention
            if effective_member_data is not None
            else RetentionConfig()
        )
        return retention_statement(retention)

    def effective_modules() -> dict[str, bool]:
        if module_provider is not None:
            return module_provider()
        if settings is not None:
            return settings.config.modules.enabled_map()
        return {name: True for name in ("bbs", "ai", "watch", "env", "fed")}

    async def radio_channel_map() -> dict[str, Any]:
        radio_status: dict[str, Any] = {
            "available": False,
            "stale": False,
            "verified_at": None,
            "channels": [],
        }
        if radio_configuration_status is not None:
            try:
                radio_status = await radio_configuration_status()
            except (ConnectionError, OSError, TimeoutError):
                radio_status["stale"] = True
        radio_up = status_provider().get("radio") == "up"
        available = bool(radio_status.get("available") and radio_up)
        raw_channels = radio_status.get("channels", [])
        current = {
            int(channel["index"]): channel
            for channel in raw_channels
            if 0 <= int(channel.get("index", -1)) <= 7
        }
        policies = {
            int(channel["index"]): channel
            for channel in radio_status.get("outpost_channel_policies", [])
            if 0 <= int(channel.get("index", -1)) <= 7
        }
        retained: dict[int, int | None] = {}
        if database is not None:
            rows = await database.read(
                """
                SELECT channel,MAX(created_at) AS last_seen_at
                FROM message_log WHERE channel BETWEEN 0 AND 7
                GROUP BY channel ORDER BY channel
                """
            )
            retained = {int(row["channel"]): row["last_seen_at"] for row in rows}
        items = []
        for index in sorted(set(current) | set(retained)):
            channel = current.get(index, {})
            role = str(channel.get("role", "UNKNOWN"))
            last_verified_active = role not in {"DISABLED", "UNKNOWN"}
            name = str(channel.get("name", "")).strip()
            if not name:
                name = str(policies.get(index, {}).get("name", "")).strip()
            items.append(
                {
                    "index": index,
                    "name": name or f"Channel {index}",
                    "role": role,
                    "active": available and last_verified_active,
                    "last_verified_active": last_verified_active,
                    "historical": index in retained,
                    "last_seen_at": retained.get(index),
                }
            )
        return {
            "available": available,
            "stale": bool(radio_status.get("stale") or (items and not available)),
            "verified_at": radio_status.get("verified_at"),
            "items": items,
        }

    async def require_active_radio_channels(channels: list[int]) -> None:
        if radio_configuration_status is None:
            return
        channel_map = await radio_channel_map()
        if not channel_map["available"]:
            raise ValueError(
                "Radio is disconnected; reconnect it and verify the channel map before sending."
            )
        active = {item["index"] for item in channel_map["items"] if item["active"]}
        inactive = sorted(set(channels) - active)
        if inactive:
            slots = ", ".join(str(index) for index in inactive)
            raise ValueError(
                f"Radio channel slot(s) {slots} are no longer active; refresh and choose "
                "an active channel."
            )

    def viewer_allowed(method: str, path: str) -> bool:
        """Default-deny API capabilities for an unattended wallboard session."""
        normalized = path.rstrip("/") or "/"
        if (method, normalized) in VIEWER_SELF_SERVICE:
            return True
        if method == "DELETE" and re.fullmatch(r"/api/v1/auth/sessions/[A-Za-z0-9_-]+", normalized):
            return True
        return method in {"GET", "HEAD"} and normalized in VIEWER_READ_PATHS

    def apply_security_headers(request: Request, response: Response) -> Response:
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' https://tile.openstreetmap.org; object-src 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        if request.url.scheme == "https" and effective_web_config.transport.hsts_seconds:
            response.headers["Strict-Transport-Security"] = (
                f"max-age={effective_web_config.transport.hsts_seconds}"
            )
        if request.url.path.endswith((".html", ".js", ".css")) or request.url.path == "/":
            response.headers["Cache-Control"] = "no-cache"
        if request.url.path.startswith("/api/v1/auth/") or request.url.path in {
            "/api/v1/wallboard/summary",
            "/api/v1/sitrep",
            "/api/v1/readiness",
            "/api/v1/diagnostics/readiness",
            "/api/v1/diagnostics/status",
        }:
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.middleware("http")
    async def authentication(request: Request, call_next: Any) -> Response:
        path = request.url.path
        access = route_access_policy(path)
        if path in AUTHENTICATED_NON_API_PATHS:
            if effective_web_config.metrics_access == "disabled":
                return JSONResponse(
                    {"error": {"code": "not_found", "message": "Metrics are disabled."}},
                    status_code=404,
                )
            if effective_web_config.metrics_access == "loopback":
                host = request.client.host if request.client is not None else ""
                try:
                    local_metrics = ipaddress.ip_address(host).is_loopback
                except ValueError:
                    local_metrics = False
                if not local_metrics:
                    return JSONResponse(
                        {
                            "error": {
                                "code": "loopback_required",
                                "message": "Metrics require local access.",
                            }
                        },
                        status_code=403,
                    )
                access = "public"
        if auth is not None and access == "authenticated":
            session = await auth.session(request.cookies.get("outpost_session"))
            if session is None:
                return JSONResponse(
                    {"error": {"code": "unauthorized", "message": "Sign in required."}},
                    status_code=401,
                )
            auth_path = path.startswith("/api/v1/auth/")
            request.state.web_session = session
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
            if (
                session.role == "viewer"
                and request.method != "OPTIONS"
                and not viewer_allowed(request.method, path)
            ):
                return JSONResponse(
                    {
                        "error": {
                            "code": "read_only",
                            "message": (
                                "This read-only wallboard account cannot access that operation."
                            ),
                        }
                    },
                    status_code=403,
                )
            admin_only = path.startswith("/api/v1/auth/accounts") or (
                request.method in {"POST", "DELETE"}
                and path.startswith("/api/v1/backups/")
                and (path.endswith("/restore") or request.method == "DELETE")
            )
            if admin_only and session.role != "administrator":
                return JSONResponse(
                    {
                        "error": {
                            "code": "administrator_required",
                            "message": "An administrator account is required.",
                        }
                    },
                    status_code=403,
                )
            if step_up_path(request.method, path) and (
                session.step_up_until is None or session.step_up_until <= int(time.time())
            ):
                return JSONResponse(
                    {
                        "error": {
                            "code": "step_up_required",
                            "message": "Confirm your operator credentials to continue.",
                        },
                        "mfa_required": session.mfa_enabled,
                    },
                    status_code=428,
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
        actor_token = None
        if auth is not None and hasattr(request.state, "web_session"):
            actor_token = set_current_actor(f"web:{request.state.web_session.username}")
        try:
            return cast(Response, await call_next(request))
        finally:
            if actor_token is not None:
                reset_current_actor(actor_token)

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
                return cast(Response, await call_next(request))
            finally:
                if gated_request:
                    await restore_coordinator.leave_mutation()

    # Registered after all response-generating middleware so denials and
    # maintenance responses receive the same browser policy as route responses.
    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        return apply_security_headers(request, await call_next(request))

    tile_root = absolute_tile_root(tile_path)

    @app.get("/tiles/manifest.json", response_model=None)
    async def tile_manifest() -> Response:
        status = inspect_tile_pack(tile_root)
        if status.state != "ready":
            return JSONResponse(
                {
                    "status": status.state,
                    "message": (
                        "No offline tile pack is installed."
                        if status.state == "missing"
                        else "The installed offline tile pack is unreadable."
                    ),
                },
                status_code=404 if status.state == "missing" else 503,
                headers={"Cache-Control": "no-store"},
            )
        assert status.manifest is not None
        return JSONResponse(
            {**status.manifest, "status": "ready"},
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/favicon.ico", include_in_schema=False, response_model=None)
    async def favicon() -> Response:
        icon = Path(static_dir or "") / "favicon.svg"
        return FileResponse(icon, media_type="image/svg+xml")

    @app.get("/generate_204", include_in_schema=False, response_model=None)
    @app.get("/hotspot-detect.html", include_in_schema=False, response_model=None)
    @app.get("/ncsi.txt", include_in_schema=False, response_model=None)
    @app.get("/connecttest.txt", include_in_schema=False, response_model=None)
    async def captive_setup_entry() -> RedirectResponse:
        return RedirectResponse("/", status_code=307)

    @app.get("/tiles/{zoom}/{x}/{y}.{extension}", response_model=None)
    async def tile_image(zoom: int, x: int, y: int, extension: str) -> Response:
        if not (0 <= zoom <= 22 and 0 <= x < 2**zoom and 0 <= y < 2**zoom):
            return Response(status_code=404)
        match = find_tile(tile_root, zoom, x, y, extension)
        if match is None:
            return Response(status_code=404)
        tile, media_type = match
        return FileResponse(
            tile, media_type=media_type, headers={"Cache-Control": "public, max-age=86400"}
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
            result = await auth.login(
                body.password,
                source,
                username=body.username,
                code=body.code,
                user_agent=request.headers.get("user-agent"),
            )
            if result is None:
                return JSONResponse(
                    {"error": {"code": "invalid_login", "message": "Invalid credentials."}},
                    status_code=401,
                )
            if isinstance(result, MfaChallenge):
                return JSONResponse(
                    {"mfa_required": True, "username": result.username}, status_code=202
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
            return session.__dict__

        @app.get("/api/v1/auth/session")
        async def auth_session(request: Request) -> dict[str, Any]:
            session = await auth.session(request.cookies.get("outpost_session"))
            assert session is not None
            return {"authenticated": True, **session.__dict__}

        @app.post("/api/v1/auth/step-up", response_model=None)
        async def auth_step_up(body: StepUpBody, request: Request) -> dict[str, Any] | Response:
            session = await auth.step_up(
                request.cookies.get("outpost_session", ""),
                body.password,
                body.code,
                source=request.client.host if request.client else "unknown",
            )
            if session is None:
                return JSONResponse(
                    {
                        "error": {
                            "code": "step_up_failed",
                            "message": "Password or verification code is invalid.",
                        }
                    },
                    status_code=401,
                )
            return {"ok": True, "step_up_until": session.step_up_until}

        @app.get("/api/v1/auth/accounts")
        async def auth_accounts() -> dict[str, Any]:
            login_security = await auth.login_security_status()
            items = await auth.accounts(login_security)
            radios = await auth.operator_radios()
            return {
                "items": items,
                "count": len(items),
                "operator_radios": radios,
                "operator_radio_count": len(radios),
                "login_security": login_security,
            }

        @app.post("/api/v1/auth/accounts", response_model=None)
        async def auth_account_create(
            body: AccountCreateBody, request: Request
        ) -> dict[str, object] | Response:
            session = request.state.web_session
            try:
                return await auth.create_account(
                    body.username,
                    body.display_name,
                    body.role,
                    body.initial_password,
                    session.username,
                )
            except ValueError as error:
                return JSONResponse(
                    {"error": {"code": "account_invalid", "message": str(error)}},
                    status_code=422,
                )

        @app.patch("/api/v1/auth/accounts/{account_id}", response_model=None)
        async def auth_account_update(
            account_id: int, body: AccountPatchBody, request: Request
        ) -> dict[str, object] | Response:
            try:
                return await auth.update_account(
                    account_id,
                    display_name=body.display_name,
                    role=body.role,
                    enabled=body.enabled,
                    actor=request.state.web_session.username,
                )
            except ValueError as error:
                return JSONResponse(
                    {"error": {"code": "account_invalid", "message": str(error)}},
                    status_code=422,
                )

        @app.post("/api/v1/auth/accounts/{account_id}/password", response_model=None)
        async def auth_account_password(
            account_id: int, body: AccountPasswordBody, request: Request
        ) -> dict[str, bool] | Response:
            try:
                await auth.reset_password(
                    account_id,
                    body.temporary_password,
                    request.state.web_session.username,
                )
            except ValueError as error:
                return JSONResponse(
                    {"error": {"code": "account_invalid", "message": str(error)}},
                    status_code=422,
                )
            return {"ok": True}

        @app.patch("/api/v1/auth/accounts/{account_id}/radio", response_model=None)
        async def auth_account_radio(
            account_id: int, body: AccountRadioBody, request: Request
        ) -> dict[str, object] | Response:
            try:
                return await auth.link_operator_radio(
                    account_id,
                    body.member_id,
                    request.state.web_session.username,
                )
            except ValueError as error:
                return JSONResponse(
                    {"error": {"code": "account_radio_invalid", "message": str(error)}},
                    status_code=422,
                )

        @app.get("/api/v1/auth/sessions")
        async def auth_sessions(request: Request) -> dict[str, Any]:
            session = request.state.web_session
            items = await auth.sessions(
                session.account_id, request.cookies.get("outpost_session", "")
            )
            return {"items": items, "count": len(items)}

        @app.delete("/api/v1/auth/sessions/{session_id}", response_model=None)
        async def auth_session_revoke(
            session_id: str, request: Request, response: Response
        ) -> dict[str, bool] | Response:
            session = request.state.web_session
            current = next(
                (
                    item
                    for item in await auth.sessions(
                        session.account_id, request.cookies.get("outpost_session", "")
                    )
                    if item["id"] == session_id
                ),
                None,
            )
            if not await auth.revoke_session(session.account_id, session_id, session.username):
                return JSONResponse(
                    {"error": {"code": "not_found", "message": "Session not found."}},
                    status_code=404,
                )
            if current and current["current"]:
                response.delete_cookie("outpost_session")
            return {"ok": True}

        @app.delete("/api/v1/auth/sessions")
        async def auth_sessions_revoke(request: Request, response: Response) -> dict[str, int]:
            session = request.state.web_session
            count = await auth.revoke_all_sessions(session.account_id, session.username)
            response.delete_cookie("outpost_session")
            return {"revoked": count}

        @app.post("/api/v1/auth/mfa/begin")
        async def auth_mfa_begin(request: Request) -> dict[str, str]:
            session = request.state.web_session
            return await auth.begin_mfa(session.account_id, session.username)

        @app.post("/api/v1/auth/mfa/confirm", response_model=None)
        async def auth_mfa_confirm(
            body: MfaConfirmBody, request: Request
        ) -> dict[str, object] | Response:
            session = request.state.web_session
            try:
                codes = await auth.confirm_mfa(session.account_id, session.username, body.code)
            except ValueError as error:
                return JSONResponse(
                    {"error": {"code": "mfa_invalid", "message": str(error)}},
                    status_code=422,
                )
            return {"ok": True, "recovery_codes": codes}

        @app.delete("/api/v1/auth/mfa")
        async def auth_mfa_disable(request: Request) -> dict[str, bool]:
            session = request.state.web_session
            await auth.disable_mfa(session.account_id, session.username)
            return {"ok": True}

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
        ai = status.get("ai", {})
        ai_ready = not (
            isinstance(ai, dict)
            and ai.get("required_for_readiness") is True
            and ai.get("ready") is not True
        )
        ready = radio == "up" and tasks_healthy and ai_ready
        return JSONResponse(
            {
                "status": "ok" if ready else "degraded",
                "version": __version__,
            },
            status_code=200 if ready else 503,
        )

    @app.get("/api/v1/runtime")
    async def runtime_mode() -> dict[str, object]:
        runtime = status_provider().get("runtime", {})
        if not isinstance(runtime, dict):
            runtime = {}
        return {
            "mode": str(runtime.get("mode", "live")),
            "simulated": bool(runtime.get("simulated", False)),
            "source": runtime.get("source"),
            "store": str(runtime.get("store", "live")),
            "transmit": str(runtime.get("transmit", "radio")),
        }

    @app.get("/api/v1/diagnostics/status", response_class=JSONResponse, response_model=None)
    async def diagnostic_status(request: Request) -> dict[str, Any] | Response:
        host = request.client.host if request.client is not None else ""
        try:
            local_request = ipaddress.ip_address(host).is_loopback
        except ValueError:
            local_request = False
        if not local_request:
            return JSONResponse(
                {"error": {"code": "loopback_required", "message": "Local access required."}},
                status_code=403,
            )
        status = status_provider()
        tasks = status.get("tasks", {})
        if not isinstance(tasks, dict):
            tasks = {}
        safe_tasks = {
            str(name): {
                key: value
                for key, value in task.items()
                if key
                in {
                    "state",
                    "failure_domain",
                    "required",
                    "started_at",
                    "last_started_at",
                    "last_ok_at",
                    "stopped_at",
                    "degraded_reason",
                    "failure_count",
                    "consecutive_failures",
                    "restart_count",
                    "last_error",
                    "last_error_at",
                    "next_retry_at",
                    "circuit_open",
                }
            }
            for name, task in tasks.items()
            if isinstance(task, dict)
        }
        radio_config = status.get("radio_config", {})
        safe_radio_config = (
            {
                key: value
                for key, value in radio_config.items()
                if key in {"region", "preset", "channels", "missing_policy_channels"}
            }
            if isinstance(radio_config, dict)
            else {}
        )
        ai = status.get("ai", {})
        safe_ai = (
            {
                key: value
                for key, value in ai.items()
                if key
                in {
                    "provider",
                    "model",
                    "external",
                    "pending",
                    "circuit_open",
                    "circuit_open_until",
                    "ready",
                    "health_state",
                    "health_detail",
                    "health_checked_at",
                    "required_for_readiness",
                }
            }
            if isinstance(ai, dict)
            else {}
        )
        receiver = status.get("same_receiver", {})
        safe_receiver = (
            {
                key: value
                for key, value in receiver.items()
                if key
                in {
                    "state",
                    "restart_count",
                    "last_audio_at",
                    "last_signal_at",
                    "last_decode_at",
                    "next_restart_at",
                }
            }
            if isinstance(receiver, dict)
            else {}
        )
        providers = weather.provider_health() if weather is not None else {}
        safe_providers = {
            str(name): {
                key: value for key, value in provider.items() if key in {"status", "failures"}
            }
            for name, provider in providers.items()
            if isinstance(provider, dict)
        }
        intents = status.get("intents", {})
        safe_intents = (
            {
                key: value
                for key, value in intents.items()
                if key
                in {"path", "exists", "loaded", "rejected", "builtin", "state", "error", "issues"}
            }
            if isinstance(intents, dict)
            else {}
        )
        return {
            "radio": status.get("radio", "unknown"),
            "radio_config": safe_radio_config,
            "tasks_healthy": status.get("tasks_healthy"),
            "subsystems_healthy": status.get("subsystems_healthy"),
            "tasks": safe_tasks,
            "ai": safe_ai,
            "same_receiver": safe_receiver,
            "providers": safe_providers,
            "intents": safe_intents,
            "readiness": (
                await self_check.latest()
                if self_check is not None
                else {"status": "unavailable", "checks": []}
            ),
        }

    if self_check is not None:

        @app.post(
            "/api/v1/diagnostics/readiness",
            response_class=JSONResponse,
            response_model=None,
        )
        async def diagnostic_readiness(request: Request) -> dict[str, Any] | Response:
            host = request.client.host if request.client is not None else ""
            try:
                local_request = ipaddress.ip_address(host).is_loopback
            except ValueError:
                local_request = False
            if not local_request:
                return JSONResponse(
                    {"error": {"code": "loopback_required", "message": "Local access required."}},
                    status_code=403,
                )
            return await self_check.run("diagnostics-cli")

        @app.get("/api/v1/readiness")
        async def readiness() -> dict[str, Any]:
            return await self_check.latest()

        @app.post("/api/v1/readiness/run")
        async def run_readiness() -> dict[str, Any]:
            return await self_check.run(f"dashboard:{current_actor_ref()}")

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
        if federation_topology is not None:

            @app.get("/api/v1/federation/topology")
            async def federation_topology_view() -> dict[str, Any]:
                return await federation_topology.overview()

            @app.put("/api/v1/federation/topology/peers/{mesh_id}", response_model=None)
            async def federation_topology_policy(
                mesh_id: str, body: FederationTopologyPolicyBody
            ) -> dict[str, Any] | Response:
                try:
                    policy = await federation_topology.set_policy(
                        mesh_id, **body.model_dump(), actor=current_actor()
                    )
                except ValueError as error:
                    return JSONResponse(
                        {
                            "error": {
                                "code": "topology_policy_failed",
                                "message": str(error),
                            }
                        },
                        status_code=409,
                    )
                return policy.json()

        if federation_relay is not None:

            @app.get("/api/v1/federation/relay")
            async def federation_relay_view() -> dict[str, Any]:
                return {
                    "summary": await federation_relay.summary(),
                    "queue": await federation_relay.queue(),
                    "policies": [policy.json() for policy in await federation_relay.policies()],
                    "origins": await federation_relay.origins(),
                    "identity": await federation_relay.identity_status(),
                }

            @app.post("/api/v1/federation/relay", response_model=None)
            async def federation_relay_create(
                body: FederationRelayCreateBody,
            ) -> dict[str, str] | Response:
                try:
                    envelope_id = await federation_relay.create(
                        body.destination,
                        body.scope,
                        body.payload,
                        expires_in=body.expires_in,
                        hop_limit=body.hop_limit,
                        idempotency_key=body.idempotency_key,
                        actor=current_actor(),
                    )
                except ValueError as error:
                    return JSONResponse(
                        {"error": {"code": "relay_create_failed", "message": str(error)}},
                        status_code=409,
                    )
                return {"envelope_id": envelope_id, "state": "queued"}

            @app.put("/api/v1/federation/relay/peers/{mesh_id}", response_model=None)
            async def federation_relay_policy(
                mesh_id: str, body: FederationRelayPolicyBody
            ) -> dict[str, Any] | Response:
                try:
                    policy = await federation_relay.set_policy(
                        mesh_id,
                        **body.model_dump(),
                        actor=current_actor(),
                    )
                except ValueError as error:
                    return JSONResponse(
                        {"error": {"code": "relay_policy_failed", "message": str(error)}},
                        status_code=409,
                    )
                return policy.json()

            @app.patch("/api/v1/federation/relay/origins/{origin_node}", response_model=None)
            async def federation_relay_origin(
                origin_node: str, body: FederationRelayOriginBody
            ) -> dict[str, str] | Response:
                try:
                    await federation_relay.review_origin(
                        origin_node.lower(),
                        body.state,
                        current_actor(),
                        fingerprint=body.fingerprint,
                    )
                except ValueError as error:
                    return JSONResponse(
                        {"error": {"code": "relay_origin_failed", "message": str(error)}},
                        status_code=409,
                    )
                return {"origin_node": origin_node.lower(), "state": body.state}

            @app.post("/api/v1/federation/relay/identity/rotate", response_model=None)
            async def federation_relay_rotate_identity() -> dict[str, Any] | Response:
                try:
                    return await federation_relay.rotate_identity(current_actor())
                except ValueError as error:
                    return JSONResponse(
                        {"error": {"code": "relay_identity_failed", "message": str(error)}},
                        status_code=409,
                    )

            @app.patch("/api/v1/federation/relay/{envelope_id}", response_model=None)
            async def federation_relay_action(
                envelope_id: str, body: FederationRelayActionBody
            ) -> dict[str, str] | Response:
                try:
                    await federation_relay.item_action(envelope_id, body.action, current_actor())
                except ValueError as error:
                    return JSONResponse(
                        {"error": {"code": "relay_action_failed", "message": str(error)}},
                        status_code=409,
                    )
                return {"envelope_id": envelope_id, "state": body.action}

        if federation_mail_send is not None and database is not None:

            @app.get("/api/v1/federation/mail")
            async def federation_mail_list() -> dict[str, Any]:
                rows = await database.read(
                    "SELECT d.*,p.mesh_id,p.node_name FROM fed_mail_delivery d "
                    "JOIN fed_peer p ON p.id=d.peer_id ORDER BY d.created_at DESC LIMIT 100"
                )
                usage_rows = await database.read(
                    "SELECT p.id,p.mesh_id,p.node_name,p.quota_mail_per_hour,"
                    "p.quota_mail_per_recipient_per_hour,COALESCE(u.inbound_accepted,0) "
                    "inbound_accepted,COALESCE(u.inbound_rejected,0) inbound_rejected "
                    "FROM fed_peer p LEFT JOIN fed_mail_usage u ON u.peer_id=p.id "
                    "AND u.window_start=unixepoch()-unixepoch()%3600 "
                    "WHERE p.state IN ('active','paused') AND p.relay_mail=1 ORDER BY p.mesh_id"
                )
                recipient_rows = await database.read(
                    "SELECT r.peer_id,r.recipient_handle,r.inbound_accepted "
                    "FROM fed_mail_recipient_usage r JOIN fed_peer p ON p.id=r.peer_id "
                    "WHERE r.window_start=unixepoch()-unixepoch()%3600 AND p.relay_mail=1 "
                    "ORDER BY r.peer_id,r.inbound_accepted DESC,r.recipient_handle"
                )
                recipients: dict[int, list[dict[str, Any]]] = {}
                for recipient in recipient_rows:
                    recipients.setdefault(int(recipient["peer_id"]), []).append(
                        {
                            "recipient_handle": recipient["recipient_handle"],
                            "inbound_accepted": int(recipient["inbound_accepted"]),
                        }
                    )
                return {
                    "items": [dict(row) for row in rows],
                    "usage": [
                        {
                            **dict(row),
                            "recipients": recipients.get(int(row["id"]), []),
                        }
                        for row in usage_rows
                    ],
                }

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
            return {
                "items": [{**peer.__dict__, **federation.liveness(peer)} for peer in peers],
                "count": len(peers),
            }

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
                await write_audit(
                    database,
                    actor_kind="web",
                    actor_ref=current_actor_ref(),
                    action="federation.peer_state",
                    target=f"fed_peer:{peer.id}",
                    detail=body.state,
                )
            return peer.__dict__

        @app.delete("/api/v1/federation/peers/{mesh_id}", response_model=None)
        async def federation_peer_forget(mesh_id: str) -> dict[str, bool] | Response:
            try:
                peer = await federation.by_mesh_id(mesh_id)
                await federation.forget(mesh_id, current_actor())
            except ValueError as error:
                return JSONResponse(
                    {"error": {"code": "peer_forget_failed", "message": str(error)}},
                    status_code=409,
                )
            if database is not None:
                await write_audit(
                    database,
                    actor_kind="web",
                    actor_ref=current_actor_ref(),
                    action="federation.peer_forget",
                    target=f"fed_peer:{peer.mesh_id}",
                    detail=peer.node_name,
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
                    "?) ON CONFLICT(old_mesh_id) DO UPDATE SET "
                    "successor_peer_id=excluded.successor_peer_id,old_node_name=excluded.old_node_name,"
                    "adopted_at=excluded.adopted_at,adopted_by=excluded.adopted_by",
                    (body.old_mesh_id.lower(), peer.id, body.old_node_name, current_actor()),
                )
                await write_audit(
                    database,
                    actor_kind="web",
                    actor_ref=current_actor_ref(),
                    action="federation.origin_adopt",
                    target=f"fed_peer:{peer.id}",
                    detail=body.old_mesh_id.lower(),
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
                    applied_by=current_actor(),
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
                        checkpoint_status = str(checkpoint.get("status") or "active")
                        catchup_map[int(cursor["peer_id"])] = {
                            "active": checkpoint_status == "active"
                            and (bool(checkpoint.get("pending")) or bool(before)),
                            "waiting": bool(checkpoint.get("pending")),
                            "status": checkpoint_status,
                            "reason": checkpoint.get("reason"),
                            "used": checkpoint.get("used", 0),
                            "budget": checkpoint.get("budget"),
                            "rounds": checkpoint.get("rounds", 0),
                            "resume_after": checkpoint.get("resume_after"),
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
                                "status": None,
                                "reason": None,
                                "used": 0,
                                "budget": None,
                                "rounds": 0,
                                "resume_after": None,
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
                            "connectivity": (
                                "online"
                                if federation.is_online_at(str(peer["state"]), peer["last_seen_at"])
                                else "offline"
                                if peer["state"] == "active"
                                else None
                            ),
                            "sync_paused": peer["state"] == "active"
                            and not federation.is_online_at(
                                str(peer["state"]), peer["last_seen_at"]
                            ),
                            "stale_after_seconds": federation.peer_stale_seconds,
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
                    "reviewed_by=?,rejection_reason=? WHERE id=?",
                    (current_actor(), body.reason, item_id),
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
                return cast(dict[str, Any], peer.__dict__)

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
                return cast(dict[str, Any], peer.__dict__)

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

    @app.get("/api/v1/radio/channels")
    async def radio_channels(response: Response) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        return await radio_channel_map()

    if radio_configuration_status is not None:

        @app.get("/api/v1/radio/config")
        async def radio_configuration_view(response: Response) -> dict[str, Any]:
            response.headers["Cache-Control"] = "no-store"
            return await radio_configuration_status()

    if radio_configuration_preflight is not None:

        @app.post("/api/v1/radio/config/preflight", response_model=None)
        async def radio_configuration_preflight_view(
            body: RadioConfigurationBody, response: Response
        ) -> dict[str, Any] | Response:
            response.headers["Cache-Control"] = "no-store"
            section, values = body.change()
            try:
                return await radio_configuration_preflight(section, values)
            except (
                ConnectionError,
                KeyError,
                OSError,
                TimeoutError,
                TypeError,
                ValueError,
            ) as error:
                payload: dict[str, Any] = {
                    "error": {"code": "radio_preflight_failed", "message": str(error)}
                }
                operation = getattr(error, "operation", None)
                if operation is not None:
                    payload["operation"] = operation
                return JSONResponse(payload, status_code=409, headers={"Cache-Control": "no-store"})

    if radio_configuration_configure is not None or radio_configuration_apply is not None:

        @app.put("/api/v1/radio/config", response_model=None)
        async def radio_configuration_update(
            body: RadioConfigurationBody, response: Response
        ) -> dict[str, Any] | Response:
            response.headers["Cache-Control"] = "no-store"
            section, values = body.change()
            try:
                if radio_configuration_apply is not None:
                    if body.preflight_id is None:
                        raise ValueError("review a fresh radio preflight before applying changes")
                    result = await radio_configuration_apply(body.preflight_id, section, values)
                elif radio_configuration_configure is not None:
                    result = await radio_configuration_configure(section, values)
                else:  # pragma: no cover - route construction guarantees a callback
                    raise RuntimeError("radio configuration callback is unavailable")
            except (
                ConnectionError,
                KeyError,
                OSError,
                TimeoutError,
                TypeError,
                ValueError,
            ) as error:
                payload: dict[str, Any] = {
                    "error": {
                        "code": "radio_config_failed",
                        "message": str(error),
                    }
                }
                operation = getattr(error, "operation", None)
                if operation is not None:
                    payload["operation"] = operation
                return JSONResponse(
                    payload,
                    status_code=409,
                    headers={"Cache-Control": "no-store"},
                )
            if database is not None and radio_configuration_apply is None:
                secret_fields = {"password", "psk"}
                changed_fields = sorted(
                    name
                    for name, value in values.items()
                    if value is not None and name not in secret_fields
                )
                detail: dict[str, Any] = {"fields": changed_fields}
                if section == "channel":
                    detail["channel"] = int(values["index"])
                await write_audit(
                    database,
                    actor_kind="web",
                    actor_ref=current_actor_ref(),
                    action="radio.config_update",
                    target=f"radio/{section}",
                    detail=detail,
                )
            return result

    @app.get("/api/v1/status")
    async def status() -> dict[str, Any]:
        return status_provider()

    if situation is not None:

        @app.get("/api/v1/sitrep", response_model=None)
        async def situation_brief(
            request: Request,
            ai: bool = Query(default=False),
            since: datetime | None = None,
        ) -> dict[str, Any] | Response:
            session = getattr(request.state, "web_session", None)
            capability = (
                BriefingCapability.PUBLIC
                if session is not None and session.role == "viewer"
                else BriefingCapability.OPERATOR
            )
            viewer = (
                BriefingViewer("web_account", int(session.account_id))
                if session is not None and session.role != "viewer"
                else None
            )
            since_epoch = None
            if since is not None:
                if since.tzinfo is None:
                    return JSONResponse(
                        {
                            "error": {
                                "code": "sitrep_since_timezone_required",
                                "message": "The since time must include a UTC offset.",
                            }
                        },
                        status_code=422,
                    )
                since_epoch = int(since.timestamp())
                if since_epoch > int(situation.clock.now().timestamp()):
                    return JSONResponse(
                        {
                            "error": {
                                "code": "sitrep_since_in_future",
                                "message": "The since time cannot be in the future.",
                            }
                        },
                        status_code=422,
                    )
            return await situation.snapshot(
                capability,
                include_ai=ai,
                viewer=viewer,
                since=since_epoch,
            )

    @app.get("/api/v1/web/transport", tags=["web-transport"])
    async def web_transport(request: Request) -> dict[str, object]:
        return transport_status(
            effective_web_config,
            request_secure=request.url.scheme == "https",
        )

    @app.get("/api/v1/modules")
    async def modules() -> dict[str, Any]:
        return {
            "items": {
                name: {"enabled": enabled, "restart_required_to_change": True}
                for name, enabled in effective_modules().items()
            },
            "change_policy": "restart_required",
        }

    @app.get("/api/v1/dashboard/poll", response_model=None)
    async def dashboard_poll(request: Request) -> Response:
        reviews = {
            "total": 0,
            "board": 0,
            "incidents": 0,
            "alerts": 0,
            "members": 0,
            "data_requests": 0,
        }
        actionable_mail = 0
        same_pending = 0
        local_incident_reviews = 0
        if database is not None:
            rows = await database.read(
                f"""WITH reviews AS (
                     SELECT COUNT(*) total,
                            COALESCE(SUM(stream LIKE 'board:%'),0) board,
                            COALESCE(SUM(stream='incidents'),0) incidents,
                            COALESCE(SUM(stream='alerts'),0) alerts
                     FROM fed_inbox_item WHERE state='pending'
                   )
                   SELECT reviews.*,
                     (SELECT COUNT(*) FROM (
                        SELECT conversation_key FROM mail
                        WHERE conversation_key IS NOT NULL AND archived_at IS NULL AND
                          ((operator_read_at IS NULL AND mail_direction<>'out')
                           OR state IN ('failed','undeliverable'))
                        GROUP BY conversation_key
                        UNION
                        SELECT conversation_key FROM member_data_request WHERE state='pending'
                      )) actionable,
                     (SELECT COUNT(*) FROM same_event
                      WHERE review_state IN ('pending','duplicate')) same_pending,
                     (SELECT COUNT(*) FROM member
                      WHERE directory_state='active' AND
                        {NEEDS_REVIEW_SQL}) member_key_reviews,
                     (SELECT COUNT(*) FROM incident
                      WHERE reporter_id IS NOT NULL AND status='open'
                        AND merged_into_id IS NULL) local_incident_reviews,
                     (SELECT COUNT(*) FROM member_data_request
                      WHERE state='pending') member_data_requests
                   FROM reviews"""  # noqa: S608 - review expression is a fixed application constant.
            )
            reviews = {key: int(rows[0][key]) for key in ("total", "board", "incidents", "alerts")}
            reviews["members"] = int(rows[0]["member_key_reviews"])
            reviews["data_requests"] = int(rows[0]["member_data_requests"])
            actionable_mail = int(rows[0]["actionable"])
            same_pending = int(rows[0]["same_pending"])
            local_incident_reviews = int(rows[0]["local_incident_reviews"])
        value = {
            "modules": {
                "items": {
                    name: {"enabled": enabled, "restart_required_to_change": True}
                    for name, enabled in effective_modules().items()
                },
                "change_policy": "restart_required",
            },
            "reviews": reviews,
            "watch": {"incidents_pending_review": local_incident_reviews},
            "environment": {"same_pending": same_pending},
            "mail": {"actionable": actionable_mail},
            "readiness": (
                await self_check.latest()
                if self_check is not None
                else {"status": "unavailable", "safety_failures": 0, "checks": []}
            ),
        }
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        etag = f'"{sha256(encoded).hexdigest()[:24]}"'
        headers = {"ETag": etag, "Cache-Control": "private, max-age=0, must-revalidate"}
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)
        return Response(encoded, media_type="application/json", headers=headers)

    if database is not None:
        member_triage = MemberTriageService(database)

        if ai_service is not None and ai_store is not None:

            @app.get("/api/v1/ai/status")
            async def ai_status() -> dict[str, Any]:
                return await ai_service.status()

            @app.get("/api/v1/ai/interactions")
            async def ai_interactions(limit: int = Query(100, ge=1, le=250)) -> dict[str, Any]:
                return {"items": await ai_store.interactions(limit)}

            @app.get("/api/v1/ai/kb")
            async def ai_kb() -> dict[str, Any]:
                return {"items": await ai_store.documents()}

            @app.post("/api/v1/ai/kb", response_model=None)
            async def ai_kb_create(body: KBDocumentBody) -> dict[str, Any] | Response:
                try:
                    result = await ai_store.save_document(
                        **body.model_dump(), actor=current_actor_ref()
                    )
                except ValueError as error:
                    return JSONResponse(
                        {"error": {"code": "invalid_kb", "message": str(error)}},
                        status_code=422,
                    )
                return result.as_dict()

            @app.patch("/api/v1/ai/kb/{document_id}", response_model=None)
            async def ai_kb_update(
                document_id: int, body: KBDocumentBody
            ) -> dict[str, Any] | Response:
                try:
                    result = await ai_store.save_document(
                        **body.model_dump(),
                        document_id=document_id,
                        actor=current_actor_ref(),
                    )
                except ValueError as error:
                    return JSONResponse(
                        {"error": {"code": "invalid_kb", "message": str(error)}},
                        status_code=422,
                    )
                return result.as_dict()

            @app.delete("/api/v1/ai/kb/{document_id}", response_model=None)
            async def ai_kb_delete(document_id: int) -> dict[str, Any] | Response:
                if not await ai_store.delete_document(document_id, current_actor_ref()):
                    return JSONResponse(
                        {"error": {"code": "not_found", "message": "Document not found."}},
                        status_code=404,
                    )
                return {"deleted": True}

            @app.patch("/api/v1/ai/interactions/{interaction_id}/rating", response_model=None)
            async def ai_rate(interaction_id: int, body: AIRatingBody) -> dict[str, Any] | Response:
                if not await ai_store.rate(interaction_id, body.rating):
                    return JSONResponse(
                        {"error": {"code": "not_found", "message": "Interaction not found."}},
                        status_code=404,
                    )
                return {"rated": body.rating}

            @app.delete("/api/v1/ai/members/{member_id}/history")
            async def ai_delete_member_history(member_id: int) -> dict[str, int]:
                deleted = await ai_store.delete_member_history(member_id, current_actor_ref())
                return {"deleted": deleted}

            @app.post("/api/v1/ai/interactions/{interaction_id}/promote", response_model=None)
            async def ai_promote(
                interaction_id: int, body: AIPromoteBody
            ) -> dict[str, Any] | Response:
                try:
                    result = await ai_store.promote_interaction(
                        interaction_id, body.title, current_actor_ref()
                    )
                except ValueError as error:
                    return JSONResponse(
                        {"error": {"code": "invalid_promotion", "message": str(error)}},
                        status_code=422,
                    )
                return {"document_id": result.document_id}

            @app.get("/api/v1/ai/refusal-rules")
            async def ai_refusal_rules() -> dict[str, Any]:
                return {"items": await ai_store.refusal_rules()}

            @app.post("/api/v1/ai/refusal-rules", response_model=None)
            async def ai_add_refusal(body: AIRefusalBody) -> dict[str, Any] | Response:
                try:
                    rule_id = await ai_store.add_refusal_rule(
                        body.phrase, body.reason, current_actor_ref()
                    )
                except ValueError as error:
                    return JSONResponse(
                        {"error": {"code": "invalid_rule", "message": str(error)}},
                        status_code=422,
                    )
                return {"id": rule_id}

            @app.delete("/api/v1/ai/refusal-rules/{rule_id}", response_model=None)
            async def ai_delete_refusal(rule_id: int) -> dict[str, bool] | Response:
                if not await ai_store.delete_refusal_rule(rule_id, current_actor_ref()):
                    return JSONResponse(
                        {"error": {"code": "not_found", "message": "Rule not found."}},
                        status_code=404,
                    )
                return {"deleted": True}

            if ai_test is not None:

                @app.post("/api/v1/ai/test")
                async def ai_test_console(body: AITestBody) -> dict[str, object]:
                    return await ai_test(body.question)

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

                    if alerts is not None:

                        @app.post(
                            "/api/v1/environment/earthquakes/{quake_id}/approve",
                            response_model=None,
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
                                body.name,
                                body.latitude,
                                body.longitude,
                                body.category,
                                body.notes,
                                current_actor_ref(),
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
                                waypoint_id,
                                body.model_dump(exclude_none=True),
                                current_actor_ref(),
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
                            await waypoints.delete(waypoint_id, current_actor_ref())
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
                await write_audit(
                    database,
                    actor_kind="web",
                    actor_ref=current_actor_ref(),
                    action="radio.reconnect",
                    target="radio",
                )
                return {"status": "reconnecting"}

        if backups is not None:

            @app.get("/api/v1/backups")
            async def backup_list() -> dict[str, Any]:
                return {"items": backups.list()}

            @app.post("/api/v1/backups")
            async def backup_create() -> dict[str, Any]:
                path = await backups.create()
                await write_audit(
                    database,
                    actor_kind="web",
                    actor_ref=current_actor_ref(),
                    action="backup.create",
                    target=path.name,
                )
                return {
                    "backup": next(item for item in backups.list() if item["name"] == path.name)
                }

            @app.delete("/api/v1/backups/{name}", response_model=None)
            async def backup_delete(name: str, body: BackupDeleteBody) -> dict[str, str] | Response:
                try:
                    path = backups.remove_recovery(name, body.confirmation)
                except ValueError as error:
                    return JSONResponse(
                        {"error": {"code": "delete_rejected", "message": str(error)}},
                        status_code=422,
                    )
                await write_audit(
                    database,
                    actor_kind="web",
                    actor_ref=current_actor_ref(),
                    action="backup.recovery_delete",
                    target=name,
                )
                return {"status": "deleted", "name": path.name}

            @app.get("/api/v1/backups/{name}", response_class=FileResponse)
            async def backup_download(name: str) -> Response:
                path = backups.resolve_download(name)
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

        if maintenance is not None:

            @app.get("/api/v1/maintenance/storage")
            async def maintenance_storage() -> dict[str, Any]:
                return await maintenance.storage_report()

            @app.get("/api/v1/maintenance/preview")
            async def maintenance_preview() -> dict[str, Any]:
                return (await maintenance.preview()).as_dict()

            @app.post("/api/v1/maintenance/run", response_model=None)
            async def maintenance_run(
                body: MaintenanceRunBody,
            ) -> dict[str, Any] | Response:
                if body.confirmation != "CLEANUP":
                    return JSONResponse(
                        {
                            "error": {
                                "code": "confirmation_required",
                                "message": "Enter CLEANUP to run retention maintenance.",
                            }
                        },
                        status_code=422,
                    )
                try:
                    result = await maintenance.run(actor_kind="web", actor_ref=current_actor_ref())
                except RuntimeError as error:
                    return JSONResponse(
                        {"error": {"code": "maintenance_busy", "message": str(error)}},
                        status_code=409,
                    )
                return {"result": result.as_dict()}

        if incidents is not None:

            async def incident_origins(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
                assert database is not None
                peers = {
                    str(row["mesh_id"]): str(row["node_name"] or row["mesh_id"])
                    for row in await database.read("SELECT mesh_id,node_name FROM fed_peer")
                }
                for value in values:
                    origins = await incidents.origins(int(value["id"]))
                    remote_origins = [
                        origin for origin in origins if origin["source_kind"] == "federation"
                    ]
                    mesh_id = str(remote_origins[0]["origin_node"]) if remote_origins else None
                    value["remote"] = bool(remote_origins)
                    value["origin_mesh_id"] = mesh_id
                    value["origin_name"] = peers.get(mesh_id, mesh_id) if mesh_id else None
                    value["origins"] = origins
                return values

            @app.get("/api/v1/incidents")
            async def incident_list(
                status: str | None = None,
                type: str | None = None,
                limit: int = Query(50, ge=1, le=200),
            ) -> dict[str, Any]:
                values = await incidents.list(status=status, kind=type, limit=limit)
                return {"items": await incident_origins([value.json() for value in values])}

            @app.get("/api/v1/incidents/history")
            async def incident_history(
                type: str | None = None,
                limit: int = Query(100, ge=1, le=200),
            ) -> dict[str, Any]:
                values = await incidents.history(kind=type, limit=limit)
                return {
                    "items": await incident_origins([value.json() for value in values]),
                    "retention_days": incidents.history_retention_days,
                }

            async def incident_report_value(
                incident_id: int, since: datetime | None = None
            ) -> dict[str, Any] | Response:
                assert effective_incident_reports is not None
                since_epoch = None
                if since is not None:
                    if since.tzinfo is None:
                        return JSONResponse(
                            {
                                "error": {
                                    "code": "timeline_since_timezone_required",
                                    "message": "The since time must include a UTC offset.",
                                }
                            },
                            status_code=422,
                        )
                    since_epoch = int(since.timestamp())
                try:
                    return await effective_incident_reports.build(incident_id, since=since_epoch)
                except ValueError as error:
                    status = 404 if "not found" in str(error).lower() else 422
                    return JSONResponse(
                        {"error": {"code": "incident_report_unavailable", "message": str(error)}},
                        status_code=status,
                    )

            @app.get("/api/v1/incidents/{incident_id}/timeline", response_model=None)
            async def incident_timeline(
                incident_id: int, since: datetime | None = None
            ) -> dict[str, Any] | Response:
                return await incident_report_value(incident_id, since)

            @app.get("/api/v1/incidents/{incident_id}/handover", response_model=None)
            async def incident_handover(
                incident_id: int, request: Request
            ) -> dict[str, Any] | Response:
                assert effective_incident_reports is not None
                session = getattr(request.state, "web_session", None)
                if session is None:
                    return JSONResponse(
                        {"error": {"code": "unauthorized", "message": "Sign in required."}},
                        status_code=401,
                    )
                try:
                    return await effective_incident_reports.handover(
                        incident_id, int(session.account_id)
                    )
                except ValueError as error:
                    return JSONResponse(
                        {
                            "error": {
                                "code": "incident_report_unavailable",
                                "message": str(error),
                            }
                        },
                        status_code=404,
                    )

            @app.get("/api/v1/incidents/{incident_id}/timeline.csv", response_model=None)
            async def incident_timeline_csv(
                incident_id: int, since: datetime | None = None
            ) -> Response:
                value = await incident_report_value(incident_id, since)
                if isinstance(value, Response):
                    return value
                assert effective_incident_reports is not None
                reference = value["incident"]["local_ref"]
                assert database is not None
                await write_audit(
                    database,
                    actor_kind="web",
                    actor_ref=current_actor_ref(),
                    action="incident.report_export",
                    target=f"incident:{incident_id}",
                    detail={"format": "csv", "event_count": len(value["timeline"])},
                )
                return Response(
                    effective_incident_reports.csv_export(value),
                    media_type="text/csv",
                    headers={
                        "Content-Disposition": (
                            f'attachment; filename="outpost-incident-{reference}-timeline.csv"'
                        ),
                        "X-Outpost-Data-Classification": "coarse-operational-record",
                        "Cache-Control": "no-store",
                    },
                )

            @app.get("/api/v1/incidents/{incident_id}/offline.html", response_model=None)
            async def incident_offline_html(
                incident_id: int, since: datetime | None = None
            ) -> Response:
                value = await incident_report_value(incident_id, since)
                if isinstance(value, Response):
                    return value
                assert effective_incident_reports is not None
                reference = value["incident"]["local_ref"]
                assert database is not None
                await write_audit(
                    database,
                    actor_kind="web",
                    actor_ref=current_actor_ref(),
                    action="incident.report_export",
                    target=f"incident:{incident_id}",
                    detail={"format": "offline_html", "event_count": len(value["timeline"])},
                )
                return Response(
                    effective_incident_reports.offline_html(value),
                    media_type="text/html",
                    headers={
                        "Content-Disposition": (
                            f'attachment; filename="outpost-incident-{reference}-report.html"'
                        ),
                        "X-Outpost-Data-Classification": "coarse-operational-record",
                        "Cache-Control": "no-store",
                    },
                )

            @app.get("/api/v1/watch/map")
            async def watch_map(hours_ago: int = Query(0, ge=0, le=24)) -> dict[str, Any]:
                assert database is not None
                cutoff = int(datetime.now(UTC).timestamp()) - hours_ago * 3600
                incident_rows = await database.read(
                    """
                    SELECT * FROM incident
                    WHERE lat IS NOT NULL AND lon IS NOT NULL AND created_at<=?
                      AND merged_into_id IS NULL
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
                        body.text, None, force=body.force, operator_label=current_actor()
                    )
                except ValueError as error:
                    return JSONResponse(
                        {"error": {"code": "invalid_incident", "message": str(error)}},
                        status_code=422,
                    )
                if similar:
                    return JSONResponse({"similar": similar.json()}, status_code=409)
                assert created is not None
                await write_audit(
                    database,
                    actor_kind="web",
                    actor_ref=current_actor_ref(),
                    action="incident.create",
                    target=f"incident:{created.id}",
                    detail=created.title,
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
                canonical_id = value.merged_into_id or value.id
                canonical = await incidents.by_id(canonical_id)
                return {
                    **value.json(),
                    "canonical": (
                        canonical.json() if canonical and canonical.id != value.id else None
                    ),
                    "updates": await incidents.updates(canonical_id, 100),
                    "origins": await incidents.origins(canonical_id),
                    "provenance": await incidents.provenance(canonical_id),
                    "match_candidates": await incidents.match_candidates(value.id),
                }

            @app.post("/api/v1/incidents/{incident_id}/location", response_model=None)
            async def incident_location(
                incident_id: int, body: IncidentLocationBody
            ) -> dict[str, Any] | Response:
                try:
                    value = await incidents.operator_location(
                        incident_id, body.location, actor=current_actor()
                    )
                except ValueError as error:
                    return JSONResponse(
                        {"error": {"code": "invalid_location", "message": str(error)}},
                        status_code=422,
                    )
                return value.json()

            @app.patch("/api/v1/incidents/{incident_id}", response_model=None)
            async def incident_patch(
                incident_id: int, body: IncidentPatchBody
            ) -> dict[str, Any] | Response:
                changes = body.model_dump(exclude_none=True)
                if not changes:
                    return JSONResponse(
                        {"error": {"code": "empty_update", "message": "No changes supplied."}},
                        status_code=400,
                    )
                try:
                    updated = await incidents.operator_patch(
                        incident_id,
                        status=body.status,
                        severity=body.severity,
                        resolution=body.resolution,
                        actor=current_actor(),
                    )
                except ValueError as error:
                    return JSONResponse(
                        {"error": {"code": "invalid_update", "message": str(error)}},
                        status_code=422,
                    )
                await write_audit(
                    database,
                    actor_kind="web",
                    actor_ref=current_actor_ref(),
                    action="incident.update",
                    target=f"incident:{incident_id}",
                    detail=",".join(changes),
                )
                return updated.json()

            @app.post("/api/v1/incidents/{source_id}/merge", response_model=None)
            async def incident_merge(
                source_id: int, body: IncidentMergeBody
            ) -> dict[str, Any] | Response:
                try:
                    value = await incidents.merge(source_id, body.target_id, current_actor())
                except ValueError as error:
                    return JSONResponse(
                        {"error": {"code": "invalid_merge", "message": str(error)}},
                        status_code=422,
                    )
                await write_audit(
                    database,
                    actor_kind="web",
                    actor_ref=current_actor_ref(),
                    action="incident.merge",
                    target=f"incident:{source_id}",
                    detail=f"canonical incident:{body.target_id}",
                )
                return value.json()

            @app.post("/api/v1/incidents/{source_id}/unmerge", response_model=None)
            async def incident_unmerge(source_id: int) -> dict[str, Any] | Response:
                try:
                    value = await incidents.unmerge(source_id, current_actor())
                except ValueError as error:
                    return JSONResponse(
                        {"error": {"code": "invalid_unmerge", "message": str(error)}},
                        status_code=422,
                    )
                await write_audit(
                    database,
                    actor_kind="web",
                    actor_ref=current_actor_ref(),
                    action="incident.unmerge",
                    target=f"incident:{source_id}",
                    detail="identity restored",
                )
                return value.json()

            @app.post("/api/v1/incidents/{source_id}/reject-match", response_model=None)
            async def incident_reject_match(
                source_id: int, body: IncidentMergeBody
            ) -> dict[str, bool] | Response:
                try:
                    await incidents.reject_match(source_id, body.target_id, current_actor())
                except ValueError as error:
                    return JSONResponse(
                        {"error": {"code": "invalid_match", "message": str(error)}},
                        status_code=422,
                    )
                await write_audit(
                    database,
                    actor_kind="web",
                    actor_ref=current_actor_ref(),
                    action="incident.match_reject",
                    target=f"incident:{source_id}",
                    detail=f"candidate incident:{body.target_id}",
                )
                return {"ok": True}

            @app.post("/api/v1/incidents/{incident_id}/updates", response_model=None)
            async def incident_update(
                incident_id: int, body: IncidentUpdateBody
            ) -> dict[str, Any] | Response:
                try:
                    value = await incidents.operator_update(
                        incident_id, body.kind, body.note, actor=current_actor()
                    )
                except ValueError as error:
                    return JSONResponse(
                        {"error": {"code": "invalid_update", "message": str(error)}},
                        status_code=422,
                    )
                await write_audit(
                    database,
                    actor_kind="web",
                    actor_ref=current_actor_ref(),
                    action=f"incident.{body.kind}",
                    target=f"incident:{incident_id}",
                    detail=body.note[:500] or None,
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

            @app.post("/api/v1/alerts/estimate", response_model=None)
            async def alert_estimate(body: AlertCreateBody) -> dict[str, object] | Response:
                try:
                    channels = body.channels or [
                        channel_slot(settings.config, "watch", 3) if settings else 3
                    ]
                    await require_active_radio_channels(channels)
                    return await alerts.airtime_preview(
                        body.severity,
                        body.headline,
                        channels,
                        incident_ref=body.incident_ref,
                        lat=body.lat,
                        lon=body.lon,
                        radius_km=body.radius_km,
                    )
                except ValueError as error:
                    return JSONResponse(
                        {"error": {"code": "invalid_alert", "message": str(error)}},
                        status_code=422,
                    )

            @app.post("/api/v1/alerts", response_model=None)
            async def alert_create(body: AlertCreateBody) -> dict[str, Any] | Response:
                try:
                    channels = body.channels or [
                        channel_slot(settings.config, "watch", 3) if settings else 3
                    ]
                    await require_active_radio_channels(channels)
                    estimate = await alerts.airtime_preview(
                        body.severity,
                        body.headline,
                        channels,
                        incident_ref=body.incident_ref,
                        lat=body.lat,
                        lon=body.lon,
                        radius_km=body.radius_km,
                    )
                    if estimate["requires_confirmation"] and not body.airtime_confirmation:
                        return JSONResponse(
                            {
                                "error": {
                                    "code": "airtime_confirmation_required",
                                    "message": (
                                        "This alert would cross an airtime constraint. "
                                        "Review its displacement and confirm explicitly."
                                    ),
                                },
                                "airtime": estimate,
                            },
                            status_code=409,
                        )
                    value = await alerts.raise_alert(
                        body.severity,
                        body.headline,
                        current_actor(),
                        incident_ref=body.incident_ref,
                        channels=channels,
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
                await write_audit(
                    database,
                    actor_kind="web",
                    actor_ref=current_actor_ref(),
                    action="alert.raise_coalesced" if value.coalesced else "alert.raise",
                    target=f"alert:{value.id}",
                    detail=value.headline,
                )
                return value.json()

            @app.post("/api/v1/alerts/{alert_id}/cancel", response_model=None)
            async def alert_cancel(
                alert_id: int, body: AlertCancelBody
            ) -> dict[str, Any] | Response:
                try:
                    value = await alerts.cancel(alert_id, body.resolution, current_actor())
                except ValueError as error:
                    return JSONResponse(
                        {"error": {"code": "invalid_alert", "message": str(error)}},
                        status_code=422,
                    )
                await write_audit(
                    database,
                    actor_kind="web",
                    actor_ref=current_actor_ref(),
                    action="alert.cancel",
                    target=f"alert:{value.id}",
                    detail=body.resolution[:160],
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
                await write_audit(
                    database,
                    actor_kind="web",
                    actor_ref=current_actor_ref(),
                    action="alert.escalation_halt",
                    target=f"alert:{value.id}",
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

            if cap_alerts is not None and settings is not None:

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
                        result = await cap_alerts.poll(location.lat, location.lon)
                        if same_events is not None:
                            result["same_reconciled"] = await same_events.reconcile_cap_duplicates()
                        return result
                    except OSError as error:
                        return JSONResponse(
                            {"error": {"code": "provider_unavailable", "message": str(error)}},
                            status_code=503,
                        )

                @app.post("/api/v1/environment/alerts/{cap_id}/approve", response_model=None)
                async def environment_alert_approve(cap_id: int) -> dict[str, Any] | Response:
                    try:
                        value = await cap_alerts.approve(cap_id, alerts)
                        if same_events is not None:
                            await same_events.reconcile_cap_duplicates()
                    except ValueError as error:
                        return JSONResponse(
                            {"error": {"code": "not_eligible", "message": str(error)}},
                            status_code=422,
                        )
                    await write_audit(
                        database,
                        actor_kind="web",
                        actor_ref=current_actor_ref(),
                        action="cap.approve",
                        target=f"cap:{cap_id}",
                    )
                    return value

                @app.post("/api/v1/environment/alerts/{cap_id}/dismiss", response_model=None)
                async def environment_alert_dismiss(cap_id: int) -> dict[str, str] | Response:
                    try:
                        await cap_alerts.dismiss(cap_id, current_actor_ref())
                        if same_events is not None:
                            await same_events.reconcile_cap_duplicates()
                    except ValueError as error:
                        return JSONResponse(
                            {"error": {"code": "not_pending", "message": str(error)}},
                            status_code=422,
                        )
                    return {"status": "dismissed"}

            if same_events is not None:

                @app.get("/api/v1/environment/same")
                async def environment_same(include_expired: bool = False) -> dict[str, Any]:
                    health = (
                        same_receiver_health()
                        if same_receiver_health is not None
                        else same_events.health()
                    )
                    return {
                        "items": await same_events.list(include_expired=include_expired),
                        "health": health,
                    }

                @app.post("/api/v1/environment/same/{same_id}/approve", response_model=None)
                async def environment_same_approve(same_id: int) -> dict[str, Any] | Response:
                    try:
                        value = await same_events.approve(same_id, alerts)
                    except ValueError as error:
                        return JSONResponse(
                            {"error": {"code": "not_eligible", "message": str(error)}},
                            status_code=422,
                        )
                    await write_audit(
                        database,
                        actor_kind="web",
                        actor_ref=current_actor_ref(),
                        action="same.approve",
                        target=f"same:{same_id}",
                    )
                    return value

                @app.post("/api/v1/environment/same/{same_id}/dismiss", response_model=None)
                async def environment_same_dismiss(same_id: int) -> dict[str, str] | Response:
                    try:
                        await same_events.dismiss(same_id)
                    except ValueError as error:
                        return JSONResponse(
                            {"error": {"code": "not_pending", "message": str(error)}},
                            status_code=422,
                        )
                    await write_audit(
                        database,
                        actor_kind="web",
                        actor_ref=current_actor_ref(),
                        action="same.dismiss",
                        target=f"same:{same_id}",
                    )
                    return {"status": "dismissed"}

        if checkins is not None:

            @app.get("/api/v1/responder-groups")
            async def responder_group_list() -> dict[str, Any]:
                return {
                    "items": await checkins.groups(),
                    "eligible_members": await checkins.responder_candidates(),
                }

            @app.post("/api/v1/responder-groups", response_model=None)
            async def responder_group_create(
                body: ResponderGroupCreateBody,
            ) -> dict[str, Any] | Response:
                try:
                    value = await checkins.create_group(
                        body.name, body.response_type, current_actor()
                    )
                except ValueError as error:
                    return JSONResponse(
                        {"error": {"code": "invalid_responder_group", "message": str(error)}},
                        status_code=422,
                    )
                await write_audit(
                    database,
                    actor_kind="web",
                    actor_ref=current_actor_ref(),
                    action="responder_group.create",
                    target=f"responder_group:{value['id']}",
                    detail={"name": value["name"], "response_type": value["response_type"]},
                )
                return value

            @app.put("/api/v1/responder-groups/{group_id}/members", response_model=None)
            async def responder_group_members(
                group_id: int, body: ResponderGroupMembersBody
            ) -> dict[str, Any] | Response:
                try:
                    value = await checkins.set_group_members(
                        group_id, body.member_ids, current_actor()
                    )
                except ValueError as error:
                    return JSONResponse(
                        {"error": {"code": "invalid_group_members", "message": str(error)}},
                        status_code=422,
                    )
                await write_audit(
                    database,
                    actor_kind="web",
                    actor_ref=current_actor_ref(),
                    action="responder_group.members",
                    target=f"responder_group:{group_id}",
                    detail={"member_count": len(value["members"])},
                )
                return value

            @app.patch("/api/v1/responder-groups/{group_id}", response_model=None)
            async def responder_group_update(
                group_id: int, body: ResponderGroupUpdateBody
            ) -> dict[str, Any] | Response:
                try:
                    value = await checkins.update_group(group_id, body.name, body.response_type)
                except ValueError as error:
                    return JSONResponse(
                        {"error": {"code": "invalid_responder_group", "message": str(error)}},
                        status_code=422,
                    )
                await write_audit(
                    database,
                    actor_kind="web",
                    actor_ref=current_actor_ref(),
                    action="responder_group.update",
                    target=f"responder_group:{group_id}",
                    detail={"name": value["name"], "response_type": value["response_type"]},
                )
                return value

            @app.delete("/api/v1/responder-groups/{group_id}", response_model=None)
            async def responder_group_delete(group_id: int) -> dict[str, str] | Response:
                try:
                    await checkins.delete_group(group_id)
                except ValueError as error:
                    return JSONResponse(
                        {"error": {"code": "group_in_use", "message": str(error)}},
                        status_code=422,
                    )
                await write_audit(
                    database,
                    actor_kind="web",
                    actor_ref=current_actor_ref(),
                    action="responder_group.delete",
                    target=f"responder_group:{group_id}",
                )
                return {"status": "deleted"}

            @app.get("/api/v1/welfare-schedules")
            async def welfare_schedule_list() -> dict[str, Any]:
                return {
                    "items": [checkins.schedule_json(value) for value in await checkins.schedules()]
                }

            @app.post("/api/v1/welfare-schedules/preview", response_model=None)
            async def welfare_schedule_preview(
                body: WelfareSchedulePreviewBody,
            ) -> dict[str, Any] | Response:
                try:
                    return await checkins.schedule_preview(
                        body.name,
                        body.roster_policy,
                        body.responder_group_id,
                        cadence=body.cadence,
                        day_of_period=body.day_of_period,
                        local_time=body.local_time,
                        window_minutes=body.window_minutes,
                    )
                except ValueError as error:
                    return JSONResponse(
                        {"error": {"code": "invalid_schedule", "message": str(error)}},
                        status_code=422,
                    )

            @app.post("/api/v1/welfare-schedules", response_model=None)
            async def welfare_schedule_create(
                body: WelfareScheduleCreateBody,
            ) -> dict[str, Any] | Response:
                try:
                    preview = await checkins.schedule_preview(
                        body.name,
                        body.roster_policy,
                        body.responder_group_id,
                        cadence=body.cadence,
                        day_of_period=body.day_of_period,
                        local_time=body.local_time,
                        window_minutes=body.window_minutes,
                    )
                    if body.preview_token != preview["preview_token"]:
                        return JSONResponse(
                            {
                                "error": {
                                    "code": "schedule_preview_changed",
                                    "message": (
                                        "The drill audience or airtime changed. Review the updated "
                                        "preview before saving."
                                    ),
                                },
                                "preview": preview,
                            },
                            status_code=409,
                        )
                    if (
                        preview["airtime"]["requires_confirmation"]
                        and not body.airtime_confirmation
                    ):
                        return JSONResponse(
                            {
                                "error": {
                                    "code": "airtime_confirmation_required",
                                    "message": (
                                        "This drill schedule crosses an airtime constraint. "
                                        "Review and confirm its hard send ceiling."
                                    ),
                                },
                                "preview": preview,
                            },
                            status_code=409,
                        )
                    value = await checkins.create_schedule(
                        body.name,
                        body.cadence,
                        body.day_of_period,
                        body.local_time,
                        body.roster_policy,
                        current_actor(),
                        responder_group_id=body.responder_group_id,
                        window_minutes=body.window_minutes,
                        suppress_if_real_event=body.suppress_if_real_event,
                        airtime_confirmation=body.airtime_confirmation,
                        preview_token=body.preview_token,
                    )
                except ValueError as error:
                    return JSONResponse(
                        {"error": {"code": "invalid_schedule", "message": str(error)}},
                        status_code=422,
                    )
                await write_audit(
                    database,
                    actor_kind="web",
                    actor_ref=current_actor_ref(),
                    action="welfare_schedule.create",
                    target=f"welfare_schedule:{value.id}",
                    detail={
                        "recipient_limit": value.recipient_limit,
                        "airtime_limit_ms": value.airtime_limit_ms,
                    },
                )
                return checkins.schedule_json(value)

            @app.patch("/api/v1/welfare-schedules/{schedule_id}", response_model=None)
            async def welfare_schedule_state(
                schedule_id: int, body: WelfareScheduleStateBody
            ) -> dict[str, Any] | Response:
                try:
                    value = await checkins.set_schedule_enabled(schedule_id, body.enabled)
                except ValueError as error:
                    return JSONResponse(
                        {"error": {"code": "invalid_schedule_state", "message": str(error)}},
                        status_code=422,
                    )
                await write_audit(
                    database,
                    actor_kind="web",
                    actor_ref=current_actor_ref(),
                    action="welfare_schedule.state",
                    target=f"welfare_schedule:{schedule_id}",
                    detail={"enabled": body.enabled},
                )
                return checkins.schedule_json(value)

            @app.delete("/api/v1/welfare-schedules/{schedule_id}", response_model=None)
            async def welfare_schedule_delete(schedule_id: int) -> dict[str, str] | Response:
                try:
                    await checkins.delete_schedule(schedule_id)
                except ValueError as error:
                    return JSONResponse(
                        {"error": {"code": "not_found", "message": str(error)}},
                        status_code=404,
                    )
                await write_audit(
                    database,
                    actor_kind="web",
                    actor_ref=current_actor_ref(),
                    action="welfare_schedule.delete",
                    target=f"welfare_schedule:{schedule_id}",
                )
                return {"status": "deleted"}

            @app.get("/api/v1/welfare-report")
            async def welfare_report() -> dict[str, Any]:
                return await checkins.participation_report()

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
                    value = await checkins.open_event(
                        body.name,
                        body.roster_policy,
                        current_actor(),
                        responder_group_id=body.responder_group_id,
                    )
                except ValueError as error:
                    return JSONResponse(
                        {"error": {"code": "invalid_event", "message": str(error)}},
                        status_code=422,
                    )
                await write_audit(
                    database,
                    actor_kind="web",
                    actor_ref=current_actor_ref(),
                    action="event.open",
                    target=f"event:{value.id}",
                    detail=value.name,
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
                await write_audit(
                    database,
                    actor_kind="web",
                    actor_ref=current_actor_ref(),
                    action="event.close",
                    target=f"event:{value.id}",
                    detail=value.name,
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
                    airtime = await checkins.solicitation_airtime(event_id, recipients)
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
                    "airtime": airtime,
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
                    estimate = await checkins.solicitation_airtime(event_id)
                    if estimate["requires_confirmation"] and not body.airtime_confirmation:
                        return JSONResponse(
                            {
                                "error": {
                                    "code": "airtime_confirmation_required",
                                    "message": (
                                        "This welfare batch would cross an airtime constraint. "
                                        "Review its displacement and confirm explicitly."
                                    ),
                                },
                                "airtime": estimate,
                            },
                            status_code=409,
                        )
                    result = await checkins.solicit(event_id)
                except ValueError as error:
                    return JSONResponse(
                        {"error": {"code": "solicitation_rejected", "message": str(error)}},
                        status_code=422,
                    )
                await write_audit(
                    database,
                    actor_kind="web",
                    actor_ref=current_actor_ref(),
                    action="event.solicit",
                    target=f"event:{event_id}",
                    detail=f"recipients:{result['recipient_count']}",
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
                        quota_mail_per_recipient_per_hour=(peer.quota_mail_per_recipient_per_hour),
                        service_permissions=peer.service_permissions,
                        quota_services_per_hour=peer.quota_services_per_hour,
                        service_concurrency=peer.service_concurrency,
                        service_max_response_bytes=peer.service_max_response_bytes,
                        service_airtime_seconds_per_hour=peer.service_airtime_seconds_per_hour,
                    )
                await write_audit(
                    database,
                    actor_kind="web",
                    actor_ref=current_actor_ref(),
                    action="federation.board_policy",
                    target=f"board:{board_id}",
                    detail={"slug": slug, "enabled": enabled},
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
                  SUM(CASE WHEN directory_state='active' AND
                             (handle IS NOT NULL OR
                              trust IN ('member','trusted','responder','operator'))
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
                "maintenance": (
                    await maintenance.health()
                    if maintenance is not None
                    else {"status": "unavailable", "completed_at": None, "failures": {}}
                ),
                "readiness": (
                    await self_check.latest()
                    if self_check is not None
                    else {"status": "unavailable", "safety_failures": 0, "checks": []}
                ),
            }

        @app.get("/api/v1/wallboard/summary")
        async def wallboard_summary() -> dict[str, Any]:
            """Aggregate operational status without identities, content, or exact locations."""
            runtime = status_provider()
            counts = await database.read(
                """
                SELECT
                  SUM(directory_state='active' AND
                      (handle IS NOT NULL OR
                       trust IN ('member','trusted','responder','operator'))) members_total,
                  SUM(last_seen>=unixepoch()-86400) heard_24h,
                  SUM(last_seen>=unixepoch()-604800) heard_7d
                FROM member
                """
            )
            traffic = await database.read(
                """
                SELECT direction,COUNT(*) count,COALESCE(SUM(byte_len),0) bytes
                FROM message_log WHERE created_at>=unixepoch()-86400 GROUP BY direction
                """
            )
            boards = await database.read(
                """
                SELECT b.title,b.description,COUNT(t.id) thread_count
                FROM board b LEFT JOIN thread t ON t.board_id=b.id AND t.hidden=0
                WHERE b.archived=0 AND b.min_read_trust='guest'
                GROUP BY b.id ORDER BY b.sort_order,b.id
                """
            )
            channels = await database.read(
                """
                SELECT name,description,slot FROM channel_dir
                WHERE published=1 ORDER BY slot,name
                """
            )
            queue_counts = runtime.get("queues", {})
            queue_total = (
                sum(int(value or 0) for value in queue_counts.values())
                if isinstance(queue_counts, dict)
                else 0
            )
            modules = {
                "items": {
                    name: {"enabled": enabled, "restart_required_to_change": True}
                    for name, enabled in effective_modules().items()
                },
                "change_policy": "restart_required",
            }
            return {
                "status": {
                    "node": str(runtime.get("node") or "Outpost"),
                    "radio": str(runtime.get("radio") or "down"),
                    "airtime_used_ratio": float(runtime.get("airtime_used_ratio") or 0),
                    "queues": {"governed": queue_total},
                    "tasks_healthy": runtime.get("tasks_healthy") is not False,
                },
                "overview": {
                    "members": {key: int(value or 0) for key, value in dict(counts[0]).items()},
                    "traffic_24h": {
                        ("outbound" if row["direction"] == "out" else "inbound"): {
                            "count": int(row["count"]),
                            "bytes": int(row["bytes"]),
                        }
                        for row in traffic
                    },
                },
                "boards": {"items": [dict(row) for row in boards]},
                "channels": {"items": [dict(row) for row in channels]},
                "navigation": {
                    "modules": modules,
                    "reviews": {
                        "total": 0,
                        "board": 0,
                        "incidents": 0,
                        "alerts": 0,
                        "members": 0,
                    },
                    "environment": {"same_pending": 0},
                    "mail": {"actionable": 0},
                },
                "privacy": {
                    "mode": "aggregate",
                    "omitted": [
                        "identities",
                        "stable_identifiers",
                        "message_content",
                        "mail_metadata",
                        "coordinates",
                        "welfare_notes",
                        "operator_notes",
                    ],
                },
            }

        @app.get("/api/v1/members/map")
        async def member_map(
            view: Literal["approved", "discovered", "all"] = "approved",
        ) -> dict[str, Any]:
            now = int(datetime.now(UTC).timestamp())
            view_conditions = {
                "approved": "(m.handle IS NOT NULL OR m.trust IN "
                "('member','trusted','responder','operator'))",
                "discovered": "m.handle IS NULL AND m.trust IN ('guest','blocked')",
                "all": "1=1",
            }
            rows = await database.read(
                f"""SELECT m.id,m.mesh_id,m.handle,m.long_name,m.short_name,m.trust,
                          CASE WHEN m.handle IS NULL AND m.trust IN ('guest','blocked')
                               THEN 'discovered' ELSE 'approved' END AS category,
                          m.last_seen,m.last_heard_snr,
                          m.hops_away,json_extract(m.prefs,'$.position') AS privacy,
                          p.lat,p.lon,p.received_at,p.source,p.expires_at
                   FROM member m JOIN member_position p ON p.member_id=m.id
                   WHERE m.directory_state='active'
                     AND {view_conditions[view]}
                     AND p.expires_at>?
                   ORDER BY p.received_at DESC""",  # noqa: S608 - view is a fixed expression.
                (now,),
            )
            items = [dict(row) for row in rows]
            groups_by_member: dict[int, list[dict[str, Any]]] = {}
            for membership in await database.read(
                "SELECT gm.member_id,g.id,g.name,g.response_type "
                "FROM responder_group_member gm JOIN responder_group g ON g.id=gm.group_id "
                "ORDER BY g.name COLLATE NOCASE"
            ):
                groups_by_member.setdefault(int(membership["member_id"]), []).append(
                    {
                        "id": int(membership["id"]),
                        "name": membership["name"],
                        "response_type": membership["response_type"],
                    }
                )
            for item in items:
                received_at, expires_at = int(item["received_at"]), int(item["expires_at"])
                item["age_seconds"] = max(0, now - received_at)
                item["deletes_in_seconds"] = max(0, expires_at - now)
                item["retention_hours"] = max(1, (expires_at - received_at) // 3_600)
                item["privacy"] = item["privacy"] or "coarse"
                item["visibility"] = (
                    "operator exact; discovered from mesh broadcast"
                    if item["category"] == "discovered"
                    else f"operator exact; member {item['privacy']}"
                )
                item["responder_groups"] = groups_by_member.get(int(item["id"]), [])
                item["last_seen"] = _timestamp(item["last_seen"])
                item["received_at"] = _timestamp(received_at)
                item["expires_at"] = _timestamp(expires_at)
            return {"items": items}

        @app.delete("/api/v1/members/{member_id}/position", response_model=None)
        async def member_position_delete(member_id: int) -> dict[str, bool] | Response:
            assert effective_member_data is not None
            try:
                result = await effective_member_data.delete_position(
                    member_id,
                    actor_kind="web",
                    actor_ref=current_actor_ref(),
                )
            except ValueError:
                result = {"positions": 0}
            if not result["positions"]:
                return JSONResponse(
                    {
                        "error": {
                            "code": "not_found",
                            "message": "Member position not found.",
                        }
                    },
                    status_code=404,
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
                await write_audit(
                    transaction,
                    actor_kind="web",
                    actor_ref=current_actor_ref(),
                    action="member.position_purge",
                    target="member_position",
                    detail={
                        "deleted": count,
                        "pending_deleted": pending_count,
                        "expired_at_or_before": now,
                    },
                    created_at=now,
                )
            return {"deleted": count, "pending_deleted": pending_count}

        @app.get("/api/v1/members", response_model=None)
        async def members(
            cursor: int = Query(0, ge=0),
            limit: int = Query(50, ge=1, le=200),
            view: Literal["approved", "discovered", "archived", "all"] = "approved",
            saved: Literal["new", "recent", "stale", "member", "responder", "review"] | None = None,
            query: str = Query(default="", max_length=100),
        ) -> dict[str, Any] | Response:
            try:
                result = await member_triage.list(
                    view=view, saved=saved, query=query, cursor=cursor, limit=limit
                )
            except MemberTriageError as error:
                return JSONResponse(
                    {"error": {"code": error.code, "message": str(error)}}, status_code=422
                )
            for item in result["items"]:
                for key in (
                    "first_seen",
                    "last_seen",
                    "directory_state_at",
                    "reviewed_at",
                    "pki_verified_at",
                    "pki_last_seen_at",
                    "position_received_at",
                    "position_expires_at",
                ):
                    if item.get(key) is not None:
                        item[key] = _timestamp(int(item[key]))
            return result

        @app.get("/api/v1/members/export", response_model=None)
        async def member_export(ids: str = Query(min_length=1, max_length=1400)) -> Response:
            try:
                member_ids = [int(value) for value in ids.split(",")]
                content, count = await member_triage.export(member_ids, actor=current_actor())
            except (ValueError, MemberTriageError) as error:
                code = error.code if isinstance(error, MemberTriageError) else "invalid_selection"
                return JSONResponse(
                    {"error": {"code": code, "message": str(error)}}, status_code=422
                )
            return Response(
                content,
                media_type="text/csv",
                headers={
                    "Content-Disposition": 'attachment; filename="outpost-member-triage.csv"',
                    "X-Outpost-Export-Count": str(count),
                },
            )

        @app.post("/api/v1/members/bulk", response_model=None)
        async def member_bulk(body: MemberBulkBody) -> dict[str, Any] | Response:
            try:
                return await member_triage.bulk(
                    body.member_ids, body.action, body.reason, actor=current_actor()
                )
            except MemberTriageError as error:
                return JSONResponse(
                    {"error": {"code": error.code, "message": str(error)}}, status_code=422
                )

        @app.get("/api/v1/members/{member_id}", response_model=None)
        async def member_detail(member_id: int) -> dict[str, Any] | Response:
            result = await member_triage.detail(member_id)
            if result is None:
                return JSONResponse(
                    {"error": {"code": "not_found", "message": "Member not found."}},
                    status_code=404,
                )
            member = result["member"]
            for key in (
                "first_seen",
                "last_seen",
                "directory_state_at",
                "reviewed_at",
                "pki_verified_at",
                "pki_last_seen_at",
                "position_received_at",
                "position_expires_at",
            ):
                if member.get(key) is not None:
                    member[key] = _timestamp(int(member[key]))
            for item in result["recent_activity"]:
                item["created_at"] = _timestamp(int(item["created_at"]))
            for item in result["trust_history"]:
                item["created_at"] = _timestamp(int(item["created_at"]))
            for item in result["pki_events"]:
                item["created_at"] = _timestamp(int(item["created_at"]))
            return result

        @app.post("/api/v1/members/{member_id}/pki", response_model=None)
        async def member_pki_review(
            member_id: int, body: MemberPkiReviewBody
        ) -> dict[str, Any] | Response:
            try:
                return await member_triage.review_pki(
                    member_id,
                    body.action,
                    body.reason,
                    actor=current_actor(),
                )
            except MemberTriageError as error:
                status = 404 if error.code == "not_found" else 422
                return JSONResponse(
                    {"error": {"code": error.code, "message": str(error)}}, status_code=status
                )

        @app.post("/api/v1/members/{member_id}/state", response_model=None)
        async def member_state(member_id: int, body: MemberStateBody) -> dict[str, Any] | Response:
            try:
                return await member_triage.set_state(
                    member_id, body.action, body.reason, actor=current_actor()
                )
            except MemberTriageError as error:
                status = 404 if error.code == "not_found" else 422
                return JSONResponse(
                    {"error": {"code": error.code, "message": str(error)}}, status_code=status
                )

        @app.get("/api/v1/mesh/messages")
        async def mesh_messages(
            cursor: int = Query(0, ge=0),
            limit: int = Query(50, ge=1, le=200),
            direction: str | None = None,
            channel: int | None = Query(None, ge=0, le=7),
            outcome: str | None = None,
        ) -> dict[str, Any]:
            conditions: list[str] = ["1=1"]
            params: list[object] = []
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
            conditions: list[str] = ["1=1"]
            params: list[object] = []
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

        operator_inbox = OperatorInboxService(database)

        @app.get("/api/v1/member-data-requests")
        async def member_data_requests(
            state: Literal["pending", "approved", "rejected", "all"] = "pending",
        ) -> dict[str, Any]:
            assert effective_member_data is not None
            items = await effective_member_data.list_requests(state)
            for item in items:
                for key in ("requested_at", "reviewed_at"):
                    if item[key] is not None:
                        item[key] = _timestamp(item[key])
            return {
                "items": items,
                "pending": await effective_member_data.pending_count(),
                "removal_policy": retention_statement(effective_member_data.retention)[
                    "removal_policy"
                ],
            }

        @app.post("/api/v1/member-data-requests/{request_id}/review", response_model=None)
        async def member_data_request_review(
            request_id: int, body: MemberDataReviewBody
        ) -> dict[str, Any] | Response:
            assert effective_member_data is not None
            try:
                result = await effective_member_data.review(
                    request_id,
                    body.action,
                    body.reason,
                    current_actor_ref(),
                )
            except ValueError as error:
                return JSONResponse(
                    {"error": {"code": "review_failed", "message": str(error)}},
                    status_code=409,
                )
            for key in ("requested_at", "reviewed_at"):
                if result[key] is not None:
                    result[key] = _timestamp(result[key])
            return result

        @app.get("/api/v1/mail/conversations")
        async def mail_conversations(
            q: str = Query(default="", max_length=100),
            status: Literal["all", "unread", "read", "failed"] = "all",
            archive: Literal["active", "archived", "all"] = "active",
            route: Literal["all", "local", "federated"] = "all",
            kind: Literal["all", "member", "system"] = "all",
            limit: int = Query(default=100, ge=1, le=200),
        ) -> dict[str, Any]:
            result = await operator_inbox.list(
                query=q,
                status=status,
                archive=archive,
                route=route,
                kind=kind,
                limit=limit,
            )
            for item in result["items"]:
                for key in ("created_at", "updated_at", "archived_at"):
                    if item[key] is not None:
                        item[key] = _timestamp(item[key])
            return result

        @app.get("/api/v1/mail/conversations/{conversation_key}", response_model=None)
        async def mail_conversation(conversation_key: str) -> dict[str, Any] | Response:
            result = await operator_inbox.open(conversation_key)
            if result is None:
                return JSONResponse(
                    {"error": {"code": "not_found", "message": "Conversation not found."}},
                    status_code=404,
                )
            conversation = result["conversation"]
            for key in ("created_at", "updated_at", "archived_at"):
                if conversation[key] is not None:
                    conversation[key] = _timestamp(conversation[key])
            for item in result["messages"]:
                for key in ("created_at", "delivered_at", "operator_read_at", "archived_at"):
                    if item[key] is not None:
                        item[key] = _timestamp(item[key])
            return result

        @app.patch("/api/v1/mail/conversations/{conversation_key}", response_model=None)
        async def mail_conversation_state(
            conversation_key: str, body: MailConversationStateBody
        ) -> dict[str, bool] | Response:
            if not await operator_inbox.set_state(conversation_key, body.state):
                return JSONResponse(
                    {"error": {"code": "not_found", "message": "Conversation not found."}},
                    status_code=404,
                )
            return {"ok": True}

        @app.post("/api/v1/mail/conversations/{conversation_key}/reply", response_model=None)
        async def mail_conversation_reply(
            conversation_key: str, body: MailConversationReplyBody
        ) -> dict[str, object] | Response:
            route = await operator_inbox.reply_route(conversation_key)
            if route is None or federation_mail_reply is None:
                return JSONResponse(
                    {
                        "error": {
                            "code": "reply_unavailable",
                            "message": "This conversation has no safe federated reply route.",
                        }
                    },
                    status_code=409,
                )
            try:
                result = await federation_mail_reply(
                    route["source_peer_mesh_id"],
                    route["reply_recipient_handle"],
                    route["subject"] or "Mesh reply",
                    body.body,
                    route["federation_conversation_id"],
                    route["message_kind"],
                    route["participant_handle"],
                )
            except ValueError as error:
                return JSONResponse(
                    {"error": {"code": "mail_reply_failed", "message": str(error)}},
                    status_code=409,
                )
            await write_audit(
                database,
                actor_kind="web",
                actor_ref=current_actor_ref(),
                action="mail.conversation.reply",
                target=f"conversation:{conversation_key}",
                detail={
                    "peer_mesh_id": route["source_peer_mesh_id"],
                    "recipient": route["reply_recipient_handle"],
                },
            )
            return result

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
            await write_audit(
                database,
                actor_kind="web",
                actor_ref=current_actor_ref(),
                action="mail.view",
                target=f"mail:{mail_id}",
            )
            return JSONResponse(item)

        if radio_operations is not None:

            @app.get("/api/v1/mesh/queue")
            async def mesh_queue(
                state: Literal[
                    "current", "active", "failed", "expired", "terminal", "all"
                ] = "current",
                limit: int = Query(25, ge=1, le=100),
                cursor: int | None = Query(None, gt=0),
            ) -> dict[str, Any]:
                return await radio_operations.history(state, limit=limit, cursor=cursor)

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

            @app.get("/api/v1/mesh/power")
            async def mesh_power() -> dict[str, Any]:
                return await radio_operations.power()

            @app.post("/api/v1/mesh/estimate", response_model=None)
            async def mesh_estimate(body: MeshSendBody) -> dict[str, object] | Response:
                try:
                    await require_active_radio_channels([body.channel])
                    return radio_operations.estimate(
                        body.text, body.destination, body.channel, body.traffic_class
                    )
                except ValueError as error:
                    return JSONResponse(
                        {"error": {"code": "estimate_rejected", "message": str(error)}},
                        status_code=422,
                    )

            @app.post("/api/v1/mesh/send", response_model=None)
            async def mesh_send(body: MeshSendBody) -> dict[str, int] | Response:
                try:
                    await require_active_radio_channels([body.channel])
                    estimate = radio_operations.estimate(
                        body.text, body.destination, body.channel, body.traffic_class
                    )
                    if estimate["requires_confirmation"] and not body.airtime_confirmation:
                        return JSONResponse(
                            {
                                "error": {
                                    "code": "airtime_confirmation_required",
                                    "message": (
                                        "This message would cross an airtime constraint. "
                                        "Review its displacement and confirm explicitly."
                                    ),
                                },
                                "airtime": estimate,
                            },
                            status_code=409,
                        )
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
            notes_supplied = "notes" in body.model_fields_set
            if body.trust is None and not notes_supplied:
                return JSONResponse(
                    {"error": {"code": "empty_update", "message": "No changes supplied."}},
                    status_code=400,
                )
            try:
                result = await member_triage.update(
                    member_id,
                    trust=body.trust,
                    notes=body.notes,
                    notes_supplied=notes_supplied,
                    reason=body.reason,
                    actor=current_actor(),
                )
            except MemberTriageError as error:
                status = 404 if error.code == "not_found" else 422
                return JSONResponse(
                    {"error": {"code": error.code, "message": str(error)}}, status_code=status
                )
            return JSONResponse(result)

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
                (int(body.hidden), current_actor(), body.reason[:160], post_id),
            )
            if row["seq"] == 1:
                await database.write(
                    "UPDATE thread SET hidden=? WHERE id=?",
                    (int(body.hidden), row["thread_id"]),
                )
            await write_audit(
                database,
                actor_kind="web",
                actor_ref=current_actor_ref(),
                action="bbs.hide" if body.hidden else "bbs.unhide",
                target=f"post:{post_id}",
                detail=body.reason[:160],
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
                item["detail"], item["detail_format"] = display_audit_detail(item["detail"])
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

    @app.api_route(
        "/metrics/", methods=["GET", "HEAD"], include_in_schema=False, response_model=None
    )
    @app.api_route(
        "/metrics", methods=["GET", "HEAD"], include_in_schema=False, response_model=None
    )
    async def prometheus_metrics() -> Response:
        return Response(generate_latest(), headers={"Content-Type": CONTENT_TYPE_LATEST})

    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="dashboard")
    app.add_middleware(WebTransportMiddleware, config=effective_web_config.transport)
    return app
