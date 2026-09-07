"""Explicit guarded admission and storage receipts; no automatic sender timer."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from outpost.fed.framing import FrameCodec, MessageType, wire_int
from outpost.fed.incident_events import MODE, STREAMS, content_digest
from outpost.fed.peers import FederationPeerService, Peer
from outpost.fed.revisions import source_revision
from outpost.fed.sync import FederationSyncService
from outpost.store import Transaction
from outpost.transport.governor import AirtimeGovernor, OutboundItem
from outpost.transport.models import TrafficClass

GUARD = "incident_event_v1"
BINDING = (
    "producer_mesh_id",
    "peer_mesh_id",
    "epoch",
    "revision",
    "digest",
    "scope",
    "secret_digest",
)


@dataclass(frozen=True)
class IncidentAdmission:
    state: str
    frame_ids: tuple[int, ...]
    counter: int


class IncidentSender:
    def __init__(
        self,
        sync: FederationSyncService,
        peers: FederationPeerService,
        governor: AirtimeGovernor,
        codec: FrameCodec,
        identity: Callable[[], str | None],
        routing: Callable[[], tuple[int, int]],
    ) -> None:
        if governor.outbox is None or governor.outbox.database is not sync.database:
            raise ValueError("incident sender requires the shared durable outbox")
        if peers.database is not sync.database:
            raise ValueError("incident sender requires shared peer transaction ownership")
        self.sync, self.peers, self.governor, self.codec = sync, peers, governor, codec
        self.identity, self.routing = identity, routing
        governor.outbox.attempt_guards[GUARD] = self.authorize_attempt

    @staticmethod
    def _matches(row: dict[str, Any], binding: dict[str, Any]) -> bool:
        return all(row[key] == binding[key] for key in BINDING)

    async def _current(
        self,
        tx: Transaction,
        peer_id: int,
        stream: str,
        uid: str,
    ) -> tuple[Peer, dict[str, Any], dict[str, Any], bytes]:
        """One writer view of current intent, payload, lineage and parent evidence."""
        handoff = self.sync.incident_handoff
        peer = await handoff._peer(tx, peer_id)
        local_id = self.identity()
        if (
            not local_id
            or local_id != self.sync.local_mesh_id
            or not self.sync.incident_events.supported(peer)
            or not self.peers.is_online(peer)
        ):
            raise ValueError("incident sender is outside current identity/peer policy")
        epoch, high = await handoff._lineage(tx)
        scope = handoff._scope(peer)
        rows = await tx.read(
            "SELECT i.*,r.revision AS current_revision,h.epoch AS checkpoint_epoch,"
            "h.observed_revision,h.producer_mesh_id,h.peer_mesh_id "
            "FROM fed_incident_intent i LEFT JOIN fed_revision r "
            "ON r.stream=i.stream AND r.uid=i.uid "
            "LEFT JOIN fed_incident_handoff h ON h.peer_id=i.peer_id "
            "WHERE i.peer_id=? AND i.stream=? AND i.uid=?",
            (peer_id, stream, uid),
        )
        if not rows:
            raise ValueError("incident sender intent is not staged")
        row = dict(rows[0])
        if (
            row["state"] != "pending"
            or row["epoch"] != epoch
            or row["checkpoint_epoch"] != epoch
            or row["observed_revision"] is None
            or row["observed_revision"] > high
            or row["revision"] > high
            or row["current_revision"] != row["revision"]
            or row["scope"] != scope
            or row["producer_mesh_id"] != local_id
            or row["peer_mesh_id"] != peer.mesh_id
            or self.sync._local_uid(self.sync.wire_uid(uid)) != uid
        ):
            raise ValueError("incident sender source, scope or lineage changed")
        items = await self.sync.export_items(
            peer,
            [{"stream": stream, "uid": self.sync.wire_uid(uid)}],
            transaction=tx,
        )
        if not items or content_digest(items[0]["payload"]) != row["digest"]:
            raise ValueError("incident sender content is no longer exportable or exact")
        event = {
            "stream": stream,
            "uid": self.sync.wire_uid(uid),
            "epoch": epoch,
            "revision": row["revision"],
            "payload": items[0]["payload"],
        }
        secret = await self.peers.secret(peer.mesh_id, transaction=tx)
        row["secret_digest"] = hashlib.sha256(secret).hexdigest()
        if stream == "incident_updates":
            parent = event["payload"].get("incident_uid")
            if parent != row["parent_uid"] or not isinstance(parent, str):
                raise ValueError("incident sender parent identity changed")
            parent_uid = self.sync._local_uid(parent)
            if parent_uid is None:
                raise ValueError("incident sender parent is not locally produced")
            _, parent_binding, _, _ = await self._current(tx, peer_id, "incidents", parent_uid)
            stored = await self._association(tx, peer_id, "incidents", parent_uid)
            if (
                not stored
                or stored["stored_at"] is None
                or not self._matches(stored, parent_binding)
            ):
                raise ValueError("incident sender requires exact current parent storage receipt")
        if (
            self.identity() != local_id
            or self.sync.local_mesh_id != local_id
            or not self.sync.incident_events.supported(peer)
            or handoff._scope(peer) != scope
        ):
            raise ValueError("incident sender identity/module policy changed during validation")
        return peer, row, event, secret

    async def _association(
        self,
        tx: Transaction,
        peer_id: int,
        stream: str,
        uid: str,
    ) -> dict[str, Any] | None:
        rows = await tx.read(
            "SELECT * FROM fed_incident_dispatch WHERE peer_id=? AND stream=? AND uid=?",
            (peer_id, stream, uid),
        )
        return dict(rows[0]) if rows else None

    def _frames(
        self, peer: Peer, event: dict[str, Any], counter: int, secret: bytes
    ) -> list[bytes]:
        return self.codec.encode(
            MessageType.INCIDENT,
            {
                "mesh_id": self.sync.local_mesh_id,
                "target_mesh_id": peer.mesh_id,
                "mode": MODE,
                "event": event,
            },
            counter,
            secret,
        )

    async def admit(
        self,
        peer_id: int,
        stream: str,
        uid: str,
        *,
        retry: bool = False,
    ) -> IncidentAdmission:
        """Admit one staged identity; explicit retry replaces terminal unreceipted work.

        No network I/O, nested writer or automatic polling. Returned queue state
        never means remote storage; the receipt consumer records that separately.
        """
        wire_int(peer_id, "peer id", minimum=1)
        if not isinstance(stream, str) or stream not in STREAMS or not isinstance(uid, str):
            raise ValueError("invalid incident sender identity")
        async with self.sync.database.transaction() as tx:
            peer, binding, event, secret = await self._current(tx, peer_id, stream, uid)
            prior = await self._association(tx, peer_id, stream, uid)
            if prior and self._matches(prior, binding):
                ids = tuple(json.loads(prior["frame_ids"]))
                if prior["stored_at"] is not None:
                    return IncidentAdmission("stored", ids, prior["counter"])
                work = await tx.read(
                    "SELECT state FROM outbound_work WHERE queue_key=?", (prior["queue_key"],)
                )
                if any(row["state"] in {"pending", "held", "sending"} for row in work):
                    return IncidentAdmission("queued", ids, prior["counter"])
                if not retry:
                    state = (
                        "awaiting_receipt"
                        if len(work) == len(ids)
                        and all(row["state"] in {"sent", "acked", "awaiting_ack"} for row in work)
                        else "retry_required"
                    )
                    return IncidentAdmission(state, ids, prior["counter"])
            counter = await self.peers.next_counter(peer.mesh_id, transaction=tx)
            frames = self._frames(peer, event, counter, secret)
            queue_key = "incident-event:" + uuid.uuid4().hex
            channel, portnum = self.routing()
            admission = await self.governor.admit_many_result(
                [
                    OutboundItem(
                        text="",
                        binary_payload=frame,
                        dest="^all",
                        channel=channel,
                        portnum=portnum,
                        traffic_class=TrafficClass.FEDERATION,
                        want_ack=False,
                        multipart=len(frames) > 1,
                        guard_kind=GUARD,
                        queue_key=queue_key,
                        supersedes=prior["queue_key"] if prior else None,
                    )
                    for frame in frames
                ],
                transaction=tx,
            )
            if admission.rejection_reason is not None:
                raise ValueError(
                    "incident sender admission rejected: " + admission.rejection_reason
                )
            await tx.write(
                "INSERT INTO fed_incident_dispatch(peer_id,stream,uid,producer_mesh_id,"
                "peer_mesh_id,"
                "epoch,revision,digest,scope,secret_digest,queue_key,counter,frame_ids) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(peer_id,stream,uid) DO UPDATE SET "
                "producer_mesh_id=excluded.producer_mesh_id,peer_mesh_id=excluded.peer_mesh_id,"
                "epoch=excluded.epoch,revision=excluded.revision,digest=excluded.digest,"
                "scope=excluded.scope,secret_digest=excluded.secret_digest,queue_key=excluded.queue_key,"
                "counter=excluded.counter,frame_ids=excluded.frame_ids,stored_at=NULL",
                (
                    peer_id,
                    stream,
                    uid,
                    *(binding[key] for key in BINDING),
                    queue_key,
                    counter,
                    json.dumps(admission.item_ids),
                ),
            )
            # Mutable runtime module/identity/routing state can change while SQL yields.
            if (
                self.identity() != binding["producer_mesh_id"]
                or self.sync.local_mesh_id != binding["producer_mesh_id"]
                or not self.sync.incident_events.supported(peer)
                or self.sync.incident_handoff._scope(peer) != binding["scope"]
                or self.routing() != (channel, portnum)
            ):
                raise ValueError("incident sender runtime policy changed before commit")
        return IncidentAdmission("queued", admission.item_ids, counter)

    async def authorize_attempt(self, tx: Transaction, work: dict[str, Any]) -> bool:
        """Called inside durable attempt reservation, including startup/transport retry."""
        rows = await tx.read(
            "SELECT * FROM fed_incident_dispatch WHERE queue_key=?", (work["queue_key"],)
        )
        if not rows or rows[0]["stored_at"] is not None:
            return False
        saved = dict(rows[0])
        try:
            peer, current, event, secret = await self._current(
                tx,
                saved["peer_id"],
                saved["stream"],
                saved["uid"],
            )
            if not self._matches(saved, current):
                return False
            ids = json.loads(saved["frame_ids"])
            frames = self._frames(peer, event, saved["counter"], secret)
            return (
                isinstance(ids, list)
                and len(ids) == len(frames)
                and work["id"] in ids
                and frames[ids.index(work["id"])] == work["binary_payload"]
                and work["destination"] == "^all"
                and not work["want_ack"]
                and work["traffic_class"] == TrafficClass.FEDERATION.value
                and (work["channel"], work["portnum"]) == self.routing()
                # The governor's tick-start timestamp may predate writer waits.
                and work["expires_at"] > self.peers.clock.now().timestamp()
            )
        except ValueError:
            return False  # Policy/content denial; storage faults still fail the core task.

    async def receive(
        self,
        peer: Peer,
        receipt: dict[str, Any],
        now: int,
        *,
        authenticated_secret: bytes | None,
    ) -> bool:
        """Authenticated dispatcher only. A stored version is not reviewed or acknowledged."""
        wire_int(now, "incident receipt time")
        if not isinstance(authenticated_secret, bytes):
            raise ValueError("incident receipt requires its verified authentication key")
        fields = {
            "mode",
            "mesh_id",
            "target_mesh_id",
            "stream",
            "uid",
            "epoch",
            "revision",
            "digest",
            "state",
        }
        if (
            set(receipt) != fields
            or type(receipt["mode"]) is not int
            or receipt["mode"] != MODE
            or receipt["state"] != "stored"
            or receipt["mesh_id"] != peer.mesh_id
            or receipt["target_mesh_id"] != self.identity()
            or not isinstance(receipt["stream"], str)
            or receipt["stream"] not in STREAMS
            or not isinstance(receipt["uid"], str)
            or not isinstance(receipt["digest"], str)
            or len(receipt["digest"]) != 64
            or any(c not in "0123456789abcdef" for c in receipt["digest"])
        ):
            raise ValueError("invalid targeted incident storage receipt")
        source_revision(receipt)
        local_id = self.identity()
        if (
            not local_id
            or local_id != self.sync.local_mesh_id
            or not receipt["uid"].startswith(local_id + ":")
        ):
            raise ValueError("incident receipt producer identity changed")
        local_uid = self.sync._local_uid(receipt["uid"])
        if not local_uid:
            raise ValueError("invalid incident receipt producer UID")
        async with self.sync.database.transaction() as tx:
            current = await self.sync.incident_handoff._peer(tx, peer.id)
            if current.mesh_id != peer.mesh_id or not self.sync.incident_events.supported(current):
                raise ValueError("incident receipt is outside current peer policy")
            saved = await self._association(tx, peer.id, receipt["stream"], local_uid)
            secret = await self.peers.secret(peer.mesh_id, transaction=tx)
            if (
                not saved
                or not hmac.compare_digest(secret, authenticated_secret)
                or any(saved[key] != receipt[key] for key in ("epoch", "revision", "digest"))
                or saved["peer_mesh_id"] != peer.mesh_id
                or saved["producer_mesh_id"] != local_id
                or saved["secret_digest"] != hashlib.sha256(secret).hexdigest()
            ):
                return False
            await tx.write(
                "UPDATE fed_incident_dispatch SET stored_at=COALESCE(stored_at,?) "
                "WHERE queue_key=?",
                (now, saved["queue_key"]),
            )
            await tx.write(
                "UPDATE outbound_work SET state='cancelled',completed_at=? "
                "WHERE queue_key=? AND state IN ('pending','held','failed')",
                (now, saved["queue_key"]),
            )
            ids = set(json.loads(saved["frame_ids"]))
            tx.after_commit(lambda: self.governor._remove_ids(ids))
            if (
                self.identity() != local_id
                or self.sync.local_mesh_id != local_id
                or not self.sync.incident_events.supported(current)
            ):
                raise ValueError("incident receipt runtime policy changed before commit")
        return True
