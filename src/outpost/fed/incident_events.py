"""Negotiated event ingress: committed quarantine is not human acceptance."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

from outpost.fed.framing import wire_int
from outpost.fed.peers import FederationPeerService, Peer
from outpost.fed.revisions import source_revision
from outpost.store import Transaction

if TYPE_CHECKING:
    from outpost.fed.sync import FederationSyncService

CAPABILITY = "incident_events"
MODE = 1
RATE_CURSOR = "_incident_event_rate"
STREAMS = {"incidents", "incident_updates"}


def content_digest(payload: dict[str, Any]) -> str:
    """SHA-256 of the existing canonical JSON encoding, with non-finite JSON refused."""
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True, allow_nan=False)
    if len(encoded.encode()) > 12_000:
        raise ValueError("incident event payload is too large")
    return hashlib.sha256(encoded.encode()).hexdigest()


class IncidentEvents:
    def __init__(self, sync: FederationSyncService) -> None:
        self.sync = sync

    def supported(self, peer: Peer) -> bool:
        return (
            peer.state == "active"
            and type(peer.capabilities.get(CAPABILITY)) is int
            and peer.capabilities.get(CAPABILITY) == MODE
            and type(peer.capabilities.get("reconciliation")) is int
            and peer.capabilities.get("reconciliation") == 2
            and peer.sync_incidents
            and self.sync.module_enabled("fed")
            and self.sync.module_enabled("watch")
        )

    async def receive(self, peer: Peer, event: dict[str, Any], now: int) -> dict[str, Any]:
        """Called only after framing authentication, target and fresh-counter checks.

        The current policy, quota, inbox and revision receipt share one writer.
        A returned value proves commit; the app admits its reply afterwards.
        Fresh-counter retries reconstruct the receipt from retained exact content.
        """
        wire_int(now, "incident event receive time")
        if set(event) != {"stream", "uid", "epoch", "revision", "payload"}:
            raise ValueError("invalid incident event fields")
        stream, uid, payload = event["stream"], event["uid"], event["payload"]
        if (
            not isinstance(stream, str)
            or stream not in STREAMS
            or not isinstance(uid, str)
            or not uid.startswith(f"{peer.mesh_id}:")
            or not len(peer.mesh_id) + 1 < len(uid) <= 160
            or not isinstance(payload, dict)
            or payload.get("uid") != uid
        ):
            raise ValueError("invalid original-producer incident event")
        revision = source_revision(event)
        assert revision is not None
        epoch, sequence = revision
        digest = content_digest(payload)
        async with self.sync.database.transaction() as tx:
            rows = await tx.read("SELECT * FROM fed_peer WHERE id=?", (peer.id,))
            if not rows:
                raise ValueError("incident event peer no longer exists")
            current = FederationPeerService._peer(rows[0])
            if current.mesh_id != peer.mesh_id or not self.supported(current):
                raise ValueError("incident event is outside current peer policy")
            checkpoints = await tx.read(
                "SELECT cursor FROM fed_cursor WHERE peer_id=? AND stream='_reconcile' "
                "AND direction='recv'",
                (peer.id,),
            )
            checkpoint = json.loads(checkpoints[0]["cursor"]) if checkpoints else {}
            if not isinstance(checkpoint, dict):
                raise ValueError("invalid incident event reconciliation checkpoint")
            known = await tx.read(
                "SELECT epoch FROM fed_revision_receipt WHERE peer_id=? LIMIT 1", (peer.id,)
            )
            if (
                checkpoint.get("status") == "blocked"
                or checkpoint.get("epoch") not in (None, epoch)
                or (known and known[0]["epoch"] != epoch)
            ):
                raise ValueError("incident event lineage is blocked; operator review required")
            await self._scope(tx, current, event)
            stored = await tx.read(
                "SELECT r.epoch,r.revision,i.payload_json,i.source_epoch,i.source_revision "
                "FROM fed_revision_receipt r LEFT JOIN fed_inbox_item i "
                "ON i.peer_id=r.peer_id AND i.stream=r.stream AND i.uid=r.uid "
                "WHERE r.peer_id=? AND r.stream=? AND r.uid=?",
                (peer.id, stream, uid),
            )
            duplicate = False
            if stored:
                prior = stored[0]
                if prior["epoch"] != epoch:
                    raise ValueError("incident event lineage changed; operator review required")
                if sequence < prior["revision"]:
                    raise ValueError("stale incident event revision")
                if sequence == prior["revision"]:
                    if (
                        prior["payload_json"] is None
                        or (prior["source_epoch"], prior["source_revision"]) != revision
                        or content_digest(json.loads(prior["payload_json"])) != digest
                    ):
                        raise ValueError(
                            "incident event content conflicts or is no longer retained"
                        )
                    duplicate = True
            if not duplicate:
                await self._charge(tx, current, now)
                await self.sync.quarantine_transaction(tx, current, event, now)
                saved = await tx.read(
                    "SELECT payload_json FROM fed_inbox_item WHERE peer_id=? AND stream=? "
                    "AND uid=? AND source_epoch=? AND source_revision=?",
                    (peer.id, stream, uid, epoch, sequence),
                )
                if not saved or content_digest(json.loads(saved[0]["payload_json"])) != digest:
                    raise ValueError("incident event content was not stored exactly")
        return {
            "mode": MODE,
            "stream": stream,
            "uid": uid,
            "epoch": epoch,
            "revision": sequence,
            "digest": digest,
            "state": "stored",
        }

    async def _scope(self, tx: Transaction, peer: Peer, event: dict[str, Any]) -> None:
        payload = event["payload"]
        if event["stream"] == "incident_updates":
            if not self.sync.incident_updates.supported(peer):
                raise ValueError("incident event note capability is required")
            self.sync.incident_updates.validate(peer.mesh_id, event["uid"], payload)
            parents = await tx.read(
                "SELECT payload_json FROM fed_inbox_item WHERE peer_id=? AND stream='incidents' "
                "AND uid=? AND source_epoch=? AND source_revision IS NOT NULL",
                (peer.id, payload["incident_uid"], event["epoch"]),
            )
            if not parents:
                raise ValueError("store the original parent before its incident event note")
            payload = json.loads(parents[0]["payload_json"])
        for key in ("title", "type", "severity", "status"):
            if not isinstance(payload.get(key), str) or not payload[key].strip():
                raise ValueError("invalid incident event text")
        for key in ("body", "location_text", "reporter_label", "resolution_note"):
            if payload.get(key) is not None and not isinstance(payload[key], str):
                raise ValueError("invalid incident event text")
        for key in ("created_at", "updated_at", "expires_at", "resolved_at"):
            if key in {"created_at", "updated_at"} or payload.get(key) is not None:
                wire_int(payload.get(key), "incident event timestamp", maximum=253402300799)
        for key in ("lat", "lon", "radius_m"):
            if payload.get(key) is not None and type(payload[key]) not in (int, float):
                raise ValueError("invalid incident event position")
        values = self.sync._incident_values(payload, revisioned=True)
        if not self.sync.incident_allowed(peer, values["lat"], values["lon"]):
            raise ValueError("incident event is outside current geographic policy")

    async def _charge(self, tx: Transaction, peer: Peer, now: int) -> None:
        rows = await tx.read(
            "SELECT cursor FROM fed_cursor WHERE peer_id=? AND stream=? AND direction='recv'",
            (peer.id, RATE_CURSOR),
        )
        window = json.loads(rows[0]["cursor"]) if rows else {"start": now, "count": 0}
        if not isinstance(window, dict):
            raise ValueError("invalid incident event quota state")
        start = wire_int(window.get("start"), "incident event quota start")
        count = wire_int(window.get("count"), "incident event quota count")
        # Fixed one-hour windows; a backwards wall-clock step does not refill.
        if now >= start + 3600:
            start, count = now, 0
        if count >= peer.quota_items_per_hour:
            raise ValueError("peer incident event quota exceeded")
        await tx.write(
            "INSERT INTO fed_cursor(peer_id,stream,direction,cursor,updated_at) "
            "VALUES(?,?,'recv',?,?) ON CONFLICT(peer_id,stream,direction) "
            "DO UPDATE SET cursor=excluded.cursor,updated_at=excluded.updated_at",
            (peer.id, RATE_CURSOR, json.dumps({"start": start, "count": count + 1}), now),
        )
