from __future__ import annotations

import asyncio
import json
import secrets
from dataclasses import dataclass

from outpost.bbs.admin import BBSAdmin
from outpost.bbs.channels import ChannelDirectory
from outpost.bbs.digests import DigestService
from outpost.bbs.mail import MailService
from outpost.bbs.service import BBSService
from outpost.clock import SystemClock
from outpost.commands.alerts import specs as alert_specs
from outpost.commands.bbs import specs as bbs_specs
from outpost.commands.checkin import specs as checkin_specs
from outpost.commands.directory import specs as directory_specs
from outpost.commands.environment import specs as environment_specs
from outpost.commands.identity import specs as identity_specs
from outpost.commands.mail import specs as mail_specs
from outpost.commands.operator import specs as operator_specs
from outpost.commands.watch import specs as watch_specs
from outpost.config import Config
from outpost.env import (
    AstronomyService,
    CapAlertService,
    FallbackWeatherProvider,
    NWSProvider,
    OpenMeteoProvider,
    SeismicService,
    WaypointService,
    WeatherService,
)
from outpost.fed import (
    FederationMailService,
    FederationPeerService,
    FederationSyncService,
    FrameCodec,
    FrameError,
    MessageType,
    Reassembler,
)
from outpost.radio_operations import RadioOperations
from outpost.render.renderer import render_response
from outpost.router.models import Line, Response, ResponseKind
from outpost.router.router import Router
from outpost.router.session import SessionStore
from outpost.security.rate_limit import RateLimiter
from outpost.store import Database
from outpost.store.backups import BackupService
from outpost.store.maintenance import MaintenanceService
from outpost.store.members import MemberRepo
from outpost.store.message_log import MessageLogRepo
from outpost.transport.chunker import chunk_text
from outpost.transport.governor import AirtimeGovernor, OutboundItem
from outpost.transport.inbound import InboundPipeline
from outpost.transport.metrics import ACK_OUTCOME, INBOUND
from outpost.transport.models import Severity, TrafficClass
from outpost.transport.radio_link import MeshtasticRadioLink
from outpost.transport.supervisor import RadioSupervisor
from outpost.watch import AlertService, CheckinService, IncidentService
from outpost.watch.incidents import Incident
from outpost.web.api import create_web_app
from outpost.web.auth import WebAuthService
from outpost.web.settings import RuntimeSettings


