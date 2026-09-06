"""Original-producer plain notes; advisory content, never responder actions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from outpost.fed.framing import wire_int
from outpost.fed.peers import FederationPeerService, Peer
from outpost.fed.revisions import CAPABILITY as REVISION_CAPABILITY
from outpost.fed.revisions import MODE as REVISION_MODE
from outpost.fed.revisions import source_revision
from outpost.store import Transaction

if TYPE_CHECKING:
    from outpost.fed.sync import FederationSyncService

CAPABILITY = "incident_updates"
MODE = 1
STREAM = "incident_updates"


class IncidentUpdates:
    def __init__(self, sync: FederationSyncService) -> None:
        self.sync = sync

    def supported(self, peer: Peer) -> bool:
        return (
            peer.capabilities.get(CAPABILITY) == MODE
            and type(peer.capabilities.get(CAPABILITY)) is int
            and peer.capabilities.get(REVISION_CAPABILITY) == REVISION_MODE
            and peer.sync_incidents
            and self.sync.module_enabled("watch")
        )

    @staticmethod
    def validate(source: str, uid: str, payload: dict[str, Any]) -> None:
        prefix = f"{source}:"
        for identity in (uid, payload.get("incident_uid")):
            if (
                not isinstance(identity, str)
                or not identity.startswith(prefix)
                or not identity.removeprefix(prefix)
                or len(identity) > 160
            ):
                raise ValueError("incident note identity does not match its original producer")
        if payload.get("uid") != uid or payload.get("kind") != "update":
            raise ValueError("only original-producer plain update notes are supported")
        body, author = payload.get("body"), payload.get("author_label")
        if not isinstance(body, str) or not body.strip() or len(body) > 500:
            raise ValueError("incident note must contain 1..500 characters")
        if not isinstance(author, str) or not author.strip() or len(author) > 160:
            raise ValueError("invalid incident note author label")
        # Reports render with datetime.fromtimestamp; reject values it cannot
        # represent instead of letting a reviewed note break the whole timeline.
        wire_int(payload.get("created_at"), "incident note timestamp", maximum=253402300799)

    async def export(self, peer: Peer, local_uid: str) -> dict[str, Any] | None:
        if not self.supported(peer) or not self.sync.local_mesh_id:
            return None
        rows = await self.sync.database.read(
            "SELECT u.uid,u.origin_incident_uid,u.body,u.author_label,u.created_at,"
            "i.lat,i.lon FROM incident_update u JOIN incident i ON i.id=u.incident_id "
            "WHERE u.uid=? AND u.source_node IS NULL AND u.kind='update' "
            "AND u.uid NOT LIKE '!%:%' AND i.uid NOT LIKE '!%:%' "
            "AND u.origin_incident_uid=i.uid AND u.lat IS NULL AND u.lon IS NULL",
            (local_uid,),
        )
        if not rows or not self.sync.incident_allowed(peer, rows[0]["lat"], rows[0]["lon"]):
            return None
        row = rows[0]
        payload = {
            "uid": self.sync.wire_uid(row["uid"]),
            "incident_uid": self.sync.wire_uid(row["origin_incident_uid"]),
            "kind": "update",
            "body": row["body"],
            "author_label": row["author_label"],
            "created_at": row["created_at"],
        }
        try:
            self.validate(self.sync.local_mesh_id, payload["uid"], payload)
        except ValueError:
            return None  # Never silently truncate unsupported historical content.
        return payload

    async def import_note(
        self, tx: Transaction, inbox: Any, payload: dict[str, Any], operator: str, now: int
    ) -> None:
        peers = await tx.read("SELECT * FROM fed_peer WHERE id=?", (inbox["peer_id"],))
        peer = FederationPeerService._peer(peers[0])
        if peer.state != "active" or not self.supported(peer):
            raise ValueError("incident note sync is no longer allowed")
        uid = str(inbox["uid"])
        self.validate(peer.mesh_id, uid, payload)
        revision = source_revision(
            {"epoch": inbox["source_epoch"], "revision": inbox["source_revision"]}
        )
        assert revision is not None
        epoch, sequence = revision
        parents = await tx.read(
            "SELECT o.incident_id,o.original_incident_id,o.source_epoch,i.lat,i.lon "
            "FROM incident_origin o JOIN incident i ON i.id=o.original_incident_id "
            "WHERE o.origin_uid=? AND o.origin_node=? AND o.source_kind='federation' "
            "AND i.uid=o.origin_uid",
            (payload["incident_uid"], peer.mesh_id),
        )
        if not parents:
            raise ValueError("Import the original parent incident before reviewing its note")
        parent = parents[0]
        if not self.sync.incident_allowed(peer, parent["lat"], parent["lon"]):
            raise ValueError("parent incident is outside current peer geographic policy")
        if parent["source_epoch"] not in (None, epoch):
            raise ValueError("incident note producer lineage differs from its parent")
        existing = await tx.read("SELECT * FROM incident_update WHERE uid=?", (uid,))
        author = f"federation:{peer.mesh_id}: {payload['author_label']}"
        if existing:
            note = existing[0]
            if (
                note["source_node"] != peer.mesh_id
                or note["origin_incident_uid"] != payload["incident_uid"]
                or note["incident_id"] != parent["original_incident_id"]
            ):
                raise ValueError("incident note cannot replace or reparent another origin")
            if note["source_epoch"] != epoch:
                raise ValueError("incident note producer lineage changed")
            if note["source_revision"] is None or sequence <= note["source_revision"]:
                raise ValueError("incident note revision is already imported or stale")
            await tx.write(
                "UPDATE incident_update SET body=?,author_label=?,created_at=?,"
                "source_revision=? WHERE uid=?",
                (payload["body"], author, payload["created_at"], sequence, uid),
            )
        else:
            seq = await tx.read(
                "SELECT COALESCE(MAX(seq),0)+1 value FROM incident_update WHERE incident_id=?",
                (parent["original_incident_id"],),
            )
            await tx.write(
                "INSERT INTO incident_update(uid,incident_id,seq,author_label,kind,body,"
                "created_at,source_node,origin_incident_uid,source_epoch,source_revision) "
                "VALUES(?,?,?,?,'update',?,?,?,?,?,?)",
                (
                    uid,
                    parent["original_incident_id"],
                    seq[0]["value"],
                    author,
                    payload["body"],
                    payload["created_at"],
                    peer.mesh_id,
                    payload["incident_uid"],
                    epoch,
                    sequence,
                ),
            )
        # Original row ownership survives merge/unmerge. Canonical provenance makes
        # the reviewed note visible without mutating any local incident fields.
        await self.sync._incident_provenance(
            tx,
            incident_id=int(parent["incident_id"]),
            origin_uid=payload["incident_uid"],
            source_node=peer.mesh_id,
            event_kind="federation_note_revised" if existing else "federation_note_imported",
            payload={**payload, "source_epoch": epoch, "source_revision": sequence},
            source_updated_at=payload["created_at"],
            recorded_at=now,
            actor=operator,
        )
