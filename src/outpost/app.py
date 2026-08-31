from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import json
import secrets
import sqlite3
from collections import defaultdict, deque
from collections.abc import Callable, Coroutine
from contextvars import ContextVar
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import UTC, datetime
from typing import Any, Literal

import cbor2

from outpost.ai import AIService, create_provider
from outpost.ai.retrieval import RetrievalEngine
from outpost.ai.store import AIStore
from outpost.audit import write_audit
from outpost.bbs.admin import BBSAdmin
from outpost.bbs.channels import ChannelDirectory
from outpost.bbs.digests import DigestService
from outpost.bbs.mail import MailService
from outpost.bbs.service import BBSService
from outpost.channel_profile import channel_slot, outpost_display_name
from outpost.clock import Clock, SystemClock
from outpost.commands.ai import specs as ai_specs
from outpost.commands.alerts import specs as alert_specs
from outpost.commands.bbs import specs as bbs_specs
from outpost.commands.checkin import specs as checkin_specs
from outpost.commands.directory import specs as directory_specs
from outpost.commands.environment import specs as environment_specs
from outpost.commands.identity import specs as identity_specs
from outpost.commands.mail import specs as mail_specs
from outpost.commands.operations import specs as operations_specs
from outpost.commands.operator import specs as operator_specs
from outpost.commands.situation import specs as situation_specs
from outpost.commands.watch import specs as watch_specs
from outpost.config import Config
from outpost.env import (
    AstronomyService,
    CapAlertService,
    FallbackWeatherProvider,
    NWSProvider,
    OpenMeteoProvider,
    SameReceiver,
    SameService,
    SeismicService,
    WaypointService,
    WeatherService,
)
from outpost.fed import (
    FederationMailService,
    FederationPeerService,
    FederationRelayService,
    FederationSyncService,
    FederationTopologyService,
    FrameCodec,
    FrameError,
    MessageType,
    Peer,
    Reassembler,
    RelayDispatchContext,
    wire_bytes,
    wire_int,
)
from outpost.member_data import MemberDataService
from outpost.operations_center import MeshOperationsCenter
from outpost.operator_context import current_actor
from outpost.radio_configuration import RadioConfigurationManager
from outpost.radio_operations import RadioOperations
from outpost.radio_power import RadioPowerMonitor
from outpost.render.renderer import render_response
from outpost.router.models import DispatchTrace, Line, Response, ResponseKind
from outpost.router.router import Router
from outpost.router.session import SessionStore
from outpost.security.rate_limit import RateLimiter
from outpost.self_check import SelfCheckService
from outpost.situation import SituationBriefingService
from outpost.store import Database, Transaction
from outpost.store.backups import BackupService, RestoreCoordinator
from outpost.store.maintenance import MaintenanceService
from outpost.store.members import Member, MemberRepo
from outpost.store.message_log import MessageLogRepo
from outpost.store.outbox import OutboxStore
from outpost.task_supervision import TaskFailureDomain, restart_delay
from outpost.transport.chunker import chunk_text
from outpost.transport.governor import (
    AirtimeGovernor,
    OutboundItem,
)
from outpost.transport.inbound import InboundPipeline
from outpost.transport.metrics import (
    ACK_OUTCOME,
    COMMAND_REPLY_DELIVERY,
    INBOUND,
    INBOUND_DROPPED,
    INBOUND_HANDLER_FAILURES,
    INBOUND_QUEUE_DEPTH,
    INBOUND_WORKERS_BUSY,
)
from outpost.transport.models import InboundMessage, Severity, TrafficClass
from outpost.transport.radio_frequency import frequency_plan
from outpost.transport.radio_link import MeshtasticRadioLink
from outpost.transport.supervisor import RadioSupervisor
from outpost.watch import AlertService, CheckinService, IncidentReportService, IncidentService
from outpost.watch.delivery import AudienceDelivery
from outpost.watch.incidents import Incident
from outpost.web.api import create_web_app
from outpost.web.auth import WebAuthService
from outpost.web.settings import RuntimeSettings


def _int_value(value: object) -> int:
    if not isinstance(value, (str, bytes, bytearray, int, float)):
        raise TypeError("value is not numeric")
    return int(value)


def _float_value(value: object) -> float:
    if not isinstance(value, (str, int, float)):
        raise TypeError("value is not numeric")
    return float(value)


_INBOUND_LOG_ID: ContextVar[int | None] = ContextVar("outpost_inbound_log_id", default=None)
_MAX_RECONCILIATION_ROUNDS = 16


