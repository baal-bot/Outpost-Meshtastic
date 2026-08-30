from __future__ import annotations

import uuid
from dataclasses import dataclass

from outpost.audit import write_audit
from outpost.clock import Clock
from outpost.store import Database
from outpost.transport.governor import AirtimeGovernor, OutboundItem
from outpost.transport.metrics import SAFETY_NOTIFICATION_DELIVERY
from outpost.transport.models import Severity, TrafficClass


@dataclass(frozen=True)
class AudienceDelivery:
    audience: str
    destinations: tuple[str, ...]
    channels: tuple[int, ...]
    item_ids: tuple[int, ...]
    rejection_reason: str | None = None

    @property
    def admitted(self) -> int:
        return len(self.item_ids)

    @property
    def state(self) -> str:
        if self.item_ids:
            return "delivered"
        return "empty_audience" if not self.destinations else "refused"

    @property
    def failure_reason(self) -> str | None:
        if self.item_ids:
            return None
        return "empty_audience" if not self.destinations else self.rejection_reason or "refused"


class AudienceNotifier:
    """Resolve safety audiences and make zero-recipient delivery operator-visible."""

    def __init__(self, database: Database, governor: AirtimeGovernor, clock: Clock) -> None:
        self.database, self.governor, self.clock = database, governor, clock

    async def destinations(
        self, audience: str, *, exclude_mesh_ids: tuple[str, ...] = ()
    ) -> list[str]:
        if audience == "all":
            return ["^all"]
        roles: tuple[str, ...]
        if audience == "responders":
            roles = ("responder", "operator")
        elif audience == "trusted":
            roles = ("trusted", "responder", "operator")
        else:
            raise ValueError(f"Unsupported notification audience: {audience}")
        placeholders = ",".join("?" for _ in roles)
        exclusion = ""
        params: tuple[object, ...] = roles
        if exclude_mesh_ids:
            exclusion = f" AND mesh_id NOT IN ({','.join('?' for _ in exclude_mesh_ids)})"
            params += exclude_mesh_ids
        rows = await self.database.read(
            f"SELECT mesh_id FROM member WHERE trust IN ({placeholders}){exclusion} "  # noqa: S608
            "AND directory_state='active' ORDER BY mesh_id",
            params,
        )
        return [str(row["mesh_id"]) for row in rows]

    async def deliver(
        self,
        *,
        purpose: str,
        target: str,
        audience: str,
        text: str,
        channels: list[int],
        traffic_class: TrafficClass = TrafficClass.ALERT,
        severity: Severity = Severity.URGENT,
        exclude_mesh_ids: tuple[str, ...] = (),
        queue_key: str | None = None,
        supersedes: str | None = None,
        dedupe_token: str | None = None,
    ) -> AudienceDelivery:
        destinations = await self.destinations(audience, exclude_mesh_ids=exclude_mesh_ids)
        items = [
            OutboundItem(
                text=text,
                dest=destination,
                channel=int(channel),
                traffic_class=traffic_class,
                severity=severity,
                want_ack=destination != "^all",
                queue_key=queue_key,
                supersedes=supersedes if index == 0 else None,
                dedupe_token=dedupe_token,
            )
            for index, (destination, channel) in enumerate(
                (destination, channel) for destination in destinations for channel in channels
            )
        ]
        if items:
            admission = await self.governor.admit_many_result(items)
            result = AudienceDelivery(
                audience,
                tuple(destinations),
                tuple(int(channel) for channel in channels),
                admission.item_ids,
                admission.rejection_reason,
            )
        else:
            result = AudienceDelivery(
                audience, tuple(destinations), tuple(int(channel) for channel in channels), ()
            )
        await self._record(purpose, target, result)
        return result

    async def _record(self, purpose: str, target: str, result: AudienceDelivery) -> None:
        outcome = result.failure_reason or "delivered"
        SAFETY_NOTIFICATION_DELIVERY.labels(purpose, result.audience, outcome).inc()
        conversation_key = f"system:delivery:{purpose}:{target}"
        now = int(self.clock.now().timestamp())
        if result.admitted:
            await self.database.write(
                "UPDATE mail SET state='delivered',delivered_at=? "
                "WHERE conversation_key=? AND state='failed'",
                (now, conversation_key),
            )
            return
        detail = {
            "purpose": purpose,
            "audience": result.audience,
            "reason": outcome,
            "destinations": len(result.destinations),
            "channels": list(result.channels),
        }
        await write_audit(
            self.database,
            actor_kind="system",
            actor_ref="delivery",
            action="safety.delivery.zero",
            target=target,
            detail=detail,
            created_at=now,
            outcome="failure",
        )
        existing = await self.database.read(
            "SELECT id FROM mail WHERE conversation_key=? AND state='failed' LIMIT 1",
            (conversation_key,),
        )
        if existing:
            return
        body = (
            f"No recipient was reached for {purpose.replace('_', ' ')} ({target}). "
            f"Audience: {result.audience}. Reason: {outcome}. Operator review required."
        )
        await self.database.write(
            "INSERT INTO mail(uid,from_label,to_label,subject,body,created_at,state,expires_at,"
            "conversation_key,message_kind,mail_direction,participant_handle,operator_actor) "
            "VALUES(?,?,?,?,?,?,'failed',?,?, 'system','local','outpost','system:delivery')",
            (
                str(uuid.uuid4()),
                "outpost",
                "operator",
                "Safety delivery needs review",
                body,
                now,
                now + 30 * 86_400,
                conversation_key,
            ),
        )