@dataclass
class OutpostApp:
    config: Config

    def __post_init__(self) -> None:
        self.clock = SystemClock()
        self.database = Database(self.config.store.path)
        self.radio = MeshtasticRadioLink(self.config.radio, self.clock)
        self.supervisor = RadioSupervisor(
            self.radio,
            self.config.radio.reconnect,
            self.clock,
            self.config.radio.liveness_timeout_s,
        )
        self.inbound_pipeline = InboundPipeline("", set(self.config.radio.bridge_node_ids))
        self.governor = AirtimeGovernor(self.radio, self.config.airtime, self.clock)
        self.radio_operations = RadioOperations(self.database, self.governor, self.clock)
        members = MemberRepo(self.database, self.clock)
        self.message_log = MessageLogRepo(self.database, self.clock)
        sessions = SessionStore(self.clock, self.config.router.session_idle_minutes)
        limiter = RateLimiter(
            self.clock,
            self.config.security.global_rate_ceiling,
            self.database,
        )
        self.router = Router(self.config, members, sessions, limiter)
        bbs = BBSService(self.database, self.clock, "local")
        directory = ChannelDirectory(self.database)
        mail = MailService(
            self.database,
            members,
            self.clock,
            "local",
            self.config.mail.hold_unknown_days,
            self._send_federated_operator_reply,
        )
        self.digests = DigestService(self.database, self.clock, self.config)
        self.incidents = IncidentService(self.database, self.clock, "local")
        self.alerts = AlertService(self.database, self.governor, self.clock, self.config)
        self.checkins = CheckinService(self.database, self.governor, self.clock)
        self.weather = WeatherService(
            self.database,
            self.clock,
            self.config.env,
            FallbackWeatherProvider(
                [NWSProvider(self.config.env), OpenMeteoProvider(self.config.env)]
            ),
        )
        self.cap_alerts = CapAlertService(self.database, self.clock, self.config.env)
        self.astronomy = AstronomyService(self.clock)
        self.seismic = SeismicService(self.database, self.clock, self.config.env)
        self.waypoints = WaypointService(self.database, self.clock)
        self.federation = FederationPeerService(self.database, self.clock, "")
        self.federation_sync = FederationSyncService(self.database)
        self.federation_mail = FederationMailService(self.database, self.federation, self.clock)
        self.federation_codec = FrameCodec(self.config.fed.max_fragments)
        self.federation_reassembler = Reassembler(self.config.fed.reassembly_timeout_s)
        for spec in (
            *identity_specs(members, mail, self.config.security.require_approval),
            *bbs_specs(bbs, self.config.bbs.self_delete_minutes),
            *mail_specs(mail),
            *directory_specs(directory),
            *operator_specs(bbs),
            *(watch_specs(self.incidents) if self.config.modules.watch.enabled else ()),
            *(alert_specs(self.alerts) if self.config.modules.watch.enabled else ()),
            *(checkin_specs(self.checkins) if self.config.modules.watch.enabled else ()),
            *(
                environment_specs(
                    self.weather,
                    self.config,
                    self.cap_alerts,
                    self.astronomy,
                    self.seismic,
                    self.waypoints,
                )
                if self.config.modules.env.enabled
                else ()
            ),
        ):
            self.router.registry.register(spec)
        reserved_slugs = {
            value.lower()
            for spec in self.router.registry.commands()
            for value in (spec.name, *spec.aliases)
        }
        self.bbs_admin = BBSAdmin(
            self.database, self.clock, reserved_slugs, federation_notify=self._notify_board_change
        )
        self.web_auth = WebAuthService(self.database, self.config.web.auth.session_hours)
        self.runtime_settings = RuntimeSettings(self.database, self.config)
        self.backups = BackupService(self.database)
        self.maintenance = MaintenanceService(self.database, self.backups, self.clock, self.config)
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
        )
        self._tasks: list[asyncio.Task[None]] = []

    async def import_federation_inbox(self, item_id: int) -> str:
        return await self.federation_sync.import_inbox(
            item_id, "web:operator", int(self.clock.now().timestamp())
        )

    async def send_federation_mail(
        self, peer_id: str, recipient: str, subject: str, body: str
    ) -> dict[str, object]:
        envelope = await self.federation_mail.seal(
            peer_id, recipient, f"operator@{self.config.node.short_name}", subject, body
        )
        await self._send_federation_value(
            peer_id,
            MessageType.MAIL_RELAY,
            {"mesh_id": self.federation.local_mesh_id, **envelope},
        )
        await self.database.write(
            "UPDATE fed_mail_delivery SET state='sent',attempts=1,updated_at=unixepoch() "
            "WHERE relay_id=?",
            (envelope["relay_id"],),
        )
        return {"relay_id": str(envelope["relay_id"]), "state": "sent"}

    async def _send_federated_operator_reply(self, peer_id: str, body: str) -> None:
        await self.send_federation_mail(peer_id, "operator", "Mesh reply", body)

    async def _notify_board_change(self, slug: str, post_id: int) -> None:
        if not self.config.modules.fed.enabled:
            return
        posts = await self.database.read("SELECT uid FROM post WHERE id=?", (post_id,))
        if not posts:
            return
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
            if not self.radio.local_node_id:
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
        await self.runtime_settings.load()
        self.federation_sync.local_mesh_id = self.radio.local_node_id
        if self.radio.local_node_id:
            await self.federation_sync.import_approved_replies(
                "federation:auto-thread", int(self.clock.now().timestamp())
            )
        initial_password = await self.web_auth.ensure_credential()
        if initial_password:
            print(f"OUTPOST INITIAL OPERATOR PASSWORD: {initial_password}", flush=True)
        self._tasks = [
            asyncio.create_task(self.supervisor.run(), name="radio-supervisor"),
            asyncio.create_task(self._governor_loop(), name="airtime-governor"),
            asyncio.create_task(self._inbound_loop(), name="inbound-router"),
            asyncio.create_task(self._digest_loop(), name="bbs-digests"),
            asyncio.create_task(self._maintenance_loop(), name="store-maintenance"),
            asyncio.create_task(self._watch_loop(), name="watch-scheduler"),
            asyncio.create_task(self._environment_loop(), name="environment-poller"),
            asyncio.create_task(self._federation_hello_loop(), name="federation-discovery"),
            asyncio.create_task(self._federation_service_loop(), name="federation-services"),
            asyncio.create_task(self._federation_sync_loop(), name="federation-sync"),
            asyncio.create_task(self._federation_delivery_loop(), name="federation-delivery"),
        ]

    async def _federation_hello_loop(self) -> None:
        while True:
            if self.config.modules.fed.enabled and self._queue_federation_hello("^all"):
                await self.clock.sleep(self.config.fed.hello_interval_hours * 3_600)
            else:
                await self.clock.sleep(30)

    def _queue_federation_hello(
        self, destination: str, *, target_mesh_id: str | None = None
    ) -> bool:
        local_id = self.radio.local_node_id
        if not local_id:
            return False
        self.federation.local_mesh_id = local_id
        capabilities = {
            "internet": True,
            "weather": self.config.modules.env.enabled,
            "alerts": self.config.modules.watch.enabled,
            "bbs": self.config.modules.bbs.enabled,
            "ai": self.config.modules.ai.enabled,
        }
        counter = int(self.clock.now().timestamp()) & 0xFFFFFFFF
        hello = {
            "mesh_id": local_id,
            "name": self.config.node.name,
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
        admitted = self.governor.enqueue_many(
            [
                OutboundItem(
                    text="",
                    binary_payload=frame,
                    portnum=self.config.radio.federation_portnum,
                    dest=destination,
                    channel=0,
                    traffic_class=TrafficClass.FEDERATION,
                    want_ack=destination != "^all",
                    multipart=len(frames) > 1,
                )
                for frame in frames
            ]
        )
        return admitted is not None

    def _queue_federation_frames(
        self, frames: list[bytes], destination: str, *, want_ack: bool
    ) -> list[int]:
        admitted = self.governor.enqueue_many(
            [
                OutboundItem(
                    text="",
                    binary_payload=frame,
                    portnum=self.config.radio.federation_portnum,
                    dest=destination,
                    channel=0,
                    traffic_class=TrafficClass.FEDERATION,
                    want_ack=want_ack,
                    multipart=len(frames) > 1,
                )
                for frame in frames
            ]
        )
        if admitted is None:
            raise ValueError("federation queue rejected the complete message")
        return admitted

    async def initiate_federation_pairing(self, mesh_id: str) -> object:
        local_id = self.radio.local_node_id
        if not local_id:
            raise ValueError("radio identity is not available")
        self.federation.local_mesh_id = local_id
        peer, payload = await self.federation.create_pairing_request(mesh_id)
        frames = self.federation_codec.encode(MessageType.PAIR_REQ, payload, 0, None)
        mqtt_only = "mqtt" in peer.discovery_transports and "radio" not in peer.discovery_transports
        self._queue_federation_frames(
            frames, "^all" if mqtt_only else mesh_id, want_ack=not mqtt_only
        )
        return peer

    async def approve_federation_pairing(self, mesh_id: str, code: str) -> object:
        secret = await self.federation.pairing_secret(mesh_id)
        peer = await self.federation.approve_local(mesh_id, "web:operator", code)
        frames = self.federation_codec.encode(
            MessageType.PAIR_CONFIRM,
            {"mesh_id": self.federation.local_mesh_id, "approved": True},
            0,
            secret,
        )
        self._queue_federation_frames(frames, mesh_id, want_ack=True)
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
        now = int(self.clock.now().timestamp())
        request_id = secrets.token_hex(12)
        candidates = [peer.mesh_id for peer in peers]
        await self.database.write(
            "INSERT INTO fed_service_request(request_id,direction,peer_mesh_id,service,args_json,"
            "status,candidate_peers,created_at,updated_at,expires_at) "
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
        await self._send_service_query(request_id, candidates[0], service, args, now + 180)
        return (await self.federation_service_requests())[0]

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
        self._queue_federation_frames(frames, peer_id, want_ack=True)

    async def _execute_peer_service(
        self, service: str, args: dict[str, object]
    ) -> tuple[dict[str, object], dict[str, object]]:
        now = int(self.clock.now().timestamp())
        if service == "weather":
            location = self.config.node.location
            lat = float(args.get("lat", location.lat if location else 0))
            lon = float(args.get("lon", location.lon if location else 0))
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
                "fetched_at": snapshot.fetched_at,
                "serving_outpost": self.federation.local_mesh_id,
            }
        if service == "alerts":
            alerts = (await self.cap_alerts.list())[:5]
            result = {
                "items": [
                    {
                        "event": item["event"],
                        "headline": item["headline"],
                        "severity": item["severity"],
                        "area_desc": item["area_desc"],
                        "expires_at": item["expires_at"],
                    }
                    for item in alerts
                ]
            }
            return result, {
                "provider": "NWS CAP",
                "fetched_at": self.cap_alerts.last_poll_at,
                "cache_age_seconds": (
                    now - self.cap_alerts.last_poll_at if self.cap_alerts.last_poll_at else None
                ),
                "serving_outpost": self.federation.local_mesh_id,
            }
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

    async def _federation_service_loop(self) -> None:
        while True:
            now = int(self.clock.now().timestamp())
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
            await self.clock.sleep(15)

    async def _send_federation_value(
        self, peer_id: str, msg_type: MessageType, value: dict[str, object]
    ) -> None:
        local_id = self.radio.local_node_id
        if not local_id:
            raise ValueError("radio identity is not available")
        self.federation.local_mesh_id = local_id
        value = {**value, "mesh_id": local_id}
        secret = await self.federation.secret(peer_id)
        counter = await self.federation.next_counter(peer_id)
        frames = self.federation_codec.encode(msg_type, value, counter, secret)
        self._queue_federation_frames(frames, peer_id, want_ack=True)

    async def _federation_sync_loop(self) -> None:
        while True:
            if self.config.modules.fed.enabled and not self.radio.local_node_id:
                await self.clock.sleep(30)
                continue
            if self.config.modules.fed.enabled:
                for peer in await self.federation.list("active"):
                    if not (peer.boards or peer.sync_incidents or peer.relay_alerts):
                        continue
                    try:
                        await self._send_federation_value(
                            peer.mesh_id,
                            MessageType.SYNC_REQ,
                            {"mesh_id": self.federation.local_mesh_id, "limit": 8},
                        )
                    except (FrameError, ValueError):
                        continue
            await self.clock.sleep(self.config.fed.sync_interval_minutes * 60)

    async def _federation_delivery_loop(self) -> None:
        while True:
            if self.config.modules.fed.enabled and self.radio.local_node_id:
                now = int(self.clock.now().timestamp())
                rows = await self.database.read(
                    "SELECT d.peer_id,d.post_id,d.uid,d.stream,p.mesh_id,post.uid local_uid "
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
                        items = await self.federation_sync.export_items(
                            peer, [{"stream": str(row["stream"]), "uid": uid}]
                        )
                        if not items:
                            raise ValueError("durable federation post could not be exported")
                        await self._send_federation_value(
                            str(row["mesh_id"]), MessageType.ITEM, {"item": items[0]}
                        )
                        await self.database.write(
                            "UPDATE fed_post_delivery SET state='sent',attempts=attempts+1,"
                            "updated_at=?,error=NULL WHERE peer_id=? AND post_id=?",
                            (now, row["peer_id"], row["post_id"]),
                        )
                    except (FrameError, ValueError) as error:
                        await self.database.write(
                            "UPDATE fed_post_delivery SET attempts=attempts+1,updated_at=?,error=? "
                            "WHERE peer_id=? AND post_id=?",
                            (now, str(error)[:120], row["peer_id"], row["post_id"]),
                        )
            await self.clock.sleep(30)

    async def _handle_federation_discovery(self, message: object) -> None:
        payload = getattr(message, "payload", None)
        sender = getattr(message, "from_id", "")
        if not isinstance(payload, bytes) or not sender:
            return
        try:
            if len(payload) < 3:
                return
            msg_type = MessageType(payload[2])
            secret = None
            if msg_type is MessageType.PAIR_CONFIRM:
                secret = await self.federation.pairing_secret(sender)
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
            if authenticated and not (
                await self.federation.accept_counter(sender, fragment.counter)
            ):
                raise FrameError("replayed federation service frame")
            if not isinstance(value, dict) or value.get("mesh_id") != sender:
                raise FrameError("federation identity does not match packet sender")
            target = value.get("target_mesh_id")
            if target is not None and target != self.radio.local_node_id:
                return
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
                    int(value.get("protocol", 1)),
                    capabilities,
                    "mqtt" if getattr(message, "via_mqtt", False) else "radio",
                )
                # A node may have joined just after our infrequent broadcast HELLO.
                # Answer broadcasts directly so both peer directories converge without
                # another broadcast or an endless HELLO response loop.
                if not getattr(message, "is_direct", False) and target is None:
                    if getattr(message, "via_mqtt", False):
                        self._queue_federation_hello("^all", target_mesh_id=sender)
                    else:
                        self._queue_federation_hello(sender)
            elif msg_type is MessageType.PAIR_REQ:
                _, acknowledgement, _ = await self.federation.accept_pairing_request(
                    sender, bytes(value["public_key"]), bytes(value["nonce"])
                )
                frames = self.federation_codec.encode(
                    MessageType.PAIR_ACK, acknowledgement, 0, None
                )
                self._queue_federation_frames(
                    frames,
                    sender if getattr(message, "is_direct", False) else "^all",
                    want_ack=bool(getattr(message, "is_direct", False)),
                )
            elif msg_type is MessageType.PAIR_ACK:
                await self.federation.accept_pairing_ack(
                    sender, bytes(value["public_key"]), bytes(value["nonce"])
                )
            elif msg_type is MessageType.PAIR_CONFIRM and value.get("approved") is True:
                await self.federation.confirm_remote(sender)
            elif msg_type is MessageType.SERVICE_QUERY:
                await self._handle_service_query(sender, value)
            elif msg_type is MessageType.SERVICE_RESPONSE:
                await self._handle_service_response(sender, value)
            elif msg_type is MessageType.SYNC_REQ:
                peer = await self.federation.by_mesh_id(sender)
                limit = max(1, min(int(value.get("limit", 8)), 8))
                manifest = [
                    item.json() for item in await self.federation_sync.manifest(peer, limit)
                ]
                await self._send_federation_value(
                    sender,
                    MessageType.SYNC_MANIFEST,
                    {"mesh_id": self.federation.local_mesh_id, "items": manifest},
                )
            elif msg_type is MessageType.SYNC_NOTIFY:
                stream = str(value.get("stream", ""))
                peer = await self.federation.by_mesh_id(sender)
                if not stream.startswith("board:") or stream[6:] not in peer.boards:
                    raise ValueError("federation change notification is outside peer policy")
                await self._send_federation_value(sender, MessageType.SYNC_REQ, {"limit": 8})
            elif msg_type is MessageType.SYNC_MANIFEST:
                manifest = value.get("items", [])
                if not isinstance(manifest, list):
                    raise ValueError("invalid sync manifest")
                missing = await self.federation_sync.missing(manifest)
                if missing:
                    await self._send_federation_value(
                        sender,
                        MessageType.ITEM_REQ,
                        {"mesh_id": self.federation.local_mesh_id, "items": missing[:8]},
                    )
                else:
                    await self._send_federation_value(
                        sender,
                        MessageType.SYNC_DONE,
                        {"mesh_id": self.federation.local_mesh_id, "received": 0},
                    )
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
                await self._send_federation_value(
                    sender,
                    MessageType.SYNC_DONE,
                    {"mesh_id": self.federation.local_mesh_id, "sent": sent},
                )
            elif msg_type is MessageType.ITEM:
                item = value.get("item")
                if not isinstance(item, dict):
                    raise ValueError("invalid federation item")
                peer = await self.federation.by_mesh_id(sender)
                received = await self.federation_sync.quarantine(
                    peer, item, int(self.clock.now().timestamp())
                )
                await self.database.write(
                    "INSERT INTO fed_cursor(peer_id,stream,direction,cursor,updated_at) "
                    "VALUES(?,?,'recv',?,unixepoch()) ON CONFLICT(peer_id,stream,direction) "
                    "DO UPDATE SET cursor=excluded.cursor,updated_at=excluded.updated_at",
                    (peer.id, str(item.get("stream", "")), str(item.get("uid", ""))),
                )
                if received and str(item.get("stream", "")).startswith("board:"):
                    payload = item.get("payload")
                    if isinstance(payload, dict):
                        slug = str(item["stream"])[6:]
                        if await self.federation_sync.approved_thread(
                            slug, str(payload.get("thread_uid", ""))
                        ):
                            inbox = await self.database.read(
                                "SELECT id FROM fed_inbox_item WHERE peer_id=? AND stream=? "
                                "AND uid=? AND state='pending'",
                                (peer.id, str(item["stream"]), str(item.get("uid", ""))),
                            )
                            if inbox:
                                await self.federation_sync.import_inbox(
                                    int(inbox[0]["id"]),
                                    "federation:auto-thread",
                                    int(self.clock.now().timestamp()),
                                )
                receipt = await self.database.read(
                    "SELECT state FROM fed_inbox_item WHERE peer_id=? AND stream=? AND uid=?",
                    (peer.id, str(item.get("stream", "")), str(item.get("uid", ""))),
                )
                if receipt:
                    await self._send_federation_value(
                        sender,
                        MessageType.ITEM_RECEIPT,
                        {
                            "uid": str(item.get("uid", "")),
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
                    await self.database.write(
                        "UPDATE fed_mail_delivery SET state=?,error=?,updated_at=unixepoch() "
                        "WHERE relay_id=? AND direction='out'",
                        (
                            state,
                            str(value.get("error") or "")[:120] or None,
                            str(value["relay_id"]),
                        ),
                    )
        except (FrameError, KeyError, TypeError, ValueError) as error:
            packet_id = getattr(message, "packet_id", None)
            if packet_id is not None:
                await self.database.write(
                    "UPDATE message_log SET outcome='rejected',drop_reason=? "
                    "WHERE direction='in' AND packet_id=?",
                    (str(error)[:120], packet_id),
                )
            return

    async def _handle_service_query(self, sender: str, value: dict[str, object]) -> None:
        request_id = str(value["request_id"])
        service = str(value["service"])
        args = value.get("args", {})
        expires_at = int(value["expires_at"])
        now = int(self.clock.now().timestamp())
        if len(request_id) > 64 or service not in {"weather", "alerts", "knowledge"}:
            raise ValueError("invalid service request")
        if not isinstance(args, dict) or expires_at <= now or int(value.get("ttl", 0)) < 0:
            raise ValueError("expired or invalid service request")
        existing = await self.database.read(
            "SELECT request_id FROM fed_service_request WHERE request_id=?", (request_id,)
        )
        if existing:
            return
        await self.database.write(
            "INSERT INTO fed_service_request(request_id,direction,peer_mesh_id,service,args_json,"
            "status,created_at,updated_at,expires_at) VALUES(?,'in',?,?,?,'pending',?,?,?)",
            (
                request_id,
                sender,
                service,
                json.dumps(args, separators=(",", ":")),
                now,
                now,
                expires_at,
            ),
        )
        ok, result, provenance, error = True, {}, {}, None
        try:
            result, provenance = await self._execute_peer_service(service, args)
        except (OSError, ValueError) as caught:
            ok, error = False, str(caught)[:160]
        await self.database.write(
            "UPDATE fed_service_request SET status=?,result_json=?,provenance_json=?,error=?,"
            "completed_at=?,updated_at=? WHERE request_id=?",
            (
                "complete" if ok else "failed",
                json.dumps(result, separators=(",", ":")),
                json.dumps(provenance, separators=(",", ":")),
                error,
                now,
                now,
                request_id,
            ),
        )
        secret = await self.federation.secret(sender)
        counter = await self.federation.next_counter(sender)
        frames = self.federation_codec.encode(
            MessageType.SERVICE_RESPONSE,
            {
                "request_id": request_id,
                "mesh_id": self.federation.local_mesh_id,
                "ok": ok,
                "result": self._service_result_to_wire(service, result),
                "provenance": provenance,
                "error": error,
            },
            counter,
            secret,
        )
        self._queue_federation_frames(frames, sender, want_ack=True)

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
                json.dumps(value.get("provenance", {}), separators=(",", ":")),
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
                except OSError as error:
                    print(f"NWS alert poll failed: {error}", flush=True)
                try:
                    await self.seismic.poll(location.lat, location.lon)
                except OSError as error:
                    print(f"USGS earthquake poll failed: {error}", flush=True)
            await self.clock.sleep(300)

    async def _watch_loop(self) -> None:
        while True:
            await self.alerts.advance_due()
            await self.incidents.expire_due()
            await self.clock.sleep(15)

    async def reconnect_radio(self) -> None:
        await self.radio.close()

    async def _digest_loop(self) -> None:
        while True:
            for delivery in await self.digests.due():
                parts = chunk_text(
                    delivery.text,
                    max_parts=self.config.airtime.max_parts.get("digest", 4),
                )
                scheduled = self.governor.enqueue_many(
                    [
                        OutboundItem(
                            text=part,
                            dest=delivery.mesh_id,
                            channel=0,
                            traffic_class=TrafficClass.DIGEST,
                            want_ack=True,
                            multipart=len(parts) > 1,
                        )
                        for part in parts
                    ]
                )
                if scheduled is not None:
                    await self.digests.mark_scheduled(delivery)
            await self.clock.sleep(30)

    async def _maintenance_loop(self) -> None:
        while True:
            try:
                if await self.maintenance.due():
                    await self.maintenance.run()
            except Exception as error:
                print(f"Outpost maintenance failed: {error}", flush=True)
            await self.clock.sleep(60)

    async def shutdown(self) -> None:
        await self.supervisor.stop()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.database.close()

    async def _governor_loop(self) -> None:
        while True:
            item = await self.governor.tick()
            if item is not None:
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
            await self.clock.sleep(0.25)

    async def _inbound_loop(self) -> None:
        async for inbound in self.radio.inbound():
            if self.radio.local_node_id:
                self.inbound_pipeline.local_node_id = self.radio.local_node_id
            message = self.inbound_pipeline.process(inbound)
            if message is None:
                continue
            await self.message_log.record_inbound(message)
            INBOUND.labels(
                str(message.portnum), str(message.channel), str(message.is_direct).lower()
            ).inc()
            if (
                self.config.modules.fed.enabled
                and message.portnum == self.config.radio.federation_portnum
            ):
                await self._handle_federation_discovery(message)
                continue
            if message.portnum == 5 and message.request_id is not None:
                outcome = "acked" if message.routing_error in {None, "NONE"} else "naked"
                if await self.message_log.resolve_ack(message.request_id, outcome):
                    ACK_OUTCOME.labels(outcome).inc()
                continue
            if (
                self.config.modules.watch.enabled
                and message.latitude is not None
                and message.longitude is not None
            ):
                member = await self.router.members.resolve(message.from_id)
                await self.incidents.record_position(
                    member, message.latitude, message.longitude, prompt=message.is_direct
                )
                if not message.is_direct:
                    continue
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
                and message.text
                and self.incidents.emergency_keyword(
                    message.text, self.config.watch.emergency_keywords
                )
            ):
                member = await self.router.members.resolve(message.from_id)
                incident, created = await self.incidents.emergency_trigger(
                    member, message.text, self.config.watch.emergency_cooldown_minutes
                )
                if created:
                    await self._notify_emergency_responders(incident, member.mesh_id)
                response = Response(
                    ResponseKind.ACK,
                    [
                        Line(
                            f"⚠ INC {incident.local_ref} filed. Contact 911. Not emergency service."
                        )
                    ],
                )
            elif self.config.modules.watch.enabled and message.is_direct and message.text:
                member = await self.router.members.resolve(message.from_id)
                if self.incidents.is_position_share_notice(message.text):
                    response = Response(ResponseKind.NONE)
                else:
                    pending = await self.incidents.create_from_pending(message.text, member)
                    if pending is None:
                        response = await self.router.dispatch(message)
                    else:
                        created, similar = pending
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
                            assert created is not None
                            response = Response(
                                ResponseKind.ACK,
                                [
                                    Line(
                                        f"✓ INC {created.local_ref} {created.type} · shared GPS "
                                        f"{created.lat:.3f},{created.lon:.3f}"
                                    )
                                ],
                            )
            else:
                response = await self.router.dispatch(message)
            if response.kind == ResponseKind.NONE:
                continue
            text = render_response(response)
            max_parts = self.config.airtime.max_parts.get(response.airtime_class.value, 1)
            parts = chunk_text(text, max_parts=max_parts)
            self.governor.enqueue_many(
                [
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
            )

    async def _notify_emergency_responders(self, incident: Incident, sender_mesh_id: str) -> None:
        rows = await self.database.read(
            "SELECT mesh_id FROM member WHERE trust IN ('responder','operator') AND mesh_id<>?",
            (sender_mesh_id,),
        )
        for row in rows:
            self.governor.enqueue(
                OutboundItem(
                    text=(
                        f"⚠ Emergency keyword · INC {incident.local_ref} · {incident.title[:80]}"
                    ),
                    dest=row["mesh_id"],
                    channel=0,
                    traffic_class=TrafficClass.ALERT,
                    severity=Severity.URGENT,
                    want_ack=True,
                )
            )

    def status(self) -> dict[str, object]:
        configured = set(self.config.channels)
        available = set(self.radio.snapshot.channels)
        return {
            "node": self.config.node.name,
            "radio": self.radio.state.value,
            "airtime_used_ratio": self.governor.used_airtime / 3_600,
            "queues": self.governor.queue_depths(),
            "alert_delivery": self.governor.alert_delivery_status(),
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
