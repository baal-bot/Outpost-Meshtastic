"""Bounded metadata diagnostics, not delivery receipts or a payload store."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from outpost.fed.peers import Peer
from outpost.fed.revisions import source_revision

if TYPE_CHECKING:
    from outpost.fed.sync import FederationSyncService

CAPABILITY = "item_failures"
MODE = 1
CODE = "payload_too_large"
CURSOR = "_encoding_failures"
LIMIT = 8


def supported(peer: Peer) -> bool:
    return (
        peer.capabilities.get("reconciliation") == 2
        and type(peer.capabilities.get("reconciliation")) is int
        and type(peer.capabilities.get(CAPABILITY)) is int
        and peer.capabilities.get(CAPABILITY) == MODE
    )


def validate(item: dict[str, Any]) -> None:
    if (
        item.get("failure") != CODE
        or "payload" in item
        or "unavailable" in item
        or not isinstance(item.get("digest"), str)
        or not re.fullmatch(r"[0-9a-f]{16}", item["digest"])
    ):
        raise ValueError("invalid federation item failure")
    for field, limit in (("stream", 80), ("uid", 160)):
        if not isinstance(item.get(field), str) or not 1 <= len(item[field]) <= limit:
            raise ValueError("invalid federation item failure identity")
    if source_revision(item) is None:
        raise ValueError("federation item failure requires a producer revision")


class ItemFailures:
    def __init__(self, sync: FederationSyncService) -> None:
        self.sync = sync

    def envelope(self, item: dict[str, Any]) -> dict[str, Any]:
        result = {key: item[key] for key in ("stream", "uid", "epoch", "revision", "cycle")}
        result.update(
            failure=CODE,
            digest=self.sync._payload_digest(
                json.dumps(item["payload"], separators=(",", ":"), sort_keys=True)
            ),
        )
        validate(result)
        return result

    async def record(self, peer: Peer, failure: dict[str, Any], now: int) -> None:
        validate(failure)
        await self._update(peer, failure, now, failed=True)

    async def admitted(self, peer: Peer, item: dict[str, Any], now: int) -> None:
        # Queue admission clears an obsolete encoding diagnostic, not evidence
        # that any RF transmission, quarantine or operator acceptance occurred.
        await self._update(peer, item, now, failed=False)

    async def _update(self, peer: Peer, item: dict[str, Any], now: int, *, failed: bool) -> None:
        async with self.sync.database.transaction() as tx:
            rows = await tx.read(
                "SELECT cursor FROM fed_cursor WHERE peer_id=? AND stream=? AND direction='send'",
                (peer.id, CURSOR),
            )
            entries = json.loads(rows[0]["cursor"]) if rows else []
            match = next(
                (e for e in entries if (e["stream"], e["uid"]) == (item["stream"], item["uid"])),
                None,
            )
            if failed:
                # An old encoding attempt must not replace a newer revision's
                # diagnostic, including after a concurrent local edit or restore.
                heads = await tx.read(
                    "SELECT r.revision,l.epoch FROM fed_revision r CROSS JOIN "
                    "fed_revision_lineage l WHERE r.stream=? AND r.uid=?",
                    (item["stream"], self.sync._local_uid(item["uid"])),
                )
                if not heads or (heads[0]["epoch"], heads[0]["revision"]) != (
                    item["epoch"],
                    item["revision"],
                ):
                    return
                entry = {
                    key: item[key]
                    for key in ("stream", "uid", "epoch", "revision", "digest", "failure")
                }
                same = match and all(match[key] == entry[key] for key in entry)
                entry.update(
                    observed_at=now,
                    attempts=min(match["attempts"] + 1, 1000000)
                    if same and match is not None
                    else 1,
                )
                entries = [entry, *(e for e in entries if e is not match)][:LIMIT]
            elif (
                match and match["epoch"] == item["epoch"] and match["revision"] <= item["revision"]
            ):
                entries.remove(match)
            else:
                return
            await tx.write(
                "INSERT INTO fed_cursor(peer_id,stream,direction,cursor,updated_at) "
                "VALUES(?,?,'send',?,?) ON CONFLICT(peer_id,stream,direction) "
                "DO UPDATE SET cursor=excluded.cursor,updated_at=excluded.updated_at",
                (peer.id, CURSOR, json.dumps(entries, separators=(",", ":")), now),
            )
