from __future__ import annotations

import json
import os
import re
from datetime import time
from ipaddress import ip_network
from pathlib import Path
from typing import Annotated, Literal, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

TRAFFIC_CLASSES = {"alert", "reply", "ai", "bulletin", "digest", "federation"}
DEFAULT_TILES_PATH = "/var/lib/outpost/.data/tiles"
DEFAULT_RELEASES_PATH = "/opt/outpost/releases"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Location(StrictModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class NodeConfig(StrictModel):
    model_config = ConfigDict(extra="forbid", validate_default=True)

    name: str = "Outpost"
    short_name: str = "CRO"
    operator_contact: str = "ray@example.org"
    emergency_number: str = "911"
    timezone: str = "America/New_York"
    locale: str = "en_US"
    units: Literal["metric", "imperial"] = "metric"
    location: Location | None = None
    disclaimer: str = "Community system. Not 911."

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as error:
            if not available_timezones():
                raise ValueError(
                    "node.timezone cannot be validated because IANA tzdata is not installed"
                ) from error
            raise ValueError(
                f"node.timezone is not a valid installed IANA zone: {value}"
            ) from error
        return value

    @field_validator("locale")
    @classmethod
    def validate_locale(cls, value: str) -> str:
        if re.fullmatch(r"[a-z]{2,3}(?:_[A-Z]{2})?", value) is None:
            raise ValueError("node.locale must use ll or ll_CC form, for example en_US")
        return value

    @model_validator(mode="after")
    def validate_short_name(self) -> NodeConfig:
        if not 1 <= len(self.short_name.encode()) <= 4:
            raise ValueError("node.short_name must be 1..4 UTF-8 bytes")
        return self


class SerialConfig(StrictModel):
    port: str = "/dev/ttyUSB0"


class TcpConfig(StrictModel):
    host: str = "192.168.1.50"
    port: int = Field(default=4403, ge=1, le=65535)


class BleConfig(StrictModel):
    address: str = "AA:BB:CC:DD:EE:FF"


class ReconnectConfig(StrictModel):
    initial_s: float = Field(default=2, gt=0)
    max_s: float = Field(default=120, gt=0)
    jitter: float = Field(default=0.2, ge=0, le=1)


class RadioPowerConfig(StrictModel):
    warning_percent: int = Field(default=30, ge=1, le=100)
    critical_percent: int = Field(default=15, ge=0, le=99)
    sample_interval_s: int = Field(default=300, ge=30, le=3_600)
    trend_hours: int = Field(default=24, ge=1, le=168)
    shed_discretionary: bool = False
    shed_below_percent: int = Field(default=15, ge=0, le=100)

    @model_validator(mode="after")
    def validate_thresholds(self) -> RadioPowerConfig:
        if self.critical_percent >= self.warning_percent:
            raise ValueError("radio.power.critical_percent must be below warning_percent")
        return self


class RadioConfig(StrictModel):
    transport: Literal["serial", "tcp", "ble"] = "serial"
    serial: SerialConfig = Field(default_factory=SerialConfig)
    tcp: TcpConfig = Field(default_factory=TcpConfig)
    ble: BleConfig = Field(default_factory=BleConfig)
    reconnect: ReconnectConfig = Field(default_factory=ReconnectConfig)
    power: RadioPowerConfig = Field(default_factory=RadioPowerConfig)
    liveness_timeout_s: int = Field(default=300, gt=0)
    federation_portnum: int = Field(default=260, ge=256, le=511)
    bridge_node_ids: list[str] = Field(default_factory=list)


class QuietHours(StrictModel):
    start: str = "22:00"
    end: str = "06:00"
    classes: list[str] = Field(default_factory=lambda: ["digest", "bulletin", "federation"])

    @model_validator(mode="after")
    def validate_window(self) -> QuietHours:
        for field_name in ("start", "end"):
            try:
                parsed = time.fromisoformat(getattr(self, field_name))
            except ValueError as error:
                raise ValueError(
                    f"airtime.quiet_hours.{field_name} must be an ISO local time such as 22:00"
                ) from error
            if parsed.tzinfo is not None:
                raise ValueError(
                    f"airtime.quiet_hours.{field_name} must be a local time without an offset"
                )
        unknown = set(self.classes) - TRAFFIC_CLASSES
        if unknown:
            raise ValueError(f"airtime.quiet_hours.classes has unknown classes: {sorted(unknown)}")
        return self


class AirtimeConfig(StrictModel):
    budget_percent: float = Field(default=8.0, gt=0)
    utilisation_ceiling: float = Field(default=25.0, gt=0, le=40)
    emergency_reserve_percent: float = Field(default=4.0, ge=0)
    min_gap_s: float = Field(default=2.0, ge=0)
    interpart_delay_s: float = Field(default=12.0, ge=0)
    queue_max_items: int = Field(default=500, gt=0)
    dedupe_window_s: int = Field(default=300, ge=0)
    quiet_hours: QuietHours = Field(default_factory=QuietHours)
    class_shares: dict[str, float] = Field(
        default_factory=lambda: {
            "alert": 0.30,
            "reply": 0.30,
            "ai": 0.15,
            "bulletin": 0.05,
            "digest": 0.10,
            "federation": 0.10,
        }
    )
    max_parts: dict[str, int] = Field(
        default_factory=lambda: {
            "reply": 3,
            "ai": 2,
            "digest": 4,
            "alert": 2,
            "bulletin": 2,
            "federation": 1,
        }
    )

    @model_validator(mode="after")
    def validate_budget(self) -> AirtimeConfig:
        unknown = set(self.class_shares) - TRAFFIC_CLASSES
        if unknown:
            raise ValueError(f"airtime.class_shares has unknown classes: {sorted(unknown)}")
        missing = TRAFFIC_CLASSES - set(self.class_shares)
        if missing:
            raise ValueError(f"airtime.class_shares is missing classes: {sorted(missing)}")
        if any(value < 0 or value > 1 for value in self.class_shares.values()):
            raise ValueError("airtime.class_shares values must be between 0 and 1")
        unknown_parts = set(self.max_parts) - TRAFFIC_CLASSES
        if unknown_parts:
            raise ValueError(f"airtime.max_parts has unknown classes: {sorted(unknown_parts)}")
        missing_parts = TRAFFIC_CLASSES - set(self.max_parts)
        if missing_parts:
            raise ValueError(f"airtime.max_parts is missing classes: {sorted(missing_parts)}")
        if any(value < 1 for value in self.max_parts.values()):
            raise ValueError("airtime.max_parts values must be at least 1")
        if sum(self.class_shares.values()) > 1.0 + 1e-9:
            raise ValueError("airtime.class_shares must sum to <= 1.0")
        total = self.budget_percent + self.emergency_reserve_percent
        if total > 20:
            raise ValueError("airtime budget + emergency reserve must be <= 20%")
        if total >= self.utilisation_ceiling:
            raise ValueError("airtime budget + reserve must be below utilisation ceiling")
        return self


class ChannelConfig(StrictModel):
    name: str
    ai: bool = False
    bbs: Literal["none", "read_only", "full"] = "none"
    alerts: bool = True
    accept_reports: bool = True


class RouterConfig(StrictModel):
    prefix: str = "!"
    session_idle_minutes: int = 30
    page_ttl_minutes: int = 15
    inbound_workers: int = Field(default=4, ge=1, le=32)
    inbound_queue_max: int = Field(default=256, ge=1, le=4096)
    member_lock_timeout_s: float = 60
    intents_file: str = "config/intents.yaml"


class Enabled(StrictModel):
    enabled: bool = False


class ModulesConfig(StrictModel):
    bbs: Enabled = Field(default_factory=lambda: Enabled(enabled=True))
    ai: Enabled = Field(default_factory=Enabled)
    watch: Enabled = Field(default_factory=Enabled)
    env: Enabled = Field(default_factory=Enabled)
    fed: Enabled = Field(default_factory=Enabled)

    def enabled_map(self) -> dict[str, bool]:
        return {
            name: bool(getattr(self, name).enabled) for name in ("bbs", "ai", "watch", "env", "fed")
        }

    def is_enabled(self, name: str) -> bool:
        return self.enabled_map().get(name, True)


class AIProviderEndpoint(StrictModel):
    base_url: str = ""
    context_tokens: int = Field(default=2048, ge=256)
    api_key_env: str = Field(default="", pattern=r"^[A-Z][A-Z0-9_]*$|^$")


class AIHailoVLMConfig(StrictModel):
    model_path: Path = Path("/var/lib/outpost/models/Qwen3-VL-2B-Instruct.hef")
    context_tokens: int = Field(default=2048, ge=256)
    optimize_memory_on_device: bool = True


class AIBudgetConfig(StrictModel):
    system_tokens: int = Field(default=240, ge=64, le=260)
    evidence_tokens: int = Field(default=820, ge=0, le=4096)
    history_tokens: int = Field(default=200, ge=0, le=512)
    question_tokens: int = Field(default=110, ge=32, le=512)
    reserve_output_tokens: int = Field(default=220, ge=64, le=512)
    safety_margin_percent: float = Field(default=15, ge=15, le=40)


class AIKeepWarmConfig(StrictModel):
    enabled: bool = True
    interval_s: int = Field(default=240, ge=30, le=3600)


class AICircuitBreakerConfig(StrictModel):
    failures: int = Field(default=5, ge=1, le=20)
    window_minutes: int = Field(default=10, ge=1, le=60)
    open_minutes: int = Field(default=15, ge=1, le=120)


class AIConfig(StrictModel):
    provider: Literal["hailo_vlm", "hailo", "llamacpp", "ollama", "openai_compat", "null"] = (
        "hailo_vlm"
    )
    model: str = "Qwen3-VL-2B-Instruct"
    timeout_s: float = Field(default=45, gt=0, le=180)
    max_concurrency: int = Field(default=1, ge=1, le=4)
    queue_depth: int = Field(default=3, ge=0, le=20)
    max_output_tokens: int = Field(default=220, ge=32, le=512)
    required_for_readiness: bool = True
    budget: AIBudgetConfig = Field(default_factory=AIBudgetConfig)
    keep_warm: AIKeepWarmConfig = Field(default_factory=AIKeepWarmConfig)
    circuit_breaker: AICircuitBreakerConfig = Field(default_factory=AICircuitBreakerConfig)
    persona_addendum: str = Field(default="", max_length=200)
    hailo_vlm: AIHailoVLMConfig = Field(default_factory=AIHailoVLMConfig)
    hailo: AIProviderEndpoint = Field(
        default_factory=lambda: AIProviderEndpoint(base_url="http://127.0.0.1:8000")
    )
    llamacpp: AIProviderEndpoint = Field(
        default_factory=lambda: AIProviderEndpoint(
            base_url="http://127.0.0.1:8080", context_tokens=4096
        )
    )
    ollama: AIProviderEndpoint = Field(
        default_factory=lambda: AIProviderEndpoint(
            base_url="http://127.0.0.1:11434", context_tokens=4096
        )
    )
    openai_compat: AIProviderEndpoint = Field(
        default_factory=lambda: AIProviderEndpoint(
            context_tokens=4096, api_key_env="OUTPOST_AI_API_KEY"
        )
    )

    @model_validator(mode="after")
    def validate_provider_budget(self) -> AIConfig:
        endpoint = getattr(self, self.provider, None)
        if endpoint is not None and endpoint.context_tokens < 1600:
            raise ValueError("ai provider context_tokens must be at least 1600")
        if self.max_output_tokens > self.budget.reserve_output_tokens:
            raise ValueError("ai.max_output_tokens must not exceed budget.reserve_output_tokens")
        return self


class SameConfig(StrictModel):
    enabled: bool = False
    frequency_mhz: float = Field(default=162.55, ge=162.4, le=162.55)
    county_codes: list[str] = Field(default_factory=list)
    silence_alarm_minutes: int = Field(default=720, ge=5)
    device: str = Field(default="0", min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")
    sample_rate: Literal[22050, 24000, 32000, 44100, 48000] = 48000
    oversampling: int = Field(default=4, ge=1, le=16)
    gain_db: float | None = Field(default=None, ge=0, le=50)
    ppm: int = Field(default=0, ge=-200, le=200)
    signal_rms_threshold: int = Field(default=300, ge=0, le=32767)
    audio_stall_seconds: int = Field(default=15, ge=5, le=300)
    restart_initial_seconds: int = Field(default=2, ge=1, le=60)
    restart_max_seconds: int = Field(default=120, ge=5, le=900)
    rtl_fm_path: str = "rtl_fm"
    samedec_path: str = "samedec"

    @model_validator(mode="after")
    def validate_receiver(self) -> SameConfig:
        frequencies = {162.400, 162.425, 162.450, 162.475, 162.500, 162.525, 162.550}
        if round(self.frequency_mhz, 3) not in frequencies:
            raise ValueError("env.same.frequency_mhz must be a NOAA Weather Radio channel")
        if any(len(code) != 6 or not code.isdigit() for code in self.county_codes):
            raise ValueError("env.same.county_codes entries must be six digits")
        if len(set(self.county_codes)) != len(self.county_codes):
            raise ValueError("env.same.county_codes must not contain duplicates")
        if self.enabled and not self.county_codes:
            raise ValueError("env.same.county_codes is required when the receiver is enabled")
        if self.restart_max_seconds < self.restart_initial_seconds:
            raise ValueError("env.same.restart_max_seconds must be >= restart_initial_seconds")
        return self


class EnvConfig(StrictModel):
    user_agent: str = "(CHANGE-ME.example.org, ray@example.org)"
    refresh_minutes: int = Field(default=15, ge=5)
    max_age_hours: int = Field(default=6, ge=1, le=48)
    request_timeout_s: float = Field(default=10, gt=0, le=30)
    earthquake_radius_km: float = Field(default=500, ge=10, le=5000)
    earthquake_review_magnitude: float = Field(default=4.5, ge=1, le=9)
    same: SameConfig = Field(default_factory=SameConfig)


class FedMqttConfig(StrictModel):
    enabled: bool = False
    use_radio_module: bool = True


class FedConfig(StrictModel):
    max_fragments: int = Field(default=8, ge=1, le=8)
    reassembly_timeout_s: int = Field(default=300, ge=30)
    hello_interval_hours: int = Field(default=12, ge=1)
    sync_interval_minutes: int = Field(default=60, ge=5)
    sync_retry_minutes: int = Field(default=10, ge=5, le=60)
    max_items_per_cycle: int = Field(default=20, ge=1, le=100)
    peer_stale_hours: int = Field(default=72, ge=1)
    incident_radius_km: float = Field(default=25, ge=1, le=500)
    mqtt: FedMqttConfig = Field(default_factory=FedMqttConfig)


class WebAuth(StrictModel):
    session_hours: int = 12
    failure_window_seconds: int = Field(default=900, ge=60, le=86_400)
    source_failure_limit: int = Field(default=5, ge=1, le=100)
    account_failure_limit: int = Field(default=10, ge=2, le=200)
    global_failure_limit: int = Field(default=50, ge=5, le=1_000)
    throttle_base_seconds: int = Field(default=1, ge=1, le=30)
    throttle_max_seconds: int = Field(default=16, ge=1, le=300)

    @model_validator(mode="after")
    def validate_throttling(self) -> WebAuth:
        if self.account_failure_limit < self.source_failure_limit:
            raise ValueError("web.auth account_failure_limit must cover source_failure_limit")
        if self.global_failure_limit < self.account_failure_limit:
            raise ValueError("web.auth global_failure_limit must cover account_failure_limit")
        if self.throttle_max_seconds < self.throttle_base_seconds:
            raise ValueError("web.auth throttle_max_seconds must cover throttle_base_seconds")
        return self


class WebTransport(StrictModel):
    mode: Literal["trusted_http", "direct_https", "trusted_proxy"] = "trusted_http"
    certificate_file: Path | None = None
    private_key_file: Path | None = None
    trusted_proxies: list[str] = Field(default_factory=list)
    public_port: int = Field(default=443, ge=1, le=65535)
    hsts_seconds: int = Field(default=31_536_000, ge=0, le=63_072_000)

    @model_validator(mode="after")
    def validate_mode(self) -> WebTransport:
        if self.mode == "direct_https":
            if self.certificate_file is None or self.private_key_file is None:
                raise ValueError(
                    "web.transport direct_https requires certificate_file and private_key_file"
                )
            if not self.certificate_file.is_absolute() or not self.private_key_file.is_absolute():
                raise ValueError("web.transport TLS file paths must be absolute")
            if self.trusted_proxies:
                raise ValueError("direct_https must not configure trusted_proxies")
        elif self.mode == "trusted_proxy":
            if not self.trusted_proxies:
                raise ValueError("trusted_proxy requires at least one explicit trusted proxy")
            if self.certificate_file is not None or self.private_key_file is not None:
                raise ValueError("trusted_proxy TLS material belongs on the terminating proxy")
        elif self.certificate_file is not None or self.private_key_file is not None:
            raise ValueError("trusted_http does not use TLS certificate files")
        if self.mode != "trusted_proxy" and self.public_port != 443:
            raise ValueError("web.transport.public_port applies only to trusted_proxy")
        for value in self.trusted_proxies:
            try:
                network = ip_network(value, strict=False)
            except ValueError as error:
                raise ValueError(f"invalid trusted proxy address or network: {value}") from error
            if network.prefixlen == 0:
                raise ValueError("trusted proxy networks must not trust every address")
        return self


class WebConfig(StrictModel):
    bind: str = "0.0.0.0"  # noqa: S104 - LAN bind is an explicit product requirement.
    port: int = Field(default=8080, ge=1, le=65535)
    auth: WebAuth = Field(default_factory=WebAuth)
    metrics_access: Literal["authenticated", "loopback", "disabled"] = "authenticated"
    transport: WebTransport = Field(default_factory=WebTransport)


class RetentionConfig(StrictModel):
    posts_days: int = Field(default=90, ge=1)
    mail_days: int = Field(default=180, ge=1)
    member_positions_hours: int = Field(default=168, ge=1, le=720)
    message_log_days: int = Field(default=30, ge=1)
    message_log_max_rows: int = Field(default=500_000, ge=1_000)
    authentication_days: int = Field(default=30, ge=1, le=365)
    digest_days: int = Field(default=90, ge=1, le=730)
    incident_history_days: int = Field(default=30, ge=30, le=3_650)
    watch_history_days: int = Field(default=365, ge=30, le=3_650)
    environment_history_days: int = Field(default=30, ge=1, le=365)
    provider_cache_days: int = Field(default=2, ge=1, le=30)
    ai_interaction_content_days: int = Field(default=30, ge=1, le=365)
    ai_interaction_metrics_days: int = Field(default=180, ge=30, le=3_650)
    federation_service_days: int = Field(default=7, ge=1, le=90)
    federation_history_days: int = Field(default=30, ge=1, le=365)
    outbound_history_days: int = Field(default=30, ge=1, le=3_650)
    radio_power_days: int = Field(default=30, ge=1, le=365)
    situation_snapshot_days: int = Field(default=30, ge=1, le=365)

    @model_validator(mode="after")
    def validate_ai_retention(self) -> RetentionConfig:
        if self.ai_interaction_metrics_days < self.ai_interaction_content_days:
            raise ValueError(
                "ai_interaction_metrics_days must be at least ai_interaction_content_days"
            )
        dependencies = {
            "watch_history_days": self.watch_history_days,
            "outbound_history_days": self.outbound_history_days,
            "message_log_days": self.message_log_days,
        }
        shorter = [name for name, days in dependencies.items() if days < self.incident_history_days]
        if shorter:
            raise ValueError(
                f"{', '.join(shorter)} must be at least incident_history_days so incident "
                "reports cannot silently lose supporting evidence"
            )
        return self


class BackupConfig(StrictModel):
    enabled: bool = True
    keep: int = Field(default=14, ge=1)
    pre_upgrade_keep: int = Field(default=3, ge=1, le=50)
    pre_rollback_days: int = Field(default=30, ge=1, le=365)
    superseded_release_keep: int = Field(default=1, ge=0, le=10)

    @model_validator(mode="after")
    def preserve_release_rollback_points(self) -> BackupConfig:
        minimum = self.superseded_release_keep + 2
        if self.pre_upgrade_keep < minimum:
            raise ValueError(
                "store.backup.pre_upgrade_keep must preserve current, previous, and prior releases"
            )
        return self


class StoreConfig(StrictModel):
    path: str = "/var/lib/outpost/outpost.db"
    tiles_path: str = DEFAULT_TILES_PATH
    releases_path: str = DEFAULT_RELEASES_PATH
    maintenance_hour: int = Field(default=3, ge=0, le=23)
    maintenance_batch_rows: int = Field(default=250, ge=25, le=2_000)
    maintenance_max_rows: int = Field(default=10_000, ge=250, le=100_000)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    backup: BackupConfig = Field(default_factory=BackupConfig)

    @field_validator("tiles_path", "releases_path")
    @classmethod
    def absolute_storage_path(cls, value: str) -> str:
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise ValueError("persistent storage paths must be absolute")
        return str(path.resolve(strict=False))


class SecurityConfig(StrictModel):
    require_approval: bool = False
    coarse_precision_m: int = Field(default=500, gt=0)
    global_rate_ceiling: int = Field(default=60, gt=0)
    safety_repeat_window_seconds: int = Field(default=120, ge=10, le=3600)
    safety_attempt_retention_hours: int = Field(default=72, ge=1, le=720)


class BBSConfig(StrictModel):
    immediate_max_per_hour: int = Field(default=3, ge=0)
    immediate_enabled: bool = True
    self_delete_minutes: int = Field(default=30, ge=0)


class MailConfig(StrictModel):
    hold_unknown_days: int = Field(default=14, ge=1)


class EscalationStage(StrictModel):
    after_minutes: int = Field(ge=0)
    notify: Literal["responders", "trusted", "all"]
    channels: list[Annotated[int, Field(ge=0, le=7)]]
    repeat: bool = False
    proximity: Literal["any", "footprint"] = "any"


class EscalationPolicy(StrictModel):
    stages: list[EscalationStage]
    ack_threshold: int = Field(default=0, ge=0)


class EscalationConfig(StrictModel):
    caution: EscalationPolicy = Field(
        default_factory=lambda: EscalationPolicy(
            stages=[EscalationStage(after_minutes=0, notify="all", channels=[3])]
        )
    )
    urgent: EscalationPolicy = Field(
        default_factory=lambda: EscalationPolicy(
            stages=[
                EscalationStage(after_minutes=0, notify="responders", channels=[3]),
                EscalationStage(after_minutes=10, notify="trusted", channels=[3]),
                EscalationStage(after_minutes=20, notify="all", channels=[0, 3]),
            ],
            ack_threshold=2,
        )
    )
    critical: EscalationPolicy = Field(
        default_factory=lambda: EscalationPolicy(
            stages=[
                EscalationStage(after_minutes=0, notify="all", channels=[0, 3]),
                EscalationStage(after_minutes=10, notify="all", channels=[0, 3], repeat=True),
            ],
            ack_threshold=3,
        )
    )


class WatchConfig(StrictModel):
    position_max_age_minutes: int = Field(default=30, ge=1)
    dedupe_radius_m: int = Field(default=500, ge=1)
    dedupe_window_minutes: int = Field(default=120, ge=1)
    emergency_keywords_enabled: bool = False
    emergency_keywords: list[str] = Field(
        default_factory=lambda: ["sos", "mayday", "emergency", "help me", "911"]
    )
    emergency_cooldown_minutes: int = Field(default=10, ge=1)
    alert_repeat_max: int = Field(default=3, ge=1, le=10)
    alert_repeat_interval_minutes: int = Field(default=20, ge=1)
    alert_submission_dedupe_seconds: int = Field(default=30, ge=5, le=300)
    escalation: EscalationConfig = Field(default_factory=EscalationConfig)


class Config(StrictModel):
    _environment_overrides: tuple[str, ...] = PrivateAttr(default=())

    node: NodeConfig = Field(default_factory=NodeConfig)
    radio: RadioConfig = Field(default_factory=RadioConfig)
    airtime: AirtimeConfig = Field(default_factory=AirtimeConfig)
    channels: dict[Annotated[int, Field(ge=0, le=7)], ChannelConfig] = Field(default_factory=dict)
    router: RouterConfig = Field(default_factory=RouterConfig)
    modules: ModulesConfig = Field(default_factory=ModulesConfig)
    ai: AIConfig = Field(default_factory=AIConfig)
    env: EnvConfig = Field(default_factory=EnvConfig)
    fed: FedConfig = Field(default_factory=FedConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    bbs: BBSConfig = Field(default_factory=BBSConfig)
    mail: MailConfig = Field(default_factory=MailConfig)
    watch: WatchConfig = Field(default_factory=WatchConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    store: StoreConfig = Field(default_factory=StoreConfig)

    @model_validator(mode="after")
    def cross_validate(self) -> Config:
        if self.modules.env.enabled and "CHANGE-ME" in self.env.user_agent:
            raise ValueError("env.user_agent must be configured when the env module is enabled")
        if self.modules.ai.enabled:
            provider = getattr(self.ai, self.ai.provider, None)
            if self.ai.provider not in {"null", "hailo_vlm"} and (
                provider is None or not provider.base_url
            ):
                raise ValueError(f"ai.{self.ai.provider}.base_url is required when AI is enabled")
        return self

    @property
    def environment_overrides(self) -> tuple[str, ...]:
        return self._environment_overrides


_MISSING = object()


def _default_at_path(path: tuple[str, ...]) -> object:
    current: object = Config().model_dump(mode="python")
    for index, part in enumerate(path):
        if index == 1 and path[0] == "channels":
            current = ChannelConfig(name="").model_dump(mode="python")
            continue
        if not isinstance(current, dict):
            return _MISSING
        current = current.get(part, _MISSING)
        if current is _MISSING:
            return _MISSING
    return current


def _overlay_key(
    target: dict[object, object], raw: str, parent: tuple[str, ...], variable: str
) -> object:
    if parent == ("channels",):
        try:
            numeric = int(raw)
        except ValueError:
            return raw
        matches: list[object] = []
        for key in target:
            if key == numeric or key == raw:
                matches.append(key)
        if len(matches) > 1:
            raise ValueError(f"{variable} matches duplicate channel keys {raw!r}")
        return matches[0] if matches else numeric
    matches = [key for key in target if str(key).lower() == raw]
    if len(matches) > 1:
        raise ValueError(f"{variable} matches duplicate configuration key {raw!r}")
    return matches[0] if matches else raw


def _env_overlay(data: dict[str, object], prefix: str = "OUTPOST__") -> tuple[str, ...]:
    overridden: list[str] = []
    for variable, value in sorted(os.environ.items()):
        if not variable.startswith(prefix):
            continue
        path = tuple(variable[len(prefix) :].lower().split("__"))
        if not path or any(not part for part in path):
            raise ValueError(f"{variable} has an empty configuration path component")
        target = cast(dict[object, object], data)
        parent: tuple[str, ...] = ()
        for raw in path[:-1]:
            if not isinstance(target, dict):
                location = ".".join(parent) or "configuration root"
                raise ValueError(f"{variable} cannot descend through non-mapping {location}")
            key = _overlay_key(target, raw, parent, variable)
            child = target.get(key, _MISSING)
            if child is _MISSING:
                child = {}
                target[key] = child
            elif not isinstance(child, dict):
                location = ".".join((*parent, raw))
                raise ValueError(f"{variable} cannot descend through non-mapping {location}")
            target = child
            parent = (*parent, raw)
        leaf = _overlay_key(target, path[-1], parent, variable)
        existing = target.get(leaf, _MISSING)
        expected = _default_at_path(path)
        if isinstance(existing, str) or isinstance(expected, str):
            target[leaf] = value
        else:
            try:
                target[leaf] = json.loads(value)
            except json.JSONDecodeError:
                target[leaf] = value
        overridden.append(".".join(path))
    return tuple(overridden)


def load_config(path: str | Path | None = None) -> Config:
    source_value: str | Path = (
        path if path is not None else os.getenv("OUTPOST_CONFIG", "config/config.yaml")
    )
    source = Path(source_value)
    data = yaml.safe_load(source.read_text()) if source.exists() else {}
    if not isinstance(data, dict):
        raise ValueError(f"config root must be a mapping: {source}")
    overrides = _env_overlay(data)
    config = Config.model_validate(data)
    config._environment_overrides = overrides
    return config
