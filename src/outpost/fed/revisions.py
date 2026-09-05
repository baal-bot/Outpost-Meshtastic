"""Bounded, producer-owned change discovery; timestamps are not replication cursors."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from outpost.fed.framing import wire_int
from outpost.fed.peers import Peer

if TYPE_CHECKING:
    from outpost.fed.sync import FederationSyncService

MODE = 2
CAPABILITY = "reconciliation"
SCAN_LIMIT = 100


class RevisionReset(ValueError):
    def __init__(self, cycle: str, epoch: str, scope: str, *, rollback: bool = False) -> None:
        super().__init__("federation revision scope or lineage changed")
        self.manifest = {
            "mode": MODE,
            "cycle": cycle,
            "epoch": epoch,
            "scope": scope,
            "reset": not rollback,
            "rollback": rollback,
        }


def token(value: object, field: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        raise ValueError(f"invalid federation {field}")
    return value


def source_revision(item: dict[str, Any]) -> tuple[str, int] | None:
    if "epoch" not in item and "revision" not in item:
        return None
    return token(item.get("epoch"), "epoch"), wire_int(
        item.get("revision"), "source revision", minimum=1
    )


class RevisionIndex:
    def __init__(self, sync: FederationSyncService) -> None:
        self.sync = sync
        self.database = sync.database

    def scope(self, peer: Peer) -> str:
        return self.sync._payload_digest(
            json.dumps(
                [
                    sorted(peer.boards),
                    peer.sync_incidents,
                    peer.relay_alerts,
                    peer.incident_lat,
                    peer.incident_lon,
                    peer.incident_radius_km,
                    self.sync.module_enabled("bbs"),
                    self.sync.module_enabled("watch"),
                ],
                separators=(",", ":"),
            )
        )

    async def page(self, peer: Peer, request: dict[str, Any]) -> dict[str, Any]:
        if peer.state != "active":
            raise ValueError("sync requires an active peer")
        cycle = token(request.get("cycle"), "cycle")
        after = wire_int(request.get("after", 0), "after")
        limit = min(
            wire_int(request.get("limit", 8), "limit", minimum=1, maximum=8),
            wire_int(request.get("budget", 8), "budget", minimum=1, maximum=100),
        )
        # The writer lock makes heads, scope-filtered payloads, and their digests a
        # consistent observation. It never holds a transaction across radio I/O.
        async with self.database.transaction() as tx:
            epoch = str((await tx.read("SELECT epoch FROM fed_revision_lineage"))[0]["epoch"])
            high = await tx.read("SELECT seq FROM sqlite_sequence WHERE name='fed_revision'")
            head = int(high[0]["seq"]) if high else 0
            scope = self.scope(peer)
            requested_epoch = request.get("epoch")
            if requested_epoch is not None:
                token(requested_epoch, "epoch")
            if requested_epoch not in (None, epoch) or request.get("scope") not in (None, scope):
                return {"mode": MODE, "cycle": cycle, "reset": True, "epoch": epoch, "scope": scope}
            snapshot = request.get("snapshot")
            snapshot = head if snapshot is None else wire_int(snapshot, "snapshot")
            if requested_epoch == epoch and (after > head or snapshot > head):
                return {
                    "mode": MODE,
                    "cycle": cycle,
                    "epoch": epoch,
                    "scope": scope,
                    "rollback": True,
                }
            if snapshot > head:
                raise ValueError("snapshot exceeds producer watermark")
            if after > snapshot:
                raise ValueError("federation cursor exceeds producer watermark")
            rows = await tx.read(
                "SELECT revision,stream,uid FROM fed_revision "
                "WHERE revision>? AND revision<=? ORDER BY revision LIMIT ?",
                (after, snapshot, SCAN_LIMIT),
            )
            items: list[dict[str, Any]] = []
            next_after = after
            for row in rows:
                next_after = int(row["revision"])
                exported = await self.sync.export_items(
                    peer, [{"stream": row["stream"], "uid": self.sync.wire_uid(row["uid"])}]
                )
                if exported:
                    payload = exported[0]["payload"]
                    items.append(
                        {
                            "s": row["stream"],
                            "u": exported[0]["uid"],
                            "r": next_after,
                            "d": self.sync._payload_digest(
                                json.dumps(payload, separators=(",", ":"), sort_keys=True)
                            ),
                        }
                    )
                if len(items) == limit:
                    break
            more = await tx.read(
                "SELECT 1 FROM fed_revision WHERE revision>? AND revision<=? LIMIT 1",
                (next_after, snapshot),
            )
            if not more:
                next_after = snapshot
            return {
                "mode": MODE,
                "cycle": cycle,
                "epoch": epoch,
                "scope": scope,
                "snapshot": snapshot,
                "after": after,
                "next": next_after,
                "done": not more,
                "items": items,
            }

    async def export(self, peer: Peer, request: dict[str, Any]) -> list[dict[str, Any]]:
        cycle = token(request.get("cycle"), "cycle")
        epoch = token(request.get("epoch"), "epoch")
        requests = request.get("items")
        if not isinstance(requests, list) or len(requests) > 8:
            raise ValueError("invalid federation revision requests")
        async with self.database.transaction() as tx:
            current_epoch = (await tx.read("SELECT epoch FROM fed_revision_lineage"))[0]["epoch"]
            if epoch != current_epoch or request.get("scope") != self.scope(peer):
                raise RevisionReset(cycle, str(current_epoch), self.scope(peer))
            results: list[dict[str, Any]] = []
            for item in requests:
                if not isinstance(item, dict):
                    raise ValueError("invalid federation revision request")
                stream, uid = str(item.get("stream", "")), str(item.get("uid", ""))
                expected = wire_int(item.get("revision"), "revision", minimum=1)
                local_uid = self.sync._local_uid(uid)
                if local_uid is None:
                    raise ValueError("revision request is not owned by this producer")
                rows = await tx.read(
                    "SELECT revision FROM fed_revision WHERE stream=? AND uid=?",
                    (stream, local_uid),
                )
                if not rows or int(rows[0]["revision"]) < expected:
                    raise RevisionReset(cycle, epoch, self.scope(peer), rollback=True)
                exported = await self.sync.export_items(peer, [{"stream": stream, "uid": uid}])
                result = (
                    exported[0] if exported else {"stream": stream, "uid": uid, "unavailable": True}
                )
                results.append(
                    {**result, "epoch": epoch, "revision": int(rows[0]["revision"]), "cycle": cycle}
                )
            return results

    async def missing(
        self, peer: Peer, epoch: str, items: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        requested = []
        for item in items:
            rows = await self.database.read(
                "SELECT epoch,revision,digest FROM fed_revision_receipt "
                "WHERE peer_id=? AND stream=? AND uid=?",
                (peer.id, item["s"], item["u"]),
            )
            if (
                not rows
                or rows[0]["epoch"] != epoch
                or int(rows[0]["revision"]) < item["r"]
                or (int(rows[0]["revision"]) == item["r"] and rows[0]["digest"] != item["d"])
            ):
                requested.append({"stream": item["s"], "uid": item["u"], "revision": item["r"]})
        return requested
