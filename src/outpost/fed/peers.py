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
    quota_items_per_hour: int


class FederationPeerService:
    def __init__(self, database: Database, clock: Clock, local_mesh_id: str) -> None:
        self.database, self.clock, self.local_mesh_id = database, clock, local_mesh_id

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
            quota_items_per_hour=int(row["quota_items_per_hour"]),
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

    async def forget(self, mesh_id: str) -> None:
        peer = await self.by_mesh_id(mesh_id)
        if peer.state != "rejected":
            raise ValueError("only rejected peers can be forgotten")
        await self.database.write("DELETE FROM fed_peer WHERE id=?", (peer.id,))

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
        if (incident_lat is None) != (incident_lon is None):
            raise ValueError("incident boundary requires both latitude and longitude")
        if incident_lat is not None and not -90 <= incident_lat <= 90:
            raise ValueError("incident latitude must be -90 to 90")
        if incident_lon is not None and not -180 <= incident_lon <= 180:
            raise ValueError("incident longitude must be -180 to 180")
        if not 1 <= incident_radius_km <= 500:
            raise ValueError("incident radius must be 1-500 km")
        await self.database.write(
            "UPDATE fed_peer SET boards=?,sync_incidents=?,incident_lat=?,incident_lon=?,"
            "incident_radius_km=?,relay_alerts=?,"
            "quota_items_per_hour=?,relay_mail=?,quota_mail_per_hour=? WHERE id=?",
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
                peer.id,
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
        await self.database.write(
            "UPDATE fed_peer SET tx_counter=tx_counter+1 WHERE mesh_id=? AND state='active'",
            (mesh_id,),
        )
        peer = await self.by_mesh_id(mesh_id)
        if peer.state != "active":
            raise ValueError("federation peer is not active")
        return peer.tx_counter

    async def accept_counter(self, mesh_id: str, counter: int) -> bool:
        peer = await self.by_mesh_id(mesh_id)
        if peer.state != "active" or counter <= peer.rx_counter:
            return False
        await self.database.write(
            "UPDATE fed_peer SET rx_counter=? WHERE id=? AND rx_counter<?",
            (counter, peer.id, counter),
        )
        updated = await self.by_mesh_id(mesh_id)
        return updated.rx_counter == counter
