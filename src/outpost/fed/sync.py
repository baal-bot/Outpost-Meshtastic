from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from outpost.fed.peers import Peer
from outpost.store import Database


@dataclass(frozen=True)
class ManifestItem:
    stream: str
    uid: str
    version: int
    digest: str

    def json(self) -> dict[str, str | int]:
        return {"s": self.stream, "u": self.uid, "v": self.version, "d": self.digest}


class FederationSyncService:
    def __init__(self, database: Database, local_mesh_id: str = "") -> None:
        self.database = database
        self.local_mesh_id = local_mesh_id

    def _wire_uid(self, uid: str) -> str:
        if uid.startswith("!") and ":" in uid:
            return uid
        return f"{self.local_mesh_id}:{uid}" if self.local_mesh_id else uid

    def _local_uid(self, uid: str) -> str | None:
        if not self.local_mesh_id:
            return uid
        prefix = f"{self.local_mesh_id}:"
        return uid[len(prefix) :] if uid.startswith(prefix) else None

    @staticmethod
    def _digest(*values: Any) -> str:
        joined = "\x1f".join("" if value is None else str(value) for value in values)
        return hashlib.sha256(joined.encode()).hexdigest()[:16]

    async def manifest(self, peer: Peer, limit: int = 100) -> list[ManifestItem]:
        if peer.state != "active":
            raise ValueError("sync requires an active peer")
        items: list[ManifestItem] = []
        if peer.boards:
            placeholders = ",".join("?" for _ in peer.boards)
            rows = await self.database.read(
                f"""SELECT p.uid,p.created_at,p.edited_at,p.body,p.author_label,t.uid thread_uid,
                    b.slug FROM post p JOIN thread t ON t.id=p.thread_id
                    JOIN board b ON b.id=t.board_id
                    WHERE b.federated=1 AND b.slug IN ({placeholders}) AND p.hidden=0
                    ORDER BY COALESCE(p.edited_at,p.created_at) DESC LIMIT ?""",  # noqa: S608
                (*peer.boards, limit),
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
        if peer.sync_incidents:
            rows = await self.database.read(
                "SELECT uid,updated_at,status,severity,title,body,lat,lon FROM incident "
                "ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            )
            items.extend(
                ManifestItem(
                    "incidents", self._wire_uid(row["uid"]), row["updated_at"], self._digest(*row)
                )
                for row in rows
            )
        if peer.relay_alerts:
            rows = await self.database.read(
                "SELECT uid,raised_at,cancelled_at,severity,headline,expires_at FROM alert "
                "ORDER BY raised_at DESC LIMIT ?",
                (limit,),
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
        return items[:limit]

    async def missing(self, manifest: list[dict[str, Any]]) -> list[dict[str, str]]:
        requested: list[dict[str, str]] = []
        tables = {"incidents": "incident", "alerts": "alert"}
        for item in manifest[:100]:
            stream = str(item.get("stream", item.get("s", "")))
            uid = str(item.get("uid", item.get("u", "")))
            if not uid or len(uid) > 160:
                continue
            table = tables.get(stream, "post" if stream.startswith("board:") else None)
            if table is None:
                continue
            rows = await self.database.read(f"SELECT uid FROM {table} WHERE uid=?", (uid,))  # noqa: S608
            if not rows:
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
            local_uid = self._local_uid(uid)
            if local_uid is None:
                continue
            rows: list[Any] = []
            if stream.startswith("board:") and stream[6:] in peer.boards:
                rows = await self.database.read(
                    """SELECT p.uid,p.seq,p.author_label,p.origin_node,p.body,p.created_at,
                       p.edited_at,t.uid thread_uid,t.subject,b.slug FROM post p
                       JOIN thread t ON t.id=p.thread_id JOIN board b ON b.id=t.board_id
                       WHERE p.uid=? AND p.hidden=0 AND b.federated=1 AND b.slug=?""",
                    (local_uid, stream[6:]),
                )
            elif stream == "incidents" and peer.sync_incidents:
                rows = await self.database.read(
                    "SELECT uid,type,severity,status,title,body,lat,lon,location_text,radius_m,"
                    "reporter_label,origin_node,created_at,updated_at,expires_at,resolved_at,"
                    "resolution_note FROM incident WHERE uid=?",
                    (local_uid,),
                )
            elif stream == "alerts" and peer.relay_alerts:
                rows = await self.database.read(
                    "SELECT uid,severity,headline,body,source,source_ref,raised_by,raised_at,"
                    "effective_at,expires_at,cancelled_at FROM alert WHERE uid=?",
                    (local_uid,),
                )
            if rows:
                payload = dict(rows[0])
                payload["uid"] = uid
                if stream.startswith("board:"):
                    payload["thread_uid"] = self._wire_uid(str(payload["thread_uid"]))
                    payload["origin_node"] = uid.split(":", 1)[0]
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
        payload = item.get("payload")
        allowed = (
            (stream.startswith("board:") and stream[6:] in peer.boards)
            or (stream == "incidents" and peer.sync_incidents)
            or (stream == "alerts" and peer.relay_alerts)
        )
        if not allowed or not uid or len(uid) > 160 or not isinstance(payload, dict):
            raise ValueError("inbound item is outside peer sync policy")
        encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        if len(encoded.encode()) > 12_000:
            raise ValueError("inbound federation item is too large")
        recent = await self.database.read(
            "SELECT COUNT(*) count FROM fed_inbox_item WHERE peer_id=? AND received_at>?",
            (peer.id, now - 3600),
        )
        if int(recent[0]["count"]) >= peer.quota_items_per_hour:
            raise ValueError("peer federation item quota exceeded")
        existing = await self.database.read(
            "SELECT id FROM fed_inbox_item WHERE peer_id=? AND stream=? AND uid=?",
            (peer.id, stream, uid),
        )
        if existing:
            return False
        await self.database.write(
            "INSERT INTO fed_inbox_item(peer_id,stream,uid,payload_json,digest,received_at) "
            "VALUES(?,?,?,?,?,?)",
            (peer.id, stream, uid, encoded, str(item.get("digest", ""))[:64], now),
        )
        return True

    async def import_inbox(self, item_id: int, operator: str, now: int) -> str:
        rows = await self.database.read(
            "SELECT i.*,p.mesh_id,p.node_name,p.boards,p.sync_incidents,p.relay_alerts "
            "FROM fed_inbox_item i "
            "JOIN fed_peer p ON p.id=i.peer_id WHERE i.id=? AND i.state='pending'",
            (item_id,),
        )
        if not rows:
            raise ValueError("pending federation item not found")
        row = rows[0]
        stream, uid, payload = row["stream"], row["uid"], json.loads(row["payload_json"])
        if stream.startswith("board:"):
            slug = stream[6:]
            if slug not in json.loads(row["boards"]):
                raise ValueError("board is no longer allowed for this peer")
            boards = await self.database.read(
                "SELECT id FROM board WHERE slug=? AND federated=1 AND archived=0", (slug,)
            )
            if not boards:
                raise ValueError("destination board is not federated")
            thread_uid = str(payload["thread_uid"])
            threads = await self.database.read(
                "SELECT id FROM thread WHERE uid=? AND board_id=?", (thread_uid, boards[0]["id"])
            )
            if threads:
                thread_id = int(threads[0]["id"])
            else:
                thread_id = await self.database.write(
                    "INSERT INTO thread(uid,board_id,subject,origin_node,created_at,last_post_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        thread_uid,
                        boards[0]["id"],
                        str(payload["subject"])[:160],
                        row["mesh_id"],
                        int(payload["created_at"]),
                        int(payload["created_at"]),
                    ),
                )
            sequence = await self.database.read(
                "SELECT COALESCE(MAX(seq),0)+1 value FROM post WHERE thread_id=?", (thread_id,)
            )
            await self.database.write(
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
            await self.database.write(
                "UPDATE thread SET post_count=(SELECT COUNT(*) FROM post WHERE thread_id=?),"
                "last_post_at=MAX(last_post_at,?) WHERE id=?",
                (thread_id, int(payload["created_at"]), thread_id),
            )
        elif stream == "incidents":
            if not row["sync_incidents"]:
                raise ValueError("incident sync is no longer allowed")
            refs = await self.database.read(
                "SELECT COALESCE(MAX(local_ref),0)+1 value FROM incident"
            )
            await self.database.write(
                """INSERT OR IGNORE INTO incident(uid,local_ref,type,severity,status,title,body,
                   lat,lon,location_text,radius_m,reporter_label,origin_node,created_at,updated_at,
                   expires_at,resolved_at,resolution_note,source,unverified,flagged_for_review)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'member',1,1)""",
                (
                    uid,
                    refs[0]["value"],
                    payload["type"],
                    payload["severity"],
                    payload["status"],
                    str(payload["title"])[:64],
                    payload.get("body"),
                    payload.get("lat"),
                    payload.get("lon"),
                    payload.get("location_text"),
                    payload.get("radius_m"),
                    str(payload.get("reporter_label") or "Federated peer")[:80],
                    str(payload.get("origin_node") or "federation"),
                    int(payload["created_at"]),
                    int(payload["updated_at"]),
                    payload.get("expires_at"),
                    payload.get("resolved_at"),
                    payload.get("resolution_note"),
                ),
            )
        elif stream == "alerts":
            if not row["relay_alerts"]:
                raise ValueError("alert relay is no longer allowed")
            source = payload.get("source")
            if source not in {"operator", "incident", "cap", "same"}:
                source = "operator"
            await self.database.write(
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
        else:
            raise ValueError("unsupported federation inbox stream")
        await self.database.write(
            "UPDATE fed_inbox_item SET state='imported',reviewed_at=?,reviewed_by=? WHERE id=?",
            (now, operator, item_id),
        )
        return stream
