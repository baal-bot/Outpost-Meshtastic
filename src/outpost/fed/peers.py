from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from outpost.clock import Clock
from outpost.store import Database


@dataclass(frozen=True)
class Peer:
    id: int
    mesh_id: str
    node_name: str | None
    state: str
    protocol_version: int
    capabilities: dict[str, Any]
    discovery_transports: list[str]
    tx_counter: int
    rx_counter: int
    last_seen_at: int | None
    approved_by: str | None
    approved_at: int | None
    local_approved: bool
    remote_approved: bool
    boards: list[str]
    sync_incidents: bool
    incident_lat: float | None
    incident_lon: float | None
    incident_radius_km: float
    relay_alerts: bool
    relay_mail: bool
    quota_mail_per_hour: int
    quota_mail_per_recipient_per_hour: int
    quota_items_per_hour: int
    policy_configured: bool
    policy_applied_by: str | None
    policy_applied_at: int | None
    policy_review_at: int | None
    service_permissions: list[str]
    quota_services_per_hour: int
    service_concurrency: int
    service_max_response_bytes: int
    service_airtime_seconds_per_hour: float


class FederationPeerService:
    def __init__(
        self,
        database: Database,
        clock: Clock,
        local_mesh_id: str,
        peer_stale_hours: int = 72,
    ) -> None:
        self.database, self.clock, self.local_mesh_id = database, clock, local_mesh_id
        self.peer_stale_seconds = max(1, peer_stale_hours) * 3_600

    def is_online_at(
        self,
        state: str,
        last_seen_at: int | None,
        *,
        now: int | None = None,
    ) -> bool:
        if state != "active" or last_seen_at is None:
            return False
        stamp = int(self.clock.now().timestamp()) if now is None else now
        return stamp - last_seen_at <= self.peer_stale_seconds

    def is_online(self, peer: Peer, *, now: int | None = None) -> bool:
        return self.is_online_at(peer.state, peer.last_seen_at, now=now)

    def liveness(self, peer: Peer, *, now: int | None = None) -> dict[str, Any]:
        online = self.is_online(peer, now=now)
        paired = peer.state == "active"
        return {
            "connectivity": "online" if online else "offline" if paired else None,
            "sync_paused": paired and not online,
            "stale_after_seconds": self.peer_stale_seconds,
            "offline_since_at": (
                peer.last_seen_at + self.peer_stale_seconds
                if paired and not online and peer.last_seen_at is not None
                else None
            ),
        }

    @staticmethod
    def confirmation_code(secret: bytes, first_id: str, second_id: str) -> str:
        identities = sorted((first_id.encode(), second_id.encode()))
        digest = hashlib.sha256(
            secret + len(identities[0]).to_bytes(2, "big") + identities[0] + identities[1]
        ).digest()
        return f"{int.from_bytes(digest[:4], 'big') % 1_000_000:06d}"

    @staticmethod
    def _peer(row: Any) -> Peer:
        return Peer(
            id=row["id"],
            mesh_id=row["mesh_id"],
            node_name=row["node_name"],
            state=row["state"],
            protocol_version=row["protocol_version"],
            capabilities=json.loads(row["capabilities"]),
            discovery_transports=json.loads(row["discovery_transports"]),
            tx_counter=row["tx_counter"],
            rx_counter=row["rx_counter"],
            last_seen_at=row["last_seen_at"],
            approved_by=row["approved_by"],
            approved_at=row["approved_at"],
            local_approved=bool(row["local_approved"]),
            remote_approved=bool(row["remote_approved"]),
            boards=json.loads(row["boards"]),
            sync_incidents=bool(row["sync_incidents"]),
            incident_lat=float(row["incident_lat"]) if row["incident_lat"] is not None else None,
            incident_lon=float(row["incident_lon"]) if row["incident_lon"] is not None else None,
            incident_radius_km=float(row["incident_radius_km"]),
            relay_alerts=bool(row["relay_alerts"]),
            relay_mail=bool(row["relay_mail"]),
            quota_mail_per_hour=int(row["quota_mail_per_hour"]),
            quota_mail_per_recipient_per_hour=int(
                row["quota_mail_per_recipient_per_hour"]
            ),
            quota_items_per_hour=int(row["quota_items_per_hour"]),
            policy_configured=bool(row["policy_configured"]),
            policy_applied_by=row["policy_applied_by"],
            policy_applied_at=row["policy_applied_at"],
            policy_review_at=row["policy_review_at"],
            service_permissions=json.loads(row["service_permissions"]),
            quota_services_per_hour=int(row["quota_services_per_hour"]),
            service_concurrency=int(row["service_concurrency"]),
            service_max_response_bytes=int(row["service_max_response_bytes"]),
            service_airtime_seconds_per_hour=float(row["service_airtime_seconds_per_hour"]),
        )

    def _derive_secret(
        self,
        private: X25519PrivateKey,
        remote_public: bytes,
        local_nonce: bytes,
        remote_nonce: bytes,
        remote_id: str,
    ) -> bytes:
        if len(remote_public) != 32 or len(local_nonce) != 16 or len(remote_nonce) != 16:
            raise ValueError("invalid federation key exchange material")
        identities = sorted(
            ((self.local_mesh_id.encode(), local_nonce), (remote_id.encode(), remote_nonce)),
            key=lambda item: item[0],
        )
        context = b"outpost-federation-v1"
        for identity, nonce in identities:
            context += len(identity).to_bytes(2, "big") + identity + nonce
        return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=context).derive(
            private.exchange(X25519PublicKey.from_public_bytes(remote_public))
        )

    async def create_pairing_request(
        self, mesh_id: str, *, replace: bool = False
    ) -> tuple[Peer, dict[str, Any]]:
        peer = await self.by_mesh_id(mesh_id)
        if peer.state == "active" and not replace:
            raise ValueError("active peer requires explicit key replacement")
        if peer.state == "rejected":
            raise ValueError("rejected peer must be returned to pending before pairing")
        private = X25519PrivateKey.generate()
        private_raw = private.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
        public_raw = private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        nonce = secrets.token_bytes(16)
        await self.database.write(
            "UPDATE fed_peer SET state='pairing',pairing_private=?,pairing_nonce=?,"
            "shared_secret=NULL,local_approved=0,remote_approved=0,tx_counter=0,rx_counter=0 "
            "WHERE id=?",
            (private_raw, nonce, peer.id),
        )
        return await self.by_mesh_id(mesh_id), {
            "mesh_id": self.local_mesh_id,
            "target_mesh_id": mesh_id,
            "public_key": public_raw,
            "nonce": nonce,
        }

    async def accept_pairing_request(
        self, mesh_id: str, remote_public: bytes, remote_nonce: bytes
    ) -> tuple[Peer, dict[str, Any], str]:
        peer = await self.by_mesh_id(mesh_id)
        if peer.state == "active":
            raise ValueError("active peer requires operator-authorized key replacement")
        if peer.state == "rejected":
            raise ValueError("rejected peer cannot initiate pairing")
        private = X25519PrivateKey.generate()
        public_raw = private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        nonce = secrets.token_bytes(16)
        secret = self._derive_secret(private, remote_public, nonce, remote_nonce, mesh_id)
        await self.database.write(
            "UPDATE fed_peer SET state='pairing',shared_secret=?,pairing_private=NULL,"
            "pairing_nonce=?,local_approved=0,remote_approved=0,tx_counter=0,rx_counter=0 "
            "WHERE id=?",
            (secret, nonce, peer.id),
        )
        updated = await self.by_mesh_id(mesh_id)
        return (
            updated,
            {
                "mesh_id": self.local_mesh_id,
                "target_mesh_id": mesh_id,
                "public_key": public_raw,
                "nonce": nonce,
            },
            self.confirmation_code(secret, self.local_mesh_id, mesh_id),
        )

    async def accept_pairing_ack(
        self, mesh_id: str, remote_public: bytes, remote_nonce: bytes
    ) -> tuple[Peer, str]:
        rows = await self.database.read("SELECT * FROM fed_peer WHERE mesh_id=?", (mesh_id,))
        if not rows or rows[0]["state"] != "pairing" or rows[0]["pairing_private"] is None:
            raise ValueError("no outbound pairing request is pending")
        private = X25519PrivateKey.from_private_bytes(bytes(rows[0]["pairing_private"]))
        local_nonce = bytes(rows[0]["pairing_nonce"])
        secret = self._derive_secret(private, remote_public, local_nonce, remote_nonce, mesh_id)
        await self.database.write(
            "UPDATE fed_peer SET shared_secret=?,pairing_private=NULL WHERE mesh_id=?",
            (secret, mesh_id),
        )
        return await self.by_mesh_id(mesh_id), self.confirmation_code(
            secret, self.local_mesh_id, mesh_id
        )

    async def pairing_code(self, mesh_id: str) -> str:
        rows = await self.database.read(
            "SELECT state,shared_secret FROM fed_peer WHERE mesh_id=?", (mesh_id,)
        )
        if not rows or rows[0]["state"] != "pairing" or rows[0]["shared_secret"] is None:
            raise ValueError("pairing code is not ready")
        return self.confirmation_code(bytes(rows[0]["shared_secret"]), self.local_mesh_id, mesh_id)

    async def pairing_secret(self, mesh_id: str) -> bytes:
        rows = await self.database.read(
            "SELECT state,shared_secret FROM fed_peer WHERE mesh_id=?", (mesh_id,)
        )
        if not rows or rows[0]["state"] != "pairing" or rows[0]["shared_secret"] is None:
            raise ValueError("pairing secret is not ready")
        return bytes(rows[0]["shared_secret"])

    async def approve_local(self, mesh_id: str, operator: str, code: str) -> Peer:
        expected = await self.pairing_code(mesh_id)
        if not secrets.compare_digest(expected, code):
            raise ValueError("federation confirmation code does not match")
        now = int(self.clock.now().timestamp())
        await self.database.write(
            "UPDATE fed_peer SET local_approved=1,approved_by=?,approved_at=?,"
            "state=CASE WHEN remote_approved=1 THEN 'active' ELSE 'pairing' END WHERE mesh_id=?",
            (operator, now, mesh_id),
        )
        return await self.by_mesh_id(mesh_id)

    async def confirm_remote(self, mesh_id: str) -> Peer:
        await self.database.write(
            "UPDATE fed_peer SET remote_approved=1,"
            "state=CASE WHEN local_approved=1 THEN 'active' ELSE 'pairing' END "
            "WHERE mesh_id=? AND state='pairing' AND shared_secret IS NOT NULL",
            (mesh_id,),
        )
        return await self.by_mesh_id(mesh_id)

    async def discover(
        self,
        mesh_id: str,
        node_name: str,
        protocol_version: int,
        capabilities: dict[str, Any],
        transport: str,
    ) -> Peer:
        if mesh_id == self.local_mesh_id:
            raise ValueError("cannot discover the local Outpost as a peer")
        now = int(self.clock.now().timestamp())
        rows = await self.database.read("SELECT * FROM fed_peer WHERE mesh_id=?", (mesh_id,))
        transports = set(json.loads(rows[0]["discovery_transports"])) if rows else set()
        transports.add(transport)
        await self.database.write(
            """INSERT INTO fed_peer(mesh_id,node_name,protocol_version,capabilities,
               discovery_transports,last_seen_at,created_at) VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(mesh_id) DO UPDATE SET node_name=excluded.node_name,
               protocol_version=excluded.protocol_version,capabilities=excluded.capabilities,
               discovery_transports=excluded.discovery_transports,last_seen_at=excluded.last_seen_at""",
            (
                mesh_id,
                node_name[:80],
                protocol_version,
                json.dumps(capabilities, separators=(",", ":")),
                json.dumps(sorted(transports), separators=(",", ":")),
                now,
                now,
            ),
        )
        return await self.by_mesh_id(mesh_id)

    async def touch(self, mesh_id: str, *, at: int | None = None) -> Peer:
        """Record fully validated activity without changing the peer's trust state."""
        stamp = int(self.clock.now().timestamp()) if at is None else at
        await self.database.write(
            "UPDATE fed_peer SET last_seen_at=? WHERE mesh_id=? AND state='active'",
            (stamp, mesh_id),
        )
        return await self.by_mesh_id(mesh_id)

    async def by_mesh_id(self, mesh_id: str) -> Peer:
        rows = await self.database.read("SELECT * FROM fed_peer WHERE mesh_id=?", (mesh_id,))
        if not rows:
            raise ValueError("federation peer not found")
        return self._peer(rows[0])

    async def list(self, state: str | None = None) -> list[Peer]:
        if state is None:
            rows = await self.database.read(
                "SELECT * FROM fed_peer ORDER BY last_seen_at DESC, node_name, mesh_id"
            )
        else:
            rows = await self.database.read(
                "SELECT * FROM fed_peer WHERE state=? "
                "ORDER BY last_seen_at DESC, node_name, mesh_id",
                (state,),
            )
        return [self._peer(row) for row in rows]

    async def set_state(self, mesh_id: str, state: str) -> Peer:
        if state not in {"pending", "paused", "rejected"}:
            raise ValueError("unsupported operator peer state")
        peer = await self.by_mesh_id(mesh_id)
        revoke_trust = peer.state == "active" and state == "pending"
        await self.database.write(
            "UPDATE fed_peer SET state=?, shared_secret=CASE WHEN ? OR ?='rejected' "
            "THEN NULL ELSE shared_secret END, pairing_private=CASE WHEN ? OR ?='rejected' "
            "THEN NULL ELSE pairing_private END, pairing_nonce=CASE WHEN ? OR ?='rejected' "
            "THEN NULL ELSE pairing_nonce END, local_approved=CASE WHEN ? OR ?='rejected' "
            "THEN 0 ELSE local_approved END, remote_approved=CASE WHEN ? OR ?='rejected' "
            "THEN 0 ELSE remote_approved END, tx_counter=CASE WHEN ? THEN 0 ELSE tx_counter END, "
            "rx_counter=CASE WHEN ? THEN 0 ELSE rx_counter END, approved_by=CASE WHEN ? "
            "THEN NULL ELSE approved_by END, approved_at=CASE WHEN ? THEN NULL "
            "ELSE approved_at END WHERE id=?",
            (
                state,
                revoke_trust,
                state,
                revoke_trust,
                state,
                revoke_trust,
                state,
                revoke_trust,
                state,
                revoke_trust,
                state,
                revoke_trust,
                revoke_trust,
                revoke_trust,
                revoke_trust,
                peer.id,
            ),
        )
        return await self.by_mesh_id(mesh_id)

    async def forget(self, mesh_id: str, actor: str = "system") -> None:
        peer = await self.by_mesh_id(mesh_id)
        if peer.state != "rejected":
            raise ValueError("only rejected peers can be forgotten")
        now = int(self.clock.now().timestamp())
        async with self.database.transaction() as transaction:
            await transaction.write(
                "INSERT INTO fed_peer_tombstone(mesh_id,node_name,forgotten_at,forgotten_by) "
                "VALUES(?,?,?,?) ON CONFLICT(mesh_id) DO UPDATE SET "
                "node_name=excluded.node_name,forgotten_at=excluded.forgotten_at,"
                "forgotten_by=excluded.forgotten_by",
                (peer.mesh_id, peer.node_name, now, actor[:160]),
            )
            await transaction.write("DELETE FROM fed_peer WHERE id=?", (peer.id,))

    async def update_sync_policy(
        self,
        mesh_id: str,
        *,
        boards: list[str],
        sync_incidents: bool,
        relay_alerts: bool,
        quota_items_per_hour: int,
        incident_lat: float | None = None,
        incident_lon: float | None = None,
        incident_radius_km: float = 25,
        relay_mail: bool = False,
        quota_mail_per_hour: int = 20,
        quota_mail_per_recipient_per_hour: int | None = None,
        service_permissions: list[str] | None = None,
        quota_services_per_hour: int | None = None,
        service_concurrency: int | None = None,
        service_max_response_bytes: int | None = None,
        service_airtime_seconds_per_hour: float | None = None,
        applied_by: str = "web:operator",
        policy_review_at: int | None = None,
        enable_boards: list[str] | None = None,
        confirm_enable_boards: bool = False,
    ) -> Peer:
        peer = await self.by_mesh_id(mesh_id)
        if peer.state not in {"active", "paused"}:
            raise ValueError("sync policy requires a paired peer")
        cleaned = sorted({slug.strip().lower() for slug in boards if slug.strip()})
        if len(cleaned) > 20 or any(len(slug) > 40 for slug in cleaned):
            raise ValueError("board sync policy is too large")
        if not 1 <= quota_items_per_hour <= 500:
            raise ValueError("item quota must be 1-500 per hour")
        if not 1 <= quota_mail_per_hour <= 100:
            raise ValueError("mail quota must be 1-100 per hour")
        recipient_mail_quota = (
            peer.quota_mail_per_recipient_per_hour
            if quota_mail_per_recipient_per_hour is None
            else quota_mail_per_recipient_per_hour
        )
        if not 1 <= recipient_mail_quota <= 100:
            raise ValueError("per-recipient mail quota must be 1-100 per hour")
        permissions = sorted(
            set(peer.service_permissions if service_permissions is None else service_permissions)
        )
        if any(service not in {"weather", "alerts", "knowledge"} for service in permissions):
            raise ValueError("peer service permission is not supported")
        service_quota = (
            peer.quota_services_per_hour
            if quota_services_per_hour is None
            else quota_services_per_hour
        )
        concurrency = (
            peer.service_concurrency if service_concurrency is None else service_concurrency
        )
        response_bytes = (
            peer.service_max_response_bytes
            if service_max_response_bytes is None
            else service_max_response_bytes
        )
        airtime_seconds = (
            peer.service_airtime_seconds_per_hour
            if service_airtime_seconds_per_hour is None
            else service_airtime_seconds_per_hour
        )
        if not 1 <= service_quota <= 60:
            raise ValueError("peer service quota must be 1-60 per hour")
        if not 1 <= concurrency <= 4:
            raise ValueError("peer service concurrency must be 1-4")
        if not 256 <= response_bytes <= 1600:
            raise ValueError("peer service response limit must be 256-1600 bytes")
        if not 1 <= airtime_seconds <= 120:
            raise ValueError("peer service airtime limit must be 1-120 seconds per hour")
        if (incident_lat is None) != (incident_lon is None):
            raise ValueError("incident boundary requires both latitude and longitude")
        if incident_lat is not None and not -90 <= incident_lat <= 90:
            raise ValueError("incident latitude must be -90 to 90")
        if incident_lon is not None and not -180 <= incident_lon <= 180:
            raise ValueError("incident longitude must be -180 to 180")
        if not 1 <= incident_radius_km <= 500:
            raise ValueError("incident radius must be 1-500 km")
        now = int(self.clock.now().timestamp())
        if policy_review_at is not None and policy_review_at <= now:
            raise ValueError("policy review date must be in the future")
        boards_to_enable: list[str] = []
        if enable_boards is not None:
            requested_enable = sorted(
                {slug.strip().lower() for slug in enable_boards if slug.strip()}
            )
            if set(requested_enable) - set(cleaned):
                raise ValueError("globally enabled boards must also be selected for this peer")
            board_rows = []
            if cleaned:
                placeholders = ",".join("?" for _ in cleaned)
                board_rows = await self.database.read(
                    f"SELECT slug,federated FROM board WHERE slug IN ({placeholders})",  # noqa: S608
                    cleaned,
                )
            found = {str(row["slug"]): bool(row["federated"]) for row in board_rows}
            missing = sorted(set(cleaned) - set(found))
            if missing:
                raise ValueError(f"unknown board selection: {', '.join(missing)}")
            boards_to_enable = sorted(slug for slug, enabled in found.items() if not enabled)
            if boards_to_enable and not confirm_enable_boards:
                raise ValueError(
                    "global board federation confirmation required for: "
                    + ", ".join(boards_to_enable)
                )
            if set(boards_to_enable) - set(requested_enable):
                raise ValueError("every selected private board requires explicit global enablement")
        before = {
            "boards": peer.boards,
            "sync_incidents": peer.sync_incidents,
            "incident_lat": peer.incident_lat,
            "incident_lon": peer.incident_lon,
            "incident_radius_km": peer.incident_radius_km,
            "relay_alerts": peer.relay_alerts,
            "relay_mail": peer.relay_mail,
            "quota_items_per_hour": peer.quota_items_per_hour,
            "quota_mail_per_hour": peer.quota_mail_per_hour,
            "quota_mail_per_recipient_per_hour": peer.quota_mail_per_recipient_per_hour,
            "service_permissions": peer.service_permissions,
            "quota_services_per_hour": peer.quota_services_per_hour,
            "service_concurrency": peer.service_concurrency,
            "service_max_response_bytes": peer.service_max_response_bytes,
            "service_airtime_seconds_per_hour": peer.service_airtime_seconds_per_hour,
            "policy_review_at": peer.policy_review_at,
        }
        after = {
            "boards": cleaned,
            "sync_incidents": sync_incidents,
            "incident_lat": incident_lat,
            "incident_lon": incident_lon,
            "incident_radius_km": incident_radius_km,
            "relay_alerts": relay_alerts,
            "relay_mail": relay_mail,
            "quota_items_per_hour": quota_items_per_hour,
            "quota_mail_per_hour": quota_mail_per_hour,
            "quota_mail_per_recipient_per_hour": recipient_mail_quota,
            "service_permissions": permissions,
            "quota_services_per_hour": service_quota,
            "service_concurrency": concurrency,
            "service_max_response_bytes": response_bytes,
            "service_airtime_seconds_per_hour": airtime_seconds,
            "policy_review_at": policy_review_at,
        }
        async with self.database.transaction() as transaction:
            if boards_to_enable:
                placeholders = ",".join("?" for _ in boards_to_enable)
                await transaction.write(
                    f"UPDATE board SET federated=1 WHERE slug IN ({placeholders})",  # noqa: S608
                    boards_to_enable,
                )
            await transaction.write(
                "UPDATE fed_peer SET boards=?,sync_incidents=?,incident_lat=?,incident_lon=?,"
                "incident_radius_km=?,relay_alerts=?,"
                "quota_items_per_hour=?,relay_mail=?,quota_mail_per_hour=?,"
                "quota_mail_per_recipient_per_hour=?,last_sync_at=NULL,"
                "service_permissions=?,quota_services_per_hour=?,service_concurrency=?,"
                "service_max_response_bytes=?,service_airtime_seconds_per_hour=?,"
                "policy_configured=1,policy_applied_by=?,policy_applied_at=?,policy_review_at=? "
                "WHERE id=?",
                (
                    json.dumps(cleaned, separators=(",", ":")),
                    int(sync_incidents),
                    incident_lat,
                    incident_lon,
                    incident_radius_km,
                    int(relay_alerts),
                    quota_items_per_hour,
                    int(relay_mail),
                    quota_mail_per_hour,
                    recipient_mail_quota,
                    json.dumps(permissions, separators=(",", ":")),
                    service_quota,
                    concurrency,
                    response_bytes,
                    airtime_seconds,
                    applied_by[:120],
                    now,
                    policy_review_at,
                    peer.id,
                ),
            )
            await transaction.write(
                "DELETE FROM fed_cursor WHERE peer_id=? AND stream='_reconcile' "
                "AND direction='recv'",
                (peer.id,),
            )
            await transaction.write(
                "INSERT INTO audit_log(actor_kind,actor_ref,action,target,detail,created_at) "
                "VALUES('web',?,'federation.policy_update',?,?,?)",
                (
                    applied_by[:120],
                    peer.mesh_id,
                    json.dumps(
                        {
                            "before": before,
                            "after": after,
                            "globally_enabled_boards": boards_to_enable,
                        },
                        separators=(",", ":"),
                    ),
                    now,
                ),
            )
        return await self.by_mesh_id(mesh_id)

    async def secret(self, mesh_id: str) -> bytes:
        rows = await self.database.read(
            "SELECT shared_secret FROM fed_peer WHERE mesh_id=? AND state='active'", (mesh_id,)
        )
        if not rows or rows[0]["shared_secret"] is None:
            raise ValueError("active federation secret unavailable")
        return bytes(rows[0]["shared_secret"])

    async def next_counter(self, mesh_id: str) -> int:
        async with self.database.transaction() as transaction:
            rows = await transaction.read(
                "UPDATE fed_peer SET tx_counter=tx_counter+1 "
                "WHERE mesh_id=? AND state='active' RETURNING tx_counter",
                (mesh_id,),
            )
        if not rows:
            raise ValueError("federation peer is not active")
        return int(rows[0]["tx_counter"])

    async def accept_counter(self, mesh_id: str, counter: int) -> bool:
        async with self.database.transaction() as transaction:
            rows = await transaction.read(
                "UPDATE fed_peer SET rx_counter=? WHERE mesh_id=? AND state='active' "
                "AND rx_counter<? RETURNING rx_counter",
                (counter, mesh_id, counter),
            )
        return bool(rows)

    async def admit_service_request(
        self,
        mesh_id: str,
        request_id: str,
        service: str,
        args_json: str,
        args_fingerprint: str,
        now: int,
        expires_at: int,
    ) -> tuple[Peer, str, dict[str, Any] | None]:
        """Atomically authorize and account for one inbound peer-service request."""
        window_start = now - now % 3_600
        async with self.database.transaction() as transaction:
            peer_rows = await transaction.read(
                "SELECT * FROM fed_peer WHERE mesh_id=? AND state='active'", (mesh_id,)
            )
            if not peer_rows:
                raise ValueError("peer service requires an active peer")
            peer = self._peer(peer_rows[0])
            existing = await transaction.read(
                "SELECT * FROM fed_service_request WHERE request_id=?", (request_id,)
            )
            if existing:
                value = dict(existing[0])
                if value["peer_mesh_id"] != mesh_id or value["direction"] != "in":
                    raise ValueError("peer service request id collides with another request")
                return peer, "replay", value
            await transaction.write(
                "INSERT INTO fed_service_usage(peer_id,window_start) VALUES(?,?) "
                "ON CONFLICT(peer_id,window_start) DO NOTHING",
                (peer.id, window_start),
            )
            usage = (
                await transaction.read(
                    "SELECT * FROM fed_service_usage WHERE peer_id=? AND window_start=?",
                    (peer.id, window_start),
                )
            )[0]
            pending = await transaction.read(
                "SELECT COUNT(*) count FROM fed_service_request WHERE direction='in' "
                "AND peer_mesh_id=? AND status='pending' AND expires_at>?",
                (mesh_id, now),
            )
            circuit = await transaction.read(
                "SELECT open_until FROM fed_service_circuit WHERE peer_id=? AND service=?",
                (peer.id, service),
            )
            outcome = "admitted"
            if service not in peer.service_permissions:
                outcome = "permission_denied"
            elif circuit and int(circuit[0]["open_until"] or 0) > now:
                outcome = "circuit_open"
            elif int(usage["requests"]) >= peer.quota_services_per_hour:
                outcome = "request_quota"
            elif int(pending[0]["count"]) >= peer.service_concurrency:
                outcome = "concurrency_quota"
            if outcome != "admitted":
                await transaction.write(
                    "UPDATE fed_service_usage SET denied=denied+1 "
                    "WHERE peer_id=? AND window_start=?",
                    (peer.id, window_start),
                )
                await transaction.write(
                    "INSERT INTO fed_service_request(request_id,direction,peer_mesh_id,service,"
                    "args_json,args_fingerprint,status,created_at,updated_at,expires_at,"
                    "completed_at,error) VALUES(?,'in',?,?,?,?, 'failed',?,?,?,?,?)",
                    (
                        request_id,
                        mesh_id,
                        service,
                        args_json,
                        args_fingerprint,
                        now,
                        now,
                        expires_at,
                        now,
                        outcome,
                    ),
                )
                await transaction.write(
                    "DELETE FROM fed_service_request WHERE request_id IN ("
                    "SELECT request_id FROM fed_service_request WHERE peer_mesh_id=? "
                    "AND direction='in' AND status<>'pending' ORDER BY updated_at DESC "
                    "LIMIT -1 OFFSET 500)",
                    (mesh_id,),
                )
                return peer, outcome, None
            await transaction.write(
                "UPDATE fed_service_usage SET requests=requests+1 "
                "WHERE peer_id=? AND window_start=?",
                (peer.id, window_start),
            )
            await transaction.write(
                "INSERT INTO fed_service_request(request_id,direction,peer_mesh_id,service,"
                "args_json,args_fingerprint,status,created_at,updated_at,expires_at) "
                "VALUES(?,'in',?,?,?,?,'pending',?,?,?)",
                (
                    request_id,
                    mesh_id,
                    service,
                    args_json,
                    args_fingerprint,
                    now,
                    now,
                    expires_at,
                ),
            )
            await transaction.write(
                "DELETE FROM fed_service_request WHERE request_id IN ("
                "SELECT request_id FROM fed_service_request WHERE peer_mesh_id=? "
                "AND direction='in' AND status<>'pending' ORDER BY updated_at DESC "
                "LIMIT -1 OFFSET 500)",
                (mesh_id,),
            )
        return peer, "admitted", None

    async def record_service_provider_outcome(
        self, peer: Peer, service: str, failed: bool, now: int
    ) -> None:
        """Open a five-minute circuit after three consecutive provider failures."""
        async with self.database.transaction() as transaction:
            if not failed:
                await transaction.write(
                    "INSERT INTO fed_service_circuit(peer_id,service,consecutive_failures,"
                    "open_until,updated_at) VALUES(?,?,0,NULL,?) "
                    "ON CONFLICT(peer_id,service) DO UPDATE SET consecutive_failures=0,"
                    "open_until=NULL,updated_at=excluded.updated_at",
                    (peer.id, service, now),
                )
                return
            rows = await transaction.read(
                "SELECT consecutive_failures FROM fed_service_circuit "
                "WHERE peer_id=? AND service=?",
                (peer.id, service),
            )
            failures = (int(rows[0]["consecutive_failures"]) if rows else 0) + 1
            await transaction.write(
                "INSERT INTO fed_service_circuit(peer_id,service,consecutive_failures,"
                "open_until,updated_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(peer_id,service) DO UPDATE SET "
                "consecutive_failures=excluded.consecutive_failures,"
                "open_until=excluded.open_until,updated_at=excluded.updated_at",
                (peer.id, service, failures, now + 300 if failures >= 3 else None, now),
            )

    async def reserve_service_response(
        self, peer: Peer, response_bytes: int, airtime_seconds: float, now: int
    ) -> str | None:
        """Reserve a response against the peer's byte and rolling hourly airtime policy."""
        window_start = now - now % 3_600
        async with self.database.transaction() as transaction:
            await transaction.write(
                "INSERT INTO fed_service_usage(peer_id,window_start) VALUES(?,?) "
                "ON CONFLICT(peer_id,window_start) DO NOTHING",
                (peer.id, window_start),
            )
            usage = (
                await transaction.read(
                    "SELECT response_airtime_seconds FROM fed_service_usage "
                    "WHERE peer_id=? AND window_start=?",
                    (peer.id, window_start),
                )
            )[0]
            if response_bytes > peer.service_max_response_bytes:
                await transaction.write(
                    "UPDATE fed_service_usage SET denied=denied+1 "
                    "WHERE peer_id=? AND window_start=?",
                    (peer.id, window_start),
                )
                return "response_byte_quota"
            if (
                float(usage["response_airtime_seconds"]) + airtime_seconds
                > peer.service_airtime_seconds_per_hour
            ):
                await transaction.write(
                    "UPDATE fed_service_usage SET denied=denied+1 "
                    "WHERE peer_id=? AND window_start=?",
                    (peer.id, window_start),
                )
                return "airtime_quota"
            await transaction.write(
                "UPDATE fed_service_usage SET response_bytes=response_bytes+?,"
                "response_airtime_seconds=response_airtime_seconds+? "
                "WHERE peer_id=? AND window_start=?",
                (response_bytes, airtime_seconds, peer.id, window_start),
            )
        return None
