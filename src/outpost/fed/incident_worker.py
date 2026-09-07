"""Bounded automatic scheduling of retained incident intents, never direct radio I/O."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from typing import Any

from outpost.audit import write_audit
from outpost.fed.framing import FrameTooLarge
from outpost.fed.incident_sender import IncidentDeferred, IncidentSender
from outpost.store import Transaction

POLL_SECONDS = 5
PEERS_PER_TICK = 4
HEADS_PER_LANE = 4
ITEMS_PER_LANE = 2
MAX_ACTIVE_PER_PEER = 2
DELIVERY_TTL = 1800
MAX_APPLICATION_ATTEMPTS = 3
RECEIPT_BACKOFF = (60, 120, 240)
TERMINAL = frozenset({"stored", "expired", "cancelled", "blocked", "retry_exhausted"})
NEVER = 253402300799


class IncidentWorker:
    def __init__(self, sender: IncidentSender) -> None:
        self.sender = sender
        self.database = sender.sync.database
        self.clock = sender.peers.clock
        self._lock = asyncio.Lock()
        self._after_peer = 0
        self._last_mono: float | None = None
        self._policy_time = self.clock.now().timestamp()

    def _now(self) -> int:
        """A backward wall step cannot shorten in-process backoff or extend TTL.

        Persisted future deadlines remain conservative across restart. This is
        not RTC/forward-clock-step qualification or a trusted remote timestamp.
        """
        mono = self.clock.monotonic()
        self._policy_time = max(
            self.clock.now().timestamp(),
            self._policy_time
            + (max(0, mono - self._last_mono) if self._last_mono is not None else 0),
        )
        self._last_mono = mono
        return int(self._policy_time)

    async def tick(self) -> None:
        async with self._lock:
            if not self.sender.identity() or not all(
                self.sender.sync.module_enabled(name) for name in ("fed", "watch")
            ):
                return
            rows = await self.database.read(
                "SELECT id FROM fed_peer WHERE state='active' AND sync_incidents=1 AND id>? "
                "ORDER BY id LIMIT ?",
                (self._after_peer, PEERS_PER_TICK),
            )
            if not rows and self._after_peer:
                rows = await self.database.read(
                    "SELECT id FROM fed_peer WHERE state='active' AND sync_incidents=1 "
                    "ORDER BY id LIMIT ?",
                    (PEERS_PER_TICK,),
                )
            self._after_peer = rows[-1]["id"] if len(rows) == PEERS_PER_TICK else 0
            for row in rows:
                peer_id = row["id"]
                try:
                    await self.sender.sync.incident_handoff.stage_automatic(
                        peer_id, limit=HEADS_PER_LANE
                    )
                except ValueError:
                    await self._peer_status(peer_id, "policy_or_lineage_blocked")
                    continue
                await self._peer_status(peer_id, "running")
                for lane in ("fresh", "backfill"):
                    pending = await self.database.read(
                        "SELECT * FROM fed_incident_intent INDEXED BY idx_incident_delivery_due "
                        "WHERE peer_id=? AND lane=? AND next_attempt_at<=? "
                        "ORDER BY next_attempt_at,urgency_rank,first_revision LIMIT ?",
                        (peer_id, lane, self._now(), ITEMS_PER_LANE),
                    )
                    for item in pending:
                        await self._deliver(dict(item))

    async def _peer_status(self, peer_id: int, state: str) -> None:
        await self.database.write(
            "INSERT INTO fed_cursor(peer_id,stream,direction,cursor,updated_at) "
            "VALUES(?,'_incident_worker','send',?,?) "
            "ON CONFLICT(peer_id,stream,direction) DO UPDATE SET "
            "cursor=excluded.cursor,updated_at=excluded.updated_at "
            "WHERE fed_cursor.cursor<>excluded.cursor "
            "OR fed_cursor.updated_at<=excluded.updated_at-60",
            (peer_id, '{"state":"' + state + '"}', self._now()),
        )

    @staticmethod
    def _key(item: dict[str, Any]) -> tuple[Any, ...]:
        return (item["peer_id"], item["stream"], item["uid"])

    async def _set(
        self,
        tx: Transaction,
        item: dict[str, Any],
        state: str,
        reason: str | None = None,
        *,
        due: int | None = None,
    ) -> None:
        await tx.write(
            "UPDATE fed_incident_intent SET delivery_state=?,delivery_reason=?,next_attempt_at=? "
            "WHERE peer_id=? AND stream=? AND uid=?",
            (
                state,
                reason,
                NEVER if state in TERMINAL else due or self._now() + POLL_SECONDS,
                *self._key(item),
            ),
        )

    async def _stop(
        self,
        tx: Transaction,
        item: dict[str, Any],
        state: str,
        reason: str,
        prior: dict[str, Any] | None,
    ) -> None:
        await self._set(tx, item, state, reason)
        if prior:
            rows = await tx.read(
                "SELECT id FROM outbound_work WHERE queue_key=? "
                "AND state IN ('pending','held','failed')",
                (prior["queue_key"],),
            )
            await tx.write(
                "UPDATE outbound_work SET state='cancelled',completed_at=? "
                "WHERE queue_key=? AND state IN ('pending','held','failed')",
                (self._now(), prior["queue_key"]),
            )
            ids = {row["id"] for row in rows}
            tx.after_commit(lambda: self.sender.governor._remove_ids(ids))

    async def _deliver(self, selected: dict[str, Any]) -> None:
        # Arm before fallible encoding/admission. Queue rejection, cancellation or
        # a long writer wait cannot restart the finite window on every poll.
        now = self._now()
        await self.database.write(
            "UPDATE fed_incident_intent SET scheduled_at=?,deadline_at=? "
            "WHERE peer_id=? AND stream=? AND uid=? AND scheduled_at IS NULL "
            "AND epoch=? AND revision=? AND scope=? AND digest IS ?",
            (
                now,
                now + DELIVERY_TTL,
                *self._key(selected),
                selected["epoch"],
                selected["revision"],
                selected["scope"],
                selected["digest"],
            ),
        )
        try:
            await self._attempt(selected)
        except (FrameTooLarge, IncidentDeferred, ValueError) as error:
            if isinstance(error, FrameTooLarge):
                state, reason = "blocked", "payload_too_large"
            elif isinstance(error, IncidentDeferred):
                state, reason = "waiting", error.reason
            else:
                state, reason = "blocked", "policy_or_content_changed"
            # The failed admission transaction rolled back its counter and frames.
            # Persist only a matching-version diagnostic, never overwrite a newer head.
            async with self.database.transaction() as tx:
                now = self._now()
                current = await tx.read(
                    "SELECT deadline_at FROM fed_incident_intent "
                    "WHERE peer_id=? AND stream=? AND uid=?",
                    self._key(selected),
                )
                if (
                    current
                    and current[0]["deadline_at"] is not None
                    and now >= current[0]["deadline_at"]
                ):
                    state, reason = "expired", "delivery_deadline"
                await tx.write(
                    "UPDATE fed_incident_intent SET scheduled_at=COALESCE(scheduled_at,?),"
                    "deadline_at=COALESCE(deadline_at,?),delivery_state=?,delivery_reason=?,"
                    "next_attempt_at=? WHERE peer_id=? AND stream=? AND uid=? "
                    "AND epoch=? AND revision=? AND scope=? AND digest IS ?",
                    (
                        now,
                        now + DELIVERY_TTL,
                        state,
                        reason,
                        NEVER
                        if state in TERMINAL
                        else now + (30 if reason == "peer_offline" else POLL_SECONDS),
                        *self._key(selected),
                        selected["epoch"],
                        selected["revision"],
                        selected["scope"],
                        selected["digest"],
                    ),
                )

    async def _attempt(self, selected: dict[str, Any]) -> None:
        async with self.database.transaction() as tx:
            rows = await tx.read(
                "SELECT * FROM fed_incident_intent WHERE peer_id=? AND stream=? AND uid=?",
                self._key(selected),
            )
            if not rows:
                return
            item = dict(rows[0])
            if any(item[key] != selected[key] for key in ("epoch", "revision", "scope", "digest")):
                return
            now = self._now()
            if item["delivery_state"] in TERMINAL or item["next_attempt_at"] > now:
                return
            if item["scheduled_at"] is None:
                item["scheduled_at"], item["deadline_at"] = now, now + DELIVERY_TTL
                await tx.write(
                    "UPDATE fed_incident_intent SET scheduled_at=?,deadline_at=? "
                    "WHERE peer_id=? AND stream=? AND uid=?",
                    (now, now + DELIVERY_TTL, *self._key(item)),
                )
            prior = await self.sender._association(tx, *self._key(item))
            if item["state"] != "pending":
                await self._stop(tx, item, "blocked", item["state"], prior)
                return
            same = prior is not None and all(
                prior[key] == item[key] for key in ("epoch", "revision", "digest", "scope")
            )
            if item["deadline_at"] <= now and not (same and prior and prior["stored_at"]):
                await self._stop(tx, item, "expired", "delivery_deadline", prior)
                return
            peer, binding, event, _ = await self.sender._current(tx, *self._key(item))
            same = prior is not None and self.sender._matches(prior, binding)
            retry = False
            if same and prior:
                if prior["stored_at"] is not None:
                    await self._set(tx, item, "stored")
                    return
                work = await tx.read(
                    "SELECT state,completed_at,last_error FROM outbound_work WHERE queue_key=?",
                    (prior["queue_key"],),
                )
                states = {row["state"] for row in work}
                if states & {"cancelled", "retracted", "superseded"}:
                    await self._stop(tx, item, "cancelled", "transport_cancelled", prior)
                    return
                if "expired" in states:
                    await self._stop(tx, item, "expired", "transport_expired", prior)
                    return
                if any(row["last_error"] == "dispatch authorization denied" for row in work):
                    await self._stop(tx, item, "blocked", "dispatch_policy_denied", prior)
                    return
                if states & {"pending", "held", "sending"}:
                    await self._set(tx, item, "queued")
                    return
                if not work:
                    await self._stop(tx, item, "blocked", "transport_history_missing", prior)
                    return
                attempts = max(1, item["application_attempts"])
                completed = max(row["completed_at"] or now for row in work)
                due = int(completed) + RECEIPT_BACKOFF[min(attempts, MAX_APPLICATION_ATTEMPTS) - 1]
                if now < due:
                    await self._set(
                        tx, item, "awaiting_receipt", "storage_receipt_missing", due=due
                    )
                    return
                if attempts >= MAX_APPLICATION_ATTEMPTS:
                    await self._stop(tx, item, "retry_exhausted", "storage_receipt_missing", prior)
                    return
                retry = True
            active = await tx.read(
                "SELECT d.queue_key,i.lane FROM fed_incident_dispatch d "
                "JOIN fed_incident_intent i USING(peer_id,stream,uid) WHERE d.peer_id=? "
                "AND d.queue_key<>? AND EXISTS(SELECT 1 FROM outbound_work w "
                "WHERE w.queue_key=d.queue_key AND w.state IN ('pending','held','sending')) "
                "LIMIT ?",
                (peer.id, prior["queue_key"] if prior else "", MAX_ACTIVE_PER_PEER),
            )
            if len(active) >= MAX_ACTIVE_PER_PEER or any(
                row["lane"] == item["lane"] for row in active
            ):
                await self._set(tx, item, "waiting", "peer_queue_full")
                return
            if item["application_attempts"] >= MAX_APPLICATION_ATTEMPTS:
                await self._stop(tx, item, "retry_exhausted", "application_attempt_limit", prior)
                return
            if self._now() >= item["deadline_at"]:
                await self._stop(tx, item, "expired", "delivery_deadline", prior)
                return
            priority = {"critical": -30, "urgent": -20}.get(event["payload"].get("severity"), -10)
            result = await self.sender.admit(
                *self._key(item),
                retry=retry,
                transaction=tx,
                priority=priority if item["lane"] == "fresh" else 0,
            )
            if result.state == "queued":
                await tx.write(
                    "UPDATE fed_incident_intent SET application_attempts=application_attempts+1 "
                    "WHERE peer_id=? AND stream=? AND uid=?",
                    self._key(item),
                )
            if self._now() >= item["deadline_at"]:
                current = await self.sender._association(tx, *self._key(item))
                await self._stop(tx, item, "expired", "delivery_deadline", current)
                return
            await self._set(tx, item, result.state)

    @staticmethod
    def action_token(item: dict[str, Any], queue_key: str | None = None) -> str:
        # Optimistic confirmation of the displayed version/policy, not an auth secret.
        fields = (
            "peer_id",
            "stream",
            "uid",
            "epoch",
            "revision",
            "digest",
            "scope",
            "scheduled_at",
            "application_attempts",
            "delivery_state",
        )
        return hashlib.sha256(
            json.dumps([*[item[key] for key in fields], queue_key]).encode()
        ).hexdigest()

    async def action(
        self,
        peer_id: int,
        stream: str,
        uid: str,
        action: str,
        token: str,
        actor: str,
    ) -> None:
        if action not in {"cancel", "retry"}:
            raise ValueError("unknown incident delivery action")
        async with self.database.transaction() as tx:
            rows = await tx.read(
                "SELECT * FROM fed_incident_intent WHERE peer_id=? AND stream=? AND uid=?",
                (peer_id, stream, uid),
            )
            prior = await self.sender._association(tx, peer_id, stream, uid)
            if not rows or not hmac.compare_digest(
                self.action_token(dict(rows[0]), prior["queue_key"] if prior else None), token
            ):
                raise ValueError("incident delivery changed; refresh before acting")
            item = dict(rows[0])
            if action == "cancel":
                await self._stop(tx, item, "cancelled", "operator_cancelled", prior)
            else:
                # Re-admission is explicit consent for a new finite retry window.
                # Validate the current source, peer, key and parent first.
                await self.sender._current(tx, peer_id, stream, uid)
                if prior and await tx.read(
                    "SELECT 1 FROM outbound_work WHERE queue_key=? "
                    "AND state IN ('pending','held','sending') LIMIT 1",
                    (prior["queue_key"],),
                ):
                    raise ValueError("cancel or finish active incident frames before retrying")
                now = self._now()
                await tx.write(
                    "UPDATE fed_incident_intent SET scheduled_at=?,deadline_at=?,"
                    "application_attempts=0,delivery_state='pending',delivery_reason=NULL,"
                    "next_attempt_at=0 WHERE peer_id=? AND stream=? AND uid=?",
                    (now, now + DELIVERY_TTL, peer_id, stream, uid),
                )
                result = await self.sender.admit(peer_id, stream, uid, retry=True, transaction=tx)
                if result.state == "queued":
                    await tx.write(
                        "UPDATE fed_incident_intent SET application_attempts=1 "
                        "WHERE peer_id=? AND stream=? AND uid=?",
                        (peer_id, stream, uid),
                    )
                await self._set(tx, item, result.state)
            await write_audit(
                tx,
                actor_kind="operator",
                actor_ref=actor,
                action="federation.incident_delivery." + action,
                target=f"{peer_id}:{stream}:{uid}",
                detail={"revision": item["revision"]},
                created_at=self._now(),
            )
