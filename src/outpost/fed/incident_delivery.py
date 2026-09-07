"""Bounded operator read model: storage observations never imply human action."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from outpost.fed.framing import wire_int
from outpost.fed.incident_worker import DELIVERY_TTL, MAX_APPLICATION_ATTEMPTS, IncidentWorker
from outpost.transport.models import TrafficClass


class IncidentDelivery:
    def __init__(self, worker: IncidentWorker) -> None:
        self.worker = worker

    async def status(
        self,
        *,
        after_peer: int = 0,
        after_revision: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        wire_int(after_peer, "incident delivery peer cursor")
        wire_int(after_revision, "incident delivery revision cursor")
        wire_int(limit, "incident delivery page size", minimum=1, maximum=100)
        worker, sender = self.worker, self.worker.sender
        now = worker._now()
        items = []
        async with worker.database.transaction() as tx:
            rows = await tx.read(
                "SELECT * FROM fed_incident_intent INDEXED BY idx_fed_incident_intent_pending "
                "WHERE (peer_id,first_revision)>(?,?) ORDER BY peer_id,first_revision LIMIT ?",
                (after_peer, after_revision, limit + 1),
            )
            epoch, high = await sender.sync.incident_handoff._lineage(tx)
            for row in rows[:limit]:
                item = dict(row)
                peer = await sender.sync.incident_handoff._peer(tx, item["peer_id"])
                prior = await sender._association(tx, *worker._key(item))
                source = await tx.read(
                    "SELECT revision FROM fed_revision WHERE stream=? AND uid=?",
                    (item["stream"], item["uid"]),
                )
                current_source = bool(source and source[0]["revision"] == item["revision"])
                allowed = sender.sync.incident_events.supported(peer)
                scope = sender.sync.incident_handoff._scope(peer)
                checkpoints = await tx.read(
                    "SELECT * FROM fed_incident_handoff WHERE peer_id=?",
                    (peer.id,),
                )
                lineage_valid = bool(
                    checkpoints
                    and checkpoints[0]["epoch"] == epoch
                    and checkpoints[0]["observed_revision"] <= high
                    and checkpoints[0]["producer_mesh_id"] == sender.identity()
                    and checkpoints[0]["peer_mesh_id"] == peer.mesh_id
                )
                secret_rows = await tx.read(
                    "SELECT shared_secret FROM fed_peer WHERE id=?", (peer.id,)
                )
                secret = secret_rows[0]["shared_secret"]
                exact = bool(
                    prior
                    and current_source
                    and item["epoch"] == epoch
                    and item["revision"] <= high
                    and all(
                        prior[key] == item[key] for key in ("epoch", "revision", "digest", "scope")
                    )
                    and scope == item["scope"]
                    and allowed
                    and lineage_valid
                    and prior["producer_mesh_id"] == sender.identity() == sender.sync.local_mesh_id
                    and prior["peer_mesh_id"] == peer.mesh_id
                    and secret is not None
                    and prior["secret_digest"] == hashlib.sha256(bytes(secret)).hexdigest()
                )
                work = await tx.read(
                    "SELECT state,attempts,last_error,completed_at FROM outbound_work "
                    "WHERE queue_key=? ORDER BY id LIMIT 8",
                    (prior["queue_key"] if prior else "",),
                )
                states = {entry["state"] for entry in work}
                active = sum(entry["state"] in {"pending", "held", "sending"} for entry in work)
                sent = sum(entry["state"] in {"sent", "acked", "awaiting_ack"} for entry in work)
                state, reason = item["delivery_state"], item["delivery_reason"]
                stored_at = prior["stored_at"] if exact and prior else None
                if not current_source:
                    state, reason = "pending", "source_changed"
                elif not allowed or scope != item["scope"]:
                    state, reason = "blocked", "peer_policy_changed"
                elif not lineage_valid or item["epoch"] != epoch or item["revision"] > high:
                    state, reason = "blocked", "lineage_review_required"
                elif stored_at is not None:
                    state, reason = "stored", None
                elif prior and not exact:
                    state, reason = "blocked", "binding_changed"
                elif state not in {"cancelled", "blocked", "retry_exhausted"}:
                    if item["deadline_at"] is not None and now >= item["deadline_at"]:
                        state, reason = "expired", "delivery_deadline"
                    elif exact and states & {"cancelled", "retracted", "superseded"}:
                        state, reason = "cancelled", "transport_cancelled"
                    elif exact and "expired" in states:
                        state, reason = "expired", "transport_expired"
                    elif not sender.peers.is_online(peer):
                        state, reason = "waiting", "peer_offline"
                    elif exact and active:
                        state = "queued"
                        reason = (
                            "quiet_hours"
                            if sender.governor._quiet(TrafficClass.FEDERATION)
                            else "governed_queue"
                        )
                    elif exact and work and prior and sent == len(json.loads(prior["frame_ids"])):
                        state, reason = "awaiting_receipt", "storage_receipt_missing"
                items.append(
                    {
                        "peer_id": peer.id,
                        "peer_mesh_id": peer.mesh_id,
                        "peer_name": peer.node_name,
                        "stream": item["stream"],
                        "uid": item["uid"],
                        "revision": item["revision"],
                        "lane": item["lane"],
                        "state": state,
                        "reason": reason,
                        "local_change_recorded": True,
                        "source_current": current_source,
                        "queued_frames": active if exact else 0,
                        "radio_completed_frames": sent if exact else 0,
                        "radio_attempts": sum(entry["attempts"] for entry in work) if exact else 0,
                        "remote_storage": "observed" if stored_at is not None else "not_confirmed",
                        "stored_at": stored_at,
                        "human_review": "not_reported",
                        "responder_notification": "not_reported",
                        "responder_acknowledgement": "not_reported",
                        "application_attempts": item["application_attempts"],
                        "deadline_at": item["deadline_at"],
                        "next_attempt_at": item["next_attempt_at"]
                        if item["next_attempt_at"] < 253402300799
                        else None,
                        "action_token": worker.action_token(
                            item, prior["queue_key"] if prior else None
                        ),
                        "can_cancel": active > 0
                        or state in {"pending", "queued", "waiting", "awaiting_receipt"},
                        "can_retry": active == 0
                        and state in {"expired", "cancelled", "blocked", "retry_exhausted"},
                    }
                )
            next_cursor = (
                {
                    "after_peer": rows[limit - 1]["peer_id"],
                    "after_revision": rows[limit - 1]["first_revision"],
                }
                if len(rows) > limit
                else None
            )
        return {
            "items": items,
            "next": next_cursor,
            "policy_enabled": bool(sender.identity())
            and all(sender.sync.module_enabled(name) for name in ("fed", "watch")),
            "quiet_hours_active": sender.governor._quiet(TrafficClass.FEDERATION),
            "maximum_application_attempts": MAX_APPLICATION_ATTEMPTS,
            "delivery_window_seconds": DELIVERY_TTL,
            "storage_receipt_is_human_ack": False,
        }
