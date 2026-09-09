"""Explicitly trusted, bounded peer checks of the station's running UTC clock.

This is a control protocol, not NTP or a clock setter. Only the governor sends
frames. UTC-independent freshness permits bootstrap without releasing other work.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from outpost.audit import write_audit
from outpost.clock import Clock
from outpost.fed.framing import FrameCodec, MessageType, wire_bytes, wire_int
from outpost.fed.peers import FederationPeerService
from outpost.store.database import Transaction
from outpost.timekeeping import HOLDOVER_SECONDS, PeerTimeEvidence, TimeStatus, time_status
from outpost.transport.governor import AirtimeGovernor, OutboundItem
from outpost.transport.models import TrafficClass

CAPABILITY = "time_v1"
GUARD = "federation-time-v1"
PREFIX = "federation.time."
DEADLINE = 30.0


@dataclass(frozen=True)
class Policy:
    credential: str
    trust: bool
    serve: bool


@dataclass(frozen=True)
class Challenge:
    nonce: bytes
    started: float
    credential: str
    generation: int


@dataclass(frozen=True)
class Transmission:
    peer: str
    credential: str
    generation: int
    payload: bytes
    deadline: float
    response: bool
    channel: int
    port: int


class FederationTime:
    def __init__(
        self,
        peers: FederationPeerService,
        clock: Clock,
        governor: AirtimeGovernor,
        codec: FrameCodec,
        identity: Callable[[], str],
        channel: Callable[[], int],
        port: int,
        enabled: Callable[[], bool],
    ) -> None:
        self.peers, self.clock, self.governor = peers, clock, governor
        self.database, self.codec = peers.database, codec
        self.identity, self.channel, self.port, self.enabled = identity, channel, port, enabled
        self.evidence = PeerTimeEvidence()
        self.evidence.enabled = enabled
        setattr(clock, "_peer_time_evidence", self.evidence)  # noqa: B010
        self.policies: dict[str, Policy] = {}
        self.pending: dict[str, Challenge] = {}
        self.outbound: dict[str, Transmission] = {}
        self.results: dict[str, dict[str, object]] = {}
        self.generations: dict[str, int] = {}
        self._next_request: dict[str, float] = {}
        self._next_response: dict[str, float] = {}
        self._next_global_request = 0.0
        self._next_global_response = 0.0
        self._lock = asyncio.Lock()
        self._load_lock = asyncio.Lock()
        self._loaded = False
        peers.trust_listeners.append(self.invalidate)
        governor.time_recovery_guard = self.owns
        if governor.outbox is not None:
            governor.outbox.attempt_guards[GUARD] = self.dispatch_guard

    def invalidate(self, peer: str) -> None:
        self.generations[peer] = self.generations.get(peer, 0) + 1
        self.pending.pop(peer, None)
        self.evidence.revoke(peer)
        for uid, tx in tuple(self.outbound.items()):
            if tx.peer == peer:
                self.outbound.pop(uid)
        self.results.pop(peer, None)

    async def load(self) -> None:
        async with self._load_lock:
            await self._load()

    async def _load(self) -> None:
        if self._loaded:
            return
        rows = await self.database.read(
            "SELECT key,value FROM runtime_setting WHERE key LIKE 'federation.time.%'"
        )
        for row in rows:
            value = json.loads(row["value"])
            self.policies[str(row["key"])[len(PREFIX) :]] = Policy(**value)
        self._loaded = True

    async def configure(self, peer: str, *, trust: bool, serve: bool, actor: str) -> None:
        async with self._lock:
            await self.load()
            if (trust or serve) and not self.enabled():
                raise ValueError(
                    "Federation time is unavailable while federation or recovery is held"
                )
            if peer not in self.policies and len(self.policies) >= 4 and (trust or serve):
                raise ValueError("At most four peers may have time permissions")
            secret = await self.peers.secret(peer) if trust or serve else b""
            policy = Policy(hashlib.sha256(secret).hexdigest(), trust, serve)
            self.invalidate(peer)
            async with self.database.transaction() as tx:
                if trust or serve:
                    if await self.peers.secret(peer, transaction=tx) != secret:
                        raise ValueError("Peer pairing changed while saving time permission")
                    await tx.write(
                        "INSERT INTO runtime_setting(key,value,updated_at) VALUES(?,?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
                        "updated_at=excluded.updated_at",
                        (
                            PREFIX + peer,
                            json.dumps(vars(policy)),
                            int(self.clock.now().timestamp()),
                        ),
                    )
                else:
                    await tx.write("DELETE FROM runtime_setting WHERE key=?", (PREFIX + peer,))
                await write_audit(
                    tx,
                    actor_kind="web",
                    actor_ref=actor,
                    action="federation.time_policy",
                    target=peer,
                    detail={"trust": trust, "serve": serve},
                    created_at=int(self.clock.now().timestamp()),
                )

                def publish() -> None:
                    self.invalidate(peer)
                    if trust or serve:
                        self.policies[peer] = policy
                    else:
                        self.policies.pop(peer, None)

                tx.after_commit(publish)

    async def _credential(self, peer: str, tx: Transaction | None = None) -> tuple[bytes, str]:
        secret = await self.peers.secret(peer, transaction=tx)
        fingerprint = hashlib.sha256(secret).hexdigest()
        policy = self.policies.get(peer)
        if policy is None or policy.credential != fingerprint:
            self.invalidate(peer)
            raise ValueError("Peer time permission is absent or belongs to an earlier pairing")
        return secret, fingerprint

    def _native(self) -> TimeStatus | None:
        status = time_status(self.clock)
        if (
            status.timestamp_safe
            and status.source == "linux_kernel"
            and status.state in {"synchronized", "holdover"}
        ):
            return status
        return None

    def owns(self, item: OutboundItem) -> bool:
        tx = self.outbound.get(item.uid)
        return bool(
            tx
            and self.enabled()
            and tx.generation == self.generations.get(tx.peer, 0)
            and self.clock.monotonic() <= tx.deadline
            and item.guard_kind == GUARD
            and item.binary_payload == tx.payload
            and item.traffic_class == TrafficClass.FEDERATION
            and item.dest == "^all"
            and item.channel == tx.channel
            and item.portnum == tx.port
            and not item.want_ack
            and (not tx.response or self._native() is not None)
        )

    async def dispatch_guard(self, transaction: Transaction, row: dict[str, Any]) -> bool:
        tx = self.outbound.get(str(row["uid"]))
        if tx is None:
            return False
        try:
            _, fingerprint = await self._credential(tx.peer, transaction)
        except ValueError:
            return False
        policy = self.policies.get(tx.peer)
        if not (
            policy
            and (policy.serve if tx.response else policy.trust)
            and fingerprint == tx.credential
            and self.outbound.get(str(row["uid"])) is tx
            and self.enabled()
            and self.clock.monotonic() <= tx.deadline
            and (not tx.response or self._native() is not None)
        ):
            return False
        # Reserve before RF; a restart charges these costs without relying on UTC.
        await self.governor.reserve_time_airtime(
            transaction, self.governor.estimate_toa(len(tx.payload), portnum=tx.port)
        )
        return True

    async def _send(
        self,
        peer: str,
        kind: MessageType,
        value: dict[str, object],
        *,
        started: float,
        generation: int,
    ) -> bool:
        secret, fingerprint = await self._credential(peer)
        counter = await self.peers.next_counter(peer)
        value = {"mesh_id": self.identity(), "target_mesh_id": peer, **value}
        frames = self.codec.encode(kind, value, counter, secret)
        if len(frames) != 1:
            raise ValueError("Peer time must fit one radio frame")
        uid = secrets.token_hex(16)
        tx = Transmission(
            peer,
            fingerprint,
            generation,
            frames[0],
            started + DEADLINE,
            kind == MessageType.TIME_RESPONSE,
            self.channel(),
            self.port,
        )
        self.outbound[uid] = tx
        item = OutboundItem(
            text="",
            dest="^all",
            channel=tx.channel,
            traffic_class=TrafficClass.FEDERATION,
            binary_payload=tx.payload,
            portnum=tx.port,
            want_ack=False,
            guard_kind=GUARD,
            uid=uid,
        )
        if not self.owns(item) or await self.governor.admit(item) is None:
            self.outbound.pop(uid, None)
            return False
        return True

    async def request(self, peer: str) -> dict[str, object]:
        async with self._lock:
            await self.load()
            self._expire()
            policy = self.policies.get(peer)
            if not self.enabled() or not self.identity() or policy is None or not policy.trust:
                raise ValueError("Approve this paired peer as a time source first")
            if self.governor.time_recovery_wait:
                raise ValueError("Waiting one silent airtime hour before clock recovery")
            if (await self.peers.by_mesh_id(peer)).capabilities.get(CAPABILITY) is not True:
                raise ValueError("This peer has not advertised peer time support")
            _, fingerprint = await self._credential(peer)
            now = self.clock.monotonic()
            if peer in self.pending or now < max(
                self._next_request.get(peer, 0), self._next_global_request
            ):
                raise ValueError("Peer time probe is pending or rate limited")
            challenge = Challenge(
                secrets.token_bytes(32), now, fingerprint, self.generations.get(peer, 0)
            )
            self.pending[peer] = challenge
            self._next_request[peer], self._next_global_request = now + 3_600, now + 900
            admitted = await self._send(
                peer,
                MessageType.TIME_REQUEST,
                {"n": challenge.nonce},
                started=now,
                generation=challenge.generation,
            )
            if self.pending.get(peer) is not challenge:
                raise ValueError("Peer time permission changed during admission")
            result: dict[str, object] = {"state": "waiting" if admitted else "queue_rejected"}
            self.results[peer] = result
            if not admitted:
                self.pending.pop(peer, None)
            return result

    async def receive(
        self, peer: str, kind: MessageType, value: dict[str, object], credential: bytes | None
    ) -> None:
        async with self._lock:
            await self.load()
            self._expire()
            if not self.enabled() or not self.identity():
                return
            _, fingerprint = await self._credential(peer)
            if credential is None or credential.hex() != fingerprint:
                raise ValueError("Peer time credential changed during reception")
            nonce = wire_bytes(value.get("n"), "time challenge", length=32)
            policy = self.policies[peer]
            now = self.clock.monotonic()
            if kind == MessageType.TIME_REQUEST:
                if set(value) != {"mesh_id", "target_mesh_id", "n"}:
                    raise ValueError("Invalid time request fields")
                native = self._native()
                if (
                    not policy.serve
                    or native is None
                    or now < max(self._next_response.get(peer, 0), self._next_global_response)
                ):
                    return
                self._next_response[peer], self._next_global_response = now + 3_600, now + 900
                await self._send(
                    peer,
                    MessageType.TIME_RESPONSE,
                    {
                        "n": nonce,
                        "u": round(self.clock.now().timestamp() * 1_000),
                        "e": math.ceil((native.error_seconds or 0) * 1_000) + 1,
                        "a": math.ceil(native.holdover_age_seconds or 0),
                        "s": "kernel",
                    },
                    started=now,
                    generation=self.generations.get(peer, 0),
                )
                return
            if (
                set(value) != {"mesh_id", "target_mesh_id", "n", "u", "e", "a", "s"}
                or value.get("s") != "kernel"
            ):
                raise ValueError("Only a direct native kernel time reference is accepted")
            pending = self.pending.get(peer)
            if (
                not policy.trust
                or pending is None
                or not secrets.compare_digest(pending.nonce, nonce)
            ):
                raise ValueError("No live matching peer time challenge")
            if pending.credential != fingerprint or pending.generation != self.generations.get(
                peer, 0
            ):
                raise ValueError("Peer time permission changed")
            utc = wire_int(value.get("u"), "UTC milliseconds") / 1_000
            error = wire_int(value.get("e"), "error milliseconds", maximum=30_000) / 1_000
            age = wire_int(value.get("a"), "source age", maximum=HOLDOVER_SECONDS - 1)
            self.evidence.observe(
                peer,
                utc=utc,
                error=error,
                age=age,
                started=pending.started,
                received=now,
                wall=self.clock.now().timestamp(),
            )
            self.pending.pop(peer)
            status = time_status(self.clock)
            self.results[peer] = {
                "state": "checked",
                "rtt_seconds": now - pending.started,
                "offset_seconds": utc + (now - pending.started) / 2 - self.clock.now().timestamp(),
                "clock_safe": status.timestamp_safe,
            }

    def _expire(self) -> None:
        now = self.clock.monotonic()
        for peer, pending in tuple(self.pending.items()):
            if now - pending.started > DEADLINE:
                self.pending.pop(peer)
                self.results[peer] = {"state": "timeout"}
        for uid, tx in tuple(self.outbound.items()):
            if now > tx.deadline:
                self.outbound.pop(uid)
        if not self.enabled():
            for peer in tuple(self.policies):
                self.invalidate(peer)

    async def tick(self) -> None:
        await self.load()
        self._expire()
        if not self.enabled() or self.governor.time_recovery_wait:
            return
        # Check credential continuity even when the native clock is healthy.
        for peer in tuple(self.policies):
            try:
                await self._credential(peer)
            except ValueError:
                self.invalidate(peer)
        status = time_status(self.clock)
        if status.source == "linux_kernel" and status.state == "synchronized":
            return
        for peer, policy in tuple(self.policies.items()):
            if policy.trust:
                try:
                    await self.request(peer)
                except ValueError:
                    continue
                break

    async def status(self) -> dict[str, object]:
        await self.load()
        self._expire()
        return {
            "clock": time_status(self.clock).as_dict(),
            "cooldown_seconds": self.governor.time_recovery_wait,
            "peers": {
                peer: {
                    "trust": p.trust,
                    "serve": p.serve,
                    "result": self.results.get(peer, {"state": "not_checked"}),
                }
                for peer, p in self.policies.items()
            },
        }
