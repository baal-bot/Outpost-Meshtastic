from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from outpost.audit import write_audit
from outpost.store import Database, Transaction

if TYPE_CHECKING:
    from outpost.fed.sync import FederationSyncService


class ReviewConflict(ValueError):
    """The pending item is no longer the version the operator reviewed."""


def review_token(row: Mapping[str, Any]) -> str:
    """Bind a review to receiver-owned content and provenance, not the wire digest.

    This is an optimistic-concurrency tag, not authorization or proof that a
    person read every field. It is deterministic across ordinary restarts.
    """
    fields = (
        "id",
        "peer_id",
        "stream",
        "uid",
        "state",
        "payload_json",
        "received_at",
        "source_epoch",
        "source_revision",
        "mesh_id",
        "node_name",
    )
    encoded = json.dumps([row[key] for key in fields], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


class FederationReviewService:
    """Human decisions own the version check, domain mutation and audit together."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def _pending(
        self, transaction: Transaction, item_id: int, expected_token: str
    ) -> dict[str, Any]:
        rows = await transaction.read(
            "SELECT i.*,p.mesh_id,p.node_name,p.state AS peer_state FROM fed_inbox_item i "
            "JOIN fed_peer p ON p.id=i.peer_id WHERE i.id=? AND i.state='pending'",
            (item_id,),
        )
        if not rows or not hmac.compare_digest(review_token(dict(rows[0])), expected_token):
            raise ReviewConflict(
                "Federation item changed or was reviewed. Refresh and review again."
            )
        return dict(rows[0])

    @staticmethod
    async def _audit(
        transaction: Transaction,
        item_id: int,
        actor: str,
        action: str,
        stream: str,
        fingerprint: str,
        now: int,
    ) -> None:
        kind, separator, ref = actor.partition(":")
        if not separator or kind not in {"web", "mesh"}:
            raise ValueError("Human federation review requires a web or mesh actor.")
        await write_audit(
            transaction,
            actor_kind=kind,
            actor_ref=ref,
            action=f"federation.inbox.{action}",
            target=f"federation-inbox:{item_id}",
            detail={"stream": stream, "review_fingerprint": fingerprint},
            created_at=now,
        )

    async def approve(
        self,
        sync: FederationSyncService,
        item_id: int,
        expected_token: str,
        actor: str,
        now: int,
    ) -> str:
        async with self.database.transaction() as transaction:
            row = await self._pending(transaction, item_id, expected_token)
            if row["peer_state"] != "active":
                raise ValueError("Peer is no longer active; import is not allowed.")
            # The domain core rechecks current module/peer/destination scope
            # on this same connection. No nested transaction or external I/O.
            stream = await sync.import_inbox_transaction(transaction, item_id, actor, now)
            await self._audit(transaction, item_id, actor, "import", stream, expected_token, now)
        return stream

    async def reject(
        self, item_id: int, expected_token: str, actor: str, reason: str, now: int
    ) -> None:
        async with self.database.transaction() as transaction:
            row = await self._pending(transaction, item_id, expected_token)
            await transaction.write(
                "UPDATE fed_inbox_item SET state='rejected',reviewed_at=?,reviewed_by=?,"
                "rejection_reason=? WHERE id=?",
                (now, actor, reason[:160], item_id),
            )
            await self._audit(
                transaction, item_id, actor, "reject", str(row["stream"]), expected_token, now
            )