@dataclass
class OutpostApp:
    config: Config
    clock: Clock = dataclass_field(default_factory=SystemClock)
    radio: Any = None
    runtime_mode: Literal["live", "replay", "drill"] = "live"
    runtime_source: str | None = None

    def __post_init__(self) -> None:
        self._tasks: list[asyncio.Task[None]] = []
        self._task_health: dict[str, dict[str, object]] = {}
        self._task_failure = asyncio.Event()
        self._restart_requested = asyncio.Event()
        self._fatal_task_error: str | None = None
        self._shutting_down = False
        self._inbound_pending: dict[str, deque[tuple[InboundMessage, int]]] = defaultdict(deque)
        self._inbound_active: set[str] = set()
        self._inbound_ready: asyncio.Queue[str] = asyncio.Queue()
        self._inbound_queued = 0
        self._inbound_busy = 0
        self._inbound_fast_processed = 0
        self._inbound_backlog_dropped = 0
        self._federation_control_locks: dict[str, asyncio.Lock] = {}
        self.database = Database(self.config.store.path)
        self.radio_power = RadioPowerMonitor(self.database, self.clock, self.config.radio.power)
        if self.radio is None:
            self.radio = MeshtasticRadioLink(self.config.radio, self.clock)
        self.radio_configuration = RadioConfigurationManager(
            self.database, self.radio, self.clock, self.config
        )
        self.supervisor = RadioSupervisor(
            self.radio,
            self.config.radio.reconnect,
            self.clock,
            self.config.radio.liveness_timeout_s,
            self._radio_progress,
        )
        self.inbound_pipeline = InboundPipeline("", set(self.config.radio.bridge_node_ids))
        self.governor = AirtimeGovernor(
            self.radio,
            self.config.airtime,
            self.clock,
            preset=self.radio.snapshot.preset,
            region=self.radio.snapshot.region,
            outbox=OutboxStore(self.database),
            power_config=self.config.radio.power,
            power_observer=self.radio_power.observe,
            timezone=self.config.node.timezone,
        )
        self.radio_operations = RadioOperations(
            self.database,
            self.governor,
            self.clock,
            self.config.store.retention.outbound_history_days,
            self.radio_power,
        )
        members = MemberRepo(self.database, self.clock)
        self.message_log = MessageLogRepo(self.database, self.clock)
        sessions = SessionStore(self.clock, self.config.router.session_idle_minutes)
        limiter = RateLimiter(
            self.clock,
            self.config.security.global_rate_ceiling,
            self.database,
            safety_repeat_window_seconds=self.config.security.safety_repeat_window_seconds,
        )
        self.router = Router(
            self.config, members, sessions, limiter, node_name=lambda: self.outpost_name
        )
        self.ai_store = AIStore(
            self.database, evidence_tokens=self.config.ai.budget.evidence_tokens
        )
        provider_config = (
            self.config.ai
            if self.config.modules.ai.enabled
            else self.config.ai.model_copy(update={"provider": "null"})
        )
        self.ai_service = AIService(
            self.config,
            create_provider(provider_config),
            RetrievalEngine(
                self.database,
                now=lambda: int(self.clock.now().timestamp()),
                node_status=self.status,
            ),
            self.ai_store,
            now=lambda: int(self.clock.now().timestamp()),
            node_name=lambda: self.outpost_name,
        )
        self.bbs = BBSService(
            self.database,
            self.clock,
            "local",
            self.config.router.page_ttl_minutes,
        )
        directory = ChannelDirectory(self.database)
        mail = MailService(
            self.database,
            members,
            self.clock,
            "local",
            self.config.mail.hold_unknown_days,
            self._send_federated_operator_reply,
        )
        self.member_data = MemberDataService(self.database, self.clock, self.config.store.retention)
        self.digests = DigestService(self.database, self.clock, self.config)
        self.incidents = IncidentService(
            self.database,
            self.clock,
            "local",
            self.config.store.retention.member_positions_hours,
            self.config.store.retention.incident_history_days,
            self.config.watch.position_max_age_minutes,
            self.config.watch.dedupe_radius_m,
            self.config.watch.dedupe_window_minutes,
        )
        self.alerts = AlertService(self.database, self.governor, self.clock, self.config)
        self.checkins = CheckinService(
            self.database,
            self.governor,
            self.clock,
            self.config.node.timezone,
            public_channel=lambda: channel_slot(self.config, "public", 0),
        )
        self.incident_reports = IncidentReportService(
            self.database,
            self.clock,
            self.config.store.retention,
            coarse_precision_m=self.config.security.coarse_precision_m,
        )
        self.weather = WeatherService(
            self.database,
            self.clock,
            self.config.env,
            FallbackWeatherProvider(
                [NWSProvider(self.config.env), OpenMeteoProvider(self.config.env)]
            ),
        )
        self.cap_alerts = CapAlertService(self.database, self.clock, self.config.env)
        self.same_events = SameService(self.database, self.clock, self.config.env.same)
        self.same_receiver = SameReceiver(
            self.same_events,
            self.config.env.same,
            self.clock,
            on_progress=lambda: self._task_progress("same-receiver"),
        )
        self.astronomy = AstronomyService(self.clock)
        self.seismic = SeismicService(self.database, self.clock, self.config.env)
        self.waypoints = WaypointService(self.database, self.clock)
        self.federation = FederationPeerService(
            self.database,
            self.clock,
            "",
            self.config.fed.peer_stale_hours,
        )
        self.federation_sync = FederationSyncService(
            self.database, module_enabled=self.config.modules.is_enabled
        )
        self.federation_mail = FederationMailService(self.database, self.federation, self.clock)
        self.federation_relay = FederationRelayService(self.database, self.federation, self.clock)
        self.federation_relay.register_handler("incident", self._dispatch_relay_incident)
        self.federation_relay.register_handler("request", self._dispatch_relay_request)
        self.federation_relay.register_handler("receipt", self._dispatch_relay_receipt)
        self.federation_topology = FederationTopologyService(
            self.database, self.federation, self.clock
        )
        self.federation_codec = FrameCodec(self.config.fed.max_fragments)
        self.federation_reassembler = Reassembler(self.config.fed.reassembly_timeout_s)
        self.operations_center = MeshOperationsCenter(
            self.database,
            self.clock,
            self.incidents,
            self.checkins,
            self.radio_operations,
            importer=self.import_federation_inbox_as,
            reply_sender=self.reply_operations_conversation,
        )
        self.situation = SituationBriefingService(
            self.database,
            self.clock,
            self.status,
            narrator=self.ai_service,
            modules=self.config.modules.enabled_map,
        )
        command_groups = (
            (
                identity_specs(
                    members,
                    mail,
                    self.member_data,
                    self.config.security.require_approval,
                ),
                True,
            ),
            (
                bbs_specs(self.bbs, self.config.bbs.self_delete_minutes),
                self.config.modules.bbs.enabled,
            ),
            (mail_specs(mail), True),
            (directory_specs(directory), True),
            (ai_specs(self.ai_service), self.config.modules.ai.enabled),
            (operator_specs(self.bbs), self.config.modules.bbs.enabled),
            (operations_specs(self.operations_center), True),
            (situation_specs(self.situation), True),
            (watch_specs(self.incidents), self.config.modules.watch.enabled),
            (alert_specs(self.alerts), self.config.modules.watch.enabled),
            (checkin_specs(self.checkins), self.config.modules.watch.enabled),
            (
                environment_specs(
                    self.weather,
                    self.config,
                    self.cap_alerts,
                    self.astronomy,
                    self.seismic,
                    self.waypoints,
                ),
                self.config.modules.env.enabled,
            ),
        )
        for specs, enabled in command_groups:
            for spec in specs:
                self.router.registry.register(spec, enabled=enabled)
        reserved_slugs = {
            value.lower()
            for spec in self.router.registry.known_commands()
            for value in (spec.name, *spec.aliases)
        }
        self.bbs_admin = BBSAdmin(
            self.database, self.clock, reserved_slugs, federation_notify=self._notify_board_change
        )
        auth_config = self.config.web.auth
        self.web_auth = WebAuthService(
            self.database,
            auth_config.session_hours,
            failure_window_seconds=auth_config.failure_window_seconds,
            source_failure_limit=auth_config.source_failure_limit,
            account_failure_limit=auth_config.account_failure_limit,
            global_failure_limit=auth_config.global_failure_limit,
            throttle_base_seconds=auth_config.throttle_base_seconds,
            throttle_max_seconds=auth_config.throttle_max_seconds,
        )
        self.runtime_settings = RuntimeSettings(self.database, self.config)
        self.radio_configuration.binding_updater = self.runtime_settings.bind_outpost_channels
        self.backups = BackupService(self.database, self.config.store.backup)
        self.restore_coordinator = RestoreCoordinator(
            self.backups, self._restore_database, self._request_restart
        )
        self.maintenance = MaintenanceService(self.database, self.backups, self.clock, self.config)
        self.self_check = SelfCheckService(
            self.database,
            self.config,
            self.clock,
            self.backups,
            self.router.intents,
        )
        self.radio_power.on_condition_change = lambda _condition: self.self_check.run("radio-power")
        self.web = create_web_app(
            self.status,
            self.database,
            self.web_auth,
            self.runtime_settings,
            self.reconnect_radio,
            self.backups,
            self.bbs_admin,
            self.radio_operations,
            self.incidents,
            self.alerts,
            self.checkins,
            self.weather,
            self.cap_alerts,
            self.astronomy,
            self.seismic,
            self.waypoints,
            self.federation,
            self.initiate_federation_pairing,
            self.approve_federation_pairing,
            self.radio.mqtt_status,
            self.radio.configure_mqtt,
            self.federation_service_requests,
            self.request_federation_service,
            self.import_federation_inbox,
            self.send_federation_mail,
            self.restore_coordinator,
            self.maintenance,
            self_check=self.self_check,
            module_provider=self.config.modules.enabled_map,
            federation_mail_reply=self.reply_federation_mail,
            same_events=self.same_events,
            same_receiver_health=self.same_receiver.health,
            ai_service=self.ai_service,
            ai_store=self.ai_store,
            ai_test=self.test_ai,
            federation_relay=self.federation_relay,
            federation_topology=self.federation_topology,
            radio_configuration_status=self.radio_configuration_status,
            radio_configuration_preflight=self.preflight_radio_configuration,
            radio_configuration_apply=self.configure_radio,
            situation=self.situation,
            web_config=self.config.web,
            tile_path=self.config.store.tiles_path,
            incident_reports=self.incident_reports,
            member_data=self.member_data,
        )

    def _start_background_task(
        self,
        name: str,
        factory: Callable[[], Coroutine[Any, Any, None]],
        failure_domain: TaskFailureDomain = TaskFailureDomain.CORE,
    ) -> asyncio.Task[None]:
        now = int(self.clock.now().timestamp())
        self._task_health[name] = {
            "state": "running",
            "failure_domain": failure_domain.value,
            "required": failure_domain is TaskFailureDomain.CORE,
            "started_at": now,
            "last_started_at": now,
            "last_ok_at": None,
            "stopped_at": None,
            "error": None,
            "degraded_reason": None,
            "degradation_count": 0,
            "failure_count": 0,
            "consecutive_failures": 0,
            "restart_count": 0,
            "last_error": None,
            "last_error_at": None,
            "next_retry_at": None,
            "circuit_open": False,
        }
        task = asyncio.create_task(
            self._run_background_task(name, factory, failure_domain), name=name
        )
        task.add_done_callback(self._background_task_done)
        return task

    async def _run_background_task(
        self,
        name: str,
        factory: Callable[[], Coroutine[Any, Any, None]],
        failure_domain: TaskFailureDomain,
    ) -> None:
        while True:
            try:
                await factory()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                failure = error
            else:
                failure = RuntimeError("task exited unexpectedly")
            if failure_domain is TaskFailureDomain.CORE:
                raise failure
            health = self._record_task_failure(name, failure)
            consecutive = _int_value(health["consecutive_failures"])
            delay, circuit_open = restart_delay(failure_domain, consecutive)
            health.update(
                {
                    "state": "circuit_open" if circuit_open else "backoff",
                    "circuit_open": circuit_open,
                    "next_retry_at": int(self.clock.now().timestamp()) + delay,
                }
            )
            print(
                f"Outpost task {name} degraded; retry in {delay}s: {health['last_error']}",
                flush=True,
            )
            await self.clock.sleep(delay)
            if self._shutting_down:
                return
            now = int(self.clock.now().timestamp())
            health.update(
                {
                    "state": "restarting",
                    "last_started_at": now,
                    "stopped_at": None,
                    "restart_count": _int_value(health["restart_count"]) + 1,
                    "next_retry_at": None,
                    "circuit_open": False,
                }
            )

    def _record_task_failure(self, name: str, error: BaseException | None) -> dict[str, object]:
        health = self._task_health[name]
        now = int(self.clock.now().timestamp())
        detail = (
            f"{type(error).__name__}: {error}" if error is not None else "task exited unexpectedly"
        )
        detail = " ".join(detail.split())[:240]
        health.update(
            {
                "state": "failed",
                "stopped_at": now,
                "error": detail,
                "degraded_reason": detail,
                "failure_count": _int_value(health["failure_count"]) + 1,
                "consecutive_failures": _int_value(health["consecutive_failures"]) + 1,
                "last_error": detail,
                "last_error_at": now,
                "next_retry_at": None,
                "circuit_open": False,
            }
        )
        return health

    def _record_task_degradation(self, name: str, error: BaseException) -> None:
        health = self._task_health.get(name)
        if health is None:
            return
        now = int(self.clock.now().timestamp())
        detail = " ".join(f"{type(error).__name__}: {error}".split())[:240]
        health.update(
            {
                "state": "degraded",
                "error": detail,
                "degraded_reason": detail,
                "degradation_count": _int_value(health["degradation_count"]) + 1,
                "last_error": detail,
                "last_error_at": now,
            }
        )

    async def test_ai(self, question: str) -> dict[str, object]:
        member = MemberRepo(self.database, self.clock)
        rows = await self.database.read(
            "SELECT mesh_id FROM member WHERE trust='operator' ORDER BY last_seen DESC LIMIT 1"
        )
        if rows:
            actor = await member.resolve(str(rows[0]["mesh_id"]))
        else:
            actor = Member(0, "!00000000", 0, "operator", "operator", 0, 0)
        result = await self.ai_service.answer(question, actor, -1, self.router.registry)
        return {
            "text": result.text,
            "outcome": result.outcome,
            "question_class": result.question_class,
            "grounded": result.grounded,
            "refused": result.refused,
            "refusal_reason": result.refusal_reason,
            "transmitted": False,
        }

    def _task_progress(self, name: str) -> None:
        health = self._task_health.get(name)
        if health is not None and health["state"] in {"running", "restarting", "degraded"}:
            health.update(
                {
                    "state": "running",
                    "last_ok_at": int(self.clock.now().timestamp()),
                    "error": None,
                    "degraded_reason": None,
                    "consecutive_failures": 0,
                    "next_retry_at": None,
                    "circuit_open": False,
                }
            )

    def _radio_progress(self) -> None:
        self._task_progress("radio-supervisor")
        self.governor.sync_radio_profile(self.radio.snapshot.preset, self.radio.snapshot.region)
        local_id = self.radio.local_node_id
        if local_id:
            self.inbound_pipeline.local_node_id = local_id
            self.incidents.origin_node = local_id
            self.federation.local_mesh_id = local_id
            self.federation_sync.local_mesh_id = local_id

    @property
    def outpost_name(self) -> str:
        mesh_id = self.radio.local_node_id or self.radio.snapshot.node_id
        return outpost_display_name(self.config.node.name, mesh_id)

    def _background_task_done(self, task: asyncio.Task[None]) -> None:
        name = task.get_name()
        health = self._task_health.get(name)
        if health is None:
            return
        if self._shutting_down or task.cancelled():
            health["stopped_at"] = int(self.clock.now().timestamp())
            health["state"] = "stopped"
            return
        error = task.exception()
        self._record_task_failure(name, error)
        if health["failure_domain"] == TaskFailureDomain.CORE.value:
            detail = str(health["last_error"])
            if self._fatal_task_error is None:
                self._fatal_task_error = f"{name}: {detail}"
            self._task_failure.set()

    def background_tasks_healthy(self) -> bool:
        return bool(self._task_health) and all(
            health["state"] == "running" for health in self._task_health.values()
        )

    def core_tasks_healthy(self) -> bool:
        core = [
            health
            for health in self._task_health.values()
            if health["failure_domain"] == TaskFailureDomain.CORE.value
        ]
        return bool(core) and all(health["state"] == "running" for health in core)

    async def wait_for_task_failure(self) -> str:
        await self._task_failure.wait()
        return self._fatal_task_error or "background task failed"

    async def wait_for_restart(self) -> None:
        await self._restart_requested.wait()

    def _request_restart(self) -> None:
        self._restart_requested.set()

    async def _restore_database(self, name: str) -> dict[str, object]:
        self._shutting_down = True
        await self.supervisor.stop()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        return await self.backups.restore_quiesced(name)

    async def import_federation_inbox(self, item_id: int) -> str:
        actor = current_actor()
        stream = await self.import_federation_inbox_as(item_id, actor)
        await write_audit(
            self.database,
            actor_kind="web",
            actor_ref=actor.removeprefix("web:"),
            action="federation.inbox.import",
            target=f"federation-inbox:{item_id}",
            detail={"stream": stream},
            created_at=int(self.clock.now().timestamp()),
        )
        return stream

    async def import_federation_inbox_as(self, item_id: int, actor: str) -> str:
        if not self.config.modules.fed.enabled:
            raise ValueError("federation module is disabled")
        return await self.federation_sync.import_inbox(
            item_id, actor, int(self.clock.now().timestamp())
        )

    async def _dispatch_relay_incident(
        self,
        transaction: Transaction,
        context: RelayDispatchContext,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return await self.federation_sync.import_relay_incident(
            transaction,
            payload,
            origin_node=context.origin_node,
            received_from_peer_id=context.received_from_peer_id,
            envelope_id=context.envelope_id,
            now=context.received_at,
        )

    async def _dispatch_relay_request(
        self,
        transaction: Transaction,
        context: RelayDispatchContext,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        service = str(payload.get("service") or "")
        request_id = str(payload.get("request_id") or context.envelope_id)
        args = payload.get("args", {})
        if not request_id or len(request_id) > 64:
            raise ValueError("invalid relayed service request id")
        if service not in {"weather", "alerts", "knowledge"} or not isinstance(args, dict):
            raise ValueError("invalid relayed service request")
        expires_at = context.expires_at
        if payload.get("expires_at") is not None:
            expires_at = min(
                expires_at,
                wire_int(payload["expires_at"], "expires_at"),
            )
        if expires_at <= context.received_at:
            raise ValueError("relayed service request has expired")
        normalized_args, fingerprint = self._normalized_service_args(service, args)
        args_json = json.dumps(normalized_args, separators=(",", ":"), sort_keys=True)
        _, outcome, existing = await self.federation.admit_service_request_in(
            transaction,
            context.received_from_mesh_id,
            request_id,
            service,
            args_json,
            fingerprint,
            context.received_at,
            expires_at,
        )
        if outcome == "replay":
            if existing is None or existing.get("relay_envelope_id") != context.envelope_id:
                raise ValueError("relayed service request id collides with an existing request")
            return {"request_id": request_id, "outcome": "already_queued"}
        await transaction.write(
            "UPDATE fed_service_request SET relay_envelope_id=?,relay_origin_node=? "
            "WHERE request_id=?",
            (context.envelope_id, context.origin_node, request_id),
        )
        return {"request_id": request_id, "service": service, "outcome": outcome}

    async def _dispatch_relay_receipt(
        self,
        transaction: Transaction,
        context: RelayDispatchContext,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        request_id = str(payload.get("request_id") or "")
        request_envelope_id = str(payload.get("request_envelope_id") or "")
        service = str(payload.get("service") or "")
        result = payload.get("result", {})
        provenance = payload.get("provenance", {})
        if not request_id or len(request_id) > 64:
            raise ValueError("invalid relayed service receipt id")
        if len(request_envelope_id) != 32 or any(
            character not in "0123456789abcdef" for character in request_envelope_id
        ):
            raise ValueError("invalid relayed service request envelope id")
        if service not in {"weather", "alerts", "knowledge"}:
            raise ValueError("invalid relayed service receipt")
        if not isinstance(result, dict) or not isinstance(provenance, dict):
            raise ValueError("relayed service receipt data must be objects")
        origin_rows = await transaction.read(
            "SELECT payload_cbor,created_at,expires_at FROM fed_relay_envelope "
            "WHERE envelope_id=? AND direction='origin' AND destination_node=? "
            "AND scope='request'",
            (request_envelope_id, context.origin_node),
        )
        if not origin_rows or origin_rows[0]["payload_cbor"] is None:
            raise ValueError("relayed service receipt has no matching local request")
        original_payload = cbor2.loads(bytes(origin_rows[0]["payload_cbor"]))
        if not isinstance(original_payload, dict):
            raise ValueError("local relayed service request payload is invalid")
        original_request_id = str(original_payload.get("request_id") or request_envelope_id)
        if (
            original_request_id != request_id
            or str(original_payload.get("service") or "") != service
        ):
            raise ValueError("relayed service receipt does not match its local request")
        rows = await transaction.read(
            "SELECT * FROM fed_service_request WHERE request_id=?", (request_id,)
        )
        if rows:
            existing = rows[0]
            if str(existing["direction"]) != "out":
                raise ValueError("relayed service receipt collides with an inbound request")
            if existing["relay_response_envelope_id"] == context.envelope_id:
                return {"request_id": request_id, "outcome": "already_recorded"}
            if str(existing["status"]) != "pending":
                raise ValueError("relayed service request already has a terminal response")
            if str(existing["peer_mesh_id"]) != context.origin_node:
                raise ValueError("relayed service receipt origin does not match its request")
        ok = payload.get("ok") is True
        decoded_result = self._service_result_from_wire(service, result)
        decoded_provenance = self._service_provenance_from_wire(
            service, provenance, context.received_at, context.origin_node
        )
        error = str(payload.get("error") or "")[:160] or None
        if rows:
            await transaction.write(
                "UPDATE fed_service_request SET status=?,result_json=?,provenance_json=?,"
                "error=?,completed_at=?,updated_at=?,relay_response_envelope_id=? "
                "WHERE request_id=?",
                (
                    "complete" if ok else "failed",
                    json.dumps(decoded_result, separators=(",", ":")),
                    json.dumps(decoded_provenance, separators=(",", ":")),
                    error,
                    context.received_at,
                    context.received_at,
                    context.envelope_id,
                    request_id,
                ),
            )
        else:
            await transaction.write(
                "INSERT INTO fed_service_request(request_id,direction,peer_mesh_id,service,"
                "args_json,result_json,provenance_json,status,created_at,updated_at,expires_at,"
                "completed_at,error,relay_response_envelope_id,relay_origin_node) "
                "VALUES(?,'out',?,?,?, ?,?,?,?,?,?,?,?,?,?)",
                (
                    request_id,
                    context.origin_node,
                    service,
                    json.dumps(original_payload.get("args", {}), separators=(",", ":")),
                    json.dumps(decoded_result, separators=(",", ":")),
                    json.dumps(decoded_provenance, separators=(",", ":")),
                    "complete" if ok else "failed",
                    int(origin_rows[0]["created_at"]),
                    context.received_at,
                    int(origin_rows[0]["expires_at"]),
                    context.received_at,
                    error,
                    context.envelope_id,
                    context.origin_node,
                ),
            )
        return {"request_id": request_id, "service": service, "ok": ok}

    async def reply_operations_conversation(
        self, route: dict[str, str], body: str, actor: str
    ) -> dict[str, object]:
        return await self.send_federation_mail(
            route["source_peer_mesh_id"],
            route["reply_recipient_handle"],
            route["subject"] or "Mesh reply",
            body,
            conversation_id=route["federation_conversation_id"],
            message_kind=route["message_kind"],
            participant_handle=route["participant_handle"],
            reply_to="operator",
            operator_actor=actor,
        )

    async def send_federation_mail(
        self,
        peer_id: str,
        recipient: str,
        subject: str,
        body: str,
        *,
        sender: str | None = None,
        conversation_id: str | None = None,
        message_kind: str | None = None,
        participant_handle: str | None = None,
        reply_to: str | None = None,
        operator_actor: str | None = None,
    ) -> dict[str, object]:
        if not self.config.modules.fed.enabled:
            raise ValueError("federation module is disabled")
        envelope = await self.federation_mail.seal(
            peer_id,
            recipient,
            sender or f"operator@{self.config.node.short_name}",
            subject,
            body,
            conversation_id=conversation_id,
            message_kind=message_kind,
            participant_handle=participant_handle,
            reply_to=reply_to,
            operator_actor=operator_actor or current_actor(),
        )
        await self._send_federation_value(
            peer_id,
            MessageType.MAIL_RELAY,
            {"mesh_id": self.federation.local_mesh_id, **envelope},
            traffic_class=TrafficClass.REPLY,
        )
        async with self.database.transaction() as transaction:
            await transaction.write(
                "UPDATE fed_mail_delivery SET state='sent',attempts=1,updated_at=unixepoch() "
                "WHERE relay_id=?",
                (envelope["relay_id"],),
            )
            await transaction.write(
                "UPDATE mail SET state='sent' WHERE id=(SELECT mail_id FROM fed_mail_delivery "
                "WHERE relay_id=?)",
                (envelope["relay_id"],),
            )
        return {
            "relay_id": str(envelope["relay_id"]),
            "conversation_id": str(envelope["conversation_id"]),
            "state": "sent",
        }

    async def _send_federated_operator_reply(
        self, peer_id: str, member_handle: str, conversation_id: str, subject: str, body: str
    ) -> None:
        await self.send_federation_mail(
            peer_id,
            "operator",
            subject,
            body,
            sender=f"{member_handle}@{self.config.node.short_name}",
            conversation_id=conversation_id,
            message_kind="member",
            participant_handle=member_handle,
            reply_to=member_handle,
            operator_actor=f"member:@{member_handle}",
        )

    async def reply_federation_mail(
        self,
        peer_id: str,
        recipient: str,
        subject: str,
        body: str,
        conversation_id: str,
        message_kind: str,
        participant_handle: str,
    ) -> dict[str, object]:
        return await self.send_federation_mail(
            peer_id,
            recipient,
            subject,
            body,
            conversation_id=conversation_id,
            message_kind=message_kind,
            participant_handle=participant_handle,
            reply_to="operator",
            operator_actor=current_actor(),
        )

    async def _notify_board_change(self, slug: str, post_id: int) -> None:
        if not self.config.modules.fed.enabled:
            return
        posts = await self.database.read("SELECT uid FROM post WHERE id=?", (post_id,))
        if not posts:
            return
        self.federation_sync.local_mesh_id = self.radio.local_node_id or ""
        now = int(self.clock.now().timestamp())
        for peer in await self.federation.list("active"):
            if slug not in peer.boards:
                continue
            uid = self.federation_sync.wire_uid(str(posts[0]["uid"]))
            await self.database.write(
                "INSERT INTO fed_post_delivery(peer_id,post_id,uid,stream,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(peer_id,post_id) DO UPDATE SET "
                "uid=excluded.uid,stream=excluded.stream,updated_at=excluded.updated_at",
                (peer.id, post_id, uid, f"board:{slug}", now, now),
            )
            if not self.radio.local_node_id or not self.federation.is_online(peer, now=now):
                continue
            try:
                await self._send_federation_value(
                    peer.mesh_id, MessageType.SYNC_NOTIFY, {"stream": f"board:{slug}"}
                )
                await self.database.write(
                    "UPDATE fed_post_delivery SET state='sent',attempts=attempts+1,"
                    "updated_at=?,error=NULL WHERE peer_id=? AND post_id=?",
                    (now, peer.id, post_id),
                )
            except (FrameError, ValueError) as error:
                await self.database.write(
                    "UPDATE fed_post_delivery SET attempts=attempts+1,updated_at=?,error=? "
                    "WHERE peer_id=? AND post_id=?",
                    (now, str(error)[:120], peer.id, post_id),
                )
                continue

    async def startup(self) -> None:
        await self.database.open()
        await self.radio_power.restore()
        await self.ai_store.rechunk_stale_documents()
        await self.radio_configuration.initialize()
        if self.config.modules.fed.enabled:
            try:
                await self.federation_relay.initialize()
            except Exception as error:
                print(
                    f"Federation initialization deferred after {type(error).__name__}: {error}",
                    flush=True,
                )
        if self.config.modules.ai.enabled:
            if self.config.ai.provider == "openai_compat":
                print(
                    "WARNING: AI uses an external provider; questions and retrieved data "
                    "leave this node.",
                    flush=True,
                )
            try:
                await self.ai_service.initialize()
            except Exception as error:
                print(
                    f"AI initialization degraded after {type(error).__name__}: {error}",
                    flush=True,
                )
        await self.governor.recover()
        await self.runtime_settings.load()
        await self.self_check.run("startup")
        self.federation_sync.local_mesh_id = self.radio.local_node_id
        if self.radio.local_node_id:
            await self.federation_sync.import_approved_replies(
                "federation:auto-thread", int(self.clock.now().timestamp())
            )
        await self.web_auth.ensure_credential()
        self._tasks = [
            self._start_background_task("radio-supervisor", self.supervisor.run),
            self._start_background_task("airtime-governor", self._governor_loop),
            self._start_background_task(
                "inbound-router", self._inbound_loop, TaskFailureDomain.CORE
            ),
            *[
                self._start_background_task(
                    f"inbound-worker-{worker}",
                    functools.partial(self._inbound_worker, worker),
                    TaskFailureDomain.CORE,
                )
                for worker in range(1, self.config.router.inbound_workers + 1)
            ],
            *(
                [
                    self._start_background_task(
                        "bbs-digests",
                        self._digest_loop,
                        TaskFailureDomain.RESTARTABLE_LOCAL,
                    )
                ]
                if self.config.modules.bbs.enabled
                else []
            ),
            self._start_background_task(
                "store-maintenance",
                self._maintenance_loop,
                TaskFailureDomain.RESTARTABLE_LOCAL,
            ),
            *(
                [
                    self._start_background_task(
                        "watch-scheduler",
                        self._watch_loop,
                        TaskFailureDomain.RESTARTABLE_LOCAL,
                    )
                ]
                if self.config.modules.watch.enabled
                else []
            ),
            *(
                [
                    self._start_background_task(
                        "environment-poller",
                        self._environment_loop,
                        TaskFailureDomain.OPTIONAL_PROVIDER,
                    )
                ]
                if self.config.modules.env.enabled
                else []
            ),
            *(
                [
                    self._start_background_task(
                        "same-receiver",
                        self.same_receiver.run,
                        TaskFailureDomain.RESTARTABLE_LOCAL,
                    )
                ]
                if self.config.modules.env.enabled and self.config.env.same.enabled
                else []
            ),
            *(
                [
                    self._start_background_task(
                        "ai-keep-warm",
                        self._ai_keep_warm_loop,
                        (
                            TaskFailureDomain.OPTIONAL_PROVIDER
                            if self.config.ai.provider == "openai_compat"
                            else TaskFailureDomain.RESTARTABLE_LOCAL
                        ),
                    )
                ]
                if self.config.modules.ai.enabled
                and (self.config.ai.keep_warm.enabled or self.config.ai.required_for_readiness)
                else []
            ),
            *(
                [
                    self._start_background_task(
                        "federation-discovery",
                        self._federation_hello_loop,
                        TaskFailureDomain.OPTIONAL_PROVIDER,
                    ),
                    self._start_background_task(
                        "federation-services",
                        self._federation_service_loop,
                        TaskFailureDomain.OPTIONAL_PROVIDER,
                    ),
                    self._start_background_task(
                        "federation-sync",
                        self._federation_sync_loop,
                        TaskFailureDomain.OPTIONAL_PROVIDER,
                    ),
                    self._start_background_task(
                        "federation-delivery",
                        self._federation_delivery_loop,
                        TaskFailureDomain.OPTIONAL_PROVIDER,
                    ),
                    self._start_background_task(
                        "federation-relay",
                        self._federation_relay_loop,
                        TaskFailureDomain.OPTIONAL_PROVIDER,
                    ),
                    self._start_background_task(
                        "federation-topology",
                        self._federation_topology_loop,
                        TaskFailureDomain.OPTIONAL_PROVIDER,
                    ),
                ]
                if self.config.modules.fed.enabled
                else []
            ),
        ]

    async def _federation_hello_loop(self) -> None:
        while True:
            if self.config.modules.fed.enabled and await self._queue_federation_hello("^all"):
                self._task_progress("federation-discovery")
                await self.clock.sleep(self.config.fed.hello_interval_hours * 3_600)
            else:
                self._task_progress("federation-discovery")
                await self.clock.sleep(30)

    async def _queue_federation_hello(
        self, destination: str, *, target_mesh_id: str | None = None
    ) -> bool:
        local_id = self.radio.local_node_id
        if not local_id:
            return False
        self.federation.local_mesh_id = local_id
        capabilities = {
            "internet": True,
            "weather": self.config.modules.env.enabled,
            "alerts": self.config.modules.env.enabled,
            "bbs": self.config.modules.bbs.enabled,
            "ai": self.config.modules.ai.enabled,
        }
        counter = int(self.clock.now().timestamp()) & 0xFFFFFFFF
        hello = {
            "mesh_id": local_id,
            "name": self.outpost_name,
            "protocol": 1,
            "capabilities": capabilities,
        }
        if target_mesh_id is not None:
            hello["target_mesh_id"] = target_mesh_id
        frames = self.federation_codec.encode(
            MessageType.HELLO,
            hello,
            counter,
            None,
        )
        admitted = await self.governor.admit_many(
            [
                OutboundItem(
                    text="",
                    binary_payload=frame,
                    portnum=self.config.radio.federation_portnum,
                    dest=destination,
                    channel=channel_slot(self.config, "outpost", 0),
                    traffic_class=TrafficClass.FEDERATION,
                    want_ack=destination != "^all",
                    multipart=len(frames) > 1,
                )
                for frame in frames
            ]
        )
        return admitted is not None

    async def _queue_federation_frames(
        self,
        frames: list[bytes],
        destination: str,
        *,
        want_ack: bool,
        queue_key: str | None = None,
        traffic_class: TrafficClass = TrafficClass.FEDERATION,
    ) -> list[int]:
        admitted = await self.governor.admit_many(
            [
                OutboundItem(
                    text="",
                    binary_payload=frame,
                    portnum=self.config.radio.federation_portnum,
                    dest=destination,
                    channel=channel_slot(self.config, "outpost", 0),
                    traffic_class=traffic_class,
                    want_ack=want_ack,
                    multipart=len(frames) > 1,
                    queue_key=queue_key,
                )
                for frame in frames
            ]
        )
        if admitted is None:
            raise ValueError("federation queue rejected the complete message")
        return admitted

    async def _queue_trusted_federation_frames(
        self,
        frames: list[bytes],
        *,
        queue_key: str | None = None,
        traffic_class: TrafficClass = TrafficClass.FEDERATION,
    ) -> list[int]:
        # Meshtastic direct custom-app packets can be radio-ACKed without being surfaced to the
        # destination client. Federation already authenticates and encrypts each peer's frames,
        # so use the same RF/MQTT-compatible carrier as pairing and rely on application receipts.
        return await self._queue_federation_frames(
            frames,
            "^all",
            want_ack=False,
            queue_key=queue_key,
            traffic_class=traffic_class,
        )

    @staticmethod
    def _federation_rejection_reason(error: Exception) -> str:
        reason = str(error).lower()
        if "hmac" in reason or "authentication" in reason:
            return "authentication failed"
        if "secret" in reason or "paired peer" in reason:
            return "unauthenticated peer"
        if "replay" in reason:
            return "replay detected"
        if "expired" in reason:
            return "expired message"
        if "identity" in reason:
            return "identity mismatch"
        if "outside peer" in reason or "outside peer sync policy" in reason:
            return "policy denied"
        if "reconciliation" in reason:
            return "reconciliation protocol violation"
        return "invalid federation frame"

    async def initiate_federation_pairing(self, mesh_id: str) -> object:
        if not self.config.modules.fed.enabled:
            raise ValueError("federation module is disabled")
        local_id = self.radio.local_node_id
        if not local_id:
            raise ValueError("radio identity is not available")
        self.federation.local_mesh_id = local_id
        peer, payload = await self.federation.create_pairing_request(mesh_id)
        frames = self.federation_codec.encode(MessageType.PAIR_REQ, payload, 0, None)
        # Pre-trust directed packets are not consistently bridged by Meshtastic MQTT.
        # The application-level target makes this broadcast safe for mixed RF/MQTT paths.
        await self._queue_federation_frames(frames, "^all", want_ack=False)
        return peer

    async def approve_federation_pairing(self, mesh_id: str, code: str) -> object:
        if not self.config.modules.fed.enabled:
            raise ValueError("federation module is disabled")
        secret = await self.federation.pairing_secret(mesh_id)
        peer = await self.federation.approve_local(mesh_id, current_actor(), code)
        frames = self.federation_codec.encode(
            MessageType.PAIR_CONFIRM,
            {
                "mesh_id": self.federation.local_mesh_id,
                "target_mesh_id": mesh_id,
                "approved": True,
            },
            0,
            secret,
        )
        await self._queue_federation_frames(frames, "^all", want_ack=False)
        return peer

    async def federation_service_requests(self) -> list[dict[str, object]]:
        rows = await self.database.read(
            "SELECT * FROM fed_service_request ORDER BY created_at DESC LIMIT 100"
        )
        values: list[dict[str, object]] = []
        for row in rows:
            value = dict(row)
            for field in ("args_json", "result_json", "provenance_json", "candidate_peers"):
                raw = value.pop(field)
                value[field.removesuffix("_json")] = json.loads(raw) if raw else None
            values.append(value)
        return values

    async def request_federation_service(
        self, service: str, args: dict[str, object]
    ) -> dict[str, object]:
        if not self.config.modules.fed.enabled:
            raise ValueError("federation module is disabled")
        if service not in {"weather", "alerts", "knowledge"}:
            raise ValueError("unsupported federation service")
        peers = [
            peer
            for peer in await self.federation.list("active")
            if peer.capabilities.get(service) is True
            or (service == "knowledge" and peer.capabilities.get("ai") is True)
        ]
        if not peers:
            raise ValueError(f"no active peer advertises the {service} service")
        args = dict(args)
        if service in {"weather", "alerts"} and not {"lat", "lon"}.issubset(args):
            location = self.config.node.location
            if location is None:
                raise ValueError("Outpost coordinates are required for this peer service")
            args.update({"lat": location.lat, "lon": location.lon})
        args, _ = self._normalized_service_args(service, args)
        now = int(self.clock.now().timestamp())
        request_id = secrets.token_hex(12)
        candidates = [peer.mesh_id for peer in peers]
        async with self.database.transaction() as transaction:
            await transaction.write(
                "INSERT INTO fed_service_request(request_id,direction,peer_mesh_id,service,"
                "args_json,status,candidate_peers,created_at,updated_at,expires_at) "
                "VALUES(?,'out',?,?,?,'pending',?,?,?,?)",
                (
                    request_id,
                    candidates[0],
                    service,
                    json.dumps(args, separators=(",", ":")),
                    json.dumps(candidates, separators=(",", ":")),
                    now,
                    now,
                    now + 180,
                ),
            )
            await transaction.write(
                "DELETE FROM fed_service_request WHERE request_id IN ("
                "SELECT request_id FROM fed_service_request WHERE direction='out' "
                "AND status<>'pending' ORDER BY updated_at DESC LIMIT -1 OFFSET 500)"
            )
        await self._send_service_query(request_id, candidates[0], service, args, now + 180)
        return (await self.federation_service_requests())[0]

    @staticmethod
    def _normalized_service_args(
        service: str, args: dict[str, object]
    ) -> tuple[dict[str, object], str]:
        if service in {"weather", "alerts"}:
            try:
                lat, lon = round(_float_value(args["lat"]), 4), round(_float_value(args["lon"]), 4)
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError(f"{service} coordinates are required") from error
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError(f"invalid {service} coordinates")
            normalized: dict[str, object] = {"lat": lat, "lon": lon}
        else:
            query = str(args.get("query") or "").strip()
            if not query or len(query) > 200:
                raise ValueError("knowledge query must be 1-200 characters")
            normalized = {"query": query}
        encoded = json.dumps(normalized, separators=(",", ":"), sort_keys=True)
        return normalized, hashlib.sha256(f"{service}:{encoded}".encode()).hexdigest()[:32]

    async def _send_service_query(
        self,
        request_id: str,
        peer_id: str,
        service: str,
        args: dict[str, object],
        expires_at: int,
    ) -> None:
        secret = await self.federation.secret(peer_id)
        counter = await self.federation.next_counter(peer_id)
        frames = self.federation_codec.encode(
            MessageType.SERVICE_QUERY,
            {
                "request_id": request_id,
                "mesh_id": self.federation.local_mesh_id,
                "service": service,
                "args": args,
                "expires_at": expires_at,
                "ttl": 1,
                "max_bytes": 1200,
            },
            counter,
            secret,
        )
        await self._queue_trusted_federation_frames(frames)

    async def _execute_peer_service(
        self, service: str, args: dict[str, object]
    ) -> tuple[dict[str, object], dict[str, object]]:
        if service == "weather":
            if not self.config.modules.env.enabled:
                raise ValueError("environment module is disabled")
            location = self.config.node.location
            lat = _float_value(args.get("lat", location.lat if location else 0))
            lon = _float_value(args.get("lon", location.lon if location else 0))
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError("invalid weather coordinates")
            snapshot = await self.weather.current(lat, lon)
            # Keep the routine peer weather response inside one Meshtastic frame.
            # The full local snapshot remains available on the Environment dashboard;
            # federation carries the core conditions needed by a disconnected peer.
            result = snapshot.json()
            return {
                key: result[key]
                for key in (
                    "temperature_c",
                    "precipitation_mm",
                    "wind_kph",
                    "wind_direction",
                    "weather_code",
                )
            }, {
                "provider": snapshot.provider,
                "source_kind": snapshot.source_kind,
                "delivery_kind": "peer",
                "valid_at": snapshot.observed_at,
                "valid_age_seconds": snapshot.valid_age_seconds,
                "cached": snapshot.stale,
                "fetched_at": snapshot.fetched_at,
                "serving_outpost": self.federation.local_mesh_id,
            }
        if service == "alerts":
            if not self.config.modules.env.enabled:
                raise ValueError("environment module is disabled")
            try:
                lat, lon = _float_value(args["lat"]), _float_value(args["lon"])
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError("alert coordinates are required") from error
            result, provenance = await self.cap_alerts.query_point(lat, lon)
            provenance["serving_outpost"] = self.federation.local_mesh_id
            return result, provenance
        raise ValueError("public knowledge provider is not configured")

    @staticmethod
    def _service_result_to_wire(service: str, result: dict[str, object]) -> dict[str, object]:
        if service != "weather":
            return result
        names = {
            "temperature_c": "t",
            "precipitation_mm": "p",
            "wind_kph": "w",
            "wind_direction": "d",
            "weather_code": "c",
        }
        return {wire: result[name] for name, wire in names.items() if name in result}

    @staticmethod
    def _service_result_from_wire(service: str, result: object) -> object:
        if service != "weather" or not isinstance(result, dict):
            return result
        names = {
            "t": "temperature_c",
            "p": "precipitation_mm",
            "w": "wind_kph",
            "d": "wind_direction",
            "c": "weather_code",
        }
        return {names.get(str(name), str(name)): value for name, value in result.items()}

    @staticmethod
    def _service_provenance_to_wire(
        service: str, provenance: dict[str, object]
    ) -> dict[str, object]:
        if service != "weather":
            return provenance
        valid_at = provenance.get("valid_at")
        valid_epoch: int | None = None
        if valid_at:
            try:
                parsed = datetime.fromisoformat(str(valid_at).replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
                valid_epoch = int(parsed.timestamp())
            except ValueError:
                pass
        source = {"observation": "o", "forecast": "f", "estimate": "e", "peer": "p"}
        return {
            "p": provenance.get("provider"),
            "k": source.get(str(provenance.get("source_kind")), "u"),
            "v": valid_epoch,
            "c": provenance.get("cached") is True,
            "f": provenance.get("fetched_at"),
        }

    @staticmethod
    def _service_provenance_from_wire(
        service: str, provenance: object, now: int, sender: str
    ) -> object:
        if service != "weather" or not isinstance(provenance, dict):
            return provenance
        if "p" not in provenance:
            expanded = dict(provenance)
            expanded.setdefault("delivery_kind", "peer")
            expanded.setdefault("serving_outpost", sender)
            return expanded
        valid_epoch = provenance.get("v")
        valid_at = None
        valid_age = None
        if isinstance(valid_epoch, int | float):
            valid_at = datetime.fromtimestamp(valid_epoch, UTC).isoformat()
            valid_age = max(0, now - int(valid_epoch))
        return {
            "provider": provenance.get("p"),
            "source_kind": {
                "o": "observation",
                "f": "forecast",
                "e": "estimate",
                "p": "peer",
            }.get(str(provenance.get("k")), "unknown"),
            "delivery_kind": "peer",
            "valid_at": valid_at,
            "valid_age_seconds": valid_age,
            "cached": provenance.get("c") is True,
            "fetched_at": provenance.get("f"),
            "serving_outpost": sender,
        }

    async def _queue_relay_service_response(
        self,
        row: Any,
        peer: Peer,
        ok: bool,
        result: dict[str, object],
        provenance: dict[str, object],
        error: str | None,
        now: int,
    ) -> None:
        service = str(row["service"])

        def response_payload(
            response_ok: bool,
            response_result: dict[str, object],
            response_provenance: dict[str, object],
            response_error: str | None,
        ) -> dict[str, object]:
            return {
                "request_id": str(row["request_id"]),
                "request_envelope_id": str(row["relay_envelope_id"]),
                "service": service,
                "ok": response_ok,
                "result": self._service_result_to_wire(service, response_result),
                "provenance": self._service_provenance_to_wire(service, response_provenance),
                "error": response_error,
            }

        payload = response_payload(ok, result, provenance, error)
        try:
            encoded = self.federation_relay._payload_bytes("receipt", payload)
        except ValueError:
            ok = False
            result = {}
            provenance = {"serving_outpost": self.federation.local_mesh_id}
            error = "peer service response exceeds relay payload limit"
            payload = response_payload(ok, result, provenance, error)
            encoded = self.federation_relay._payload_bytes("receipt", payload)
        response_bytes = len(encoded)
        airtime_seconds = self.governor.estimate_toa(
            response_bytes, portnum=self.config.radio.federation_portnum
        )
        denied = await self.federation.reserve_service_response(
            peer, response_bytes, airtime_seconds, now
        )
        if denied is not None:
            ok = False
            result = {}
            provenance = {"serving_outpost": self.federation.local_mesh_id}
            error = (
                "peer service response exceeds byte policy"
                if denied == "response_byte_quota"
                else "peer service airtime quota exceeded"
            )
            payload = response_payload(ok, result, provenance, error)
            encoded = self.federation_relay._payload_bytes("receipt", payload)
            response_bytes = len(encoded)
            airtime_seconds = self.governor.estimate_toa(
                response_bytes, portnum=self.config.radio.federation_portnum
            )
            denied = await self.federation.reserve_service_response(
                peer, response_bytes, airtime_seconds, now
            )
        if denied is not None:
            await self.database.write(
                "UPDATE fed_service_request SET relay_response_attempts="
                "relay_response_attempts+1,relay_response_error=?,updated_at=? "
                "WHERE request_id=?",
                (error, now, row["request_id"]),
            )
            return
        try:
            envelope_id = await self.federation_relay.create(
                str(row["relay_origin_node"]),
                "receipt",
                payload,
                expires_in=86_400,
                hop_limit=3,
                idempotency_key=(
                    "service-response:"
                    + hashlib.sha256(str(row["request_id"]).encode()).hexdigest()[:32]
                ),
                actor="system:relay-service",
            )
        except ValueError as caught:
            await self.database.write(
                "UPDATE fed_service_request SET relay_response_attempts="
                "relay_response_attempts+1,relay_response_error=?,updated_at=? "
                "WHERE request_id=?",
                (str(caught)[:160], now, row["request_id"]),
            )
            return
        await self.database.write(
            "UPDATE fed_service_request SET relay_response_envelope_id=?,"
            "relay_response_attempts=relay_response_attempts+1,relay_response_error=NULL,"
            "response_bytes=response_bytes+?,response_airtime_seconds="
            "response_airtime_seconds+?,response_count=response_count+1,updated_at=? "
            "WHERE request_id=?",
            (envelope_id, response_bytes, airtime_seconds, now, row["request_id"]),
        )

    async def _process_relay_service_requests(self, now: int) -> None:
        rows = await self.database.read(
            "SELECT * FROM fed_service_request WHERE direction='in' "
            "AND relay_envelope_id IS NOT NULL AND relay_response_envelope_id IS NULL "
            "AND (relay_response_attempts=0 OR updated_at<=?) "
            "ORDER BY created_at,request_id LIMIT 4",
            (now - 60,),
        )
        denied_errors = {
            "permission_denied": "peer service is not permitted by operator policy",
            "request_quota": "peer service request quota exceeded",
            "concurrency_quota": "peer service concurrency quota exceeded",
            "circuit_open": "peer service provider circuit is temporarily open",
        }
        for stored in rows:
            row = dict(stored)
            try:
                peer = await self.federation.by_mesh_id(str(row["peer_mesh_id"]))
            except ValueError:
                await self.database.write(
                    "UPDATE fed_service_request SET relay_response_attempts="
                    "relay_response_attempts+1,relay_response_error=?,updated_at=? "
                    "WHERE request_id=?",
                    (
                        "responsible relay peer is no longer available",
                        now,
                        row["request_id"],
                    ),
                )
                continue
            status = str(row["status"])
            if status == "pending":
                ok = True
                result: dict[str, object] = {}
                provenance: dict[str, object] = {}
                error: str | None = None
                provider_failed = False
                if now >= int(row["expires_at"]):
                    ok = False
                    error = "relayed peer service request expired before execution"
                else:
                    try:
                        async with asyncio.timeout(30):
                            result, provenance = await self._execute_peer_service(
                                str(row["service"]), json.loads(row["args_json"])
                            )
                        if (
                            row["service"] == "alerts"
                            and result.get("status") == "provider_failure"
                        ):
                            ok = False
                            provider_failed = True
                            error = str(result.get("error") or "public alert provider failed")[:160]
                    except (OSError, RuntimeError, ValueError) as caught:
                        ok, provider_failed, error = False, True, str(caught)[:160]
                    await self.federation.record_service_provider_outcome(
                        peer, str(row["service"]), provider_failed, now
                    )
                status = "complete" if ok else "failed"
                await self.database.write(
                    "UPDATE fed_service_request SET status=?,result_json=?,provenance_json=?,"
                    "error=?,completed_at=?,updated_at=? WHERE request_id=?",
                    (
                        status,
                        json.dumps(result, separators=(",", ":")),
                        json.dumps(provenance, separators=(",", ":")),
                        error,
                        now,
                        now,
                        row["request_id"],
                    ),
                )
                row.update(
                    {
                        "status": status,
                        "result_json": json.dumps(result, separators=(",", ":")),
                        "provenance_json": json.dumps(provenance, separators=(",", ":")),
                        "error": error,
                    }
                )
            else:
                result = json.loads(row["result_json"] or "{}")
                provenance = json.loads(row["provenance_json"] or "{}")
                error = denied_errors.get(str(row["error"]), row["error"])
            await self._queue_relay_service_response(
                row,
                peer,
                status == "complete",
                result,
                provenance,
                str(error)[:160] if error else None,
                now,
            )

    async def _federation_service_loop(self) -> None:
        while True:
            if not self.config.modules.fed.enabled:
                self._task_progress("federation-services")
                await self.clock.sleep(15)
                continue
            now = int(self.clock.now().timestamp())
            await self._process_relay_service_requests(now)
            rows = await self.database.read(
                "SELECT * FROM fed_service_request WHERE direction='out' AND status='pending'"
            )
            for row in rows:
                if now >= row["expires_at"]:
                    await self.database.write(
                        "UPDATE fed_service_request SET status='expired',updated_at=?,"
                        "error='No paired Outpost responded before expiry' WHERE request_id=?",
                        (now, row["request_id"]),
                    )
                    continue
                if now - row["updated_at"] < 60:
                    continue
                candidates = json.loads(row["candidate_peers"])
                attempt = int(row["attempt"]) + 1
                if attempt >= len(candidates):
                    continue
                peer_id = candidates[attempt]
                await self.database.write(
                    "UPDATE fed_service_request SET peer_mesh_id=?,attempt=?,updated_at=? "
                    "WHERE request_id=?",
                    (peer_id, attempt, now, row["request_id"]),
                )
                try:
                    await self._send_service_query(
                        row["request_id"],
                        peer_id,
                        row["service"],
                        json.loads(row["args_json"]),
                        row["expires_at"],
                    )
                except ValueError:
                    continue
            self._task_progress("federation-services")
            await self.clock.sleep(15)

    async def _send_federation_value(
        self,
        peer_id: str,
        msg_type: MessageType,
        value: dict[str, object],
        *,
        counter: int | None = None,
        queue_key: str | None = None,
        traffic_class: TrafficClass = TrafficClass.FEDERATION,
    ) -> int:
        local_id = self.radio.local_node_id
        if not local_id:
            raise ValueError("radio identity is not available")
        self.federation.local_mesh_id = local_id
        self.federation_sync.local_mesh_id = local_id
        peer = await self.federation.by_mesh_id(peer_id)
        if not self.federation.is_online(peer):
            raise ValueError(
                "peer is offline; federation traffic is paused until inbound activity resumes"
            )
        value = {**value, "mesh_id": local_id}
        secret = await self.federation.secret(peer_id)
        counter = counter or await self.federation.next_counter(peer_id)
        frames = self.federation_codec.encode(msg_type, value, counter, secret)
        await self._queue_trusted_federation_frames(
            frames, queue_key=queue_key, traffic_class=traffic_class
        )
        return counter

    @staticmethod
    def _federation_control_queue_key(peer_id: str, msg_type: MessageType) -> str:
        return f"federation:{peer_id}:{msg_type.name.lower()}"

    async def _federation_control_outstanding(self, peer_id: str, msg_type: MessageType) -> bool:
        queue_key = self._federation_control_queue_key(peer_id, msg_type)
        rows = await self.database.read(
            "SELECT queue_key,binary_payload FROM outbound_work "
            "WHERE traffic_class='federation' "
            "AND state IN ('pending','held','sending','awaiting_ack') "
            "AND (queue_key=? OR queue_key IS NULL)",
            (queue_key,),
        )
        secret: bytes | None = None
        for row in rows:
            if row["queue_key"] == queue_key:
                return True
            payload = row["binary_payload"]
            if payload is None:
                continue
            if secret is None:
                secret = await self.federation.secret(peer_id)
            try:
                fragment = self.federation_codec.decode_fragment(bytes(payload), secret)
            except FrameError:
                continue
            if fragment.msg_type is msg_type:
                return True
        return False

    async def _queue_federation_control(
        self, peer_id: str, msg_type: MessageType, value: dict[str, object]
    ) -> bool:
        if msg_type not in {
            MessageType.SYNC_REQ,
            MessageType.SYNC_MANIFEST,
            MessageType.ITEM_REQ,
            MessageType.SYNC_DONE,
        }:
            raise ValueError("federation control frame is not single-flight")
        queue_key = self._federation_control_queue_key(peer_id, msg_type)
        lock = self._federation_control_locks.setdefault(queue_key, asyncio.Lock())
        async with lock:
            if await self._federation_control_outstanding(peer_id, msg_type):
                return False
            await self._send_federation_value(
                peer_id,
                msg_type,
                value,
                queue_key=queue_key,
            )
            return True

    @staticmethod
    def _reconciliation_cursor(value: object, field: str) -> list[object] | None:
        if value is None:
            return None
        if not isinstance(value, list) or len(value) != 3:
            raise ValueError(f"invalid federation {field} cursor")
        version = wire_int(value[0], f"{field} version")
        stream, uid = value[1], value[2]
        if (
            not isinstance(stream, str)
            or not stream
            or len(stream) > 80
            or not isinstance(uid, str)
            or not uid
            or len(uid) > 160
        ):
            raise ValueError(f"invalid federation {field} cursor")
        return [version, stream, uid]

    async def _store_reconciliation_checkpoint(
        self, peer_id: int, checkpoint: dict[str, object], now: int
    ) -> None:
        await self.database.write(
            "INSERT INTO fed_cursor(peer_id,stream,direction,cursor,updated_at) "
            "VALUES(?,'_reconcile','recv',?,?) "
            "ON CONFLICT(peer_id,stream,direction) DO UPDATE SET "
            "cursor=excluded.cursor,updated_at=excluded.updated_at",
            (peer_id, json.dumps(checkpoint, separators=(",", ":")), now),
        )

    async def _stop_reconciliation(
        self,
        peer_id: int,
        checkpoint: dict[str, object],
        *,
        status: str,
        reason: str,
        before: list[object] | None,
        now: int,
    ) -> None:
        stopped = {
            **checkpoint,
            "before": before,
            "pending": False,
            "status": status,
            "reason": reason,
            "stopped_at": now,
            "resume_after": now + self.config.fed.sync_interval_minutes * 60,
        }
        await self._store_reconciliation_checkpoint(peer_id, stopped, now)

    async def _handle_sync_manifest(self, sender: str, value: dict[str, object]) -> None:
        manifest = value.get("items", [])
        if not isinstance(manifest, list):
            raise ValueError("invalid sync manifest")
        peer = await self.federation.by_mesh_id(sender)
        rows = await self.database.read(
            "SELECT cursor FROM fed_cursor WHERE peer_id=? AND stream='_reconcile' "
            "AND direction='recv'",
            (peer.id,),
        )
        if not rows:
            raise ValueError("unexpected federation reconciliation manifest")
        try:
            checkpoint = json.loads(str(rows[0]["cursor"]))
        except (TypeError, ValueError) as error:
            raise ValueError("invalid local federation reconciliation checkpoint") from error
        if not isinstance(checkpoint, dict):
            raise ValueError("invalid local federation reconciliation checkpoint")
        if checkpoint.get("status") not in {None, "active"}:
            raise ValueError("unexpected federation reconciliation manifest after cycle stopped")
        now = int(self.clock.now().timestamp())
        configured_budget = self.config.fed.max_items_per_cycle
        try:
            cycle_budget = min(configured_budget, int(checkpoint.get("budget", configured_budget)))
            used = max(0, int(checkpoint.get("used", 0)))
            rounds = max(0, int(checkpoint.get("rounds", 0)))
        except (TypeError, ValueError) as error:
            raise ValueError("invalid local federation reconciliation checkpoint") from error
        if cycle_budget < 1 or used > cycle_budget or rounds >= _MAX_RECONCILIATION_ROUNDS:
            raise ValueError("invalid local federation reconciliation checkpoint")
        snapshot = wire_int(checkpoint.get("snapshot"), "reconciliation snapshot")
        if wire_int(value.get("snapshot", snapshot), "snapshot") != snapshot:
            await self._stop_reconciliation(
                peer.id,
                checkpoint,
                status="aborted",
                reason="peer changed the reconciliation snapshot",
                before=self._reconciliation_cursor(checkpoint.get("before"), "prior"),
                now=now,
            )
            raise ValueError("peer changed the federation reconciliation snapshot")
        # The peer's remaining field is protocol metadata only. Validate it, but never use it
        # to expand the locally persisted cycle budget.
        wire_int(value.get("remaining", 0), "remaining", minimum=0, maximum=100)
        previous = self._reconciliation_cursor(checkpoint.get("before"), "prior")
        raw_next = self._reconciliation_cursor(value.get("next_before"), "continuation")
        local_remaining = cycle_budget - used
        if len(manifest) > min(8, local_remaining):
            reason = "peer exceeded the local reconciliation page budget"
            await self._stop_reconciliation(
                peer.id,
                checkpoint,
                status="aborted",
                reason=reason,
                before=previous,
                now=now,
            )
            raise ValueError(reason)
        page_cursors: list[list[object]] = []
        try:
            for item in manifest:
                if not isinstance(item, dict):
                    raise ValueError("peer supplied an invalid reconciliation page item")
                item_cursor = self._reconciliation_cursor(
                    [
                        item.get("version", item.get("v")),
                        item.get("stream", item.get("s")),
                        item.get("uid", item.get("u")),
                    ],
                    "page",
                )
                if item_cursor is None:
                    raise ValueError("peer supplied an invalid reconciliation page cursor")
                page_cursors.append(item_cursor)
        except ValueError as error:
            reason = str(error)
            await self._stop_reconciliation(
                peer.id,
                checkpoint,
                status="aborted",
                reason=reason,
                before=previous,
                now=now,
            )
            raise
        if any(_int_value(cursor[0]) > snapshot for cursor in page_cursors) or any(
            tuple(current) >= tuple(prior)
            for prior, current in zip(page_cursors, page_cursors[1:], strict=False)
        ):
            reason = "peer reconciliation page is not strictly descending within the snapshot"
            await self._stop_reconciliation(
                peer.id,
                checkpoint,
                status="aborted",
                reason=reason,
                before=previous,
                now=now,
            )
            raise ValueError(reason)
        if previous is not None and any(
            tuple(cursor) >= tuple(previous) for cursor in page_cursors
        ):
            reason = "peer reconciliation cursor did not advance"
            await self._stop_reconciliation(
                peer.id,
                checkpoint,
                status="aborted",
                reason=reason,
                before=previous,
                now=now,
            )
            raise ValueError(reason)
        if raw_next is not None:
            if not manifest:
                reason = "peer supplied a continuation cursor without page items"
            else:
                reason = (
                    "peer continuation cursor does not match the page boundary"
                    if raw_next != page_cursors[-1]
                    else ""
                )
            if not reason and previous is not None and tuple(raw_next) >= tuple(previous):
                reason = "peer reconciliation cursor did not advance"
            if reason:
                await self._stop_reconciliation(
                    peer.id,
                    checkpoint,
                    status="aborted",
                    reason=reason,
                    before=previous,
                    now=now,
                )
                raise ValueError(reason)
        used += len(manifest)
        rounds += 1
        checkpoint = {
            **checkpoint,
            "before": raw_next,
            "snapshot": snapshot,
            "pending": False,
            "status": "active",
            "reason": None,
            "budget": cycle_budget,
            "used": used,
            "rounds": rounds,
            "resume_after": None,
        }
        await self._store_reconciliation_checkpoint(peer.id, checkpoint, now)
        missing = await self.federation_sync.missing(manifest)
        if missing:
            await self._queue_federation_control(
                sender,
                MessageType.ITEM_REQ,
                {"mesh_id": self.federation.local_mesh_id, "items": missing[:8]},
            )
        else:
            await self._queue_federation_control(
                sender,
                MessageType.SYNC_DONE,
                {"mesh_id": self.federation.local_mesh_id, "received": 0},
            )
        remaining = cycle_budget - used
        if raw_next is not None and remaining > 0 and rounds < _MAX_RECONCILIATION_ROUNDS:
            queued = await self._queue_federation_control(
                sender,
                MessageType.SYNC_REQ,
                {
                    "limit": min(8, remaining),
                    "budget": remaining,
                    "snapshot": snapshot,
                    "before": raw_next,
                },
            )
            if queued:
                checkpoint["pending"] = True
                await self._store_reconciliation_checkpoint(peer.id, checkpoint, now)
        elif raw_next is not None:
            reason = (
                "local reconciliation item budget exhausted"
                if remaining <= 0
                else "local reconciliation continuation round limit reached"
            )
            await self._stop_reconciliation(
                peer.id,
                checkpoint,
                status="truncated",
                reason=reason,
                before=raw_next,
                now=now,
            )
        else:
            checkpoint["status"] = "complete"
            await self._store_reconciliation_checkpoint(peer.id, checkpoint, now)
            await self.database.write(
                "UPDATE fed_peer SET last_sync_at=? WHERE id=?", (now, peer.id)
            )

    async def _federation_sync_once(self) -> None:
        if not self.config.modules.fed.enabled or not self.radio.local_node_id:
            return
        now = int(self.clock.now().timestamp())
        for peer in await self.federation.list("active"):
            if not self.federation.is_online(peer, now=now):
                continue
            if not (peer.boards or peer.sync_incidents or peer.relay_alerts):
                continue
            rows = await self.database.read(
                "SELECT cursor,updated_at FROM fed_cursor WHERE peer_id=? "
                "AND stream='_reconcile' AND direction='recv'",
                (peer.id,),
            )
            checkpoint: dict[str, object] = {}
            updated_at = 0
            if rows:
                try:
                    checkpoint = json.loads(str(rows[0]["cursor"]))
                except (TypeError, ValueError):
                    checkpoint = {}
                updated_at = int(rows[0]["updated_at"])
            cursor = checkpoint.get("before")
            pending = bool(checkpoint.get("pending"))
            continuing = isinstance(cursor, list) and len(cursor) == 3
            stopped = checkpoint.get("status") in {"aborted", "truncated"}
            peer_row = (
                await self.database.read("SELECT last_sync_at FROM fed_peer WHERE id=?", (peer.id,))
            )[0]
            interval = self.config.fed.sync_interval_minutes * 60
            periodic_due = (
                peer_row["last_sync_at"] is None or now - int(peer_row["last_sync_at"]) >= interval
            )
            retry_due = pending and now - updated_at >= self.config.fed.sync_retry_minutes * 60
            continuation_due = continuing and not pending and not stopped and now - updated_at >= 30
            if stopped:
                try:
                    resume_after = _int_value(
                        checkpoint.get("resume_after") or updated_at + interval
                    )
                except (TypeError, ValueError):
                    resume_after = updated_at + interval
                if now < resume_after:
                    continue
            elif pending:
                if not retry_due:
                    continue
            elif continuing:
                if not continuation_due:
                    continue
            elif not periodic_due:
                continue
            snapshot = _int_value(checkpoint.get("snapshot") or now)
            if not continuing:
                cursor = None
                snapshot = now
            new_cycle = stopped or not continuing
            try:
                used = 0 if new_cycle else max(0, _int_value(checkpoint.get("used", 0)))
                rounds = 0 if new_cycle else max(0, _int_value(checkpoint.get("rounds", 0)))
            except (TypeError, ValueError):
                used, rounds = 0, 0
            budget = self.config.fed.max_items_per_cycle
            if not new_cycle and (used >= budget or rounds >= _MAX_RECONCILIATION_ROUNDS):
                reason = (
                    "local reconciliation item budget exhausted"
                    if used >= budget
                    else "local reconciliation continuation round limit reached"
                )
                await self._stop_reconciliation(
                    peer.id,
                    checkpoint,
                    status="truncated",
                    reason=reason,
                    before=self._reconciliation_cursor(cursor, "prior"),
                    now=now,
                )
                continue
            remaining = max(1, budget - used)
            next_checkpoint = {
                "before": cursor,
                "snapshot": snapshot,
                "pending": True,
                "status": "active",
                "reason": None,
                "budget": budget,
                "used": used,
                "rounds": rounds,
                "resume_after": None,
            }
            try:
                queued = await self._queue_federation_control(
                    peer.mesh_id,
                    MessageType.SYNC_REQ,
                    {
                        "limit": min(8, remaining),
                        "budget": remaining,
                        "snapshot": snapshot,
                        "before": cursor,
                    },
                )
                if not queued:
                    continue
                await self._store_reconciliation_checkpoint(peer.id, next_checkpoint, now)
            except (FrameError, ValueError):
                continue

    async def _federation_sync_loop(self) -> None:
        while True:
            await self._federation_sync_once()
            self._task_progress("federation-sync")
            await self.clock.sleep(30)

    async def _federation_delivery_loop(self) -> None:
        while True:
            if self.config.modules.fed.enabled and self.radio.local_node_id:
                now = int(self.clock.now().timestamp())
                self.federation_sync.local_mesh_id = self.radio.local_node_id
                await self.federation_sync.import_approved_replies("federation:auto-thread", now)
                pending_approvals = await self.database.read(
                    "SELECT mesh_id,shared_secret FROM fed_peer WHERE state='pairing' "
                    "AND local_approved=1 AND remote_approved=0 AND shared_secret IS NOT NULL"
                )
                for approval in pending_approvals:
                    frames = self.federation_codec.encode(
                        MessageType.PAIR_CONFIRM,
                        {
                            "mesh_id": self.radio.local_node_id,
                            "target_mesh_id": str(approval["mesh_id"]),
                            "approved": True,
                        },
                        now & 0xFFFFFFFF,
                        bytes(approval["shared_secret"]),
                    )
                    try:
                        await self._queue_federation_frames(frames, "^all", want_ack=False)
                    except ValueError:
                        pass
                rows = await self.database.read(
                    "SELECT d.peer_id,d.post_id,d.uid,d.stream,d.wire_counter,p.mesh_id,"
                    "post.uid local_uid "
                    "FROM fed_post_delivery d JOIN fed_peer p ON p.id=d.peer_id "
                    "JOIN post ON post.id=d.post_id WHERE d.state<>'delivered' "
                    "AND d.updated_at<=? AND p.state='active' ORDER BY d.updated_at LIMIT 20",
                    (now - 120,),
                )
                for row in rows:
                    uid = self.federation_sync.wire_uid(str(row["local_uid"]))
                    await self.database.write(
                        "UPDATE fed_post_delivery SET uid=? WHERE peer_id=? AND post_id=?",
                        (uid, row["peer_id"], row["post_id"]),
                    )
                    try:
                        peer = await self.federation.by_mesh_id(str(row["mesh_id"]))
                        if not self.federation.is_online(peer, now=now):
                            continue
                        items = await self.federation_sync.export_items(
                            peer, [{"stream": str(row["stream"]), "uid": uid}]
                        )
                        if not items:
                            raise ValueError("durable federation post could not be exported")
                        wire_counter = await self._send_federation_value(
                            str(row["mesh_id"]),
                            MessageType.ITEM,
                            {"item": items[0]},
                            counter=(
                                int(row["wire_counter"])
                                if row["wire_counter"] is not None
                                else None
                            ),
                        )
                        await self.database.write(
                            "UPDATE fed_post_delivery SET state='sent',attempts=attempts+1,"
                            "wire_counter=?,updated_at=?,error=NULL WHERE peer_id=? AND post_id=?",
                            (wire_counter, now, row["peer_id"], row["post_id"]),
                        )
                    except (FrameError, ValueError) as error:
                        await self.database.write(
                            "UPDATE fed_post_delivery SET attempts=attempts+1,updated_at=?,error=? "
                            "WHERE peer_id=? AND post_id=?",
                            (now, str(error)[:120], row["peer_id"], row["post_id"]),
                        )
            self._task_progress("federation-delivery")
            await self.clock.sleep(30)

    async def _send_relay_receipt(
        self, peer_id: str, envelope_id: str, state: str, reason: str | None = None
    ) -> None:
        receipt: dict[str, object] = {
            "target_mesh_id": peer_id,
            "envelope_id": envelope_id,
            "state": state,
        }
        if reason:
            receipt["reason"] = reason[:160]
        await self._send_federation_value(
            peer_id,
            MessageType.RELAY_ACK,
            receipt,
        )

    async def _federation_relay_loop(self) -> None:
        while True:
            local_id = self.radio.local_node_id
            if self.config.modules.fed.enabled and local_id:
                self.federation.local_mesh_id = local_id
                now = int(self.clock.now().timestamp())
                await self.federation_relay.expire(now=now)
                await self.federation_relay.recover_stalled(now=now)
                await self.federation_relay.recover_pending_dispatches()
                for receipt in await self.federation_relay.pending_receipts():
                    try:
                        receipt_peer = await self.federation.by_mesh_id(receipt["previous_hop"])
                    except ValueError:
                        continue
                    if not self.federation.is_online(receipt_peer, now=now):
                        continue
                    try:
                        await self._send_relay_receipt(
                            receipt["previous_hop"], receipt["envelope_id"], "delivered"
                        )
                    except (FrameError, ValueError):
                        continue
                    await self.federation_relay.mark_receipt_sent(receipt["envelope_id"])
                for item in await self.federation_relay.queue(50):
                    if item["state"] != "queued":
                        continue
                    envelope_id = str(item["envelope_id"])
                    try:
                        selected = await self.federation_relay.next_hop(envelope_id, now=now)
                        if selected is None:
                            continue
                        peer_id = selected["mesh_id"]
                        peer = await self.federation.by_mesh_id(peer_id)
                        if not self.federation.is_online(peer, now=now):
                            continue
                        secret = await self.federation.secret(peer_id)
                        counter = await self.federation.next_counter(peer_id)
                        frames = self.federation_codec.encode(
                            MessageType.RELAY_PUT,
                            {
                                "mesh_id": local_id,
                                "target_mesh_id": peer_id,
                                "envelope": await self.federation_relay.wire(envelope_id),
                            },
                            counter,
                            secret,
                        )
                        airtime = sum(
                            self.governor.estimate_toa(
                                len(frame), portnum=self.config.radio.federation_portnum
                            )
                            for frame in frames
                        )
                        reserved = await self.federation_relay.reserve_forward(
                            envelope_id,
                            peer_id,
                            airtime,
                            now=now,
                            path=selected["path"],
                        )
                        if not reserved:
                            continue
                        await self._queue_trusted_federation_frames(frames)
                    except (FrameError, ValueError) as error:
                        await self.federation_relay.mark_failed(envelope_id, str(error), now=now)
            self._task_progress("federation-relay")
            await self.clock.sleep(30)

    async def _federation_topology_loop(self) -> None:
        while True:
            local_id = self.radio.local_node_id
            if self.config.modules.fed.enabled and local_id:
                self.federation.local_mesh_id = local_id
                now = int(self.clock.now().timestamp())
                for peer_id in await self.federation_topology.due(now=now):
                    try:
                        await self._send_federation_value(
                            peer_id,
                            MessageType.TOPOLOGY_UPDATE,
                            {
                                "target_mesh_id": peer_id,
                                "topology": await self.federation_topology.advertisement(peer_id),
                            },
                        )
                    except (FrameError, ValueError):
                        continue
                    await self.federation_topology.mark_sent(peer_id, now=now)
            self._task_progress("federation-topology")
            await self.clock.sleep(60)

    async def _handle_federation_discovery(self, message: object) -> None:
        payload = getattr(message, "payload", None)
        sender = getattr(message, "from_id", "")
        if not isinstance(payload, bytes) or not sender:
            return
        await self.database.write(
            "UPDATE message_log SET airtime_class='federation' "
            "WHERE direction='in' AND packet_id=? AND peer_mesh_id=?",
            (getattr(message, "packet_id", None), sender),
        )
        if self.radio.local_node_id:
            self.federation_sync.local_mesh_id = self.radio.local_node_id
        try:
            if len(payload) < 3:
                raise FrameError("frame is shorter than federation header")
            msg_type = MessageType(payload[2])
            secret = None
            if msg_type is MessageType.PAIR_CONFIRM:
                try:
                    secret = await self.federation.pairing_secret(sender)
                except ValueError:
                    secret = await self.federation.secret(sender)
            elif msg_type in {
                MessageType.SERVICE_QUERY,
                MessageType.SERVICE_RESPONSE,
                MessageType.SYNC_REQ,
                MessageType.SYNC_MANIFEST,
                MessageType.ITEM_REQ,
                MessageType.ITEM,
                MessageType.SYNC_DONE,
                MessageType.SYNC_NOTIFY,
                MessageType.ITEM_RECEIPT,
                MessageType.MAIL_RELAY,
                MessageType.MAIL_RECEIPT,
                MessageType.RELAY_PUT,
                MessageType.RELAY_ACK,
                MessageType.TOPOLOGY_UPDATE,
            }:
                secret = await self.federation.secret(sender)
            fragment = self.federation_codec.decode_fragment(payload, secret)
            value = self.federation_reassembler.add(sender, fragment)
            if value is None:
                return
            authenticated = msg_type not in {
                MessageType.HELLO,
                MessageType.PAIR_REQ,
                MessageType.PAIR_ACK,
                MessageType.PAIR_CONFIRM,
            }
            replayed_item = False
            if authenticated and not (
                await self.federation.accept_counter(sender, fragment.counter)
            ):
                if msg_type is MessageType.ITEM:
                    replayed_item = True
                else:
                    raise FrameError("replayed federation service frame")
            if not isinstance(value, dict) or value.get("mesh_id") != sender:
                raise FrameError("federation identity does not match packet sender")
            target = value.get("target_mesh_id")
            if target is not None and target != self.radio.local_node_id:
                return
            if authenticated:
                await self.federation.touch(sender)
            self.federation.local_mesh_id = self.radio.local_node_id
            self.federation_sync.local_mesh_id = self.radio.local_node_id
            await self.federation_sync.import_approved_replies(
                "federation:auto-thread", int(self.clock.now().timestamp())
            )
            if msg_type is MessageType.HELLO:
                capabilities = value.get("capabilities", {})
                if not isinstance(capabilities, dict):
                    raise FrameError("HELLO capabilities must be a map")
                await self.federation.discover(
                    sender,
                    str(value.get("name", sender)),
                    wire_int(value.get("protocol", 1), "protocol", minimum=1, maximum=255),
                    capabilities,
                    "mqtt" if getattr(message, "via_mqtt", False) else "radio",
                )
                # A node may have joined just after our infrequent broadcast HELLO.
                # Answer broadcasts directly so both peer directories converge without
                # another broadcast or an endless HELLO response loop.
                if not getattr(message, "is_direct", False) and target is None:
                    if getattr(message, "via_mqtt", False):
                        await self._queue_federation_hello("^all", target_mesh_id=sender)
                    else:
                        await self._queue_federation_hello(sender)
            elif msg_type is MessageType.PAIR_REQ:
                _, acknowledgement, _ = await self.federation.accept_pairing_request(
                    sender,
                    wire_bytes(value.get("public_key"), "public_key", length=32),
                    wire_bytes(value.get("nonce"), "nonce", length=16),
                )
                frames = self.federation_codec.encode(
                    MessageType.PAIR_ACK, acknowledgement, 0, None
                )
                await self._queue_federation_frames(
                    frames,
                    sender if getattr(message, "is_direct", False) else "^all",
                    want_ack=bool(getattr(message, "is_direct", False)),
                )
            elif msg_type is MessageType.PAIR_ACK:
                await self.federation.accept_pairing_ack(
                    sender,
                    wire_bytes(value.get("public_key"), "public_key", length=32),
                    wire_bytes(value.get("nonce"), "nonce", length=16),
                )
            elif msg_type is MessageType.PAIR_CONFIRM and value.get("approved") is True:
                peer = await self.federation.confirm_remote(sender)
                if peer.local_approved and not value.get("receipt", False):
                    if secret is None:
                        raise FrameError("pairing confirmation secret is unavailable")
                    confirmation = self.federation_codec.encode(
                        MessageType.PAIR_CONFIRM,
                        {
                            "mesh_id": self.federation.local_mesh_id,
                            "target_mesh_id": sender,
                            "approved": True,
                            "receipt": True,
                        },
                        0,
                        secret,
                    )
                    await self._queue_federation_frames(confirmation, "^all", want_ack=False)
            elif msg_type is MessageType.SERVICE_QUERY:
                await self._handle_service_query(sender, value)
            elif msg_type is MessageType.SERVICE_RESPONSE:
                await self._handle_service_response(sender, value)
            elif msg_type is MessageType.SYNC_REQ:
                peer = await self.federation.by_mesh_id(sender)
                limit = wire_int(value.get("limit", 8), "limit", minimum=1, maximum=8)
                budget = wire_int(value.get("budget", limit), "budget", minimum=1, maximum=100)
                page_size = min(limit, budget)
                snapshot = wire_int(
                    value.get("snapshot", int(self.clock.now().timestamp())), "snapshot"
                )
                raw_before = value.get("before")
                before = None
                if raw_before is not None:
                    if not isinstance(raw_before, list) or len(raw_before) != 3:
                        raise ValueError("invalid federation reconciliation cursor")
                    before = (
                        wire_int(raw_before[0], "before version"),
                        str(raw_before[1]),
                        str(raw_before[2]),
                    )
                page = await self.federation_sync.manifest(
                    peer, page_size + 1, snapshot=snapshot, before=before
                )
                items = page[:page_size]
                has_more = len(page) > page_size
                next_before = None
                if has_more and items:
                    last = items[-1]
                    next_before = [last.version, last.stream, last.uid]
                await self._queue_federation_control(
                    sender,
                    MessageType.SYNC_MANIFEST,
                    {
                        "items": [item.json() for item in items],
                        "snapshot": snapshot,
                        "next_before": next_before,
                        "remaining": max(0, budget - len(items)),
                    },
                )
            elif msg_type is MessageType.SYNC_NOTIFY:
                stream = str(value.get("stream", ""))
                peer = await self.federation.by_mesh_id(sender)
                if not stream.startswith("board:") or stream[6:] not in peer.boards:
                    raise ValueError("federation change notification is outside peer policy")
                await self._queue_federation_control(sender, MessageType.SYNC_REQ, {"limit": 8})
            elif msg_type is MessageType.SYNC_MANIFEST:
                await self._handle_sync_manifest(sender, value)
            elif msg_type is MessageType.ITEM_REQ:
                requests = value.get("items", [])
                if not isinstance(requests, list):
                    raise ValueError("invalid federation item request")
                peer = await self.federation.by_mesh_id(sender)
                exported = await self.federation_sync.export_items(peer, requests)
                sent = 0
                for item in exported:
                    try:
                        await self._send_federation_value(
                            sender,
                            MessageType.ITEM,
                            {"mesh_id": self.federation.local_mesh_id, "item": item},
                        )
                        sent += 1
                    except FrameError:
                        continue
                await self._queue_federation_control(
                    sender,
                    MessageType.SYNC_DONE,
                    {"mesh_id": self.federation.local_mesh_id, "sent": sent},
                )
            elif msg_type is MessageType.ITEM:
                incoming_item = value.get("item")
                if not isinstance(incoming_item, dict):
                    raise ValueError("invalid federation item")
                peer = await self.federation.by_mesh_id(sender)
                received = False
                if not replayed_item:
                    received = await self.federation_sync.quarantine(
                        peer, incoming_item, int(self.clock.now().timestamp())
                    )
                if received and str(incoming_item.get("stream", "")).startswith("board:"):
                    payload = incoming_item.get("payload")
                    if isinstance(payload, dict):
                        slug = str(incoming_item["stream"])[6:]
                        approved = await self.federation_sync.approved_thread(
                            slug, str(payload.get("thread_uid", ""))
                        )
                        if approved or int(payload.get("seq", 0)) == 1:
                            inbox = await self.database.read(
                                "SELECT id FROM fed_inbox_item WHERE peer_id=? AND stream=? "
                                "AND uid=? AND state='pending'",
                                (
                                    peer.id,
                                    str(incoming_item["stream"]),
                                    str(incoming_item.get("uid", "")),
                                ),
                            )
                            if inbox:
                                await self.federation_sync.import_inbox(
                                    int(inbox[0]["id"]),
                                    "federation:auto-thread",
                                    int(self.clock.now().timestamp()),
                                )
                receipt = await self.database.read(
                    "SELECT state FROM fed_inbox_item WHERE peer_id=? AND stream=? AND uid=?",
                    (
                        peer.id,
                        str(incoming_item.get("stream", "")),
                        str(incoming_item.get("uid", "")),
                    ),
                )
                if receipt:
                    await self._send_federation_value(
                        sender,
                        MessageType.ITEM_RECEIPT,
                        {
                            "uid": str(incoming_item.get("uid", "")),
                            "state": str(receipt[0]["state"]),
                        },
                    )
            elif msg_type is MessageType.ITEM_RECEIPT:
                state = str(value.get("state", ""))
                if state not in {"pending", "imported", "rejected"}:
                    raise ValueError("invalid federation item receipt state")
                peer = await self.federation.by_mesh_id(sender)
                await self.database.write(
                    "UPDATE fed_post_delivery SET state='delivered',delivered_at=unixepoch(),"
                    "updated_at=unixepoch(),error=NULL WHERE peer_id=? AND uid=?",
                    (peer.id, str(value.get("uid", ""))),
                )
            elif msg_type is MessageType.SYNC_DONE:
                await self.database.write(
                    "UPDATE fed_peer SET last_sync_at=unixepoch() WHERE mesh_id=?",
                    (sender,),
                )
            elif msg_type is MessageType.MAIL_RELAY:
                try:
                    relay_id, state = await self.federation_mail.open(sender, value)
                except (KeyError, TypeError, ValueError) as error:
                    relay_id = str(value.get("relay_id", ""))
                    if relay_id and len(relay_id) <= 64:
                        await self._send_federation_value(
                            sender,
                            MessageType.MAIL_RECEIPT,
                            {
                                "mesh_id": self.federation.local_mesh_id,
                                "relay_id": relay_id,
                                "state": "failed",
                                "error": str(error)[:80],
                            },
                        )
                    raise
                await self._send_federation_value(
                    sender,
                    MessageType.MAIL_RECEIPT,
                    {
                        "mesh_id": self.federation.local_mesh_id,
                        "relay_id": relay_id,
                        "state": state,
                    },
                )
            elif msg_type is MessageType.MAIL_RECEIPT:
                state = str(value.get("state", ""))
                if state in {"delivered", "failed"}:
                    relay_id = str(value["relay_id"])
                    async with self.database.transaction() as transaction:
                        await transaction.write(
                            "UPDATE fed_mail_delivery SET state=?,error=?,updated_at=unixepoch() "
                            "WHERE relay_id=? AND direction='out'",
                            (
                                state,
                                str(value.get("error") or "")[:120] or None,
                                relay_id,
                            ),
                        )
                        await transaction.write(
                            "UPDATE mail SET state=?,delivered_at=CASE WHEN ?='delivered' "
                            "THEN unixepoch() ELSE delivered_at END WHERE id=(SELECT mail_id "
                            "FROM fed_mail_delivery WHERE relay_id=? AND direction='out')",
                            (state, state, relay_id),
                        )
            elif msg_type is MessageType.RELAY_PUT:
                envelope = value.get("envelope")
                if not isinstance(envelope, dict):
                    raise ValueError("invalid relay envelope")
                try:
                    envelope_id, state = await self.federation_relay.accept(
                        sender,
                        envelope,
                        transport=("mqtt" if getattr(message, "via_mqtt", False) else "radio"),
                    )
                except ValueError as error:
                    rejected_id = envelope.get("envelope_id")
                    if (
                        isinstance(rejected_id, str)
                        and len(rejected_id) == 32
                        and all(character in "0123456789abcdef" for character in rejected_id)
                    ):
                        try:
                            await self._send_relay_receipt(
                                sender, rejected_id, "rejected", str(error)
                            )
                        except (FrameError, ValueError):
                            pass
                    raise
                try:
                    await self._send_relay_receipt(sender, envelope_id, state)
                except (FrameError, ValueError):
                    pass
                else:
                    if state == "delivered":
                        await self.federation_relay.mark_receipt_sent(envelope_id)
            elif msg_type is MessageType.RELAY_ACK:
                envelope_id = str(value.get("envelope_id", ""))
                state = str(value.get("state", ""))
                reason = value.get("reason")
                if reason is not None and not isinstance(reason, str):
                    raise ValueError("invalid relay acknowledgement reason")
                previous = await self.federation_relay.acknowledge(
                    sender, envelope_id, state, reason
                )
                if previous is not None:
                    try:
                        await self._send_relay_receipt(previous, envelope_id, "delivered")
                    except (FrameError, ValueError):
                        pass
                    else:
                        await self.federation_relay.mark_receipt_sent(envelope_id)
            elif msg_type is MessageType.TOPOLOGY_UPDATE:
                topology = value.get("topology")
                if not isinstance(topology, dict):
                    raise ValueError("invalid federation topology update")
                await self.federation_topology.accept(sender, topology)
        except (FrameError, KeyError, OverflowError, TypeError, ValueError) as error:
            packet_id = getattr(message, "packet_id", None)
            if packet_id is not None:
                await self.database.write(
                    "UPDATE message_log SET outcome='rejected',drop_reason=? "
                    "WHERE direction='in' AND packet_id=?",
                    (self._federation_rejection_reason(error), packet_id),
                )
            return

    async def _queue_service_response(
        self,
        peer: Peer,
        request_id: str,
        service: str,
        ok: bool,
        result: dict[str, object],
        provenance: dict[str, object],
        error: str | None,
        now: int,
    ) -> tuple[bool, dict[str, object], dict[str, object], str | None, int, float]:
        async def encode_response(
            response_ok: bool,
            response_result: dict[str, object],
            response_provenance: dict[str, object],
            response_error: str | None,
        ) -> tuple[list[bytes], int, float]:
            value = {
                "request_id": request_id,
                "mesh_id": self.federation.local_mesh_id,
                "ok": response_ok,
                "result": self._service_result_to_wire(service, response_result),
                "provenance": self._service_provenance_to_wire(service, response_provenance),
                "error": response_error,
            }
            content_bytes = len(json.dumps(value, separators=(",", ":"), sort_keys=True).encode())
            if content_bytes > peer.service_max_response_bytes:
                return [], content_bytes, 0.0
            secret = await self.federation.secret(peer.mesh_id)
            counter = await self.federation.next_counter(peer.mesh_id)
            try:
                frames = self.federation_codec.encode(
                    MessageType.SERVICE_RESPONSE, value, counter, secret
                )
            except FrameError:
                return [], peer.service_max_response_bytes + 1, 0.0
            return (
                frames,
                content_bytes,
                sum(
                    self.governor.estimate_toa(
                        len(frame), portnum=self.config.radio.federation_portnum
                    )
                    for frame in frames
                ),
            )

        frames, response_bytes, airtime_seconds = await encode_response(
            ok, result, provenance, error
        )
        denied = await self.federation.reserve_service_response(
            peer, response_bytes, airtime_seconds, now
        )
        if denied is not None:
            ok = False
            result = {}
            provenance = {"serving_outpost": self.federation.local_mesh_id}
            error = (
                "peer service response exceeds byte policy"
                if denied == "response_byte_quota"
                else "peer service airtime quota exceeded"
            )
            frames, response_bytes, airtime_seconds = await encode_response(
                ok, result, provenance, error
            )
            if (
                await self.federation.reserve_service_response(
                    peer, response_bytes, airtime_seconds, now
                )
                is not None
            ):
                return ok, result, provenance, error, 0, 0.0
        try:
            await self._queue_trusted_federation_frames(frames)
        except ValueError as caught:
            return False, {}, provenance, str(caught)[:160], 0, 0.0
        return ok, result, provenance, error, response_bytes, airtime_seconds

    async def _handle_service_query(self, sender: str, value: dict[str, object]) -> None:
        request_id = str(value["request_id"])
        service = str(value["service"])
        args = value.get("args", {})
        expires_at = wire_int(value["expires_at"], "expires_at")
        ttl = wire_int(value.get("ttl", 0), "ttl", maximum=86_400)
        now = int(self.clock.now().timestamp())
        if len(request_id) > 64 or service not in {"weather", "alerts", "knowledge"}:
            raise ValueError("invalid service request")
        if not isinstance(args, dict) or expires_at <= now or ttl < 0:
            raise ValueError("expired or invalid service request")
        normalized_args, fingerprint = self._normalized_service_args(service, args)
        args_json = json.dumps(normalized_args, separators=(",", ":"), sort_keys=True)
        peer, outcome, existing = await self.federation.admit_service_request(
            sender,
            request_id,
            service,
            args_json,
            fingerprint,
            now,
            expires_at,
        )
        if outcome == "replay":
            assert existing is not None
            if existing["status"] == "pending" or int(existing["response_count"]) >= 3:
                return
            stored_result = json.loads(existing["result_json"] or "{}")
            stored_provenance = json.loads(existing["provenance_json"] or "{}")
            replay = await self._queue_service_response(
                peer,
                request_id,
                service,
                existing["status"] == "complete",
                stored_result,
                stored_provenance,
                existing["error"],
                now,
            )
            if replay[4]:
                await self.database.write(
                    "UPDATE fed_service_request SET response_bytes=response_bytes+?,"
                    "response_airtime_seconds=response_airtime_seconds+?,"
                    "response_count=response_count+1,updated_at=? WHERE request_id=?",
                    (replay[4], replay[5], now, request_id),
                )
            return
        if outcome != "admitted":
            errors = {
                "permission_denied": "peer service is not permitted by operator policy",
                "request_quota": "peer service request quota exceeded",
                "concurrency_quota": "peer service concurrency quota exceeded",
                "circuit_open": "peer service provider circuit is temporarily open",
            }
            denied = await self._queue_service_response(
                peer,
                request_id,
                service,
                False,
                {},
                {"serving_outpost": self.federation.local_mesh_id},
                errors[outcome],
                now,
            )
            await self.database.write(
                "UPDATE fed_service_request SET result_json='{}',provenance_json=?,error=?,"
                "response_bytes=?,response_airtime_seconds=?,response_count=?,updated_at=? "
                "WHERE request_id=?",
                (
                    json.dumps(denied[2], separators=(",", ":")),
                    denied[3],
                    denied[4],
                    denied[5],
                    int(denied[4] > 0),
                    now,
                    request_id,
                ),
            )
            return
        ok = True
        result: dict[str, object] = {}
        provenance: dict[str, object] = {}
        error: str | None = None
        provider_failed = False
        try:
            result, provenance = await self._execute_peer_service(service, normalized_args)
            if service == "alerts" and result.get("status") == "provider_failure":
                ok = False
                provider_failed = True
                error = str(result.get("error") or "public alert provider failed")[:160]
        except (OSError, RuntimeError, ValueError) as caught:
            ok, provider_failed, error = False, True, str(caught)[:160]
        await self.federation.record_service_provider_outcome(peer, service, provider_failed, now)
        (
            ok,
            result,
            provenance,
            error,
            response_bytes,
            airtime_seconds,
        ) = await self._queue_service_response(
            peer, request_id, service, ok, result, provenance, error, now
        )
        await self.database.write(
            "UPDATE fed_service_request SET status=?,result_json=?,provenance_json=?,error=?,"
            "completed_at=?,updated_at=?,response_bytes=?,response_airtime_seconds=?,"
            "response_count=? WHERE request_id=?",
            (
                "complete" if ok else "failed",
                json.dumps(result, separators=(",", ":")),
                json.dumps(provenance, separators=(",", ":")),
                error,
                now,
                now,
                response_bytes,
                airtime_seconds,
                int(response_bytes > 0),
                request_id,
            ),
        )

    async def _handle_service_response(self, sender: str, value: dict[str, object]) -> None:
        request_id = str(value["request_id"])
        rows = await self.database.read(
            "SELECT * FROM fed_service_request WHERE request_id=? AND direction='out'",
            (request_id,),
        )
        if not rows or rows[0]["status"] != "pending" or rows[0]["peer_mesh_id"] != sender:
            return
        now = int(self.clock.now().timestamp())
        ok = value.get("ok") is True
        if not ok:
            candidates = json.loads(rows[0]["candidate_peers"])
            attempt = int(rows[0]["attempt"]) + 1
            if attempt < len(candidates):
                next_peer = candidates[attempt]
                await self.database.write(
                    "UPDATE fed_service_request SET peer_mesh_id=?,attempt=?,updated_at=?,error=? "
                    "WHERE request_id=?",
                    (
                        next_peer,
                        attempt,
                        now,
                        str(value.get("error") or "peer service failed")[:160],
                        request_id,
                    ),
                )
                await self._send_service_query(
                    request_id,
                    next_peer,
                    rows[0]["service"],
                    json.loads(rows[0]["args_json"]),
                    rows[0]["expires_at"],
                )
                return
        await self.database.write(
            "UPDATE fed_service_request SET status=?,result_json=?,provenance_json=?,error=?,"
            "completed_at=?,updated_at=? WHERE request_id=?",
            (
                "complete" if ok else "failed",
                json.dumps(
                    self._service_result_from_wire(rows[0]["service"], value.get("result", {})),
                    separators=(",", ":"),
                ),
                json.dumps(
                    self._service_provenance_from_wire(
                        rows[0]["service"], value.get("provenance", {}), now, sender
                    ),
                    separators=(",", ":"),
                ),
                str(value.get("error") or "")[:160] or None,
                now,
                now,
                request_id,
            ),
        )

    async def _environment_loop(self) -> None:
        while True:
            location = self.config.node.location
            if self.config.modules.env.enabled and location is not None:
                try:
                    await self.cap_alerts.poll(location.lat, location.lon)
                    await self.same_events.reconcile_cap_duplicates()
                except (OSError, ValueError) as error:
                    print(f"NWS alert poll failed: {error}", flush=True)
                try:
                    await self.seismic.poll(location.lat, location.lon)
                except OSError as error:
                    print(f"USGS earthquake poll failed: {error}", flush=True)
            self._task_progress("environment-poller")
            await self.clock.sleep(300)

    async def _watch_loop(self) -> None:
        while True:
            if self.config.modules.watch.enabled:
                await self.alerts.advance_due()
                await self.incidents.expire_due()
                await self.checkins.run_due_schedules()
            self._task_progress("watch-scheduler")
            await self.clock.sleep(15)

    async def reconnect_radio(self) -> None:
        self.router.sessions.clear_sensitive()
        await self.radio.close()

    def _radio_configuration_context(self, result: dict[str, Any]) -> dict[str, Any]:
        location = self.config.node.location
        result["outpost_location"] = (
            {"latitude": location.lat, "longitude": location.lon} if location is not None else None
        )
        result["outpost_policy_channels"] = sorted(self.config.channels)
        result["outpost_identity"] = {
            "base_name": self.config.node.name,
            "display_name": self.outpost_name,
        }
        result["outpost_channel_policies"] = [
            {
                "index": index,
                "name": policy.name,
                "bbs": policy.bbs,
                "ai": policy.ai,
                "alerts": policy.alerts,
                "accept_reports": policy.accept_reports,
            }
            for index, policy in sorted(self.config.channels.items())
        ]
        if result.get("available"):
            live = {
                int(channel["index"]): str(channel.get("role", "DISABLED"))
                for channel in result.get("channels", [])
            }
            configured = set(self.config.channels)
            active = {index for index, role in live.items() if role != "DISABLED"}
            warnings = list(result.get("warnings", []))
            missing = sorted(configured - active)
            unmanaged = sorted(active - configured)
            if missing:
                warnings.append(
                    "Outpost policy references inactive radio slot(s): "
                    + ", ".join(str(index) for index in missing)
                    + "."
                )
            if unmanaged:
                warnings.append(
                    "Active radio slot(s) have no Outpost policy and reject commands: "
                    + ", ".join(str(index) for index in unmanaged)
                    + "."
                )
            result["warnings"] = warnings
            with contextlib.suppress(KeyError, StopIteration, ValueError):
                primary = next(
                    channel for channel in result.get("channels", []) if channel["index"] == 0
                )
                result["lora"]["frequency"] = frequency_plan(
                    result["lora"]["region"],
                    result["lora"]["modem_preset"],
                    result["lora"]["frequency_slot"],
                    primary.get("name", ""),
                )
        result["outpost_profile"] = self.radio_configuration.profile_context(result)
        result["operation"] = self.radio_configuration.operation()
        return result

    async def radio_configuration_status(self) -> dict[str, Any]:
        return self._radio_configuration_context(await self.radio.configuration_status())

    async def preflight_radio_configuration(
        self, section: str, values: dict[str, Any]
    ) -> dict[str, Any]:
        return await self.radio_configuration.preflight(section, values)

    async def configure_radio(
        self, operation_id: str, section: str, values: dict[str, Any]
    ) -> dict[str, Any]:
        result = await self.radio_configuration.apply(operation_id, section, values)
        return self._radio_configuration_context(result)

    async def _digest_loop(self) -> None:
        while True:
            if self.config.modules.bbs.enabled:
                for delivery in await self.digests.due():
                    parts = chunk_text(
                        delivery.text,
                        max_parts=self.config.airtime.max_parts["digest"],
                    )
                    scheduled = await self.governor.admit_many(
                        [
                            OutboundItem(
                                text=part,
                                dest=delivery.mesh_id,
                                channel=channel_slot(self.config, "public", 0),
                                traffic_class=TrafficClass.DIGEST,
                                want_ack=True,
                                multipart=len(parts) > 1,
                            )
                            for part in parts
                        ]
                    )
                    if scheduled is not None:
                        await self.digests.mark_scheduled(delivery)
            self._task_progress("bbs-digests")
            await self.clock.sleep(30)

    async def _maintenance_loop(self) -> None:
        while True:
            if await self.maintenance.due():
                await self.maintenance.run()
                await self.self_check.run("maintenance")
            self._task_progress("store-maintenance")
            await self.clock.sleep(60)

    async def _ai_keep_warm_loop(self) -> None:
        retry_seconds = 1.0
        while True:
            ready = await self.ai_service.warm()
            self._task_progress("ai-keep-warm")
            if ready:
                retry_seconds = 1.0
                delay = float(self.config.ai.keep_warm.interval_s)
            else:
                delay = retry_seconds
                retry_seconds = min(retry_seconds * 2, 30.0)
            await self.clock.sleep(delay)

    async def shutdown(self) -> None:
        self._shutting_down = True
        await self.supervisor.stop()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        try:
            await asyncio.wait_for(self.ai_service.close(), timeout=15)
        except Exception as error:
            print(f"AI provider shutdown failed: {type(error).__name__}", flush=True)
        finally:
            await self.database.close()

    async def _governor_loop(self) -> None:
        while True:
            try:
                item = await self.governor.tick()
            except (KeyError, TypeError, ValueError) as error:
                # Strict startup validation prevents known configuration faults. Keep this
                # boundary so an unexpected runtime mutation is visible without terminating
                # the sole egress loop; alert traffic gets first priority on the next tick.
                self._record_task_degradation("airtime-governor", error)
                await self.clock.sleep(0.25)
                continue
            if item is not None and not self.governor.durable:
                result = item.send_result
                await self.message_log.record_outbound(
                    peer_mesh_id=item.dest,
                    channel=item.channel,
                    portnum=item.portnum or 1,
                    packet_id=result.packet_id if result else None,
                    text=item.text if item.binary_payload is None else None,
                    byte_len=item.payload_size,
                    toa_ms=round(item.estimated_toa * 1_000),
                    airtime_class=item.traffic_class.value,
                    outcome=result.outcome if result else "timeout",
                    is_direct=item.dest != "^all",
                )
            self._task_progress("airtime-governor")
            await self.clock.sleep(0.25)

    async def _inbound_loop(self) -> None:
        async for inbound in self.radio.inbound():
            if self.radio.local_node_id:
                self.inbound_pipeline.local_node_id = self.radio.local_node_id
            message = self.inbound_pipeline.process(inbound)
            if message is None:
                continue
            log_id = await self.message_log.record_inbound(message)
            self._task_progress("inbound-router")
            INBOUND.labels(
                str(message.portnum), str(message.channel), str(message.is_direct).lower()
            ).inc()
            await self._route_inbound(message, log_id)

    def _channel_accepts_reports(self, message: InboundMessage) -> bool:
        if message.is_direct:
            return True
        policy = self.config.channels.get(message.channel)
        return bool(policy and policy.accept_reports)

    def _is_safety_inbound(self, message: InboundMessage) -> bool:
        if message.portnum == 5 and message.request_id is not None:
            return True
        if message.latitude is not None and message.longitude is not None:
            return True
        if self.router.command_token(message) in {"REPORT", "REPORT!", "OK", "HELPME", "ACK"}:
            return True
        return bool(
            self.config.modules.watch.enabled
            and self.config.watch.emergency_keywords_enabled
            and self._channel_accepts_reports(message)
            and message.text
            and self.incidents.emergency_keyword(message.text, self.config.watch.emergency_keywords)
        )

    async def _route_inbound(self, message: InboundMessage, log_id: int) -> None:
        if self._is_safety_inbound(message):
            self._inbound_fast_processed += 1
            await self._handle_inbound_safely(message, log_id)
            return
        if self._inbound_queued >= self.config.router.inbound_queue_max:
            self._inbound_backlog_dropped += 1
            INBOUND_DROPPED.labels("worker_backlog_full").inc()
            await self.message_log.mark_inbound_dropped(log_id, "worker backlog full")
            return
        pending = self._inbound_pending[message.from_id]
        should_schedule = not pending and message.from_id not in self._inbound_active
        pending.append((message, log_id))
        self._inbound_queued += 1
        INBOUND_QUEUE_DEPTH.labels("worker_backlog").set(self._inbound_queued)
        if should_schedule:
            self._inbound_ready.put_nowait(message.from_id)

    async def _inbound_worker(self, worker: int) -> None:
        task_name = f"inbound-worker-{worker}"
        while True:
            sender = await self._inbound_ready.get()
            pending = self._inbound_pending.get(sender)
            if not pending:
                continue
            self._inbound_active.add(sender)
            message, log_id = pending.popleft()
            self._inbound_queued -= 1
            self._inbound_busy += 1
            INBOUND_QUEUE_DEPTH.labels("worker_backlog").set(self._inbound_queued)
            INBOUND_WORKERS_BUSY.set(self._inbound_busy)
            try:
                await self._handle_inbound_safely(message, log_id)
                self._task_progress(task_name)
            finally:
                self._inbound_busy -= 1
                INBOUND_WORKERS_BUSY.set(self._inbound_busy)
                self._inbound_active.discard(sender)
                if pending:
                    self._inbound_ready.put_nowait(sender)
                else:
                    self._inbound_pending.pop(sender, None)

    async def _handle_inbound_safely(
        self,
        message: InboundMessage,
        log_id: int,
        trace: DispatchTrace | None = None,
    ) -> bool:
        """Contain message-specific faults while leaving infrastructure failures fatal."""
        token = _INBOUND_LOG_ID.set(log_id)
        try:
            if trace is None:
                await self._handle_inbound_message(message, ordered=False)
            else:
                await self._handle_inbound_message(message, ordered=False, trace=trace)
        except asyncio.CancelledError:
            raise
        except sqlite3.Error:
            raise
        except Exception as error:
            reason = f"handler failure: {type(error).__name__}"
            if trace is not None:
                trace.decision = reason
            INBOUND_HANDLER_FAILURES.labels(type(error).__name__).inc()
            # A failure to record the drop is an infrastructure fault and must still
            # reach CORE supervision instead of being mistaken for a poison message.
            await self.message_log.mark_inbound_dropped(log_id, reason)
            return False
        finally:
            _INBOUND_LOG_ID.reset(token)
        return True

    async def _handle_inbound_message(
        self,
        message: InboundMessage,
        *,
        ordered: bool = True,
        trace: DispatchTrace | None = None,
    ) -> None:
        if (
            self.config.modules.fed.enabled
            and message.portnum == self.config.radio.federation_portnum
        ):
            if trace is not None:
                trace.resolved_command = "FEDERATION"
                trace.decision = "federation_frame"
            await self._handle_federation_discovery(message)
            return
        if message.portnum == 5 and message.request_id is not None:
            outcome = "acked" if message.routing_error in {None, "NONE"} else "naked"
            if trace is not None:
                trace.resolved_command = "ROUTING_ACK"
                trace.decision = outcome
            if await self.message_log.resolve_ack(message.request_id, outcome):
                ACK_OUTCOME.labels(outcome).inc()
            return
        if (
            self.config.modules.watch.enabled
            and message.latitude is not None
            and message.longitude is not None
        ):
            if trace is not None:
                trace.resolved_command = "POSITION"
                trace.decision = "position_recorded"
            member = await self.router.members.resolve(
                message.from_id,
                last_heard_snr=message.rx_snr,
                hops_away=message.hops_away,
            )
            if trace is not None:
                trace.member_trust = member.trust
            await self.incidents.record_position(
                member, message.latitude, message.longitude, prompt=message.is_direct
            )
            if not message.is_direct:
                return
            response = Response(
                ResponseKind.DETAIL,
                [
                    Line(
                        "Location saved. What happened? Reply with a type and details, "
                        "for example: tree blocking road. Not 911."
                    )
                ],
            )
        elif (
            self.config.modules.watch.enabled
            and self.config.watch.emergency_keywords_enabled
            and self._channel_accepts_reports(message)
            and message.text
            and self.incidents.emergency_keyword(message.text, self.config.watch.emergency_keywords)
        ):
            if trace is not None:
                trace.resolved_command = "EMERGENCY_KEYWORD"
                trace.decision = "incident_triggered"
            member = await self.router.members.resolve(
                message.from_id,
                last_heard_snr=message.rx_snr,
                hops_away=message.hops_away,
            )
            if trace is not None:
                trace.member_trust = member.trust
            incident, incident_created = await self.incidents.emergency_trigger(
                member, message.text, self.config.watch.emergency_cooldown_minutes
            )
            if incident_created:
                await self._notify_emergency_responders(incident, member.mesh_id)
            response = Response(
                ResponseKind.ACK,
                [Line(f"⚠ INC {incident.local_ref} filed. Contact 911. Not emergency service.")],
            )
        elif self.config.modules.watch.enabled and message.is_direct and message.text:
            member = await self.router.members.resolve(
                message.from_id,
                last_heard_snr=message.rx_snr,
                hops_away=message.hops_away,
            )
            if trace is not None:
                trace.member_trust = member.trust
            if self.incidents.is_position_share_notice(message.text):
                if trace is not None:
                    trace.decision = "position_notice_ignored"
                response = Response(ResponseKind.NONE)
            else:
                pending_report = await self.incidents.create_from_pending(message.text, member)
                if pending_report is None:
                    response = await self.router.dispatch(message, ordered=ordered, trace=trace)
                else:
                    if trace is not None:
                        trace.resolved_command = "REPORT"
                        trace.decision = "pending_report"
                    created_incident, similar = pending_report
                    if similar is not None:
                        response = Response(
                            ResponseKind.DETAIL,
                            [
                                Line(
                                    f"Similar: INC {similar.local_ref} {similar.type}. "
                                    f"CONFIRM {similar.local_ref}, or REPORT! to file new."
                                )
                            ],
                        )
                    else:
                        assert created_incident is not None
                        response = Response(
                            ResponseKind.ACK,
                            [
                                Line(
                                    f"✓ INC {created_incident.local_ref} "
                                    f"{created_incident.type} · shared GPS "
                                    f"{created_incident.lat:.3f},{created_incident.lon:.3f}"
                                )
                            ],
                        )
        else:
            response = await self.router.dispatch(message, ordered=ordered, trace=trace)
            if response.kind == ResponseKind.NONE:
                return
        if response.kind == ResponseKind.NONE:
            if trace is not None:
                trace.response_kind = response.kind.value
            return
        text = render_response(response)
        configured_max_parts = self.config.airtime.max_parts[response.airtime_class.value]
        max_parts = (
            min(response.max_parts, configured_max_parts)
            if response.max_parts is not None
            else configured_max_parts
        )
        parts = chunk_text(text, max_parts=max_parts)
        if trace is not None:
            trace.response_kind = response.kind.value
            trace.response_text = text
            trace.airtime_class = response.airtime_class.value
            trace.outbound_parts = len(parts)
        outbound = [
            OutboundItem(
                text=part,
                dest=message.from_id,
                channel=message.channel,
                traffic_class=response.airtime_class,
                want_ack=True,
                multipart=len(parts) > 1,
            )
            for part in parts
        ]
        admission = await self.governor.admit_many_result(outbound)
        outcome = admission.rejection_reason or "admitted"
        if trace is not None:
            trace.admission = outcome
        COMMAND_REPLY_DELIVERY.labels(outcome).inc()
        if admission.rejection_reason is not None:
            command = ((message.text or "").split(maxsplit=1) or [""])[0].upper() or None
            for part in parts:
                await self.message_log.record_outbound(
                    peer_mesh_id=message.from_id,
                    channel=message.channel,
                    portnum=1,
                    packet_id=None,
                    text=part,
                    byte_len=len(part.encode()),
                    toa_ms=round(self.governor.estimate_toa(len(part.encode())) * 1_000),
                    airtime_class=response.airtime_class.value,
                    outcome="dropped",
                    is_direct=True,
                    command=command,
                    drop_reason=admission.rejection_reason,
                    in_reply_to_id=_INBOUND_LOG_ID.get(),
                )

    async def _notify_emergency_responders(
        self, incident: Incident, sender_mesh_id: str
    ) -> AudienceDelivery:
        delivery = await self.alerts.notifier.deliver(
            purpose="emergency_keyword",
            target=f"incident:{incident.id}",
            audience="responders",
            text=f"⚠ Emergency keyword · INC {incident.local_ref} · {incident.title[:80]}",
            channels=[channel_slot(self.config, "public", 0)],
            traffic_class=TrafficClass.ALERT,
            severity=Severity.URGENT,
            exclude_mesh_ids=(sender_mesh_id,),
            dedupe_token=f"incident:{incident.id}:emergency-notification",
        )
        await self.database.write(
            "UPDATE incident SET notification_state=?,notification_count=? WHERE id=?",
            (delivery.state, delivery.admitted, incident.id),
        )
        return delivery

    def status(self) -> dict[str, object]:
        configured = set(self.config.channels)
        available = set(self.radio.snapshot.channels)
        return {
            "node": self.outpost_name,
            "runtime": {
                "mode": self.runtime_mode,
                "simulated": self.runtime_mode != "live",
                "source": self.runtime_source,
                "store": "scratch" if self.runtime_mode != "live" else "live",
                "transmit": "simulated" if self.runtime_mode != "live" else "radio",
            },
            "modules": {
                name: {"enabled": enabled, "restart_required_to_change": True}
                for name, enabled in self.config.modules.enabled_map().items()
            },
            "radio": self.radio.state.value,
            "radio_power": self.radio_power.snapshot(),
            "airtime_used_ratio": self.governor.used_airtime / 3_600,
            "queues": self.governor.queue_depths(),
            "alert_delivery": self.governor.alert_delivery_status(),
            "same_receiver": self.same_receiver.health(),
            "ai": self.ai_service.snapshot(),
            "tasks_healthy": self.core_tasks_healthy(),
            "subsystems_healthy": self.background_tasks_healthy(),
            "task_failure": self._fatal_task_error,
            "readiness": self.self_check.snapshot(),
            "intents": self.router.intents.status(),
            "recovery": self.restore_coordinator.maintenance_status(),
            "tasks": {name: dict(health) for name, health in self._task_health.items()},
            "inbound": {
                "workers": self.config.router.inbound_workers,
                "busy": self._inbound_busy,
                "backlog": self._inbound_queued,
                "capacity": self.config.router.inbound_queue_max,
                "fast_processed": self._inbound_fast_processed,
                "backlog_dropped": self._inbound_backlog_dropped,
                "pipeline_dropped": dict(self.inbound_pipeline.dropped),
                "radio": self.radio.inbound_status(),
            },
            "radio_config": {
                "node_id": self.radio.snapshot.node_id,
                "region": self.radio.snapshot.region,
                "preset": self.radio.snapshot.preset,
                "channels": sorted(available),
                "missing_policy_channels": sorted(configured - available) if available else [],
                "gps": {
                    "lat": self.radio.snapshot.latitude,
                    "lon": self.radio.snapshot.longitude,
                },
            },
        }
