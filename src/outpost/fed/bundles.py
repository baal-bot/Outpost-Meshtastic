"""Operator-reviewed physical transfer; one writer owns import, receipts and audit."""

from __future__ import annotations

import hmac
import json
import time
from dataclasses import dataclass, replace
from typing import Any

from outpost.audit import write_audit
from outpost.clock import Clock
from outpost.fed import bundle_format as fmt
from outpost.fed.framing import wire_int
from outpost.fed.peers import FederationPeerService, Peer
from outpost.fed.review import FederationReviewService, ReviewConflict
from outpost.fed.revisions import SCAN_LIMIT
from outpost.fed.sync import FederationSyncService
from outpost.store import Database, Transaction

MAX_RECEIPTS = 4096


class BundleDenied(ValueError):
    """No current operator, pairing, or previously trusted signing key."""


@dataclass(frozen=True)
class BundleActor:
    account_id: int
    session_hash: str


class FederationBundleService:
    def __init__(
        self,
        database: Database,
        clock: Clock,
        peers: FederationPeerService,
        sync: FederationSyncService,
    ) -> None:
        self.database, self.clock, self.peers, self.sync = database, clock, peers, sync

    async def _authorize(self, tx: Transaction, actor: BundleActor) -> str:
        if not self.sync.module_enabled("fed"):
            raise BundleDenied("Federation module is disabled")
        rows = await tx.read(
            "SELECT a.username FROM web_account a JOIN web_session s ON s.account_id=a.id "
            "WHERE a.id=? AND s.token_hash=? AND s.expires_at>? AND a.enabled=1 "
            "AND a.must_change=0 AND a.role IN ('operator','administrator')",
            (actor.account_id, actor.session_hash, int(time.time())),
        )
        if not rows:
            raise BundleDenied("A current named operator session is required")
        return str(rows[0]["username"])

    async def restore_identity(self) -> None:
        """Startup-only restoration of an explicitly commissioned local identity."""
        rows = await self.database.read("SELECT mesh_id FROM fed_bundle_identity WHERE id=1")
        if rows:
            identity = str(rows[0]["mesh_id"])
            if not self.peers.local_mesh_id:
                self.peers.local_mesh_id = identity
            if not self.sync.local_mesh_id:
                self.sync.local_mesh_id = identity

    async def _identity(self, tx: Transaction) -> str:
        rows = await tx.read("SELECT mesh_id FROM fed_bundle_identity WHERE id=1")
        if not rows:
            raise BundleDenied(
                "Commission the observed local node identity before offline transfer"
            )
        identity = str(rows[0]["mesh_id"])
        if self.peers.local_mesh_id != identity or self.sync.local_mesh_id != identity:
            raise BundleDenied(
                "Local identity changed or was not restored; node adoption requires separate review"
            )
        return identity

    @staticmethod
    async def _key(tx: Transaction) -> tuple[bytes, bytes]:
        rows = await tx.read("SELECT private_key,public_key FROM fed_relay_identity WHERE id=1")
        if not rows:
            raise BundleDenied("Existing federation signing identity is not initialized")
        return bytes(rows[0]["private_key"]), bytes(rows[0]["public_key"])

    async def status(self, actor: BundleActor) -> dict[str, Any]:
        async with self.database.transaction() as tx:
            await self._authorize(tx, actor)
            rows = await tx.read("SELECT mesh_id FROM fed_bundle_identity WHERE id=1")
            keys = await tx.read("SELECT public_key FROM fed_relay_identity WHERE id=1")
            count = await tx.read("SELECT COUNT(*) n FROM fed_bundle_receipt")
            return {
                "commissioned_identity": rows[0]["mesh_id"] if rows else None,
                "observed_identity": self.peers.local_mesh_id or None,
                "fingerprint": fmt.digest(bytes(keys[0]["public_key"])) if keys else None,
                "receipt_count": count[0]["n"],
                "receipt_limit": MAX_RECEIPTS,
                "max_file_bytes": fmt.MAX_FILE_BYTES,
            }

    async def commission(self, actor: BundleActor, identity: str) -> None:
        if not fmt.NODE.fullmatch(identity):
            raise ValueError("Use the observed lowercase !1234abcd node ID")
        async with self.database.transaction() as tx:
            username = await self._authorize(tx, actor)
            if identity != self.peers.local_mesh_id or identity != self.sync.local_mesh_id:
                raise BundleDenied(
                    "Commissioning must match the already observed local radio identity"
                )
            await self._key(tx)
            old = await tx.read("SELECT mesh_id FROM fed_bundle_identity WHERE id=1")
            if old:
                if old[0]["mesh_id"] != identity:
                    raise BundleDenied("Commissioning cannot replace an existing node identity")
                return
            await tx.write(
                "INSERT INTO fed_bundle_identity VALUES(1,?,?,?)",
                (identity, int(self.clock.now().timestamp()), username),
            )
            await self._audit(tx, username, "commission", identity, {"identity": identity})

    async def _peer(self, tx: Transaction, identity: str) -> Peer:
        if not isinstance(identity, str) or not fmt.NODE.fullmatch(identity):
            raise ValueError("Invalid peer identity")
        rows = await tx.read("SELECT * FROM fed_peer WHERE mesh_id=?", (identity,))
        if not rows:
            raise BundleDenied("Bundle peer has not been paired")
        peer = self.peers._peer(rows[0])
        if (
            peer.state != "active"
            or not peer.local_approved
            or not peer.remote_approved
            or not rows[0]["shared_secret"]
        ):
            raise BundleDenied("An active mutually approved pairing is required, even offline")
        return peer

    async def _audit(
        self, tx: Transaction, username: str, action: str, target: str, detail: dict[str, Any]
    ) -> None:
        await write_audit(
            tx,
            actor_kind="web",
            actor_ref=username,
            action=f"federation.bundle.{action}",
            target=target,
            detail=detail,
            created_at=int(self.clock.now().timestamp()),
        )

    def _policy(self, peer: Peer) -> list[Any]:
        return [
            peer.id,
            peer.mesh_id,
            peer.state,
            peer.local_approved,
            peer.remote_approved,
            self.sync.revisions.scope(peer),
            peer.capabilities,
            peer.quota_items_per_hour,
        ]

    async def _allowed(self, tx: Transaction, peer: Peer, item: dict[str, Any]) -> None:
        stream, payload = item["stream"], item["payload"]
        if not self.sync.stream_enabled(stream):
            raise BundleDenied("Record module is disabled")
        if stream.startswith("board:"):
            if stream[6:] not in peer.boards or payload["slug"] != stream[6:]:
                raise BundleDenied("Board is outside current peer policy")
            rows = await tx.read(
                "SELECT 1 FROM board WHERE slug=? AND federated=1 AND archived=0", (stream[6:],)
            )
            if not rows:
                raise BundleDenied("Board is not currently federated")
        elif stream in {"incidents", "incident_updates"}:
            if not peer.sync_incidents or (
                stream == "incident_updates" and not self.sync.incident_updates.supported(peer)
            ):
                raise BundleDenied("Incident stream is outside current peer policy")
            if stream == "incidents" and not self.sync.incident_allowed(
                peer, payload["lat"], payload["lon"]
            ):
                raise BundleDenied("Incident is outside current peer geographic policy")
        elif stream != "alerts" or not peer.relay_alerts:
            raise BundleDenied("Stream is outside current peer policy")

    async def _page(
        self, tx: Transaction, destination: str, stream: str, after: int, policy: dict[str, Any]
    ) -> dict[str, Any]:
        origin = await self._identity(tx)
        _, public = await self._key(tx)
        peer = await self._peer(tx, destination)
        if destination == origin:
            raise ValueError("Select another Outpost")
        if stream not in {"incidents", "alerts", "incident_updates"} and not (
            stream.startswith("board:") and stream[6:] in peer.boards
        ):
            raise ValueError("Choose one permitted public stream")
        # A stream page is an explicit subset of the current peer policy, never an expansion.
        selected = replace(
            peer,
            boards=[stream[6:]] if stream.startswith("board:") else [],
            sync_incidents=peer.sync_incidents and stream in {"incidents", "incident_updates"},
            relay_alerts=peer.relay_alerts and stream == "alerts",
        )
        if stream != "incident_updates":
            selected = replace(
                selected,
                capabilities={
                    key: value
                    for key, value in peer.capabilities.items()
                    if key != "incident_updates"
                },
            )
        epoch = str((await tx.read("SELECT epoch FROM fed_revision_lineage"))[0]["epoch"])
        high = await tx.read("SELECT seq FROM sqlite_sequence WHERE name='fed_revision'")
        head = int(high[0]["seq"]) if high else 0
        after = wire_int(after, "page cursor", maximum=head)
        rows = await self.sync.revisions._heads(tx, selected, after, head)
        items, skipped, next_after = [], 0, after
        for row in rows[:SCAN_LIMIT]:
            next_after = int(row["revision"])
            if row["stream"] != stream:
                continue
            uid = self.sync.wire_uid(str(row["uid"]))
            if not uid.startswith(origin + ":"):
                skipped += 1
                continue
            result = await self.sync.export_items(
                peer, [{"stream": stream, "uid": uid}], transaction=tx
            )
            if not result or not fmt.eligible(result[0]["payload"], policy):
                skipped += 1
                continue
            item = {
                "stream": stream,
                "uid": uid,
                "payload": result[0]["payload"],
                "epoch": epoch,
                "revision": next_after,
            }
            fmt.validate_item(item, origin, policy)
            await self._allowed(tx, peer, item)
            items.append(item)
            if len(items) == fmt.MAX_ITEMS:
                break
        more = bool(rows and int(rows[-1]["revision"]) > next_after)
        page = {
            "origin": origin,
            "destination": destination,
            "scope": policy,
            "items": items,
            "after": after,
            "next": next_after if more else head,
            "done": not more,
            "excluded": skipped,
            "fingerprint": fmt.digest(public),
        }
        page["review_token"] = fmt.digest(fmt.canonical([page, self._policy(peer), epoch]))
        return page

    async def export(
        self,
        actor: BundleActor,
        destination: str,
        stream: str,
        after: int = 0,
        *,
        public_labels: bool = False,
        precise_locations: bool = False,
        expected_token: str | None = None,
        approve_public_content: bool = False,
    ) -> dict[str, Any] | bytes:
        policy = fmt.scope(public_labels=public_labels, precise_locations=precise_locations)
        async with self.database.transaction() as tx:
            username = await self._authorize(tx, actor)
            page = await self._page(tx, destination, stream, after, policy)
            if expected_token is None:
                return page
            if not approve_public_content:
                raise BundleDenied(
                    "Explicit approval of all public text and provenance is required"
                )
            if not hmac.compare_digest(expected_token, page["review_token"]):
                raise ReviewConflict("Export changed. Refresh and review the complete page again")
            if not page["items"]:
                raise ValueError("No eligible records in this page")
            private, public = await self._key(tx)
            core = {key: page[key] for key in ("origin", "destination", "scope", "items")}
            core["created_at"] = int(self.clock.now().timestamp())
            await self._identity(tx)
            raw = fmt.encode(core, private, public)
            await self._audit(
                tx,
                username,
                "export",
                fmt.decode(raw).digest,
                {
                    "destination": destination,
                    "count": len(page["items"]),
                    "scope": policy,
                    "fingerprint": page["fingerprint"],
                },
            )
            return raw

    async def _preview(self, tx: Transaction, bundle: fmt.Bundle) -> tuple[Peer, dict[str, Any]]:
        identity = await self._identity(tx)
        core = bundle.core
        if core["destination"] != identity:
            raise BundleDenied("Bundle targets a different Outpost")
        peer = await self._peer(tx, core["origin"])
        pins = await tx.read(
            "SELECT public_key,fingerprint,state,reviewed_at FROM fed_relay_origin_key "
            "WHERE origin_node=?",
            (peer.mesh_id,),
        )
        if (
            not pins
            or pins[0]["state"] != "trusted"
            or bytes(pins[0]["public_key"]) != bundle.public_key
        ):
            raise BundleDenied(
                "Signing key is not currently trusted. Verify it through existing origin-key "
                "review; an uploaded signature never grants trust"
            )
        seen = await tx.read("SELECT 1 FROM fed_bundle_receipt WHERE digest=?", (bundle.digest,))
        if seen:
            raise ReviewConflict("This bundle was already committed. Do not import it again")
        count = await tx.read("SELECT COUNT(*) n FROM fed_bundle_receipt")
        if int(count[0]["n"]) >= MAX_RECEIPTS:
            raise BundleDenied(
                "Bundle receipt store is full; no receipts were pruned or replay protection reset"
            )
        states = []
        effects = []
        for item in core["items"]:
            await self._allowed(tx, peer, item)
            receipt = await tx.read(
                "SELECT epoch,revision,digest FROM fed_revision_receipt "
                "WHERE peer_id=? AND stream=? AND uid=?",
                (peer.id, item["stream"], item["uid"]),
            )
            inbox = await tx.read(
                "SELECT id,state,digest,source_epoch,source_revision FROM fed_inbox_item "
                "WHERE peer_id=? AND stream=? AND uid=?",
                (peer.id, item["stream"], item["uid"]),
            )
            content_digest = self.sync._payload_digest(
                json.dumps(item["payload"], separators=(",", ":"), sort_keys=True)
            )
            effect = "import"
            if receipt:
                prior = receipt[0]
                if prior["epoch"] != item["epoch"]:
                    raise ReviewConflict(
                        "Producer lineage changed; use existing reconciliation review first"
                    )
                if int(prior["revision"]) > item["revision"]:
                    effect = "skip_older"
                elif int(prior["revision"]) == item["revision"]:
                    if prior["digest"] != content_digest:
                        raise ReviewConflict("Conflicting payload for the same producer revision")
                    effect = (
                        "skip_retained_receipt"
                        if not inbox
                        else "import_pending"
                        if inbox[0]["state"] == "pending"
                        else "skip_reviewed"
                    )
            states.append([dict(row) for row in receipt] + [dict(row) for row in inbox])
            effects.append({"uid": item["uid"], "stream": item["stream"], "effect": effect})
        high = await tx.read("SELECT seq FROM sqlite_sequence WHERE name='fed_revision'")
        preview = {
            **core,
            "digest": bundle.digest,
            "fingerprint": bundle.fingerprint,
            "effects": effects,
        }
        preview["review_token"] = fmt.digest(
            fmt.canonical(
                [
                    bundle.digest,
                    self._policy(peer),
                    pins[0]["reviewed_at"],
                    states,
                    int(high[0]["seq"]) if high else 0,
                ]
            )
        )
        return peer, preview

    async def receive(
        self,
        actor: BundleActor,
        raw: bytes,
        *,
        expected_token: str | None = None,
        approve_public_content: bool = False,
        public_labels: bool = False,
        precise_locations: bool = False,
    ) -> dict[str, Any]:
        # Parsing is bounded before any writer is held. No archive extraction or filesystem path.
        bundle = fmt.decode(raw)
        async with self.database.transaction() as tx:
            username = await self._authorize(tx, actor)
            peer, preview = await self._preview(tx, bundle)
            if expected_token is None:
                return preview
            if not hmac.compare_digest(expected_token, preview["review_token"]):
                raise ReviewConflict("Import state changed. Preview and review this file again")
            policy = fmt.scope(public_labels=public_labels, precise_locations=precise_locations)
            if not approve_public_content or any(
                bundle.core["scope"][key] and not policy[key] for key in policy
            ):
                raise BundleDenied(
                    "Explicit approval of public content and its declared privacy scope is required"
                )
            now = int(self.clock.now().timestamp())
            imported = 0
            # A mixed externally generated page still imports parents before plain notes.
            pairs = sorted(
                zip(bundle.core["items"], preview["effects"], strict=True),
                key=lambda pair: pair[0]["stream"] == "incident_updates",
            )
            for item, effect in pairs:
                if effect["effect"].startswith("skip_"):
                    continue
                await self.sync.quarantine_transaction(tx, peer, item, now)
                inbox = await tx.read(
                    "SELECT id FROM fed_inbox_item "
                    "WHERE peer_id=? AND stream=? AND uid=? AND state='pending'",
                    (peer.id, item["stream"], item["uid"]),
                )
                if not inbox:
                    continue
                item_id = int(inbox[0]["id"])
                stream = await self.sync.import_inbox_transaction(
                    tx, item_id, "web:" + username, now
                )
                await FederationReviewService._audit(
                    tx, item_id, "web:" + username, "import", stream, expected_token, now
                )
                imported += 1
            skipped = len(bundle.core["items"]) - imported
            await self._identity(tx)
            await tx.write(
                "INSERT INTO fed_bundle_receipt VALUES(?,?,?,?,?,?,?)",
                (bundle.digest, peer.mesh_id, bundle.fingerprint, now, username, imported, skipped),
            )
            await self._audit(
                tx,
                username,
                "import",
                bundle.digest,
                {
                    "origin": peer.mesh_id,
                    "fingerprint": bundle.fingerprint,
                    "scope": bundle.core["scope"],
                    "imported": imported,
                    "skipped": skipped,
                },
            )
            return {
                "state": "committed",
                "digest": bundle.digest,
                "imported": imported,
                "skipped": skipped,
            }
