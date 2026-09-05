"""Durable pull reconciliation: a page advances only after its items are stored."""

from __future__ import annotations

import asyncio
import json
import secrets
from typing import TYPE_CHECKING, Any

from outpost.fed.framing import MessageType, wire_int
from outpost.fed.peers import Peer
from outpost.fed.revisions import MODE, token

if TYPE_CHECKING:
    from outpost.app import OutpostApp

MAX_ROUNDS = 16


class Reconciliation:
    def __init__(self, app: OutpostApp) -> None:
        self.app = app
        self.database = app.database
        self.index = app.federation_sync.revisions
        self._locks: dict[int, asyncio.Lock] = {}
        self._due: dict[int, float] = {}

    def _lock(self, peer: Peer) -> asyncio.Lock:
        return self._locks.setdefault(peer.id, asyncio.Lock())

    async def _load(self, peer: Peer) -> dict[str, Any]:
        rows = await self.database.read(
            "SELECT cursor FROM fed_cursor WHERE peer_id=? AND stream='_reconcile' "
            "AND direction='recv'",
            (peer.id,),
        )
        if rows:
            value = json.loads(rows[0]["cursor"])
            if isinstance(value, dict) and value.get("mode") == MODE:
                return value
        return {}

    async def _save(self, peer: Peer, state: dict[str, Any]) -> None:
        await self.app._store_reconciliation_checkpoint(
            peer.id, state, int(self.app.clock.now().timestamp())
        )

    def _fresh(self, peer: Peer, prior: dict[str, Any]) -> dict[str, Any]:
        return {
            "mode": MODE,
            "cycle": secrets.token_hex(16),
            "epoch": prior.get("epoch"),
            "scope": prior.get("scope"),
            "local_scope": self.index.scope(peer),
            "snapshot": None if prior.get("status") == "complete" else prior.get("snapshot"),
            "after": prior.get("after", 0),
            "status": "active",
            "reason": None,
            "pending": True,
            "page": None,
            "budget": self.app.config.fed.max_items_per_cycle,
            "used": 0,
            "rounds": 0,
        }

    async def tick(self, peer: Peer) -> None:
        async with self._lock(peer):
            now = self.app.clock.monotonic()
            state = await self._load(peer)
            if state.get("status") == "blocked":
                return
            if state and state.get("local_scope") != self.index.scope(peer):
                state = self._fresh(peer, {"epoch": state.get("epoch")})
                self._due.pop(peer.id, None)
            interval = self.app.config.fed.sync_interval_minutes * 60
            if peer.id not in self._due and state.get("status") in {"complete", "truncated"}:
                # Monotonic clocks cannot be restored across boot. Conservatively
                # wait one interval for completed/budget-stopped cycles, not a
                # potentially six-hours-in-the-future wall-clock deadline.
                self._due[peer.id] = now + interval
            if now < self._due.get(peer.id, now):
                return
            if not state or state.get("status") in {"complete", "truncated"}:
                state = self._fresh(peer, state)
                await self._save(peer, state)
            await self._drive(peer, state)

    async def _drive(self, peer: Peer, state: dict[str, Any]) -> None:
        if state["status"] != "active":
            return
        budget = min(state["budget"], self.app.config.fed.max_items_per_cycle)
        page = state.get("page")
        if page is not None:
            missing = await self.index.missing(peer, state["epoch"], page["items"])
            unavailable = page.get("unavailable", [])
            missing = [item for item in missing if [item["stream"], item["uid"]] not in unavailable]
            if missing:
                await self.app._queue_federation_control(
                    peer.mesh_id,
                    MessageType.ITEM_REQ,
                    {
                        "mode": MODE,
                        "cycle": state["cycle"],
                        "epoch": state["epoch"],
                        "scope": state["scope"],
                        "items": missing,
                    },
                )
                self._due[peer.id] = (
                    self.app.clock.monotonic() + self.app.config.fed.sync_retry_minutes * 60
                )
                return
            state["after"] = page["next"]
            state["page"] = None
            if page["done"]:
                state["status"] = "complete"
                state["pending"] = False
                await self._save(peer, state)
                await self.database.write(
                    "UPDATE fed_peer SET last_sync_at=? WHERE id=?",
                    (int(self.app.clock.now().timestamp()), peer.id),
                )
                self._due[peer.id] = (
                    self.app.clock.monotonic() + self.app.config.fed.sync_interval_minutes * 60
                )
                return
        remaining = budget - state["used"]
        if remaining <= 0 or state["rounds"] >= MAX_ROUNDS:
            state.update(
                status="truncated", pending=False, reason="local reconciliation budget exhausted"
            )
            await self._save(peer, state)
            self._due[peer.id] = (
                self.app.clock.monotonic() + self.app.config.fed.sync_interval_minutes * 60
            )
            return
        # Persist before admission: a crash here retries the same page; a crash
        # after admission cannot forget the outstanding request or its budget.
        await self._save(peer, state)
        await self.app._queue_federation_control(
            peer.mesh_id,
            MessageType.SYNC_REQ,
            {
                "mode": MODE,
                "cycle": state["cycle"],
                "epoch": state["epoch"],
                "scope": state["scope"],
                "snapshot": state["snapshot"],
                "after": state["after"],
                "limit": min(8, remaining),
                "budget": remaining,
            },
        )
        self._due[peer.id] = (
            self.app.clock.monotonic() + self.app.config.fed.sync_retry_minutes * 60
        )

    async def manifest(self, peer: Peer, value: dict[str, Any]) -> None:
        async with self._lock(peer):
            state = await self._load(peer)
            if not state or value.get("cycle") != state.get("cycle"):
                return  # A delayed page never advances a different cycle.
            if state["status"] != "active":
                return
            epoch = token(value.get("epoch"), "epoch")
            if state.get("epoch") not in (None, epoch):
                state.update(
                    status="blocked",
                    pending=False,
                    reason="producer lineage changed; operator review required",
                )
                await self._save(peer, state)
                return
            if value.get("rollback") is True:
                state.update(
                    status="blocked",
                    pending=False,
                    reason="producer revision rollback; operator recovery review required",
                )
                await self._save(peer, state)
                return
            scope = value.get("scope")
            if not isinstance(scope, str) or len(scope) != 16:
                raise ValueError("invalid federation scope")
            if value.get("reset") is True:
                replacement = self._fresh(peer, {"epoch": epoch, "scope": scope})
                replacement.update(used=state["used"], rounds=state["rounds"] + 1)
                state = replacement
                await self._save(peer, state)
                await self._drive(peer, state)
                return
            if state.get("scope") not in (None, scope):
                raise ValueError("peer changed reconciliation scope without a reset")
            snapshot = wire_int(value.get("snapshot"), "snapshot")
            after = wire_int(value.get("after"), "after", maximum=snapshot)
            next_after = wire_int(value.get("next"), "next", minimum=after, maximum=snapshot)
            if after != state["after"]:
                return  # Already consumed, reordered, or not the requested page.
            if state.get("snapshot") not in (None, snapshot):
                raise ValueError("peer changed the producer watermark")
            if state.get("page") is not None:
                await self._drive(peer, state)
                return
            done = value.get("done")
            if (
                type(done) is not bool
                or (done and next_after != snapshot)
                or (not done and next_after <= after)
            ):
                raise ValueError("peer reconciliation cursor did not advance")
            items = value.get("items")
            remaining = (
                min(state["budget"], self.app.config.fed.max_items_per_cycle) - state["used"]
            )
            if not isinstance(items, list) or len(items) > min(8, remaining):
                raise ValueError("peer exceeded the local reconciliation page budget")
            previous = after
            identities = set()
            for item in items:
                if not isinstance(item, dict):
                    raise ValueError("invalid federation revision item")
                revision = wire_int(
                    item.get("r"), "revision", minimum=previous + 1, maximum=next_after
                )
                stream, uid, digest = item.get("s"), item.get("u"), item.get("d")
                if (
                    not isinstance(stream, str)
                    or not 1 <= len(stream) <= 80
                    or not isinstance(uid, str)
                    or not 1 <= len(uid) <= 160
                    or not isinstance(digest, str)
                    or len(digest) != 16
                    or (stream, uid) in identities
                ):
                    raise ValueError("invalid federation revision item")
                allowed = (
                    (stream.startswith("board:") and stream[6:] in peer.boards)
                    or (stream == "incidents" and peer.sync_incidents)
                    or (stream == "alerts" and peer.relay_alerts)
                )
                if not allowed or not self.app.federation_sync.stream_enabled(stream):
                    raise ValueError("manifest is outside peer sync policy")
                identities.add((stream, uid))
                previous = revision
            if state["rounds"] >= MAX_ROUNDS:
                raise ValueError("local reconciliation round budget exhausted")
            state.update(epoch=epoch, scope=scope, snapshot=snapshot)
            state["used"] += len(items)
            state["rounds"] += 1
            state["page"] = {
                "items": [{key: item[key] for key in ("s", "u", "r", "d")} for item in items],
                "next": next_after,
                "done": done,
            }
            await self.database.write(
                "UPDATE fed_peer SET reconciliation_version=2 WHERE id=?", (peer.id,)
            )
            await self._save(peer, state)
            await self._drive(peer, state)

    async def receive(self, peer: Peer, item: dict[str, Any]) -> bool:
        async with self._lock(peer):
            state = await self._load(peer)
            page = state.get("page")
            if (
                not state
                or state.get("status") != "active"
                or not page
                or item.get("cycle") != state["cycle"]
                or item.get("epoch") != state["epoch"]
            ):
                return False
            expected = next(
                (
                    entry
                    for entry in page["items"]
                    if entry["s"] == item.get("stream") and entry["u"] == item.get("uid")
                ),
                None,
            )
            if expected is None:
                raise ValueError("unsolicited federation revision item")
            revision = wire_int(item.get("revision"), "revision", minimum=expected["r"])
            changed = False
            if item.get("unavailable") is True:
                # The source was deleted or left the authorized scope after discovery.
                # Advance this page without exporting new private coordinates. This
                # is not evidence of replica withdrawal or operator acceptance.
                unavailable = page.setdefault("unavailable", [])
                identity = [expected["s"], expected["u"]]
                if identity not in unavailable:
                    unavailable.append(identity)
                await self._save(peer, state)
            else:
                if revision == expected["r"]:
                    digest = self.app.federation_sync._payload_digest(
                        json.dumps(item.get("payload"), separators=(",", ":"), sort_keys=True)
                    )
                    if digest != expected["d"]:
                        raise ValueError("payload does not match the advertised producer revision")
                changed = await self.app.federation_sync.quarantine(
                    peer, item, int(self.app.clock.now().timestamp())
                )
            await self._drive(peer, state)
            return changed
