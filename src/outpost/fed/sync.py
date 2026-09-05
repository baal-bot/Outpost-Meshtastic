from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from outpost.audit import write_audit
from outpost.fed.peers import Peer
from outpost.fed.revisions import RevisionIndex, source_revision
from outpost.store import Database, Transaction
from outpost.store.incident_refs import incident_reference


@dataclass(frozen=True)
class ManifestItem:
    stream: str
    uid: str
    version: int
    digest: str

    def json(self) -> dict[str, str | int]:
        return {"s": self.stream, "u": self.uid, "v": self.version, "d": self.digest}


class FederationSyncService:
    INCIDENT_TERMINAL = {"resolved", "false_alarm", "expired"}
    INCIDENT_FIELDS = (
        "type",
        "severity",
        "status",
        "title",
        "body",
        "lat",
        "lon",
        "location_text",
        "radius_m",
        "reporter_label",
        "created_at",
        "updated_at",
        "expires_at",
        "resolved_at",
        "resolution_note",
    )

    def __init__(
        self,
        database: Database,
        local_mesh_id: str = "",
        module_enabled: Callable[[str], bool] | None = None,
    ) -> None:
        self.database = database
        self.local_mesh_id = local_mesh_id
        self.module_enabled = module_enabled or (lambda _name: True)
        self.revisions = RevisionIndex(self)

    @staticmethod
    def stream_module(stream: str) -> str | None:
        if stream.startswith("board:"):
            return "bbs"
        if stream in {"incidents", "alerts"}:
            return "watch"
        return None

    def stream_enabled(self, stream: str) -> bool:
        module = self.stream_module(stream)
        return module is None or self.module_enabled(module)

    def _wire_uid(self, uid: str) -> str:
        if uid.startswith("!") and ":" in uid:
            return uid
        return f"{self.local_mesh_id}:{uid}" if self.local_mesh_id else uid

    def wire_uid(self, uid: str) -> str:
        return self._wire_uid(uid)

    @staticmethod
    def incident_allowed(peer: Peer, lat: object, lon: object) -> bool:
        if lat is None or lon is None:
            return True
        if peer.incident_lat is None or peer.incident_lon is None:
            return False
        lat1, lat2 = math.radians(float(str(lat))), math.radians(peer.incident_lat)
        delta_lat = lat2 - lat1
        delta_lon = math.radians(peer.incident_lon - float(str(lon)))
        value = math.sin(delta_lat / 2) ** 2 + (
            math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
        )
        value = min(1.0, max(0.0, value))
        distance_km = 6_371 * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value))
        return distance_km <= peer.incident_radius_km

    def _local_uid(self, uid: str) -> str | None:
        if not self.local_mesh_id:
            return uid
        prefix = f"{self.local_mesh_id}:"
        return uid[len(prefix) :] if uid.startswith(prefix) else None

    def _stored_origin_uid(self, uid: str) -> str:
        return self._local_uid(uid) or uid

    @staticmethod
    def _origin_node_for_uid(uid: str, fallback: str) -> str:
        return uid.split(":", 1)[0] if uid.startswith("!") and ":" in uid else fallback

    def local_thread_uid(self, uid: str) -> str:
        return self._local_uid(uid) or uid

    async def canonical_remote_uid(self, uid: str, transaction: Transaction | None = None) -> str:
        if not uid.startswith("!") or ":" not in uid:
            return uid
        mesh_id, suffix = uid.split(":", 1)
        store = transaction or self.database
        rows = await store.read(
            "SELECT s.old_mesh_id FROM fed_peer_successor s JOIN fed_peer p "
            "ON p.id=s.successor_peer_id WHERE p.mesh_id=?",
            (mesh_id,),
        )
        return f"{rows[0]['old_mesh_id']}:{suffix}" if rows else uid

    async def approved_thread(self, slug: str, uid: str) -> bool:
        rows = await self.database.read(
            "SELECT t.id FROM thread t JOIN board b ON b.id=t.board_id "
            "WHERE t.uid=? AND b.slug=? AND b.federated=1",
            (self.local_thread_uid(uid), slug),
        )
        return bool(rows)

    async def import_approved_replies(self, operator: str, now: int) -> int:
        if not self.module_enabled("bbs"):
            return 0
        rows = await self.database.read(
            "SELECT id,stream,payload_json FROM fed_inbox_item "
            "WHERE state='pending' AND stream LIKE 'board:%' ORDER BY id"
        )
        imported = 0
        for row in rows:
            payload = json.loads(row["payload_json"])
            slug = str(row["stream"])[6:]
            approved = await self.approved_thread(slug, str(payload.get("thread_uid", "")))
            if approved or int(payload.get("seq", 0)) == 1:
                await self.import_inbox(int(row["id"]), operator, now)
                imported += 1
        return imported

    @staticmethod
    def _digest(*values: Any) -> str:
        joined = "\x1f".join("" if value is None else str(value) for value in values)
        return hashlib.sha256(joined.encode()).hexdigest()[:16]

    @staticmethod
    def _payload_digest(encoded: str) -> str:
        return hashlib.sha256(encoded.encode()).hexdigest()[:16]

    @staticmethod
    async def _incident_provenance(
        transaction: Transaction,
        *,
        incident_id: int,
        origin_uid: str,
        source_node: str,
        event_kind: str,
        payload: dict[str, Any],
        source_updated_at: int,
        recorded_at: int,
        actor: str,
    ) -> None:
        await transaction.write(
            "INSERT INTO incident_provenance(incident_id,origin_uid,source_node,event_kind,"
            "payload_json,source_updated_at,recorded_at,actor) VALUES(?,?,?,?,?,?,?,?)",
            (
                incident_id,
                origin_uid,
                source_node,
                event_kind,
                json.dumps(payload, separators=(",", ":"), sort_keys=True),
                source_updated_at,
                recorded_at,
                actor[:160],
            ),
        )

    async def manifest(
        self,
        peer: Peer,
        limit: int = 100,
        *,
        snapshot: int | None = None,
        before: tuple[int, str, str] | None = None,
    ) -> list[ManifestItem]:
        if peer.state != "active":
            raise ValueError("sync requires an active peer")
        items: list[ManifestItem] = []
        if peer.boards and self.module_enabled("bbs"):
            placeholders = ",".join("?" for _ in peer.boards)
            rows = await self.database.read(
                f"""SELECT p.uid,p.created_at,p.edited_at,p.body,p.author_label,t.uid thread_uid,
                    b.slug FROM post p JOIN thread t ON t.id=p.thread_id
                    JOIN board b ON b.id=t.board_id
                    WHERE b.federated=1 AND b.slug IN ({placeholders}) AND p.hidden=0
                      AND t.hidden=0 AND b.archived=0
                    ORDER BY COALESCE(p.edited_at,p.created_at) DESC""",  # noqa: S608
                tuple(peer.boards),
            )
            items.extend(
                ManifestItem(
                    f"board:{row['slug']}",
                    self._wire_uid(row["uid"]),
                    row["edited_at"] or row["created_at"],
                    self._digest(
                        self._wire_uid(row["uid"]),
                        self._wire_uid(row["thread_uid"]),
                        row["body"],
                        row["author_label"],
                    ),
                )
                for row in rows
            )
        if peer.sync_incidents and self.module_enabled("watch"):
            rows = await self.database.read(
                "SELECT uid,updated_at,status,severity,title,body,lat,lon FROM incident "
                "WHERE merged_into_id IS NULL ORDER BY updated_at DESC",
            )
            items.extend(
                ManifestItem(
                    "incidents", self._wire_uid(row["uid"]), row["updated_at"], self._digest(*row)
                )
                for row in rows
                if self.incident_allowed(peer, row["lat"], row["lon"])
            )
        if peer.relay_alerts and self.module_enabled("watch"):
            rows = await self.database.read(
                "SELECT uid,raised_at,cancelled_at,severity,headline,expires_at FROM alert "
                "ORDER BY raised_at DESC",
            )
            items.extend(
                ManifestItem(
                    "alerts",
                    self._wire_uid(row["uid"]),
                    row["cancelled_at"] or row["raised_at"],
                    self._digest(*row),
                )
                for row in rows
            )
        items.sort(key=lambda item: (item.version, item.stream, item.uid), reverse=True)
        if snapshot is not None:
            items = [item for item in items if item.version <= snapshot]
        if before is not None:
            items = [item for item in items if (item.version, item.stream, item.uid) < before]
        return items[:limit]

    async def missing(self, manifest: list[dict[str, Any]]) -> list[dict[str, str]]:
        requested: list[dict[str, str]] = []
        versions = {
            "incidents": ("incident", "updated_at"),
            "alerts": ("alert", "COALESCE(cancelled_at,raised_at)"),
        }
        for item in manifest[:100]:
            stream = str(item.get("stream", item.get("s", "")))
            if not self.stream_enabled(stream):
                continue
            uid = str(item.get("uid", item.get("u", "")))
            if not uid or len(uid) > 160:
                continue
            table_version = versions.get(stream)
            if stream.startswith("board:"):
                table_version = ("post", "COALESCE(edited_at,created_at)")
            if table_version is None:
                continue
            table, version_column = table_version
            canonical_uid = await self.canonical_remote_uid(uid)
            if stream == "incidents":
                stored_uid = self._stored_origin_uid(uid)
                rows = await self.database.read(
                    "SELECT source_updated_at version FROM incident_origin "
                    "WHERE origin_uid IN (?,?,?)",
                    (uid, canonical_uid, stored_uid),
                )
                if not rows:
                    rows = await self.database.read(
                        "SELECT updated_at version FROM incident WHERE uid IN (?,?,?)",
                        (uid, canonical_uid, stored_uid),
                    )
            else:
                rows = await self.database.read(
                    f"SELECT {version_column} version FROM {table} "  # noqa: S608
                    "WHERE uid IN (?,?)",
                    (uid, canonical_uid),
                )
            remote_version = int(item.get("version", item.get("v", 0)) or 0)
            if not rows or remote_version > max(int(row["version"] or 0) for row in rows):
                requested.append({"stream": stream, "uid": uid})
        return requested

    async def export_items(
        self, peer: Peer, requests: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        if peer.state != "active":
            raise ValueError("item export requires an active peer")
        exported: list[dict[str, Any]] = []
        for request in requests[:8]:
            stream, uid = str(request.get("stream", "")), str(request.get("uid", ""))
            if not self.stream_enabled(stream):
                continue
            local_uid = self._local_uid(uid)
            if local_uid is None:
                continue
            rows: list[Any] = []
            if stream.startswith("board:") and stream[6:] in peer.boards:
                rows = await self.database.read(
                    """SELECT p.uid,p.seq,p.author_label,p.origin_node,p.body,p.created_at,
                       p.edited_at,t.uid thread_uid,t.subject,b.slug FROM post p
                       JOIN thread t ON t.id=p.thread_id JOIN board b ON b.id=t.board_id
                       WHERE p.uid=? AND p.hidden=0 AND t.hidden=0 AND b.archived=0
                       AND b.federated=1 AND b.slug=?""",
                    (local_uid, stream[6:]),
                )
            elif stream == "incidents" and peer.sync_incidents:
                rows = await self.database.read(
                    "SELECT id,uid,type,severity,status,title,body,lat,lon,location_text,radius_m,"
                    "reporter_label,origin_node,created_at,updated_at,expires_at,resolved_at,"
                    "resolution_note FROM incident WHERE uid=? AND merged_into_id IS NULL",
                    (local_uid,),
                )
                if rows and not self.incident_allowed(peer, rows[0]["lat"], rows[0]["lon"]):
                    rows = []
            elif stream == "alerts" and peer.relay_alerts:
                rows = await self.database.read(
                    "SELECT uid,severity,headline,body,source,source_ref,raised_by,raised_at,"
                    "effective_at,expires_at,cancelled_at FROM alert WHERE uid=?",
                    (local_uid,),
                )
            if rows:
                payload = dict(rows[0])
                incident_id = payload.pop("id", None)
                payload["uid"] = uid
                if stream.startswith("board:"):
                    payload["thread_uid"] = self._wire_uid(str(payload["thread_uid"]))
                    payload["origin_node"] = uid.split(":", 1)[0]
                elif stream == "incidents" and incident_id is not None:
                    origins = await self.database.read(
                        "SELECT origin_uid FROM incident_origin WHERE incident_id=? "
                        "ORDER BY origin_uid",
                        (incident_id,),
                    )
                    payload["origin_uids"] = [
                        self._wire_uid(str(value["origin_uid"])) for value in origins
                    ] or [uid]
                exported.append(
                    {
                        "stream": stream,
                        "uid": uid,
                        "payload": payload,
                        "digest": self._digest(*payload.values()),
                    }
                )
        return exported

    async def quarantine(self, peer: Peer, item: dict[str, Any], now: int) -> bool:
        stream, uid = str(item.get("stream", "")), str(item.get("uid", ""))
        if not self.stream_enabled(stream):
            raise ValueError(f"{self.stream_module(stream)} module is disabled")
        payload = item.get("payload")
        allowed = (
            (stream.startswith("board:") and stream[6:] in peer.boards)
            or (stream == "incidents" and peer.sync_incidents)
            or (stream == "alerts" and peer.relay_alerts)
        )
        if not allowed or not uid or len(uid) > 160 or not isinstance(payload, dict):
            raise ValueError("inbound item is outside peer sync policy")
        revision = source_revision(item)
        encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        if len(encoded.encode()) > 12_000:
            raise ValueError("inbound federation item is too large")
        async with self.database.transaction() as transaction:
            digest = self._payload_digest(encoded)
            receipts = await transaction.read(
                "SELECT epoch,revision,digest FROM fed_revision_receipt "
                "WHERE peer_id=? AND stream=? AND uid=?",
                (peer.id, stream, uid),
            )
            if receipts:
                receipt = receipts[0]
                if revision is None and digest == receipt["digest"]:
                    return False  # Legacy delivery retry of identical stored content.
                if revision is None or revision[0] != receipt["epoch"]:
                    raise ValueError(
                        "federation revision lineage changed; operator review required"
                    )
                if revision[1] < int(receipt["revision"]):
                    return False
                if revision[1] == int(receipt["revision"]):
                    if digest != receipt["digest"]:
                        raise ValueError("conflicting payload for the same producer revision")
                    return False
            recent = await transaction.read(
                "SELECT COUNT(*) count FROM fed_inbox_item WHERE peer_id=? AND received_at>?",
                (peer.id, now - 3600),
            )
            if int(recent[0]["count"]) >= peer.quota_items_per_hour:
                raise ValueError("peer federation item quota exceeded")
            existing = await transaction.read(
                "SELECT id,digest FROM fed_inbox_item WHERE peer_id=? AND stream=? AND uid=?",
                (peer.id, stream, uid),
            )
            if existing and str(existing[0]["digest"]) == digest:
                changed = False
            elif existing:
                await transaction.write(
                    "UPDATE fed_inbox_item SET payload_json=?,digest=?,state='pending',"
                    "received_at=?,reviewed_at=NULL,reviewed_by=NULL,rejection_reason=NULL "
                    "WHERE id=?",
                    (encoded, digest, now, existing[0]["id"]),
                )
                changed = True
            else:
                await transaction.write(
                    "INSERT INTO fed_inbox_item(peer_id,stream,uid,payload_json,digest,"
                    "received_at) VALUES(?,?,?,?,?,?)",
                    (peer.id, stream, uid, encoded, digest, now),
                )
                changed = True
            if revision is not None:
                epoch, sequence = revision
                await transaction.write(
                    "UPDATE fed_inbox_item SET source_epoch=?,source_revision=? "
                    "WHERE peer_id=? AND stream=? AND uid=?",
                    (epoch, sequence, peer.id, stream, uid),
                )
                await transaction.write(
                    "INSERT INTO fed_revision_receipt(peer_id,stream,uid,epoch,revision,digest) "
                    "VALUES(?,?,?,?,?,?) ON CONFLICT(peer_id,stream,uid) DO UPDATE SET "
                    "epoch=excluded.epoch,revision=excluded.revision,digest=excluded.digest",
                    (peer.id, stream, uid, epoch, sequence, digest),
                )
            await transaction.write(
                "INSERT INTO fed_cursor(peer_id,stream,direction,cursor,updated_at) "
                "VALUES(?,?,'recv',?,?) ON CONFLICT(peer_id,stream,direction) "
                "DO UPDATE SET cursor=excluded.cursor,updated_at=excluded.updated_at",
                (peer.id, stream, uid, now),
            )
        return changed

    @staticmethod
    def _incident_values(payload: dict[str, Any], *, revisioned: bool = False) -> dict[str, Any]:
        values: dict[str, Any] = {
            "type": str(payload["type"]),
            "severity": str(payload["severity"]),
            "status": str(payload["status"]),
            "title": str(payload["title"])[:64],
            "body": str(payload["body"])[:4000] if payload.get("body") is not None else None,
            "lat": payload.get("lat"),
            "lon": payload.get("lon"),
            "location_text": (
                str(payload["location_text"])[:500]
                if payload.get("location_text") is not None
                else None
            ),
            "radius_m": payload.get("radius_m"),
            "reporter_label": str(payload.get("reporter_label") or "Federated peer")[:80],
            "created_at": int(payload["created_at"]),
            "updated_at": int(payload["updated_at"]),
            "expires_at": payload.get("expires_at"),
            "resolved_at": payload.get("resolved_at"),
            "resolution_note": (
                str(payload["resolution_note"])[:500]
                if payload.get("resolution_note") is not None
                else None
            ),
        }
        if values["type"] not in {
            "hazard",
            "road",
            "fire",
            "medical",
            "police",
            "utility",
            "missing",
            "animal",
            "weather",
            "resource",
            "other",
        }:
            raise ValueError("invalid federated incident type")
        if values["severity"] not in {"info", "caution", "urgent", "critical"}:
            raise ValueError("invalid federated incident severity")
        if values["status"] not in {"open", "monitoring", "resolved", "false_alarm", "expired"}:
            raise ValueError("invalid federated incident status")
        if not values["title"]:
            raise ValueError("federated incident title is required")
        lat, lon = values["lat"], values["lon"]
        if (lat is None) != (lon is None):
            raise ValueError("federated incident coordinates must be a pair")
        if lat is not None and not (
            -90 <= float(str(lat)) <= 90 and -180 <= float(str(lon)) <= 180
        ):
            raise ValueError("invalid federated incident coordinates")
        if (
            int(values["created_at"]) < 0
            or int(values["updated_at"]) < 0
            or (not revisioned and int(values["updated_at"]) < int(values["created_at"]))
        ):
            raise ValueError("invalid federated incident timestamps")
        return values

    async def _adopt_incident_origins(
        self,
        transaction: Transaction,
        *,
        payload: dict[str, Any],
        primary_uid: str,
        incident_id: int,
        original_incident_id: int,
        peer_id: int,
        source_node: str,
        source_updated_at: int,
        digest: str,
        now: int,
    ) -> None:
        candidates = payload.get("origin_uids", [])
        origin_uids = [primary_uid]
        if isinstance(candidates, list):
            origin_uids.extend(
                value
                for value in candidates[:100]
                if isinstance(value, str) and value and len(value) <= 160
            )
        for wire_origin_uid in dict.fromkeys(origin_uids):
            origin_uid = self._stored_origin_uid(wire_origin_uid)
            existing = await transaction.read(
                "SELECT incident_id FROM incident_origin WHERE origin_uid=?", (origin_uid,)
            )
            if existing:
                if int(existing[0]["incident_id"]) != incident_id:
                    await transaction.write(
                        "UPDATE incident SET reconciliation_review=1 WHERE id IN (?,?)",
                        (incident_id, existing[0]["incident_id"]),
                    )
                continue
            await transaction.write(
                "INSERT INTO incident_origin(origin_uid,incident_id,original_incident_id,"
                "origin_node,source_kind,source_peer_id,first_seen_at,last_seen_at,"
                "source_updated_at,source_digest) VALUES(?,?,?,?,'federation',?,?,?,?,?)",
                (
                    origin_uid,
                    incident_id,
                    original_incident_id,
                    self._origin_node_for_uid(wire_origin_uid, source_node),
                    peer_id,
                    now,
                    now,
                    source_updated_at,
                    digest if origin_uid == primary_uid else "",
                ),
            )

    async def _import_incident(
        self,
        transaction: Transaction,
        inbox: Any,
        uid: str,
        payload: dict[str, Any],
        operator: str,
        now: int,
    ) -> None:
        revision = (
            (str(inbox["source_epoch"]), int(inbox["source_revision"]))
            if "source_revision" in inbox.keys() and inbox["source_revision"] is not None
            else None
        )
        values = self._incident_values(payload, revisioned=revision is not None)
        source_updated_at = int(values["updated_at"])
        source_node = str(inbox["mesh_id"])
        digest = str(inbox["digest"] or "")[:64]
        peer_id = int(inbox["peer_id"])
        origin_uid = self._stored_origin_uid(uid)
        origins = await transaction.read(
            "SELECT incident_id,original_incident_id,origin_node,source_kind,source_updated_at,"
            "source_digest,source_epoch,source_revision "
            "FROM incident_origin WHERE origin_uid=?",
            (origin_uid,),
        )
        if not origins:
            legacy = await transaction.read(
                "SELECT id,updated_at FROM incident WHERE uid=?", (origin_uid,)
            )
            if legacy:
                await transaction.write(
                    "INSERT INTO incident_origin(origin_uid,incident_id,original_incident_id,"
                    "origin_node,source_kind,source_peer_id,first_seen_at,last_seen_at,"
                    "source_updated_at,source_digest) VALUES(?,?,?,?,'federation',?,?,?,?,?)",
                    (
                        origin_uid,
                        legacy[0]["id"],
                        legacy[0]["id"],
                        source_node,
                        peer_id,
                        now,
                        now,
                        legacy[0]["updated_at"],
                        "",
                    ),
                )
                origins = await transaction.read(
                    "SELECT incident_id,original_incident_id,origin_node,source_kind,"
                    "source_updated_at,source_digest,source_epoch,source_revision "
                    "FROM incident_origin WHERE origin_uid=?",
                    (origin_uid,),
                )
        if not origins:
            local_ref = await incident_reference(transaction, origin_uid)
            incident_id = await transaction.write(
                """INSERT INTO incident(uid,local_ref,type,severity,status,title,body,
                   lat,lon,location_text,radius_m,reporter_label,origin_node,created_at,
                   updated_at,expires_at,resolved_at,resolution_note,source,unverified,
                   flagged_for_review)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'member',1,1)""",
                (
                    origin_uid,
                    local_ref,
                    values["type"],
                    values["severity"],
                    values["status"],
                    values["title"],
                    values["body"],
                    values["lat"],
                    values["lon"],
                    values["location_text"],
                    values["radius_m"],
                    values["reporter_label"],
                    self._origin_node_for_uid(uid, source_node),
                    values["created_at"],
                    values["updated_at"],
                    values["expires_at"],
                    values["resolved_at"],
                    values["resolution_note"],
                ),
            )
            await self._adopt_incident_origins(
                transaction,
                payload=payload,
                primary_uid=origin_uid,
                incident_id=incident_id,
                original_incident_id=incident_id,
                peer_id=peer_id,
                source_node=source_node,
                source_updated_at=source_updated_at,
                digest=digest,
                now=now,
            )
            await self._incident_provenance(
                transaction,
                incident_id=incident_id,
                origin_uid=origin_uid,
                source_node=source_node,
                event_kind="federation_imported",
                payload=values,
                source_updated_at=source_updated_at,
                recorded_at=now,
                actor=operator,
            )
            if revision is not None and self._origin_node_for_uid(uid, source_node) == source_node:
                await transaction.write(
                    "UPDATE incident_origin SET source_epoch=?,source_revision=? "
                    "WHERE origin_uid=?",
                    (*revision, origin_uid),
                )
            return

        origin = origins[0]
        incident_id = int(origin["incident_id"])
        original_id = int(origin["original_incident_id"])
        source_version = int(origin["source_updated_at"])
        source_digest = str(origin["source_digest"] or "")
        original_rows = await transaction.read("SELECT * FROM incident WHERE id=?", (original_id,))
        if not original_rows:
            raise ValueError("incident origin points to missing original")
        original = original_rows[0]
        foreign_relay = (
            str(origin["source_kind"]) == "local" or str(origin["origin_node"]) != source_node
        )
        # A relay's local sequence is never authority over another origin. Preserve
        # the existing human-review path, regardless of clocks or relay revisions.
        modern_authority = revision is not None and not foreign_relay
        prior_revision = origin["source_revision"]
        if (
            not foreign_relay
            and prior_revision is not None
            and (revision is None or revision[0] != origin["source_epoch"])
        ):
            raise ValueError("incident producer lineage changed; operator review required")
        ordering = source_updated_at - source_version
        if modern_authority and revision is not None:
            ordering = revision[1] - int(prior_revision) if prior_revision is not None else 1
        if foreign_relay and revision is not None:
            ordering = 1
        if ordering < 0:
            await transaction.write(
                "UPDATE incident_origin SET last_seen_at=? WHERE origin_uid=?", (now, origin_uid)
            )
            await self._incident_provenance(
                transaction,
                incident_id=incident_id,
                origin_uid=origin_uid,
                source_node=source_node,
                event_kind="stale_update_ignored",
                payload=values,
                source_updated_at=source_updated_at,
                recorded_at=now,
                actor=operator,
            )
            return
        same_snapshot = all(original[field] == values[field] for field in self.INCIDENT_FIELDS)
        if ordering == 0 and (
            (source_digest and digest != source_digest) or (not source_digest and not same_snapshot)
        ):
            await transaction.write(
                "UPDATE incident SET reconciliation_review=1 WHERE id=?", (incident_id,)
            )
            await self._incident_provenance(
                transaction,
                incident_id=incident_id,
                origin_uid=origin_uid,
                source_node=source_node,
                event_kind="concurrent_update_conflict",
                payload=values,
                source_updated_at=source_updated_at,
                recorded_at=now,
                actor=operator,
            )
            return
        if ordering == 0 and (source_digest == digest or same_snapshot):
            await transaction.write(
                "UPDATE incident_origin SET last_seen_at=?,source_digest=? WHERE origin_uid=?",
                (now, digest, origin_uid),
            )
            return

        adopted_identity_only = str(original["uid"]) != origin_uid
        merged = original["merged_into_id"] is not None
        if foreign_relay:
            await transaction.write(
                "UPDATE incident SET reconciliation_review=1 WHERE id=?", (incident_id,)
            )
            await self._incident_provenance(
                transaction,
                incident_id=incident_id,
                origin_uid=origin_uid,
                source_node=source_node,
                event_kind="relayed_origin_update",
                payload=values,
                source_updated_at=source_updated_at,
                recorded_at=now,
                actor=operator,
            )
            return
        update_fields = tuple(field for field in self.INCIDENT_FIELDS if field != "created_at")
        if adopted_identity_only or merged:
            await transaction.write(
                "UPDATE incident SET reconciliation_review=1 WHERE id=?", (incident_id,)
            )
            if merged:
                assignments = ",".join(f"{field}=?" for field in update_fields)
                await transaction.write(
                    f"UPDATE incident SET {assignments},unverified=1,flagged_for_review=1 "  # noqa: S608
                    "WHERE id=?",
                    (
                        *(values[field] for field in update_fields),
                        original_id,
                    ),
                )
            event_kind = "merged_origin_update" if merged else "adopted_identity_update"
        else:
            resolution_withheld = (
                str(original["status"]) == "monitoring"
                and str(values["status"]) in self.INCIDENT_TERMINAL
            )
            update_values = dict(values)
            if resolution_withheld:
                for field in ("status", "resolved_at", "resolution_note"):
                    update_values[field] = original[field]
            assignments = ",".join(f"{field}=?" for field in update_fields)
            await transaction.write(
                f"UPDATE incident SET {assignments},origin_node=?,unverified=1,"  # noqa: S608
                "flagged_for_review=1,reconciliation_review=? WHERE id=?",
                (
                    *(update_values[field] for field in update_fields),
                    source_node,
                    int(resolution_withheld),
                    original_id,
                ),
            )
            event_kind = "resolution_withheld" if resolution_withheld else "federation_updated"
        await transaction.write(
            "UPDATE incident_origin SET last_seen_at=?,source_updated_at=?,source_digest=?,"
            "source_peer_id=? WHERE origin_uid=?",
            (now, source_updated_at, digest, peer_id, origin_uid),
        )
        if modern_authority and revision is not None:
            await transaction.write(
                "UPDATE incident_origin SET source_epoch=?,source_revision=? WHERE origin_uid=?",
                (*revision, origin_uid),
            )
        await self._adopt_incident_origins(
            transaction,
            payload=payload,
            primary_uid=origin_uid,
            incident_id=incident_id,
            original_incident_id=original_id,
            peer_id=peer_id,
            source_node=source_node,
            source_updated_at=source_updated_at,
            digest=digest,
            now=now,
        )
        await self._incident_provenance(
            transaction,
            incident_id=incident_id,
            origin_uid=origin_uid,
            source_node=source_node,
            event_kind=event_kind,
            payload=values,
            source_updated_at=source_updated_at,
            recorded_at=now,
            actor=operator,
        )

    async def import_relay_incident(
        self,
        transaction: Transaction,
        payload: dict[str, Any],
        *,
        origin_node: str,
        received_from_peer_id: int,
        envelope_id: str,
        now: int,
    ) -> dict[str, Any]:
        """Import a signed relay incident through the normal reconciliation path."""
        if not self.module_enabled("watch"):
            raise ValueError("watch module is disabled")
        wrapped = payload.get("payload")
        if wrapped is not None:
            if payload.get("stream", "incidents") != "incidents" or not isinstance(wrapped, dict):
                raise ValueError("relay incident wrapper is invalid")
            incident = wrapped
            uid_value = payload.get("uid") or incident.get("uid")
            digest = str(payload.get("digest") or "")[:64]
        else:
            incident = payload
            uid_value = incident.get("uid") or incident.get("origin_uid")
            digest = str(incident.get("digest") or "")[:64]
        uid = str(uid_value or "").strip()
        if not uid or len(uid) > 160:
            raise ValueError("relay incident uid is required")
        if not uid.startswith("!"):
            uid = f"{origin_node}:{uid}"
        origin_prefix = f"{origin_node}:"
        if not uid.startswith(origin_prefix) or not uid.removeprefix(origin_prefix):
            raise ValueError("relay incident uid does not match its verified origin")
        if not digest:
            encoded = json.dumps(incident, separators=(",", ":"), sort_keys=True)
            digest = self._payload_digest(encoded)
        inbox = {
            "mesh_id": origin_node,
            "peer_id": received_from_peer_id,
            "digest": digest,
        }
        await self._import_incident(
            transaction,
            inbox,
            uid,
            incident,
            f"federation:relay:{envelope_id}",
            now,
        )
        stored_uid = self._stored_origin_uid(uid)
        rows = await transaction.read(
            "SELECT incident_id FROM incident_origin WHERE origin_uid=?", (stored_uid,)
        )
        if not rows:
            raise ValueError("relay incident import did not create an origin record")
        return {"incident_id": int(rows[0]["incident_id"]), "incident_uid": stored_uid}

    async def import_inbox(self, item_id: int, operator: str, now: int) -> str:
        async with self.database.transaction() as transaction:
            rows = await transaction.read(
                "SELECT i.*,p.mesh_id,p.node_name,p.boards,p.sync_incidents,p.relay_alerts "
                "FROM fed_inbox_item i JOIN fed_peer p ON p.id=i.peer_id "
                "WHERE i.id=? AND i.state='pending'",
                (item_id,),
            )
            if not rows:
                raise ValueError("pending federation item not found")
            row = rows[0]
            stream, uid, payload = row["stream"], row["uid"], json.loads(row["payload_json"])
            if not self.stream_enabled(str(stream)):
                raise ValueError(f"{self.stream_module(str(stream))} module is disabled")
            if stream.startswith("board:"):
                slug = stream[6:]
                if slug not in json.loads(row["boards"]):
                    raise ValueError("board is no longer allowed for this peer")
                boards = await transaction.read(
                    "SELECT id FROM board WHERE slug=? AND federated=1 AND archived=0", (slug,)
                )
                if not boards:
                    raise ValueError("destination board is not federated")
                thread_uid = await self.canonical_remote_uid(
                    str(payload["thread_uid"]), transaction
                )
                thread_uid = self.local_thread_uid(thread_uid)
                threads = await transaction.read(
                    "SELECT id FROM thread WHERE uid=? AND board_id=?",
                    (thread_uid, boards[0]["id"]),
                )
                if threads:
                    thread_id = int(threads[0]["id"])
                else:
                    thread_id = await transaction.write(
                        "INSERT INTO thread(uid,board_id,subject,origin_node,created_at,"
                        "last_post_at) VALUES(?,?,?,?,?,?)",
                        (
                            thread_uid,
                            boards[0]["id"],
                            str(payload["subject"])[:160],
                            row["mesh_id"],
                            int(payload["created_at"]),
                            int(payload["created_at"]),
                        ),
                    )
                sequence = await transaction.read(
                    "SELECT COALESCE(MAX(seq),0)+1 value FROM post WHERE thread_id=?",
                    (thread_id,),
                )
                await transaction.write(
                    "INSERT OR IGNORE INTO post(uid,thread_id,seq,author_label,origin_node,body,"
                    "created_at,edited_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        uid,
                        thread_id,
                        sequence[0]["value"],
                        str(payload["author_label"])[:80],
                        row["mesh_id"],
                        str(payload["body"])[:4000],
                        int(payload["created_at"]),
                        payload.get("edited_at"),
                    ),
                )
                if row["source_revision"] is not None:
                    # Only the original producer may revise a retained post. Do not
                    # move it into a different thread or undo local moderation.
                    posts = await transaction.read(
                        "SELECT id,thread_id,origin_node FROM post WHERE uid=?", (uid,)
                    )
                    if posts and (
                        posts[0]["origin_node"] != row["mesh_id"]
                        or not uid.startswith(f"{row['mesh_id']}:")
                        or posts[0]["thread_id"] != thread_id
                    ):
                        raise ValueError(
                            "post revision does not match its original producer/thread"
                        )
                    await transaction.write(
                        "UPDATE post SET body=?,author_label=?,edited_at=? WHERE uid=?",
                        (
                            str(payload["body"])[:4000],
                            str(payload["author_label"])[:80],
                            payload.get("edited_at"),
                            uid,
                        ),
                    )
                    await transaction.write(
                        "UPDATE thread SET subject=? WHERE id=? AND origin_node=?",
                        (str(payload["subject"])[:160], thread_id, row["mesh_id"]),
                    )
                await transaction.write(
                    "UPDATE thread SET post_count=(SELECT COUNT(*) FROM post WHERE thread_id=?),"
                    "last_post_at=MAX(last_post_at,?) WHERE id=?",
                    (thread_id, int(payload["created_at"]), thread_id),
                )
            elif stream == "incidents":
                if not row["sync_incidents"]:
                    raise ValueError("incident sync is no longer allowed")
                await self._import_incident(transaction, row, uid, payload, operator, now)
            elif stream == "alerts":
                if not row["relay_alerts"]:
                    raise ValueError("alert relay is no longer allowed")
                source = payload.get("source")
                if source not in {"operator", "incident", "cap", "same"}:
                    source = "operator"
                await transaction.write(
                    """INSERT OR IGNORE INTO alert(uid,severity,headline,body,source,source_ref,
                       channels,raised_by,raised_at,effective_at,expires_at,cancelled_at)
                       VALUES(?,?,?,?,?,?,'[]',?,?,?,?,?)""",
                    (
                        uid,
                        payload["severity"],
                        str(payload["headline"])[:140],
                        payload.get("body"),
                        source,
                        str(payload.get("source_ref") or f"federation:{row['peer_id']}")[:160],
                        f"federation:{row['peer_id']}",
                        int(payload["raised_at"]),
                        payload.get("effective_at"),
                        payload.get("expires_at"),
                        payload.get("cancelled_at"),
                    ),
                )
                if row["source_revision"] is not None:
                    if not uid.startswith(f"{row['mesh_id']}:"):
                        raise ValueError("alert revision does not match its original producer")
                    alerts = await transaction.read(
                        "SELECT raised_by FROM alert WHERE uid=?", (uid,)
                    )
                    if alerts[0]["raised_by"] != f"federation:{row['peer_id']}":
                        raise ValueError("alert revision cannot replace a local or relayed alert")
                    await transaction.write(
                        "UPDATE alert SET severity=?,headline=?,body=?,effective_at=?,expires_at=?,"
                        "cancelled_at=? WHERE uid=?",
                        (
                            payload["severity"],
                            str(payload["headline"])[:140],
                            payload.get("body"),
                            payload.get("effective_at"),
                            payload.get("expires_at"),
                            payload.get("cancelled_at"),
                            uid,
                        ),
                    )
            else:
                raise ValueError("unsupported federation inbox stream")
            if row["source_revision"] is not None:
                await write_audit(
                    transaction,
                    actor_kind="operator" if not operator.startswith("federation:") else "system",
                    actor_ref=operator,
                    action="federation.revision_imported",
                    target=f"{stream}:{uid}",
                    detail={
                        "epoch": row["source_epoch"],
                        "revision": row["source_revision"],
                        "digest": row["digest"],
                    },
                    created_at=now,
                )
            await transaction.write(
                "UPDATE fed_inbox_item SET state='imported',reviewed_at=?,reviewed_by=? WHERE id=?",
                (now, operator, item_id),
            )
        return str(stream)
