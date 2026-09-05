from __future__ import annotations

from .database import Transaction


async def incident_reference(transaction: Transaction, uid: str) -> int:
    """Choose a permanent local reference while holding the serialized writer.

    The incident INSERT trigger reserves this binding in the same transaction.
    Reimporting a purged origin restores its reference, never another incident's.
    """
    rows = await transaction.read(
        "SELECT local_ref FROM incident_reference WHERE incident_uid=?", (uid,)
    )
    if rows:
        return int(rows[0]["local_ref"])
    rows = await transaction.read(
        "SELECT COALESCE(MAX(local_ref),0)+1 next_ref FROM incident_reference"
    )
    return int(rows[0]["next_ref"])
