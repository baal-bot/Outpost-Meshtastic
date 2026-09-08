"""Reviewed legacy BBS namespace association; never private-data or key adoption."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from outpost.audit import write_audit
from outpost.store import Transaction

from .peers import FederationPeerService

NODE = re.compile(r"^![0-9a-f]{8}$")


class AdoptionDenied(ValueError):
    pass


class AdoptionConflict(ValueError):
    pass


@dataclass(frozen=True)
class AdoptionActor:
    account_id: int
    session_hash: str


class FederationAdoptionService:
    def __init__(self, peers: FederationPeerService, enabled: Callable[[], bool]) -> None:
        self.peers, self.database, self.enabled = peers, peers.database, enabled
        self._secret = secrets.token_bytes(32)

    async def _context(
        self,
        tx: Transaction,
        actor: AdoptionActor,
        predecessor: str,
        successor: str,
        old_name: str | None,
    ) -> tuple[str, dict[str, Any]]:
        if not self.enabled():
            raise AdoptionDenied("Federation is disabled")
        if not NODE.fullmatch(predecessor) or not NODE.fullmatch(successor):
            raise ValueError("Use lowercase !1234abcd node identities")
        if not NODE.fullmatch(self.peers.local_mesh_id):
            raise AdoptionDenied("Establish the local station identity before adopting history")
        if len({predecessor, successor, self.peers.local_mesh_id}) != 3:
            raise AdoptionDenied("Local, predecessor and successor identities must differ")
        if old_name is not None and len(old_name) > 80:
            raise ValueError("Predecessor label is too long")
        accounts = await tx.read(
            "SELECT a.username FROM web_account a JOIN web_session s ON s.account_id=a.id "
            "WHERE a.id=? AND s.token_hash=? AND s.expires_at>? AND a.enabled=1 "
            "AND a.must_change=0 AND a.role IN ('operator','administrator')",
            (actor.account_id, actor.session_hash, int(time.time())),
        )
        if not accounts:
            raise AdoptionDenied("A current named operator session is required")
        current = await tx.read("SELECT * FROM fed_peer WHERE mesh_id=?", (successor,))
        if (
            not current
            or current[0]["state"] != "active"
            or not all(
                current[0][key] for key in ("local_approved", "remote_approved", "shared_secret")
            )
        ):
            raise AdoptionDenied("Mutually pair the successor under its own identity first")
        peer = current[0]
        if len(bytes(peer["shared_secret"])) != 32:
            raise AdoptionDenied("Successor pairing key is invalid; review its pairing")
        previous = await tx.read(
            "SELECT id,state,shared_secret FROM fed_peer WHERE mesh_id=?", (predecessor,)
        )
        if previous and (
            previous[0]["state"] != "rejected" or previous[0]["shared_secret"] is not None
        ):
            raise AdoptionDenied(
                "Explicitly reject the predecessor peer before associating history"
            )
        pins = await tx.read(
            "SELECT fingerprint,state FROM fed_relay_origin_key WHERE origin_node=?", (predecessor,)
        )
        if pins and pins[0]["state"] != "rejected":
            raise AdoptionDenied("Explicitly reject the predecessor origin signing pin as well")
        mappings = await tx.read(
            "SELECT old_mesh_id,successor_peer_id FROM fed_peer_successor "
            "WHERE old_mesh_id IN (?,?) OR successor_peer_id IN "
            "(SELECT id FROM fed_peer WHERE mesh_id IN (?,?)) ORDER BY old_mesh_id",
            (predecessor, successor, predecessor, successor),
        )
        if mappings:
            raise AdoptionConflict(
                "An identity association already exists; replacement, chains and "
                "ambiguous aliases are not supported"
            )
        history = (
            await tx.read(
                "SELECT (SELECT count(*) FROM thread t JOIN board b ON b.id=t.board_id "
                "WHERE t.uid GLOB ? AND b.min_read_trust='guest' AND t.hidden=0) threads,"
                "(SELECT count(*) FROM post p JOIN thread t ON t.id=p.thread_id "
                "JOIN board b ON b.id=t.board_id WHERE p.uid GLOB ? "
                "AND b.min_read_trust='guest' AND t.hidden=0 AND p.hidden=0) posts",
                (predecessor + ":*", predecessor + ":*"),
            )
        )[0]
        if not history["threads"] and not history["posts"]:
            raise AdoptionDenied("No retained public BBS history uses that predecessor")
        snapshot = {
            "local_identity": self.peers.local_mesh_id,
            "predecessor": predecessor,
            "successor": successor,
            "successor_id": peer["id"],
            "successor_name": peer["node_name"],
            "key_context": hashlib.sha256(bytes(peer["shared_secret"])).hexdigest(),
            "pairing_approved_at": peer["approved_at"],
            "predecessor_row": [dict(row) for row in previous],
            "predecessor_pin": [dict(row) for row in pins],
            "threads": history["threads"],
            "posts": history["posts"],
            "label": old_name,
            "operator": actor.account_id,
            "session": actor.session_hash,
            "review_window": int(self.peers.clock.monotonic() // 600),
        }
        return str(accounts[0]["username"]), snapshot

    def _tag(self, snapshot: dict[str, Any]) -> str:
        return hmac.new(
            self._secret,
            json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode(),
            hashlib.sha256,
        ).hexdigest()

    async def review(
        self,
        actor: AdoptionActor,
        predecessor: str,
        successor: str,
        old_name: str | None = None,
        *,
        expected_token: str | None = None,
        confirm_namespace: bool = False,
    ) -> dict[str, object]:
        async with self.database.transaction() as tx:
            username, snapshot = await self._context(tx, actor, predecessor, successor, old_name)
            token = self._tag(snapshot)
            if expected_token is None:
                return {
                    "review_token": token,
                    "predecessor": predecessor,
                    "successor": successor,
                    "threads": snapshot["threads"],
                    "posts": snapshot["posts"],
                    "scope": "legacy_public_bbs_namespace_only",
                    "warning": (
                        "Only use for an independently verified continuation of the same BBS "
                        "record namespace. A fresh empty replacement must not adopt numeric "
                        "suffixes. No incidents, alerts, keys, accounts, private mail, welfare "
                        "or responsibility transfer."
                    ),
                }
            if not confirm_namespace:
                raise AdoptionDenied("Explicit BBS namespace-continuity confirmation is required")
            if not hmac.compare_digest(token, expected_token):
                raise AdoptionConflict("Identity context changed or review expired; preview again")
            await tx.write(
                "INSERT INTO fed_peer_successor(old_mesh_id,successor_peer_id,old_node_name,"
                "adopted_at,adopted_by) VALUES(?,?,?,?,?)",
                (
                    predecessor,
                    snapshot["successor_id"],
                    old_name,
                    int(self.peers.clock.now().timestamp()),
                    f"web:{username}",
                ),
            )
            await write_audit(
                tx,
                actor_kind="web",
                actor_ref=username,
                action="federation.origin_adopt",
                target=f"fed_peer:{snapshot['successor_id']}",
                detail={
                    "predecessor": predecessor,
                    "successor": successor,
                    "scope": "legacy_public_bbs_namespace_only",
                    "namespace_confirmed": True,
                },
                created_at=int(self.peers.clock.now().timestamp()),
            )
            return {"ok": True, "old_mesh_id": predecessor, "successor_mesh_id": successor}
