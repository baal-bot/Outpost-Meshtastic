"""Bounded atomic source-to-peer staging; no radio, receipt or human action."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from outpost.fed.framing import wire_int
from outpost.fed.incident_events import content_digest
from outpost.fed.peers import FederationPeerService, Peer
from outpost.fed.revisions import token
from outpost.store import Transaction

if TYPE_CHECKING:
    from outpost.fed.sync import FederationSyncService

MAX_PAGE = 100


@dataclass(frozen=True)
class HandoffPage:
    after_revision: int
    scanned: int
    staged: int
    skipped: int
    remaining: bool
    scope_reset: bool


class IncidentHandoff:
    def __init__(self, sync: FederationSyncService) -> None:
        self.sync = sync

    def _scope(self, peer: Peer) -> str:
        value = [
            self.sync.local_mesh_id,
            peer.mesh_id,
            peer.sync_incidents,
            peer.incident_lat,
            peer.incident_lon,
            peer.incident_radius_km,
            self.sync.incident_updates.supported(peer),
        ]
        return hashlib.sha256(
            json.dumps(value, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()

    async def _peer(self, tx: Transaction, peer_id: int) -> Peer:
        rows = await tx.read("SELECT * FROM fed_peer WHERE id=?", (peer_id,))
        if not rows:
            raise ValueError("incident handoff peer does not exist")
        return FederationPeerService._peer(rows[0])

    @staticmethod
    async def _lineage(tx: Transaction) -> tuple[str, int]:
        rows = await tx.read("SELECT epoch FROM fed_revision_lineage WHERE id=1")
        if not rows:
            raise ValueError("incident handoff producer lineage is missing")
        epoch = token(rows[0]["epoch"], "producer epoch")
        sequence = await tx.read("SELECT seq FROM sqlite_sequence WHERE name='fed_revision'")
        return epoch, wire_int(sequence[0]["seq"] if sequence else 0, "producer watermark")

    async def stage(self, peer_id: int, *, limit: int = MAX_PAGE) -> HandoffPage:
        """Atomically stage one peer's indexed page; source rows are never consumed.

        Revision, NOT first_revision, is the durable scan watermark. Scope changes
        restart at zero, with old intents invalidated by their stored scope token.
        A new peer starts at zero too; this is bounded work, not a latency promise.
        """
        wire_int(peer_id, "peer id", minimum=1)
        wire_int(limit, "incident handoff page size", minimum=1, maximum=MAX_PAGE)
        local_id = self.sync.local_mesh_id
        if not local_id or not local_id.startswith("!") or ":" in local_id:
            raise ValueError("incident handoff requires a producer radio identity")
        async with self.sync.database.transaction() as tx:
            if self.sync.local_mesh_id != local_id:
                raise ValueError("incident handoff producer identity changed")
            peer = await self._peer(tx, peer_id)
            if not self.sync.incident_events.supported(peer):
                raise ValueError("incident handoff is outside current peer policy")
            epoch, high = await self._lineage(tx)
            scope = self._scope(peer)
            rows = await tx.read("SELECT * FROM fed_incident_handoff WHERE peer_id=?", (peer_id,))
            prior = rows[0] if rows else None
            if prior is None and await tx.read(
                "SELECT 1 FROM fed_incident_intent WHERE peer_id=? LIMIT 1", (peer_id,)
            ):
                raise ValueError("incident handoff checkpoint is missing; review required")
            if prior and (
                prior["epoch"] != epoch
                or prior["observed_revision"] > high
                or prior["producer_mesh_id"] != local_id
                or prior["peer_mesh_id"] != peer.mesh_id
            ):
                raise ValueError("incident handoff lineage/identity rollback requires review")
            reset = prior is not None and prior["scope"] != scope
            after = int(prior["after_revision"]) if prior and not reset else 0
            heads = await tx.read(
                "SELECT c.stream,c.uid,c.epoch,c.revision,r.revision AS current_revision "
                "FROM incident_change_event c INDEXED BY idx_incident_change_revision "
                "LEFT JOIN fed_revision r ON r.stream=c.stream AND r.uid=c.uid "
                "WHERE c.revision>? ORDER BY c.revision LIMIT ?",
                (after, limit + 1),
            )
            staged = skipped = 0
            for head in heads[:limit]:
                if (
                    head["epoch"] != epoch
                    or head["revision"] != head["current_revision"]
                    or head["revision"] > high
                ):
                    raise ValueError("incident handoff source head mismatch requires review")
                changed = await self._stage_head(tx, peer, dict(head), scope)
                staged += changed
                skipped += not changed
                after = head["revision"]
            # Cursor and staging decisions commit together, even for a filtered page.
            if self.sync.local_mesh_id != local_id:
                raise ValueError("incident handoff producer identity changed")
            if not self.sync.incident_events.supported(peer) or self._scope(peer) != scope:
                raise ValueError("incident handoff module policy changed before commit")
            await tx.write(
                "INSERT INTO fed_incident_handoff(peer_id,producer_mesh_id,peer_mesh_id,epoch,"
                "scope,after_revision,observed_revision) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(peer_id) DO UPDATE SET scope=excluded.scope,"
                "fresh_revision=CASE WHEN fed_incident_handoff.scope<>excluded.scope "
                "THEN NULL ELSE fed_incident_handoff.fresh_revision END,"
                "after_revision=excluded.after_revision,"
                "observed_revision=excluded.observed_revision "
                "WHERE fed_incident_handoff.scope<>excluded.scope "
                "OR fed_incident_handoff.after_revision<>excluded.after_revision "
                "OR fed_incident_handoff.observed_revision<>excluded.observed_revision",
                (peer.id, local_id, peer.mesh_id, epoch, scope, after, high),
            )
        return HandoffPage(
            after, min(len(heads), limit), staged, skipped, len(heads) > limit, reset
        )

    async def stage_automatic(self, peer_id: int, *, limit: int = 4) -> None:
        """Give backlog and fresh heads separate bounded scan opportunities.

        First discovery seeds the most recent page as fresh; older heads retain
        the ordinary durable backlog cursor. Subsequent fresh pages ascend from
        their own watermark, so a continuous stream cannot skip an older change.
        Neither cursor is evidence of radio delivery or all-peer completion.
        """
        page = await self.stage(peer_id, limit=limit)
        async with self.sync.database.transaction() as tx:
            peer = await self._peer(tx, peer_id)
            epoch, high = await self._lineage(tx)
            scope = self._scope(peer)
            rows = await tx.read("SELECT * FROM fed_incident_handoff WHERE peer_id=?", (peer_id,))
            checkpoint = rows[0] if rows else None
            if (
                not checkpoint
                or not self.sync.incident_events.supported(peer)
                or checkpoint["epoch"] != epoch
                or checkpoint["observed_revision"] > high
                or checkpoint["scope"] != scope
                or checkpoint["producer_mesh_id"] != self.sync.local_mesh_id
                or checkpoint["peer_mesh_id"] != peer.mesh_id
            ):
                raise ValueError("automatic incident staging policy/lineage changed")
            after = checkpoint["fresh_revision"]
            if after is not None and after > high:
                raise ValueError("automatic incident fresh cursor rollback requires review")
            if after is None or page.scope_reset:
                heads = await tx.read(
                    "SELECT c.*,r.revision AS current_revision FROM incident_change_event c "
                    "INDEXED BY idx_incident_change_revision LEFT JOIN fed_revision r "
                    "ON r.stream=c.stream AND r.uid=c.uid ORDER BY c.revision DESC LIMIT ?",
                    (limit,),
                )
                heads = list(reversed(heads))
                next_revision = high
            else:
                heads = await tx.read(
                    "SELECT c.*,r.revision AS current_revision FROM incident_change_event c "
                    "INDEXED BY idx_incident_change_revision LEFT JOIN fed_revision r "
                    "ON r.stream=c.stream AND r.uid=c.uid WHERE c.revision>? "
                    "ORDER BY c.revision LIMIT ?",
                    (after, limit),
                )
                next_revision = heads[-1]["revision"] if heads else high
            for head in heads:
                if (
                    head["epoch"] != epoch
                    or head["revision"] != head["current_revision"]
                    or head["revision"] > high
                ):
                    raise ValueError("automatic incident source head mismatch requires review")
                await self._stage_head(tx, peer, dict(head), scope)
                await tx.write(
                    "UPDATE fed_incident_intent SET lane='fresh' "
                    "WHERE peer_id=? AND stream=? AND uid=?",
                    (peer_id, head["stream"], head["uid"]),
                )
            if not self.sync.incident_events.supported(peer) or self._scope(peer) != scope:
                raise ValueError("automatic incident module policy changed before commit")
            await tx.write(
                "UPDATE fed_incident_handoff SET fresh_revision=?,observed_revision=? "
                "WHERE peer_id=? AND (fresh_revision IS NOT ? OR observed_revision<>?)",
                (next_revision, high, peer_id, next_revision, high),
            )

    async def _stage_head(
        self, tx: Transaction, peer: Peer, head: dict[str, Any], scope: str
    ) -> bool:
        existing = await tx.read(
            "SELECT epoch,revision,digest FROM fed_incident_intent "
            "WHERE peer_id=? AND stream=? AND uid=?",
            (peer.id, head["stream"], head["uid"]),
        )
        if existing and (
            existing[0]["epoch"] != head["epoch"] or existing[0]["revision"] > head["revision"]
        ):
            raise ValueError("incident handoff would overwrite a newer or different lineage intent")
        # A retained foreign/self-prefixed UID must not resolve to a different
        # local record after the export adapter strips the producer prefix.
        original = self.sync._local_uid(self.sync.wire_uid(head["uid"])) == head["uid"]
        items = (
            await self.sync.export_items(
                peer,
                [{"stream": head["stream"], "uid": self.sync.wire_uid(head["uid"])}],
                transaction=tx,
            )
            if original
            else []
        )
        digest = parent = None
        urgency = 3
        state = "not_exportable"
        if items:
            payload = items[0]["payload"]
            urgency = {"critical": 0, "urgent": 1, "caution": 2}.get(payload.get("severity"), 3)
            parent = payload.get("incident_uid") if head["stream"] == "incident_updates" else None
            try:
                digest = content_digest(payload)
                state = "pending"
            except (TypeError, ValueError):
                state = "invalid_payload"
        elif not existing:
            return False  # No prior work to invalidate; scope rescan can revisit later.
        if (
            existing
            and existing[0]["revision"] == head["revision"]
            and existing[0]["digest"] is not None
            and digest is not None
            and existing[0]["digest"] != digest
        ):
            raise ValueError("incident handoff content conflicts at the same revision")
        # A genuinely new version/scope starts a new finite automatic policy.
        # Re-scanning the same head never revives cancelled/expired/invalid work.
        await tx.write(
            "UPDATE fed_incident_intent SET scheduled_at=NULL,deadline_at=NULL,"
            "next_attempt_at=0,application_attempts=0,delivery_state='pending',"
            "delivery_reason=NULL WHERE peer_id=? AND stream=? AND uid=? "
            "AND (revision<>? OR scope<>?)",
            (peer.id, head["stream"], head["uid"], head["revision"], scope),
        )
        await tx.write(
            "INSERT INTO fed_incident_intent(peer_id,stream,uid,epoch,revision,first_revision,"
            "scope,digest,parent_uid,state) VALUES(?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(peer_id,stream,uid) DO UPDATE SET revision=excluded.revision,"
            "scope=excluded.scope,digest=excluded.digest,parent_uid=excluded.parent_uid,"
            "state=excluded.state",
            (
                peer.id,
                head["stream"],
                head["uid"],
                head["epoch"],
                head["revision"],
                head["revision"],
                scope,
                digest,
                parent,
                state,
            ),
        )
        await tx.write(
            "UPDATE fed_incident_intent SET urgency_rank=? WHERE peer_id=? AND stream=? AND uid=?",
            (urgency, peer.id, head["stream"], head["uid"]),
        )
        return True

    async def pending(
        self, peer_id: int, *, after: int = 0, limit: int = MAX_PAGE
    ) -> list[dict[str, Any]]:
        """Bounded metadata inspection, never authorization to admit or transmit.

        Includes blocked intents. Like the source inspection cursor, first_revision
        only pages within a pass; repeat passes from zero to observe supersession.
        """
        wire_int(peer_id, "peer id", minimum=1)
        wire_int(after, "incident intent cursor")
        wire_int(limit, "incident intent page size", minimum=1, maximum=MAX_PAGE)
        async with self.sync.database.transaction() as tx:
            peer = await self._peer(tx, peer_id)
            epoch, high = await self._lineage(tx)
            scope = self._scope(peer)
            supported = self.sync.incident_events.supported(peer)
            rows = await tx.read(
                "SELECT i.*,r.revision AS current_revision,h.observed_revision,"
                "h.epoch AS checkpoint_epoch,"
                "h.producer_mesh_id,h.peer_mesh_id FROM fed_incident_intent i "
                "INDEXED BY idx_fed_incident_intent_pending "
                "LEFT JOIN fed_revision r ON r.stream=i.stream AND r.uid=i.uid "
                "LEFT JOIN fed_incident_handoff h ON h.peer_id=i.peer_id "
                "WHERE i.peer_id=? AND i.first_revision>? ORDER BY i.first_revision LIMIT ?",
                (peer_id, after, limit),
            )
            result = []
            for row in rows:
                item = dict(row)
                if (
                    row["epoch"] != epoch
                    or row["checkpoint_epoch"] != epoch
                    or row["observed_revision"] is None
                    or row["observed_revision"] > high
                    or row["producer_mesh_id"] != self.sync.local_mesh_id
                    or row["peer_mesh_id"] != peer.mesh_id
                ):
                    item["state"] = "lineage_blocked"
                elif not supported or row["scope"] != scope:
                    item["state"] = "policy_changed"
                elif row["current_revision"] != row["revision"]:
                    item["state"] = "source_changed"
                result.append(item)
            return result
