from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, cast

from outpost.transport.toa import MAX_PAYLOAD_BYTES

from .database import Database, Transaction

ACTIVE_STATES = ("pending", "held", "sending")
CANCELLABLE_STATES = ("pending", "held", "awaiting_ack", "failed")
OPERATOR_STATES = (
    "pending",
    "held",
    "sending",
    "awaiting_ack",
    "sent",
    "acked",
    "failed",
    "expired",
    "cancelled",
    "superseded",
    "retracted",
)
RECOVERY_NOTE = "recovered after an interrupted send"


class OutboxRejected(ValueError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class Admission:
    ids: list[int]
    superseded_ids: list[int]


class _StoreTransaction(Protocol):
    async def write(self, sql: str, params: tuple[Any, ...] = ()) -> int: ...
    async def read(self, sql: str, params: tuple[Any, ...] = ()) -> list[Any]: ...


class OutboxStore:
    """Durable state machine for work admitted to the sole-egress scheduler."""

    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _valid_payload_size(record: dict[str, Any]) -> bool:
        size = record.get("byte_len")
        return type(size) is int and 0 <= size <= MAX_PAYLOAD_BYTES

    async def admit_many(
        self,
        records: list[dict[str, Any]],
        *,
        queue_max_items: int,
        dedupe_window_s: int,
        transaction: Transaction | None = None,
    ) -> Admission:
        async def admit(store: _StoreTransaction) -> Admission:
            if not all(self._valid_payload_size(record) for record in records):
                raise OutboxRejected("payload_too_large")
            supersedes = sorted(
                {str(record["supersedes"]) for record in records if record["supersedes"]}
            )
            superseded_ids: list[int] = []
            exclusion = ""
            params: tuple[Any, ...] = ACTIVE_STATES
            if supersedes:
                placeholders = ",".join("?" for _ in supersedes)
                exclusion = f" AND (queue_key IS NULL OR queue_key NOT IN ({placeholders}))"
                params += tuple(supersedes)
                rows = await store.read(
                    f"SELECT id FROM outbound_work WHERE state IN ('pending','held') "  # noqa: S608
                    f"AND queue_key IN ({placeholders})",
                    tuple(supersedes),
                )
                superseded_ids = [int(row["id"]) for row in rows]
            rows = await store.read(
                "SELECT COUNT(*) count FROM outbound_work WHERE state IN (?,?,?)"  # noqa: S608
                + exclusion,
                params,
            )
            if int(rows[0]["count"]) + len(records) > queue_max_items:
                raise OutboxRejected("queue_full")

            batch_keys: set[tuple[str, int, str]] = set()
            for record in records:
                key = (
                    str(record["destination"]),
                    int(record["channel"]),
                    str(record["dedupe_hash"]),
                )
                if key in batch_keys:
                    raise OutboxRejected("duplicate")
                batch_keys.add(key)
                recent = await store.read(
                    "SELECT id FROM outbound_work WHERE destination=? AND channel=? "
                    "AND dedupe_hash=? AND created_at>? AND state<>'retracted' LIMIT 1",
                    (*key, float(record["created_at"]) - dedupe_window_s),
                )
                if recent:
                    raise OutboxRejected("duplicate")

            if superseded_ids:
                placeholders = ",".join("?" for _ in superseded_ids)
                await store.write(
                    f"UPDATE outbound_work SET state='superseded',completed_at=? "  # noqa: S608
                    f"WHERE id IN ({placeholders})",
                    (float(records[0]["created_at"]), *superseded_ids),
                )

            ids: list[int] = []
            for record in records:
                ids.append(
                    await store.write(
                        """
                        INSERT INTO outbound_work(
                          uid,batch_uid,state,text,binary_payload,destination,channel,
                          traffic_class,severity,want_ack,priority,created_at,expires_at,
                          supersedes,queue_key,dedupe_token,dedupe_hash,portnum,multipart
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            record["uid"],
                            record["batch_uid"],
                            record["state"],
                            record["text"],
                            record["binary_payload"],
                            record["destination"],
                            record["channel"],
                            record["traffic_class"],
                            record["severity"],
                            int(record["want_ack"]),
                            record["priority"],
                            record["created_at"],
                            record["expires_at"],
                            record["supersedes"],
                            record["queue_key"],
                            record["dedupe_token"],
                            record["dedupe_hash"],
                            record["portnum"],
                            int(record["multipart"]),
                        ),
                    )
                )
            return Admission(ids, superseded_ids)

        if transaction is not None:
            return await admit(transaction)
        async with self.database.transaction() as owned:
            return await admit(owned)

    async def recover(self, now: float) -> list[dict[str, Any]]:
        async with self.database.transaction() as transaction:
            await transaction.write(
                "UPDATE outbound_attempt SET state='uncertain',completed_at=?,error=? "
                "WHERE state='started'",
                (now, RECOVERY_NOTE),
            )
            await transaction.write(
                "UPDATE outbound_work SET state='failed',completed_at=?,last_error=? "
                "WHERE state IN ('pending','held','sending','awaiting_ack') AND "
                "length(CASE WHEN binary_payload IS NOT NULL THEN binary_payload "
                "ELSE CAST(text AS BLOB) END)>?",
                (now, "payload exceeds radio byte limit", MAX_PAYLOAD_BYTES),
            )
            await transaction.write(
                "UPDATE outbound_work SET state='expired',completed_at=? "
                "WHERE state IN ('pending','held','sending','awaiting_ack') AND expires_at<=?",
                (now, now),
            )
            await transaction.write(
                "UPDATE outbound_work SET state='pending',last_error=?,next_attempt_at=? "
                "WHERE state IN ('sending','held')",
                (RECOVERY_NOTE, now),
            )
            rows = await transaction.read(
                "SELECT * FROM outbound_work WHERE state='pending' "
                "ORDER BY CASE traffic_class WHEN 'alert' THEN 0 ELSE 1 END,created_at,id"
            )
        return [dict(row) for row in rows]

    async def recent_airtime(self, now: float) -> list[dict[str, Any]]:
        rows = await self.database.read(
            """
            SELECT created_at,toa_ms,airtime_class,severity FROM (
              SELECT a.started_at created_at,a.estimated_toa_ms toa_ms,
                     w.traffic_class airtime_class,w.severity,a.id sort_id
              FROM outbound_attempt a
              JOIN outbound_work w ON w.id=a.outbox_id
              WHERE a.state IN ('sent','uncertain')
                AND a.started_at>? AND a.started_at<=?
              UNION ALL
              SELECT m.created_at,m.toa_ms,m.airtime_class,'info',m.id
              FROM message_log m
              WHERE m.direction='out' AND m.outbox_id IS NULL AND m.toa_ms IS NOT NULL
                AND m.airtime_class IN ('alert','reply','ai','bulletin','digest','federation')
                AND m.created_at>? AND m.created_at<=?
            ) ORDER BY created_at,sort_id
            """,
            (now - 3_600, now, now - 3_600, now),
        )
        return [dict(row) for row in rows]

    async def release_many(self, item_ids: list[int]) -> None:
        if not item_ids:
            return
        placeholders = ",".join("?" for _ in item_ids)
        await self.database.write(
            f"UPDATE outbound_work SET state='pending' WHERE state='held' "  # noqa: S608
            f"AND id IN ({placeholders})",
            tuple(item_ids),
        )

    async def retract_many(self, item_ids: list[int], now: float) -> None:
        if not item_ids:
            return
        placeholders = ",".join("?" for _ in item_ids)
        await self.database.write(
            f"UPDATE outbound_work SET state='retracted',completed_at=? "  # noqa: S608
            f"WHERE state IN ('pending','held') AND id IN ({placeholders})",
            (now, *item_ids),
        )

    async def cancel(self, item_id: int, now: float) -> bool:
        async with self.database.transaction() as transaction:
            rows = await transaction.read("SELECT state FROM outbound_work WHERE id=?", (item_id,))
            if not rows or str(rows[0]["state"]) not in CANCELLABLE_STATES:
                return False
            await transaction.write(
                "UPDATE outbound_work SET state='cancelled',completed_at=? WHERE id=?",
                (now, item_id),
            )
        return True

    async def expire(self, item_id: int, now: float) -> None:
        await self.database.write(
            "UPDATE outbound_work SET state='expired',completed_at=? "
            "WHERE id=? AND state IN ('pending','held')",
            (now, item_id),
        )

    async def expire_ack_waits(self, now: float) -> None:
        await self.database.write(
            "UPDATE outbound_work SET state='expired',completed_at=? "
            "WHERE state='awaiting_ack' AND expires_at<=?",
            (now, now),
        )

    async def start_attempt(self, item_id: int, now: float, estimated_toa_ms: int) -> bool:
        async with self.database.transaction() as transaction:
            rows = await transaction.read(
                "SELECT state,attempts FROM outbound_work WHERE id=?", (item_id,)
            )
            if not rows or rows[0]["state"] != "pending":
                return False
            attempt_no = int(rows[0]["attempts"]) + 1
            await transaction.write(
                "UPDATE outbound_work SET state='sending',attempts=?,"
                "last_attempt_at=?,next_attempt_at=NULL,last_error=NULL WHERE id=?",
                (attempt_no, now, item_id),
            )
            await transaction.write(
                "INSERT INTO outbound_attempt(outbox_id,attempt_no,state,started_at,"
                "estimated_toa_ms) VALUES(?,?,'started',?,?)",
                (item_id, attempt_no, now, estimated_toa_ms),
            )
        return True

    async def fail_unstarted(self, item_id: int, now: float, error: str) -> None:
        await self.database.write(
            "UPDATE outbound_work SET state='failed',completed_at=?,last_error=? "
            "WHERE id=? AND state IN ('pending','held')",
            (now, error[:240], item_id),
        )

    async def fail_attempt(
        self, item_id: int, now: float, error: str, *, retry_limit: int = 3
    ) -> tuple[str, float | None, int]:
        async with self.database.transaction() as transaction:
            rows = await transaction.read(
                "SELECT attempts,expires_at FROM outbound_work WHERE id=? AND state='sending'",
                (item_id,),
            )
            if not rows:
                return "failed", None, retry_limit
            attempts = int(rows[0]["attempts"])
            expires_at = float(rows[0]["expires_at"])
            retry_at = now + min(60, 2 ** max(0, attempts - 1) * 5)
            if attempts < retry_limit and retry_at < expires_at:
                state, completed_at = "pending", None
            else:
                state, retry_at, completed_at = "failed", None, now
            await transaction.write(
                "UPDATE outbound_work SET state=?,next_attempt_at=?,last_error=?,completed_at=? "
                "WHERE id=?",
                (state, retry_at, error[:240], completed_at, item_id),
            )
            await transaction.write(
                "UPDATE outbound_attempt SET state='uncertain',completed_at=?,error=? "
                "WHERE outbox_id=? AND attempt_no=? AND state='started'",
                (now, error[:240], item_id, attempts),
            )
        return state, retry_at, attempts

    async def complete_attempt(
        self,
        item_id: int,
        *,
        now: float,
        packet_id: int | None,
        outcome: str,
        peer_mesh_id: str,
        channel: int,
        portnum: int,
        text: str | None,
        byte_len: int,
        toa_ms: int,
        airtime_class: str,
        is_direct: bool,
        wait_for_ack: bool,
    ) -> int:
        state = "awaiting_ack" if wait_for_ack and packet_id is not None else "sent"
        completed_at = None if state == "awaiting_ack" else now
        async with self.database.transaction() as transaction:
            attempts = await transaction.read(
                "SELECT id FROM outbound_attempt WHERE outbox_id=? AND state='started' "
                "ORDER BY attempt_no DESC LIMIT 1",
                (item_id,),
            )
            existing = await transaction.read(
                "SELECT id FROM message_log WHERE outbox_id=?", (item_id,)
            )
            if existing:
                log_id = int(existing[0]["id"])
                await transaction.write(
                    "UPDATE message_log SET packet_id=?,outcome=? WHERE id=?",
                    (packet_id, outcome, log_id),
                )
            else:
                log_id = await transaction.write(
                    """
                    INSERT INTO message_log(
                      direction,peer_mesh_id,channel,portnum,is_direct,packet_id,text,
                      byte_len,toa_ms,airtime_class,outcome,transport,created_at,outbox_id
                    ) VALUES('out',?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        peer_mesh_id,
                        channel,
                        portnum,
                        int(is_direct),
                        packet_id,
                        text,
                        byte_len,
                        toa_ms,
                        airtime_class,
                        outcome,
                        "mesh",
                        int(now),
                        item_id,
                    ),
                )
            await transaction.write(
                "UPDATE outbound_work SET state=?,packet_id=?,outcome=?,completed_at=?,"
                "last_error=NULL WHERE id=?",
                (state, packet_id, outcome, completed_at, item_id),
            )
            if attempts:
                await transaction.write(
                    "UPDATE outbound_attempt SET state='sent',completed_at=?,packet_id=?,"
                    "outcome=?,message_log_id=? WHERE id=? AND state='started'",
                    (now, packet_id, outcome, log_id, attempts[0]["id"]),
                )
        return log_id

    async def list_operator_work(self, limit: int = 100) -> list[dict[str, Any]]:
        result = await self.operator_history(
            states=("pending", "held", "sending", "awaiting_ack", "failed", "expired"),
            limit=limit,
        )
        return cast(list[dict[str, Any]], result["items"])

    async def operator_history(
        self,
        *,
        states: tuple[str, ...],
        limit: int = 25,
        before_id: int | None = None,
    ) -> dict[str, Any]:
        if not states or not set(states).issubset(OPERATOR_STATES):
            raise ValueError("invalid outbound history state filter")
        if not 1 <= limit <= 100:
            raise ValueError("outbound history page size must be 1-100")
        placeholders = ",".join("?" for _ in states)
        cursor_clause = " AND id<?" if before_id is not None else ""
        params: tuple[Any, ...] = (*states, *((before_id,) if before_id is not None else ()))
        rows = await self.database.read(
            f"""
            SELECT id,state,text,destination,channel,traffic_class,severity,want_ack,
                   length(binary_payload) binary_len,created_at,expires_at,attempts,
                   last_attempt_at,next_attempt_at,packet_id,outcome,last_error,completed_at
            FROM outbound_work
            WHERE state IN ({placeholders}){cursor_clause}
            ORDER BY id DESC
            LIMIT ?
            """,  # noqa: S608 -- SQL structure comes only from validated state names
            (*params, limit + 1),
        )
        count_rows = await self.database.read(
            "SELECT state,COUNT(*) AS count FROM outbound_work GROUP BY state"
        )
        counts = {state: 0 for state in OPERATOR_STATES}
        counts.update({str(row["state"]): int(row["count"]) for row in count_rows})
        items = [dict(row) for row in rows[:limit]]
        return {
            "items": items,
            "next_cursor": int(items[-1]["id"]) if len(rows) > limit and items else None,
            "total": sum(counts[state] for state in states),
            "counts": counts,
        }
