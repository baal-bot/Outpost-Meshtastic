"""Coalesced, commit-safe and policy-guarded exact storage-receipt replies."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from outpost.fed.framing import MessageType
from outpost.fed.incident_events import MODE, STREAMS, content_digest
from outpost.fed.incident_sender import IncidentSender
from outpost.fed.peers import Peer
from outpost.fed.revisions import source_revision
from outpost.store import Transaction
from outpost.transport.governor import OutboundItem
from outpost.transport.models import TrafficClass

GUARD = "incident_storage_receipt_v1"


class IncidentReceipts:
    def __init__(self, sender: IncidentSender) -> None:
        self.sender = sender
        assert sender.governor.outbox is not None
        sender.governor.outbox.attempt_guards[GUARD] = self.authorize_attempt

    async def _current(
        self, tx: Transaction, peer_id: int, value: dict[str, Any]
    ) -> tuple[Peer, bytes]:
        sync = self.sender.sync
        peer = await sync.incident_handoff._peer(tx, peer_id)
        if (
            not sync.incident_events.supported(peer)
            or not self.sender.peers.is_online(peer)
            or not self.sender.identity()
            or self.sender.identity() != sync.local_mesh_id
            or value.get("mesh_id") != self.sender.identity()
            or value.get("target_mesh_id") != peer.mesh_id
            or value.get("stream") not in STREAMS
            or value.get("mode") != MODE
            or value.get("state") != "stored"
        ):
            raise ValueError("incident receipt reply is outside current identity/peer policy")
        rows = await tx.read(
            "SELECT i.payload_json,i.source_epoch,i.source_revision,r.epoch,r.revision "
            "FROM fed_inbox_item i JOIN fed_revision_receipt r "
            "ON r.peer_id=i.peer_id AND r.stream=i.stream AND r.uid=i.uid "
            "WHERE i.peer_id=? AND i.stream=? AND i.uid=?",
            (peer_id, value["stream"], value["uid"]),
        )
        if not rows:
            raise ValueError("incident receipt reply no longer has retained storage evidence")
        checkpoints = await tx.read(
            "SELECT cursor FROM fed_cursor WHERE peer_id=? AND stream='_reconcile' "
            "AND direction='recv'",
            (peer_id,),
        )
        checkpoint = json.loads(checkpoints[0]["cursor"]) if checkpoints else {}
        if (
            not isinstance(checkpoint, dict)
            or checkpoint.get("status") == "blocked"
            or checkpoint.get("epoch") not in (None, value["epoch"])
        ):
            raise ValueError("incident receipt reply lineage requires review")
        row = rows[0]
        payload = json.loads(row["payload_json"])
        if (
            row["epoch"] != value["epoch"]
            or row["revision"] != value["revision"]
            or row["source_epoch"] != value["epoch"]
            or row["source_revision"] != value["revision"]
            or content_digest(payload) != value["digest"]
        ):
            raise ValueError("incident receipt reply evidence changed")
        await sync.incident_events._scope(
            tx,
            peer,
            {
                "stream": value["stream"],
                "uid": value["uid"],
                "epoch": value["epoch"],
                "payload": payload,
            },
        )
        return peer, await self.sender.peers.secret(peer.mesh_id, transaction=tx)

    async def admit(self, peer_id: int, receipt: dict[str, Any]) -> tuple[int, ...]:
        if (
            set(receipt) != {"mode", "stream", "uid", "epoch", "revision", "digest", "state"}
            or type(receipt["mode"]) is not int
            or receipt["mode"] != MODE
            or not isinstance(receipt["stream"], str)
            or receipt["stream"] not in STREAMS
            or not isinstance(receipt["uid"], str)
            or not isinstance(receipt["digest"], str)
            or len(receipt["digest"]) != 64
            or any(char not in "0123456789abcdef" for char in receipt["digest"])
            or receipt["state"] != "stored"
        ):
            raise ValueError("invalid exact incident receipt reply")
        source_revision(receipt)
        value = {**receipt, "mesh_id": self.sender.identity()}
        async with self.sender.sync.database.transaction() as tx:
            peer = await self.sender.sync.incident_handoff._peer(tx, peer_id)
            value["target_mesh_id"] = peer.mesh_id
            peer, secret = await self._current(tx, peer_id, value)
            encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
            secret_digest = hashlib.sha256(secret).hexdigest()
            prior = await tx.read(
                "SELECT * FROM fed_incident_receipt_reply WHERE peer_id=? AND stream=? AND uid=?",
                (peer_id, value["stream"], value["uid"]),
            )
            saved = dict(prior[0]) if prior else None
            if saved and saved["value_json"] == encoded and saved["secret_digest"] == secret_digest:
                active = await tx.read(
                    "SELECT 1 FROM outbound_work WHERE queue_key=? "
                    "AND state IN ('pending','held','sending') LIMIT 1",
                    (saved["queue_key"],),
                )
                if active:
                    return tuple(json.loads(saved["frame_ids"]))
            counter = await self.sender.peers.next_counter(peer.mesh_id, transaction=tx)
            frames = self.sender.codec.encode(MessageType.INCIDENT_RECEIPT, value, counter, secret)
            queue_key = "incident-receipt:" + uuid.uuid4().hex
            channel, port = self.sender.routing()
            admitted = await self.sender.governor.admit_many_result(
                [
                    OutboundItem(
                        text="",
                        dest="^all",
                        channel=channel,
                        portnum=port,
                        traffic_class=TrafficClass.FEDERATION,
                        want_ack=False,
                        binary_payload=frame,
                        multipart=len(frames) > 1,
                        queue_key=queue_key,
                        guard_kind=GUARD,
                        supersedes=saved["queue_key"] if saved else None,
                    )
                    for frame in frames
                ],
                transaction=tx,
            )
            if admitted.rejection_reason is not None:
                raise ValueError("incident receipt reply queue rejected")
            await tx.write(
                "INSERT INTO fed_incident_receipt_reply(peer_id,stream,uid,value_json,"
                "secret_digest,counter,queue_key,frame_ids) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(peer_id,stream,uid) DO UPDATE SET value_json=excluded.value_json,"
                "secret_digest=excluded.secret_digest,counter=excluded.counter,"
                "queue_key=excluded.queue_key,frame_ids=excluded.frame_ids",
                (
                    peer_id,
                    value["stream"],
                    value["uid"],
                    encoded,
                    secret_digest,
                    counter,
                    queue_key,
                    json.dumps(admitted.item_ids),
                ),
            )
            if (
                self.sender.identity() != value["mesh_id"]
                or self.sender.sync.local_mesh_id != value["mesh_id"]
                or not self.sender.sync.incident_events.supported(peer)
                or self.sender.routing() != (channel, port)
            ):
                raise ValueError("incident receipt reply runtime policy changed before commit")
        return admitted.item_ids

    async def authorize_attempt(self, tx: Transaction, work: dict[str, Any]) -> bool:
        rows = await tx.read(
            "SELECT * FROM fed_incident_receipt_reply WHERE queue_key=?",
            (work["queue_key"],),
        )
        if not rows:
            return False
        row = rows[0]
        try:
            value = json.loads(row["value_json"])
            _, secret = await self._current(tx, row["peer_id"], value)
            frames = self.sender.codec.encode(
                MessageType.INCIDENT_RECEIPT,
                value,
                row["counter"],
                secret,
            )
            ids = json.loads(row["frame_ids"])
            return (
                row["secret_digest"] == hashlib.sha256(secret).hexdigest()
                and len(ids) == len(frames)
                and work["id"] in ids
                and work["binary_payload"] == frames[ids.index(work["id"])]
                and work["traffic_class"] == TrafficClass.FEDERATION.value
                and work["destination"] == "^all"
                and not work["want_ack"]
                and (work["channel"], work["portnum"]) == self.sender.routing()
            )
        except (ValueError, KeyError, TypeError):
            return False
