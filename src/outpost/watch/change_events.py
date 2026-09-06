"""Bounded inspection of atomic, coalesced source intents; no delivery claims."""

from __future__ import annotations

from dataclasses import dataclass

from outpost.store import Database

MAX_PENDING = 100


@dataclass(frozen=True)
class IncidentChange:
    stream: str
    uid: str
    epoch: str
    revision: int
    first_revision: int
    state: str


class IncidentChangeEvents:
    """Migration-owned capture covers all producer-revision mutation paths.

    This is not a per-peer outbox. There is deliberately no consume/send/receipt
    method until a dispatcher can atomically hand work to scoped peer intents.
    """

    def __init__(self, database: Database) -> None:
        self.database = database

    async def pending(self, *, after: int = 0, limit: int = MAX_PENDING) -> list[IncidentChange]:
        """Read one indexed page, preserving first-pending order on supersession.

        `after` is a page cursor within an inspection pass, NOT a durable change
        watermark: changed pending heads retain their place. Restart passes at 0.
        State `pending` means only that the producer head still matches, never
        that content exists, is exportable, or has reached a peer/radio/person.
        """
        if type(after) is not int or not 0 <= after < 2**63:
            raise ValueError("invalid incident change cursor")
        if type(limit) is not int or not 1 <= limit <= MAX_PENDING:
            raise ValueError("invalid incident change page size")
        rows = await self.database.read(
            "SELECT c.stream,c.uid,c.epoch,c.revision,c.first_revision,"
            "CASE WHEN l.epoch IS NULL OR l.epoch<>c.epoch THEN 'lineage_mismatch' "
            "WHEN r.revision IS NULL THEN 'missing_head' "
            "WHEN r.revision<>c.revision THEN 'revision_mismatch' "
            "ELSE 'pending' END AS state "
            "FROM incident_change_event c INDEXED BY idx_incident_change_pending "
            "LEFT JOIN fed_revision r ON r.stream=c.stream AND r.uid=c.uid "
            "LEFT JOIN fed_revision_lineage l ON l.id=1 "
            "WHERE c.first_revision>? ORDER BY c.first_revision LIMIT ?",
            (after, limit),
        )
        return [IncidentChange(**dict(row)) for row in rows]
