from __future__ import annotations

import hashlib
import json
import re
import secrets
from dataclasses import asdict, dataclass
from typing import Any

import cbor2
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from outpost.clock import Clock
from outpost.fed.framing import wire_bytes, wire_int
from outpost.fed.peers import FederationPeerService
from outpost.store import Database, Transaction

NODE_ID = re.compile(r"^![0-9a-fA-F]{8}$")
SCOPES = {"incident", "request", "receipt", "opaque"}
MAX_PAYLOAD_BYTES = 800
MAX_LIFETIME_SECONDS = 7 * 86_400
CLOCK_SKEW_SECONDS = 300
ACTIVE_STATES = ("queued", "quarantined", "paused", "forwarding", "forwarded")
ROTATION_CONTEXT = b"outpost-relay-origin-key-rotation-v1\x00"


@dataclass(frozen=True)
class RelayPolicy:
    peer_id: int
    mesh_id: str
    enabled: bool
    paused: bool
    scopes: list[str]
    max_stored_items: int
    max_stored_bytes: int
    rate_per_hour: int
    airtime_seconds_per_hour: float
    updated_at: int | None
    updated_by: str | None

    def json(self) -> dict[str, Any]:
        return asdict(self)


class FederationRelayService:
    """Signed, bounded custody transfer between explicitly trusted federation peers."""

    CORE_KEYS = {
        "origin",
        "destination",
        "scope",
        "idempotency_key",
        "created_at",
        "expires_at",
        "hop_limit",
        "payload",
    }
    WIRE_KEYS = CORE_KEYS | {"envelope_id", "origin_public_key", "origin_signature", "route"}
    ROTATION_KEYS = {"rotation_from_public_key", "rotation_signature"}

    def __init__(self, database: Database, peers: FederationPeerService, clock: Clock) -> None:
        self.database, self.peers, self.clock = database, peers, clock

    @staticmethod
    def _private_bytes(key: Ed25519PrivateKey) -> bytes:
        return key.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )

    @staticmethod
    def _public_bytes(key: Ed25519PublicKey) -> bytes:
        return key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)

    @staticmethod
    def _fingerprint(public_key: bytes) -> str:
        return hashlib.sha256(public_key).hexdigest()

    @staticmethod
    def _core_bytes(core: dict[str, Any]) -> bytes:
        return cbor2.dumps(core, canonical=True)

    @staticmethod
    def _envelope_id(core_bytes: bytes) -> str:
        return hashlib.sha256(core_bytes).hexdigest()[:32]

    def _local_id(self) -> str:
        value = self.peers.local_mesh_id
        if not NODE_ID.fullmatch(value):
            raise ValueError("local radio identity is unavailable")
        return value.lower()

    async def initialize(self) -> bytes:
        rows = await self.database.read("SELECT public_key FROM fed_relay_identity WHERE id=1")
        if rows:
            return bytes(rows[0]["public_key"])
        private = Ed25519PrivateKey.generate()
        private_bytes = self._private_bytes(private)
        public_bytes = self._public_bytes(private.public_key())
        now = int(self.clock.now().timestamp())
        await self.database.write(
            "INSERT OR IGNORE INTO fed_relay_identity(id,private_key,public_key,created_at) "
            "VALUES(1,?,?,?)",
            (private_bytes, public_bytes, now),
        )
        rows = await self.database.read("SELECT public_key FROM fed_relay_identity WHERE id=1")
        return bytes(rows[0]["public_key"])

    async def _identity(
        self,
    ) -> tuple[Ed25519PrivateKey, bytes, bytes | None, bytes | None]:
        await self.initialize()
        rows = await self.database.read(
            "SELECT private_key,public_key,rotation_from_public_key,rotation_signature "
            "FROM fed_relay_identity WHERE id=1"
        )
        return (
            Ed25519PrivateKey.from_private_bytes(bytes(rows[0]["private_key"])),
            bytes(rows[0]["public_key"]),
            (
                bytes(rows[0]["rotation_from_public_key"])
                if rows[0]["rotation_from_public_key"] is not None
                else None
            ),
            (
                bytes(rows[0]["rotation_signature"])
                if rows[0]["rotation_signature"] is not None
                else None
            ),
        )

    @staticmethod
    def _rotation_message(origin: str, successor_public_key: bytes) -> bytes:
        return ROTATION_CONTEXT + origin.lower().encode("ascii") + successor_public_key

    async def identity_status(self) -> dict[str, Any]:
        await self.initialize()
        rows = await self.database.read(
            "SELECT public_key,rotation_from_public_key,rotated_at,rotated_by "
            "FROM fed_relay_identity WHERE id=1"
        )
        row = rows[0]
        return {
            "fingerprint": self._fingerprint(bytes(row["public_key"])),
            "rotation_from_fingerprint": (
                self._fingerprint(bytes(row["rotation_from_public_key"]))
                if row["rotation_from_public_key"] is not None
                else None
            ),
            "rotated_at": int(row["rotated_at"]) if row["rotated_at"] is not None else None,
            "rotated_by": str(row["rotated_by"]) if row["rotated_by"] is not None else None,
        }

    async def rotate_identity(self, actor: str) -> dict[str, Any]:
        origin = self._local_id()
        previous_private, previous_public, _, _ = await self._identity()
        successor_private = Ed25519PrivateKey.generate()
        successor_public = self._public_bytes(successor_private.public_key())
        signature = previous_private.sign(self._rotation_message(origin, successor_public))
        now = int(self.clock.now().timestamp())
        detail = {
            "origin": origin,
            "previous_fingerprint": self._fingerprint(previous_public),
            "successor_fingerprint": self._fingerprint(successor_public),
        }
        async with self.database.transaction() as transaction:
            await transaction.write(
                "UPDATE fed_relay_identity SET private_key=?,public_key=?,"
                "rotation_from_public_key=?,rotation_signature=?,rotated_at=?,rotated_by=? "
                "WHERE id=1",
                (
                    self._private_bytes(successor_private),
                    successor_public,
                    previous_public,
                    signature,
                    now,
                    actor[:160],
                ),
            )
            await self._event(
                transaction, None, None, "local_origin_key_rotated", detail, now, actor
            )
            await transaction.write(
                "INSERT INTO audit_log(actor_kind,actor_ref,action,target,detail,created_at) "
                "VALUES('web',?,'federation.relay_origin_rotate',?,?,?)",
                (
                    actor[:160],
                    origin,
                    json.dumps(detail, separators=(",", ":"), sort_keys=True),
                    now,
                ),
            )
        return await self.identity_status()

    @staticmethod
    async def _event(
        store: Database | Transaction,
        envelope_id: str | None,
        peer_id: int | None,
        kind: str,
        detail: dict[str, Any],
        now: int,
        actor: str,
    ) -> None:
        await store.write(
            "INSERT INTO fed_relay_event(envelope_id,peer_id,event_kind,detail_json,created_at,"
            "actor) VALUES(?,?,?,?,?,?)",
            (
                envelope_id,
                peer_id,
                kind,
                json.dumps(detail, separators=(",", ":"), sort_keys=True),
                now,
                actor[:160],
            ),
        )

    @staticmethod
    def _policy(row: Any) -> RelayPolicy:
        return RelayPolicy(
            peer_id=int(row["id"]),
            mesh_id=str(row["mesh_id"]),
            enabled=bool(row["enabled"] or 0),
            paused=bool(row["relay_paused"] or 0),
            scopes=json.loads(str(row["scopes_json"] or "[]")),
            max_stored_items=int(row["max_stored_items"] or 50),
            max_stored_bytes=int(row["max_stored_bytes"] or 65_536),
            rate_per_hour=int(row["rate_per_hour"] or 20),
            airtime_seconds_per_hour=float(row["airtime_seconds_per_hour"] or 30),
            updated_at=int(row["updated_at"]) if row["updated_at"] is not None else None,
            updated_by=str(row["updated_by"]) if row["updated_by"] is not None else None,
        )

    async def policy(self, mesh_id: str) -> RelayPolicy:
        rows = await self.database.read(
            "SELECT p.id,p.mesh_id,r.enabled,r.paused relay_paused,r.scopes_json,"
            "r.max_stored_items,r.max_stored_bytes,r.rate_per_hour,"
            "r.airtime_seconds_per_hour,r.updated_at,r.updated_by FROM fed_peer p "
            "LEFT JOIN fed_relay_policy r ON r.peer_id=p.id WHERE p.mesh_id=?",
            (mesh_id,),
        )
        if not rows:
            raise ValueError("federation peer not found")
        return self._policy(rows[0])

    async def policies(self) -> list[RelayPolicy]:
        rows = await self.database.read(
            "SELECT p.id,p.mesh_id,r.enabled,r.paused relay_paused,r.scopes_json,"
            "r.max_stored_items,r.max_stored_bytes,r.rate_per_hour,"
            "r.airtime_seconds_per_hour,r.updated_at,r.updated_by FROM fed_peer p "
            "LEFT JOIN fed_relay_policy r ON r.peer_id=p.id "
            "WHERE p.state IN ('active','paused') ORDER BY p.mesh_id"
        )
        return [self._policy(row) for row in rows]

    async def set_policy(
        self,
        mesh_id: str,
        *,
        enabled: bool,
        paused: bool,
        scopes: list[str],
        max_stored_items: int,
        max_stored_bytes: int,
        rate_per_hour: int,
        airtime_seconds_per_hour: float,
        actor: str,
    ) -> RelayPolicy:
        peer = await self.peers.by_mesh_id(mesh_id)
        if peer.state not in {"active", "paused"}:
            raise ValueError("relay policy requires a paired peer")
        cleaned = sorted(set(scopes))
        if not cleaned or any(scope not in SCOPES for scope in cleaned):
            raise ValueError("relay policy needs one or more supported content scopes")
        if not 1 <= max_stored_items <= 500:
            raise ValueError("relay item quota must be 1-500")
        if not 1_024 <= max_stored_bytes <= 1_048_576:
            raise ValueError("relay byte quota must be 1024-1048576")
        if not 1 <= rate_per_hour <= 200:
            raise ValueError("relay rate must be 1-200 per hour")
        if not 1 <= airtime_seconds_per_hour <= 300:
            raise ValueError("relay airtime quota must be 1-300 seconds per hour")
        now = int(self.clock.now().timestamp())
        detail = {
            "enabled": enabled,
            "paused": paused,
            "scopes": cleaned,
            "max_stored_items": max_stored_items,
            "max_stored_bytes": max_stored_bytes,
            "rate_per_hour": rate_per_hour,
            "airtime_seconds_per_hour": airtime_seconds_per_hour,
        }
        async with self.database.transaction() as transaction:
            await transaction.write(
                "INSERT INTO fed_relay_policy(peer_id,enabled,paused,scopes_json,"
                "max_stored_items,max_stored_bytes,rate_per_hour,airtime_seconds_per_hour,"
                "updated_at,updated_by) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(peer_id) "
                "DO UPDATE SET enabled=excluded.enabled,paused=excluded.paused,"
                "scopes_json=excluded.scopes_json,max_stored_items=excluded.max_stored_items,"
                "max_stored_bytes=excluded.max_stored_bytes,rate_per_hour=excluded.rate_per_hour,"
                "airtime_seconds_per_hour=excluded.airtime_seconds_per_hour,"
                "updated_at=excluded.updated_at,updated_by=excluded.updated_by",
                (
                    peer.id,
                    int(enabled),
                    int(paused),
                    json.dumps(cleaned, separators=(",", ":")),
                    max_stored_items,
                    max_stored_bytes,
                    rate_per_hour,
                    airtime_seconds_per_hour,
                    now,
                    actor[:160],
                ),
            )
            await self._event(transaction, None, peer.id, "policy_updated", detail, now, actor)
            await transaction.write(
                "INSERT INTO audit_log(actor_kind,actor_ref,action,target,detail,created_at) "
                "VALUES('web',?,'federation.relay_policy',?,?,?)",
                (
                    actor[:160],
                    mesh_id,
                    json.dumps(detail, separators=(",", ":"), sort_keys=True),
                    now,
                ),
            )
        return await self.policy(mesh_id)

    @staticmethod
    def _payload_bytes(scope: str, payload: object) -> bytes:
        if scope == "opaque":
            if not isinstance(payload, bytes):
                raise ValueError("opaque relay payload must be bytes")
            encoded = payload
        else:
            if not isinstance(payload, dict):
                raise ValueError("relay payload must be an object")
            encoded = cbor2.dumps(payload, canonical=True)
        if not encoded or len(encoded) > MAX_PAYLOAD_BYTES:
            raise ValueError(f"relay payload must be 1-{MAX_PAYLOAD_BYTES} bytes")
        return encoded

    async def create(
        self,
        destination: str,
        scope: str,
        payload: object,
        *,
        expires_in: int = 86_400,
        hop_limit: int = 3,
        idempotency_key: str | None = None,
        actor: str = "system",
    ) -> str:
        origin = self._local_id()
        destination = destination.lower()
        if not NODE_ID.fullmatch(destination) or destination == origin:
            raise ValueError("relay destination must be another Meshtastic node ID")
        if scope not in SCOPES:
            raise ValueError("unsupported relay content scope")
        if not 60 <= expires_in <= MAX_LIFETIME_SECONDS:
            raise ValueError("relay expiry must be 60 seconds to 7 days")
        if not 1 <= hop_limit <= 4:
            raise ValueError("relay hop limit must be 1-4")
        key = (idempotency_key or secrets.token_hex(16)).strip()
        if not key or len(key) > 64:
            raise ValueError("invalid relay idempotency key")
        payload_bytes = self._payload_bytes(scope, payload)
        now = int(self.clock.now().timestamp())
        core = {
            "origin": origin,
            "destination": destination,
            "scope": scope,
            "idempotency_key": key,
            "created_at": now,
            "expires_at": now + expires_in,
            "hop_limit": hop_limit,
            "payload": payload_bytes,
        }
        encoded = self._core_bytes(core)
        envelope_id = self._envelope_id(encoded)
        private, public, rotation_from, rotation_signature = await self._identity()
        signature = private.sign(encoded)
        try:
            await self.database.write(
                "INSERT INTO fed_relay_envelope(envelope_id,direction,origin_node,"
                "destination_node,scope,idempotency_key,created_at,expires_at,hop_limit,"
                "payload_cbor,payload_bytes,origin_public_key,origin_signature,route_json,state,"
                "rotation_from_public_key,rotation_signature,stored_at,updated_at) "
                "VALUES(?,'origin',?,?,?,?,?,?,?,?,?,?,?,?, 'queued',?,?,?,?)",
                (
                    envelope_id,
                    origin,
                    destination,
                    scope,
                    key,
                    now,
                    now + expires_in,
                    hop_limit,
                    payload_bytes,
                    len(payload_bytes),
                    public,
                    signature,
                    json.dumps([origin], separators=(",", ":")),
                    rotation_from,
                    rotation_signature,
                    now,
                    now,
                ),
            )
        except Exception as error:
            if "UNIQUE constraint failed" in str(error):
                rows = await self.database.read(
                    "SELECT envelope_id FROM fed_relay_envelope WHERE origin_node=? "
                    "AND destination_node=? AND idempotency_key=?",
                    (origin, destination, key),
                )
                if rows:
                    return str(rows[0]["envelope_id"])
            raise
        await self._event(
            self.database,
            envelope_id,
            None,
            "created",
            {"destination": destination, "scope": scope, "hop_limit": hop_limit},
            now,
            actor,
        )
        return envelope_id

    @staticmethod
    def _core_from_wire(value: dict[str, Any]) -> dict[str, Any]:
        keys = set(value)
        if keys not in {
            frozenset(FederationRelayService.WIRE_KEYS),
            frozenset(FederationRelayService.WIRE_KEYS | FederationRelayService.ROTATION_KEYS),
        }:
            raise ValueError("relay envelope has missing or unknown fields")
        return {key: value[key] for key in FederationRelayService.CORE_KEYS}

    async def wire(self, envelope_id: str) -> dict[str, Any]:
        rows = await self.database.read(
            "SELECT * FROM fed_relay_envelope WHERE envelope_id=? AND payload_cbor IS NOT NULL",
            (envelope_id,),
        )
        if not rows:
            raise ValueError("relay envelope not found or payload was purged")
        row = rows[0]
        value = {
            "envelope_id": str(row["envelope_id"]),
            "origin": str(row["origin_node"]),
            "destination": str(row["destination_node"]),
            "scope": str(row["scope"]),
            "idempotency_key": str(row["idempotency_key"]),
            "created_at": int(row["created_at"]),
            "expires_at": int(row["expires_at"]),
            "hop_limit": int(row["hop_limit"]),
            "payload": bytes(row["payload_cbor"]),
            "origin_public_key": bytes(row["origin_public_key"]),
            "origin_signature": bytes(row["origin_signature"]),
            "route": json.loads(str(row["route_json"])),
        }
        if row["rotation_from_public_key"] is not None:
            value["rotation_from_public_key"] = bytes(row["rotation_from_public_key"])
            value["rotation_signature"] = bytes(row["rotation_signature"])
        return value

    async def _reject(self, peer_id: int | None, reason: str, now: int) -> None:
        if peer_id is not None:
            window = now - now % 3600
            await self.database.write(
                "INSERT INTO fed_relay_usage(peer_id,window_start,denied) VALUES(?,?,1) "
                "ON CONFLICT(peer_id,window_start) DO UPDATE SET denied=denied+1",
                (peer_id, window),
            )
        await self._event(
            self.database, None, peer_id, "rejected", {"reason": reason}, now, "federation"
        )

    async def _observe_origin_candidate(
        self,
        transaction: Transaction,
        *,
        origin: str,
        public_key: bytes,
        peer_id: int,
        presented_by: str,
        now: int,
        pinned_fingerprint: str | None,
        pinned_presented_by: str | None,
    ) -> str:
        fingerprint = self._fingerprint(public_key)
        rows = await transaction.read(
            "SELECT state FROM fed_relay_origin_candidate WHERE origin_node=? AND fingerprint=?",
            (origin, fingerprint),
        )
        if rows:
            await transaction.write(
                "UPDATE fed_relay_origin_candidate SET last_observed_from_peer_id=?,"
                "last_seen_at=?,observation_count=observation_count+1 "
                "WHERE origin_node=? AND fingerprint=?",
                (peer_id, now, origin, fingerprint),
            )
            return str(rows[0]["state"])
        await transaction.write(
            "INSERT INTO fed_relay_origin_candidate(origin_node,public_key,fingerprint,state,"
            "first_observed_from_peer_id,last_observed_from_peer_id,first_seen_at,last_seen_at) "
            "VALUES(?,?,?,'observed',?,?,?,?)",
            (origin, public_key, fingerprint, peer_id, peer_id, now, now),
        )
        detail = {
            "origin": origin,
            "pinned_fingerprint": pinned_fingerprint,
            "pinned_presented_by": pinned_presented_by,
            "candidate_fingerprint": fingerprint,
            "candidate_presented_by": presented_by,
        }
        await self._event(
            transaction, None, peer_id, "origin_key_candidate", detail, now, "federation"
        )
        body = (
            f"Origin {origin} presented key {fingerprint} through peer {presented_by}. "
            + (
                f"The current pin is {pinned_fingerprint}, first presented by "
                f"{pinned_presented_by or 'an unavailable peer'}. "
                if pinned_fingerprint
                else "No authoritative direct-origin key is pinned. "
            )
            + "The envelope is quarantined. Verify the fingerprints out of band, then "
            "replace or reject the candidate in Federation."
        )
        await transaction.write(
            "INSERT OR IGNORE INTO mail(uid,from_label,to_label,subject,body,created_at,state,"
            "expires_at,conversation_key,message_kind,mail_direction,participant_handle,"
            "operator_actor) VALUES(?,?,?,?,?,?,'delivered',?,?,'system','local','outpost',"
            "'system:relay')",
            (
                f"relay-origin-key:{origin}:{fingerprint}",
                "outpost",
                "operator",
                "Federation origin key needs review",
                body,
                now,
                now + 30 * 86_400,
                f"system:relay-origin-key:{origin}:{fingerprint}",
            ),
        )
        return "observed"

    async def _promote_origin_envelopes(
        self, transaction: Transaction, origin: str, public_key: bytes, now: int
    ) -> None:
        await transaction.write(
            "UPDATE fed_relay_envelope SET state=CASE WHEN destination_node=? "
            "THEN 'delivered' ELSE 'queued' END,updated_at=?,last_error=NULL "
            "WHERE origin_node=? AND origin_public_key=? AND state='quarantined' "
            "AND expires_at>?",
            (self._local_id(), now, origin, public_key, now),
        )
        await transaction.write(
            "UPDATE fed_relay_origin_candidate SET state='trusted',"
            "reviewed_at=COALESCE(reviewed_at,?),"
            "reviewed_by=COALESCE(reviewed_by,'cryptographic-or-direct-proof') "
            "WHERE origin_node=? AND public_key=?",
            (now, origin, public_key),
        )
        await transaction.write(
            "UPDATE mail SET operator_read_at=COALESCE(operator_read_at,?),archived_at=? "
            "WHERE conversation_key=?",
            (
                now,
                now,
                f"system:relay-origin-key:{origin}:{self._fingerprint(public_key)}",
            ),
        )

    async def accept(
        self,
        sender_mesh_id: str,
        value: dict[str, Any],
        *,
        now: int | None = None,
        transport: str = "radio",
    ) -> tuple[str, str]:
        stamp = int(self.clock.now().timestamp()) if now is None else now
        peer = None
        try:
            if transport not in {"radio", "mqtt"}:
                raise ValueError("invalid relay transport")
            peer = await self.peers.by_mesh_id(sender_mesh_id)
            policy = await self.policy(sender_mesh_id)
            if peer.state != "active" or not policy.enabled or policy.paused:
                raise ValueError("relay is not enabled for this peer")
            core = self._core_from_wire(value)
            origin = str(core["origin"]).lower()
            destination = str(core["destination"]).lower()
            scope = str(core["scope"])
            key = str(core["idempotency_key"])
            created_at = wire_int(core["created_at"], "created_at")
            expires_at = wire_int(core["expires_at"], "expires_at")
            hop_limit = wire_int(core["hop_limit"], "hop_limit", minimum=1, maximum=4)
            payload = wire_bytes(core["payload"], "payload")
            if not isinstance(value["route"], list):
                raise ValueError("relay route must be a list")
            route = [str(node).lower() for node in value["route"]]
            if not NODE_ID.fullmatch(origin) or not NODE_ID.fullmatch(destination):
                raise ValueError("invalid relay origin or destination")
            if scope not in policy.scopes or scope not in SCOPES:
                raise ValueError("relay content scope is not allowed for this peer")
            if not key or len(key) > 64:
                raise ValueError("invalid relay idempotency key")
            if len(payload) < 1 or len(payload) > MAX_PAYLOAD_BYTES:
                raise ValueError("relay payload exceeds the per-envelope limit")
            if (
                created_at > stamp + CLOCK_SKEW_SECONDS
                or expires_at <= stamp
                or expires_at <= created_at
                or expires_at - created_at > MAX_LIFETIME_SECONDS
            ):
                raise ValueError("invalid or expired relay timestamps")
            if not 1 <= hop_limit <= 4 or not route or len(route) > hop_limit:
                raise ValueError("relay hop limit is exhausted")
            if route[0] != origin or route[-1] != sender_mesh_id.lower():
                raise ValueError("relay custody route does not match sender")
            local_id = self._local_id()
            if local_id in route or len(set(route)) != len(route):
                raise ValueError("relay loop detected")
            if destination != local_id and len(route) >= hop_limit:
                raise ValueError("relay hop limit is exhausted before destination")
            public_key = wire_bytes(value["origin_public_key"], "origin_public_key", length=32)
            signature = wire_bytes(value["origin_signature"], "origin_signature", length=64)
            rotation_from: bytes | None = None
            rotation_signature: bytes | None = None
            if self.ROTATION_KEYS <= set(value):
                rotation_from = wire_bytes(
                    value["rotation_from_public_key"],
                    "rotation_from_public_key",
                    length=32,
                )
                rotation_signature = wire_bytes(
                    value["rotation_signature"], "rotation_signature", length=64
                )
                Ed25519PublicKey.from_public_bytes(rotation_from).verify(
                    rotation_signature, self._rotation_message(origin, public_key)
                )
            core_bytes = self._core_bytes(core)
            envelope_id = self._envelope_id(core_bytes)
            if str(value["envelope_id"]) != envelope_id:
                raise ValueError("relay envelope identity mismatch")
            Ed25519PublicKey.from_public_bytes(public_key).verify(signature, core_bytes)
        except (InvalidSignature, KeyError, OverflowError, TypeError, ValueError) as error:
            reason = (
                "relay origin signature or rotation proof failed"
                if isinstance(error, InvalidSignature)
                else str(error)
            )
            peer_id = peer.id if peer is not None else None
            await self._reject(peer_id, reason, stamp)
            raise ValueError(reason) from error

        direction = "destination" if destination == local_id else "relay"
        state = "delivered" if direction == "destination" else "queued"
        route.append(local_id)
        denial: str | None = None
        duplicate_state: str | None = None
        async with self.database.transaction() as transaction:
            duplicate = await transaction.read(
                "SELECT state FROM fed_relay_envelope WHERE envelope_id=?", (envelope_id,)
            )
            if duplicate:
                duplicate_state = str(duplicate[0]["state"])
            else:
                conflict = await transaction.read(
                    "SELECT envelope_id FROM fed_relay_envelope WHERE origin_node=? "
                    "AND destination_node=? AND idempotency_key=?",
                    (origin, destination, key),
                )
                origin_keys = await transaction.read(
                    "SELECT k.public_key,k.fingerprint,k.state,k.observed_from_peer_id,"
                    "p.mesh_id observed_from FROM fed_relay_origin_key k LEFT JOIN fed_peer p "
                    "ON p.id=k.observed_from_peer_id WHERE k.origin_node=?",
                    (origin,),
                )
                usage = await transaction.read(
                    "SELECT accepted,forwarded FROM fed_relay_usage WHERE peer_id=? "
                    "AND window_start=?",
                    (peer.id, stamp - stamp % 3600),
                )
                used_rate = int(usage[0]["accepted"] + usage[0]["forwarded"]) if usage else 0
                storage = await transaction.read(
                    "SELECT COUNT(*) count,COALESCE(SUM(payload_bytes),0) bytes "
                    "FROM fed_relay_envelope WHERE received_from_peer_id=? "
                    "AND payload_cbor IS NOT NULL AND expires_at>?",
                    (peer.id, stamp),
                )
                if conflict:
                    denial = "relay idempotency identity conflicts with retained envelope"
                elif used_rate >= policy.rate_per_hour:
                    denial = "relay peer hourly rate exceeded"
                elif int(storage[0]["count"]) >= policy.max_stored_items:
                    denial = "relay peer item storage quota exceeded"
                elif int(storage[0]["bytes"]) + len(payload) > policy.max_stored_bytes:
                    denial = "relay peer byte storage quota exceeded"
                else:
                    direct_origin = sender_mesh_id.lower() == origin
                    if origin_keys:
                        pinned = origin_keys[0]
                        pinned_key = bytes(pinned["public_key"])
                        pinned_state = str(pinned["state"])
                        if pinned_key == public_key:
                            if pinned_state == "rejected":
                                denial = "relay origin key is rejected"
                            elif direct_origin:
                                await transaction.write(
                                    "UPDATE fed_relay_origin_key SET state='trusted',"
                                    "observed_from_peer_id=?,reviewed_at=?,"
                                    "reviewed_by='direct-origin-proof' WHERE origin_node=?",
                                    (peer.id, stamp, origin),
                                )
                                await self._promote_origin_envelopes(
                                    transaction, origin, public_key, stamp
                                )
                            elif pinned_state != "trusted":
                                state = "quarantined"
                        elif (
                            pinned_state == "trusted"
                            and rotation_from == pinned_key
                            and rotation_signature is not None
                        ):
                            previous_fingerprint = str(pinned["fingerprint"])
                            successor_fingerprint = self._fingerprint(public_key)
                            await transaction.write(
                                "UPDATE fed_relay_origin_key SET public_key=?,fingerprint=?,"
                                "state='trusted',observed_from_peer_id=?,reviewed_at=?,"
                                "reviewed_by='signed-key-rotation' WHERE origin_node=?",
                                (public_key, successor_fingerprint, peer.id, stamp, origin),
                            )
                            await self._promote_origin_envelopes(
                                transaction, origin, public_key, stamp
                            )
                            await self._event(
                                transaction,
                                envelope_id,
                                peer.id,
                                "origin_key_rotated",
                                {
                                    "origin": origin,
                                    "previous_fingerprint": previous_fingerprint,
                                    "successor_fingerprint": successor_fingerprint,
                                    "presented_by": sender_mesh_id.lower(),
                                },
                                stamp,
                                "federation",
                            )
                        else:
                            candidate_state = await self._observe_origin_candidate(
                                transaction,
                                origin=origin,
                                public_key=public_key,
                                peer_id=peer.id,
                                presented_by=sender_mesh_id.lower(),
                                now=stamp,
                                pinned_fingerprint=str(pinned["fingerprint"]),
                                pinned_presented_by=(
                                    str(pinned["observed_from"])
                                    if pinned["observed_from"] is not None
                                    else None
                                ),
                            )
                            if candidate_state == "rejected":
                                denial = "relay origin key candidate is rejected"
                            else:
                                state = "quarantined"
                    else:
                        candidate = await transaction.read(
                            "SELECT state FROM fed_relay_origin_candidate "
                            "WHERE origin_node=? AND fingerprint=?",
                            (origin, self._fingerprint(public_key)),
                        )
                        if direct_origin and not (
                            candidate and str(candidate[0]["state"]) == "rejected"
                        ):
                            await transaction.write(
                                "INSERT INTO fed_relay_origin_key(origin_node,public_key,"
                                "fingerprint,state,observed_from_peer_id,first_seen_at,"
                                "reviewed_at,reviewed_by) VALUES(?,?,?,'trusted',?,?,?,?)",
                                (
                                    origin,
                                    public_key,
                                    self._fingerprint(public_key),
                                    peer.id,
                                    stamp,
                                    stamp,
                                    "direct-origin-proof",
                                ),
                            )
                            await self._promote_origin_envelopes(
                                transaction, origin, public_key, stamp
                            )
                        elif direct_origin:
                            denial = "relay origin key candidate is rejected"
                        else:
                            candidate_state = await self._observe_origin_candidate(
                                transaction,
                                origin=origin,
                                public_key=public_key,
                                peer_id=peer.id,
                                presented_by=sender_mesh_id.lower(),
                                now=stamp,
                                pinned_fingerprint=None,
                                pinned_presented_by=None,
                            )
                            if candidate_state == "rejected":
                                denial = "relay origin key candidate is rejected"
                            else:
                                state = "quarantined"
                if denial is None:
                    await transaction.write(
                        "INSERT INTO fed_relay_envelope(envelope_id,direction,origin_node,"
                        "destination_node,scope,idempotency_key,created_at,expires_at,hop_limit,"
                        "payload_cbor,payload_bytes,origin_public_key,origin_signature,route_json,"
                        "state,received_from_peer_id,received_transport,"
                        "rotation_from_public_key,rotation_signature,stored_at,updated_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            envelope_id,
                            direction,
                            origin,
                            destination,
                            scope,
                            key,
                            created_at,
                            expires_at,
                            hop_limit,
                            payload,
                            len(payload),
                            public_key,
                            signature,
                            json.dumps(route, separators=(",", ":")),
                            state,
                            peer.id,
                            transport,
                            rotation_from,
                            rotation_signature,
                            stamp,
                            stamp,
                        ),
                    )
                    await transaction.write(
                        "INSERT INTO fed_relay_usage(peer_id,window_start,accepted,bytes_stored) "
                        "VALUES(?,?,1,?) ON CONFLICT(peer_id,window_start) DO UPDATE SET "
                        "accepted=accepted+1,bytes_stored=bytes_stored+excluded.bytes_stored",
                        (peer.id, stamp - stamp % 3600, len(payload)),
                    )
                    await self._event(
                        transaction,
                        envelope_id,
                        peer.id,
                        state,
                        {
                            "origin": origin,
                            "destination": destination,
                            "route": route,
                            "transport": transport,
                        },
                        stamp,
                        "federation",
                    )
                else:
                    await transaction.write(
                        "INSERT INTO fed_relay_usage(peer_id,window_start,denied) VALUES(?,?,1) "
                        "ON CONFLICT(peer_id,window_start) DO UPDATE SET denied=denied+1",
                        (peer.id, stamp - stamp % 3600),
                    )
                    await self._event(
                        transaction,
                        envelope_id,
                        peer.id,
                        "rejected",
                        {"reason": denial},
                        stamp,
                        "federation",
                    )
        if duplicate_state is not None:
            return envelope_id, duplicate_state
        if denial is not None:
            raise ValueError(denial)
        return envelope_id, state

    async def queue(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = await self.database.read(
            "SELECT e.*,p.mesh_id received_from FROM fed_relay_envelope e "
            "LEFT JOIN fed_peer p ON p.id=e.received_from_peer_id "
            "ORDER BY e.updated_at DESC,e.envelope_id LIMIT ?",
            (max(1, min(limit, 500)),),
        )
        values: list[dict[str, Any]] = []
        for row in rows:
            value = dict(row)
            value.pop("payload_cbor", None)
            value.pop("origin_public_key", None)
            value.pop("origin_signature", None)
            value.pop("rotation_from_public_key", None)
            value.pop("rotation_signature", None)
            value["route"] = json.loads(str(value.pop("route_json")))
            values.append(value)
        return values

    async def origins(self) -> list[dict[str, Any]]:
        pins = await self.database.read(
            "SELECT k.origin_node,k.fingerprint,k.state,k.first_seen_at,k.reviewed_at,"
            "k.reviewed_by,p.mesh_id observed_from FROM fed_relay_origin_key k "
            "LEFT JOIN fed_peer p ON p.id=k.observed_from_peer_id"
        )
        candidates = await self.database.read(
            "SELECT c.origin_node,c.fingerprint,c.state,c.first_seen_at,c.last_seen_at,"
            "c.observation_count,c.reviewed_at,c.reviewed_by,"
            "first_peer.mesh_id first_observed_from,last_peer.mesh_id last_observed_from "
            "FROM fed_relay_origin_candidate c "
            "LEFT JOIN fed_peer first_peer ON first_peer.id=c.first_observed_from_peer_id "
            "LEFT JOIN fed_peer last_peer ON last_peer.id=c.last_observed_from_peer_id "
            "ORDER BY c.last_seen_at DESC"
        )
        by_origin: dict[str, list[dict[str, Any]]] = {}
        for row in candidates:
            value = dict(row)
            by_origin.setdefault(str(value.pop("origin_node")), []).append(value)
        values: dict[str, dict[str, Any]] = {}
        for row in pins:
            value = dict(row)
            value["candidates"] = by_origin.pop(str(value["origin_node"]), [])
            values[str(value["origin_node"])] = value
        for origin, observed in by_origin.items():
            values[origin] = {
                "origin_node": origin,
                "fingerprint": None,
                "state": "unverified",
                "first_seen_at": min(int(item["first_seen_at"]) for item in observed),
                "reviewed_at": None,
                "reviewed_by": None,
                "observed_from": None,
                "candidates": observed,
            }
        return sorted(
            values.values(),
            key=lambda value: max(
                [int(value["first_seen_at"])]
                + [int(item["last_seen_at"]) for item in value["candidates"]]
            ),
            reverse=True,
        )

    async def review_origin(
        self, origin_node: str, state: str, actor: str, *, fingerprint: str | None = None
    ) -> None:
        if state not in {"trusted", "rejected", "forget", "replace", "reject_candidate"}:
            raise ValueError("unsupported relay origin review action")
        if not NODE_ID.fullmatch(origin_node):
            raise ValueError("invalid relay origin node ID")
        if state in {"replace", "reject_candidate"} and not fingerprint:
            raise ValueError("candidate fingerprint is required")
        now = int(self.clock.now().timestamp())
        async with self.database.transaction() as transaction:
            pins = await transaction.read(
                "SELECT public_key,fingerprint,state FROM fed_relay_origin_key WHERE origin_node=?",
                (origin_node,),
            )
            detail: dict[str, Any] = {"origin": origin_node, "state": state}
            if state in {"trusted", "rejected"}:
                if not pins:
                    raise ValueError("relay origin key not found")
                await transaction.write(
                    "UPDATE fed_relay_origin_key SET state=?,reviewed_at=?,reviewed_by=? "
                    "WHERE origin_node=?",
                    (state, now, actor[:160], origin_node),
                )
                pinned_key = bytes(pins[0]["public_key"])
                detail["fingerprint"] = str(pins[0]["fingerprint"])
                if state == "trusted":
                    await self._promote_origin_envelopes(transaction, origin_node, pinned_key, now)
                else:
                    await transaction.write(
                        "UPDATE fed_relay_envelope SET state='rejected',updated_at=?,"
                        "last_error='origin rejected by operator' WHERE origin_node=? "
                        "AND origin_public_key=? AND state='quarantined'",
                        (now, origin_node, pinned_key),
                    )
            elif state == "forget":
                if not pins:
                    raise ValueError("relay origin key not found")
                detail["forgotten_fingerprint"] = str(pins[0]["fingerprint"])
                await transaction.write(
                    "DELETE FROM fed_relay_origin_key WHERE origin_node=?", (origin_node,)
                )
            else:
                candidates = await transaction.read(
                    "SELECT public_key,fingerprint,state,last_observed_from_peer_id,first_seen_at "
                    "FROM fed_relay_origin_candidate WHERE origin_node=? AND fingerprint=?",
                    (origin_node, fingerprint),
                )
                if not candidates:
                    raise ValueError("relay origin key candidate not found")
                candidate = candidates[0]
                candidate_key = bytes(candidate["public_key"])
                detail["candidate_fingerprint"] = str(candidate["fingerprint"])
                if state == "replace":
                    detail["replaced_fingerprint"] = str(pins[0]["fingerprint"]) if pins else None
                    await transaction.write(
                        "INSERT INTO fed_relay_origin_key(origin_node,public_key,fingerprint,"
                        "state,observed_from_peer_id,first_seen_at,reviewed_at,reviewed_by) "
                        "VALUES(?,?,?,'trusted',?,?,?,?) ON CONFLICT(origin_node) DO UPDATE SET "
                        "public_key=excluded.public_key,fingerprint=excluded.fingerprint,"
                        "state='trusted',observed_from_peer_id=excluded.observed_from_peer_id,"
                        "reviewed_at=excluded.reviewed_at,reviewed_by=excluded.reviewed_by",
                        (
                            origin_node,
                            candidate_key,
                            str(candidate["fingerprint"]),
                            candidate["last_observed_from_peer_id"],
                            int(candidate["first_seen_at"]),
                            now,
                            actor[:160],
                        ),
                    )
                    await transaction.write(
                        "UPDATE fed_relay_origin_candidate SET state='trusted',reviewed_at=?,"
                        "reviewed_by=? WHERE origin_node=? AND fingerprint=?",
                        (now, actor[:160], origin_node, fingerprint),
                    )
                    await self._promote_origin_envelopes(
                        transaction, origin_node, candidate_key, now
                    )
                else:
                    await transaction.write(
                        "UPDATE fed_relay_origin_candidate SET state='rejected',reviewed_at=?,"
                        "reviewed_by=? WHERE origin_node=? AND fingerprint=?",
                        (now, actor[:160], origin_node, fingerprint),
                    )
                    await transaction.write(
                        "UPDATE fed_relay_envelope SET state='rejected',updated_at=?,"
                        "last_error='origin key candidate rejected by operator' "
                        "WHERE origin_node=? AND origin_public_key=? AND state='quarantined'",
                        (now, origin_node, candidate_key),
                    )
                await transaction.write(
                    "UPDATE mail SET operator_read_at=COALESCE(operator_read_at,?),archived_at=? "
                    "WHERE conversation_key=?",
                    (
                        now,
                        now,
                        f"system:relay-origin-key:{origin_node}:{fingerprint}",
                    ),
                )
            await self._event(
                transaction,
                None,
                None,
                "origin_reviewed",
                detail,
                now,
                actor,
            )
            await transaction.write(
                "INSERT INTO audit_log(actor_kind,actor_ref,action,target,detail,created_at) "
                "VALUES('web',?,'federation.relay_origin_review',?,?,?)",
                (
                    actor[:160],
                    origin_node,
                    json.dumps(detail, separators=(",", ":"), sort_keys=True),
                    now,
                ),
            )

    async def item_action(self, envelope_id: str, action: str, actor: str) -> None:
        if action not in {"pause", "resume", "purge"}:
            raise ValueError("unsupported relay queue action")
        now = int(self.clock.now().timestamp())
        rows = await self.database.read(
            "SELECT state,expires_at FROM fed_relay_envelope WHERE envelope_id=?", (envelope_id,)
        )
        if not rows:
            raise ValueError("relay envelope not found")
        if action == "pause" and str(rows[0]["state"]) not in {"queued", "forwarding"}:
            raise ValueError("only queued relay envelopes can be paused")
        if action == "resume" and str(rows[0]["state"]) != "paused":
            raise ValueError("relay envelope is not paused")
        if action == "resume" and int(rows[0]["expires_at"]) <= now:
            raise ValueError("expired relay envelope cannot resume")
        if action == "purge":
            sql = (
                "UPDATE fed_relay_envelope SET state='purged',payload_cbor=NULL,payload_bytes=0,"
                "updated_at=?,last_error='payload purged by operator' WHERE envelope_id=?"
            )
            params: tuple[object, ...] = (now, envelope_id)
        else:
            state = "paused" if action == "pause" else "queued"
            sql = (
                "UPDATE fed_relay_envelope SET state=?,updated_at=?,last_error=NULL "
                "WHERE envelope_id=?"
            )
            params = (state, now, envelope_id)
        async with self.database.transaction() as transaction:
            await transaction.write(sql, params)
            await self._event(transaction, envelope_id, None, action, {}, now, actor)
            await transaction.write(
                "INSERT INTO audit_log(actor_kind,actor_ref,action,target,detail,created_at) "
                "VALUES('web',?,'federation.relay_queue',?,?,?)",
                (actor[:160], envelope_id, action, now),
            )

    async def next_hop(self, envelope_id: str, *, now: int | None = None) -> dict[str, str] | None:
        stamp = int(self.clock.now().timestamp()) if now is None else now
        rows = await self.database.read(
            "SELECT destination_node,scope,route_json,state,expires_at FROM fed_relay_envelope "
            "WHERE envelope_id=?",
            (envelope_id,),
        )
        if not rows or str(rows[0]["state"]) != "queued" or int(rows[0]["expires_at"]) <= stamp:
            return None
        route = set(json.loads(str(rows[0]["route_json"])))
        policies = await self.database.read(
            "SELECT p.mesh_id,p.last_seen_at,r.scopes_json FROM fed_peer p "
            "JOIN fed_relay_policy r ON r.peer_id=p.id "
            "WHERE p.state='active' AND r.enabled=1 AND r.paused=0 "
            "ORDER BY (p.mesh_id=?) DESC,p.last_seen_at DESC,p.mesh_id",
            (rows[0]["destination_node"],),
        )
        for policy in policies:
            mesh_id = str(policy["mesh_id"])
            if mesh_id not in route and str(rows[0]["scope"]) in json.loads(policy["scopes_json"]):
                return {
                    "mesh_id": mesh_id,
                    "path": "direct" if mesh_id == rows[0]["destination_node"] else "relay",
                }
        return None

    async def reserve_forward(
        self,
        envelope_id: str,
        next_hop: str,
        airtime_seconds: float,
        *,
        now: int | None = None,
        path: str | None = None,
    ) -> None:
        stamp = int(self.clock.now().timestamp()) if now is None else now
        policy = await self.policy(next_hop)
        if not policy.enabled or policy.paused:
            raise ValueError("next-hop relay policy is disabled or paused")
        rows = await self.database.read(
            "SELECT scope,state,destination_node FROM fed_relay_envelope WHERE envelope_id=?",
            (envelope_id,),
        )
        if not rows or str(rows[0]["state"]) != "queued":
            raise ValueError("relay envelope is not queued")
        if str(rows[0]["scope"]) not in policy.scopes:
            raise ValueError("next-hop relay scope is not allowed")
        selected_path = path or (
            "direct" if next_hop == str(rows[0]["destination_node"]) else "relay"
        )
        if selected_path not in {"direct", "relay"}:
            raise ValueError("invalid relay delivery path")
        window = stamp - stamp % 3600
        usage = await self.database.read(
            "SELECT accepted,forwarded,airtime_seconds FROM fed_relay_usage "
            "WHERE peer_id=? AND window_start=?",
            (policy.peer_id, window),
        )
        used_rate = int(usage[0]["accepted"] + usage[0]["forwarded"]) if usage else 0
        used_airtime = float(usage[0]["airtime_seconds"]) if usage else 0.0
        if used_rate >= policy.rate_per_hour:
            raise ValueError("next-hop relay hourly rate exceeded")
        if used_airtime + airtime_seconds > policy.airtime_seconds_per_hour:
            raise ValueError("next-hop relay airtime quota exceeded")
        async with self.database.transaction() as transaction:
            await transaction.write(
                "INSERT INTO fed_relay_usage(peer_id,window_start,forwarded,airtime_seconds) "
                "VALUES(?,?,1,?) ON CONFLICT(peer_id,window_start) DO UPDATE SET "
                "forwarded=forwarded+1,airtime_seconds=airtime_seconds+excluded.airtime_seconds",
                (policy.peer_id, window, airtime_seconds),
            )
            await transaction.write(
                "UPDATE fed_relay_envelope SET state='forwarding',next_hop_mesh_id=?,last_path=?,"
                "attempts=attempts+1,last_attempt_at=?,updated_at=?,last_error=NULL "
                "WHERE envelope_id=? AND state='queued'",
                (next_hop, selected_path, stamp, stamp, envelope_id),
            )
            await self._event(
                transaction,
                envelope_id,
                policy.peer_id,
                "forwarding",
                {"next_hop": next_hop, "path": selected_path},
                stamp,
                "system",
            )

    async def mark_failed(self, envelope_id: str, error: str) -> None:
        now = int(self.clock.now().timestamp())
        await self.database.write(
            "UPDATE fed_relay_envelope SET state='queued',updated_at=?,last_error=? "
            "WHERE envelope_id=? AND state IN ('queued','forwarding')",
            (now, error[:160], envelope_id),
        )

    async def acknowledge(self, sender_mesh_id: str, envelope_id: str, state: str) -> str | None:
        if state not in {"queued", "quarantined", "forwarded", "delivered"}:
            raise ValueError("invalid relay acknowledgement state")
        now = int(self.clock.now().timestamp())
        rows = await self.database.read(
            "SELECT e.received_from_peer_id,e.next_hop_mesh_id,p.mesh_id previous_hop "
            "FROM fed_relay_envelope e LEFT JOIN fed_peer p ON p.id=e.received_from_peer_id "
            "WHERE e.envelope_id=?",
            (envelope_id,),
        )
        if not rows or str(rows[0]["next_hop_mesh_id"] or "") != sender_mesh_id:
            raise ValueError("relay acknowledgement does not match the selected next hop")
        local_state = "delivered" if state == "delivered" else "forwarded"
        await self.database.write(
            "UPDATE fed_relay_envelope SET state=?,updated_at=?,last_error=NULL "
            "WHERE envelope_id=?",
            (local_state, now, envelope_id),
        )
        await self._event(
            self.database,
            envelope_id,
            None,
            "delivery_acknowledged" if state == "delivered" else "custody_acknowledged",
            {"from": sender_mesh_id, "state": state},
            now,
            "federation",
        )
        return (
            str(rows[0]["previous_hop"])
            if state == "delivered" and rows[0]["previous_hop"]
            else None
        )

    async def pending_receipts(self, limit: int = 20) -> list[dict[str, str]]:
        rows = await self.database.read(
            "SELECT e.envelope_id,p.mesh_id previous_hop FROM fed_relay_envelope e "
            "JOIN fed_peer p ON p.id=e.received_from_peer_id "
            "WHERE e.state='delivered' AND e.receipt_sent_at IS NULL "
            "ORDER BY e.updated_at,e.envelope_id LIMIT ?",
            (max(1, min(limit, 100)),),
        )
        return [
            {"envelope_id": str(row["envelope_id"]), "previous_hop": str(row["previous_hop"])}
            for row in rows
        ]

    async def mark_receipt_sent(self, envelope_id: str) -> None:
        now = int(self.clock.now().timestamp())
        await self.database.write(
            "UPDATE fed_relay_envelope SET receipt_sent_at=?,updated_at=? "
            "WHERE envelope_id=? AND state='delivered'",
            (now, now, envelope_id),
        )

    async def recover_stalled(self, *, now: int | None = None, after: int = 300) -> int:
        stamp = int(self.clock.now().timestamp()) if now is None else now
        rows = await self.database.read(
            "SELECT envelope_id FROM fed_relay_envelope WHERE state='forwarding' "
            "AND last_attempt_at<=? AND expires_at>?",
            (stamp - max(30, after), stamp),
        )
        for row in rows:
            await self.database.write(
                "UPDATE fed_relay_envelope SET state='queued',updated_at=?,"
                "last_error='custody acknowledgement timed out' "
                "WHERE envelope_id=? AND state='forwarding'",
                (stamp, row["envelope_id"]),
            )
        return len(rows)

    async def expire(self, *, now: int | None = None) -> int:
        stamp = int(self.clock.now().timestamp()) if now is None else now
        rows = await self.database.read(
            "SELECT envelope_id FROM fed_relay_envelope WHERE expires_at<=? "
            "AND state NOT IN ('expired','purged')",
            (stamp,),
        )
        for row in rows:
            async with self.database.transaction() as transaction:
                await transaction.write(
                    "UPDATE fed_relay_envelope SET state='expired',payload_cbor=NULL,"
                    "payload_bytes=0,updated_at=? WHERE envelope_id=?",
                    (stamp, row["envelope_id"]),
                )
                await self._event(
                    transaction,
                    str(row["envelope_id"]),
                    None,
                    "expired",
                    {"payload_purged": True},
                    stamp,
                    "system",
                )
        return len(rows)

    async def summary(self) -> dict[str, Any]:
        counts = await self.database.read(
            "SELECT state,COUNT(*) count,COALESCE(SUM(payload_bytes),0) bytes "
            "FROM fed_relay_envelope GROUP BY state"
        )
        events = await self.database.read(
            "SELECT event_kind,detail_json,created_at,actor FROM fed_relay_event "
            "ORDER BY created_at DESC,id DESC LIMIT 20"
        )
        return {
            "counts": {str(row["state"]): int(row["count"]) for row in counts},
            "stored_bytes": sum(int(row["bytes"]) for row in counts),
            "events": [
                {
                    "event_kind": row["event_kind"],
                    "detail": json.loads(str(row["detail_json"])),
                    "created_at": row["created_at"],
                    "actor": row["actor"],
                }
                for row in events
            ],
        }
