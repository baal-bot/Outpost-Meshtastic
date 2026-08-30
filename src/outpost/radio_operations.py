from __future__ import annotations

import re
from typing import Any

from outpost.audit import write_audit
from outpost.clock import Clock
from outpost.operator_context import current_actor_ref
from outpost.radio_power import RadioPowerMonitor
from outpost.store import Database
from outpost.transport.governor import AirtimeGovernor, OutboundItem
from outpost.transport.models import TrafficClass

MESH_ID = re.compile(r"^![0-9a-fA-F]{8}$")
HISTORY_FILTERS = {
    "current": ("pending", "held", "sending", "awaiting_ack", "failed"),
    "active": ("pending", "held", "sending", "awaiting_ack"),
    "failed": ("failed",),
    "expired": ("expired",),
    "terminal": ("sent", "acked", "failed", "expired", "cancelled", "superseded", "retracted"),
    "all": (
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
    ),
}


class RadioOperations:
    def __init__(
        self,
        database: Database,
        governor: AirtimeGovernor,
        clock: Clock,
        retention_days: int = 30,
        power: RadioPowerMonitor | None = None,
    ) -> None:
        self.database, self.governor, self.clock = database, governor, clock
        self.retention_days = retention_days
        self.power_monitor = power

    async def queue(self) -> list[dict[str, Any]]:
        if self.governor.outbox is not None:
            now = self.clock.now().timestamp()
            rows = await self.governor.outbox.list_operator_work()
            return [
                {
                    **{key: value for key, value in row.items() if key != "binary_len"},
                    "byte_len": row["binary_len"] or len(str(row["text"]).encode()),
                    "cancellable": row["state"] in {"pending", "held", "awaiting_ack", "failed"},
                    "stale": row["state"] == "awaiting_ack"
                    and now - float(row["last_attempt_at"] or row["created_at"]) >= 120,
                }
                for row in rows
            ]
        return [
            {
                "id": item.item_id,
                "destination": item.dest,
                "channel": item.channel,
                "traffic_class": item.traffic_class.value,
                "text": item.text,
                "byte_len": item.payload_size,
                "created_at_monotonic": item.created_at,
                "expires_at_monotonic": item.expires_at,
                "state": "pending",
                "attempts": item.attempts,
                "cancellable": True,
                "stale": False,
            }
            for item in self.governor.queued_items()
        ]

    def _explain(self, item: dict[str, Any]) -> dict[str, Any]:
        state = str(item["state"])
        outcome = str(item.get("outcome") or "")
        attempts = int(item.get("attempts") or 0)
        if state == "acked" or outcome == "acked":
            reason_code, explanation = "acked", "Acknowledged by the destination."
        elif outcome in {"naked", "nak"}:
            reason_code, explanation = "radio_nak", "The radio reported a NAK."
        elif outcome == "rejected":
            reason_code = "local_policy_rejection"
            explanation = "Rejected locally by Outpost policy before transmission."
        elif state == "sent" and (outcome == "not_requested" or not item.get("want_ack")):
            reason_code, explanation = "no_ack_requested", "Sent; no ACK was requested."
        elif state == "expired" and item.get("packet_id") is not None and item.get("want_ack"):
            reason_code, explanation = "ack_timeout", "Timed out waiting for an ACK."
        elif state == "expired":
            reason_code, explanation = "expired_before_send", "Expired before it could be sent."
        elif state == "superseded":
            reason_code, explanation = "superseded", "Superseded by newer queued work."
        elif state in {"cancelled", "retracted"}:
            reason_code, explanation = "cancelled", "Cancelled before transmission."
        elif state == "failed":
            reason_code = "retry_exhausted" if attempts > 1 else "transport_failure"
            explanation = (
                "Transport failed after retry exhaustion."
                if attempts > 1
                else "Transport failed before delivery."
            )
        elif state == "awaiting_ack":
            reason_code, explanation = "awaiting_ack", "Sent; waiting for an ACK."
        elif state == "sending":
            reason_code, explanation = "sending", "Transmission is in progress."
        elif state == "held":
            reason_code, explanation = "held", "Committing an admitted queue batch."
        elif state == "pending" and attempts:
            reason_code = "retry_scheduled"
            explanation = "A transport attempt failed; waiting for a bounded retry."
        else:
            reason_code, explanation = "queued", "Waiting for airtime policy."
        binary_len = int(item.get("binary_len") or 0)
        text_len = len(str(item.get("text") or "").encode())
        safe = {
            key: value for key, value in item.items() if key not in {"binary_len", "last_error"}
        }
        safe.update(
            {
                "byte_len": binary_len or text_len,
                "cancellable": state in {"pending", "held", "awaiting_ack", "failed"},
                "stale": state == "awaiting_ack"
                and self.clock.now().timestamp()
                - float(item.get("last_attempt_at") or item.get("created_at") or 0)
                >= 120,
                "reason_code": reason_code,
                "outcome_explanation": explanation,
                "outcome_at": item.get("completed_at") or item.get("last_attempt_at"),
            }
        )
        return safe

    async def history(
        self, state_filter: str = "current", *, limit: int = 25, cursor: int | None = None
    ) -> dict[str, Any]:
        states = HISTORY_FILTERS.get(state_filter)
        if states is None:
            raise ValueError("unknown outbound history filter")
        if self.governor.outbox is None:
            all_items = [self._explain(item) for item in await self.queue()]
            counts = {state: 0 for state in HISTORY_FILTERS["all"]}
            for item in all_items:
                counts[str(item["state"])] += 1
            items = [
                item
                for item in all_items
                if str(item["state"]) in states and (cursor is None or int(item["id"]) < cursor)
            ]
            items.sort(key=lambda item: int(item["id"]), reverse=True)
            return {
                "items": items[:limit],
                "next_cursor": int(items[limit - 1]["id"]) if len(items) > limit else None,
                "total": sum(counts[state] for state in states),
                "counts": counts,
                "retention_days": self.retention_days,
                "filter": state_filter,
            }
        result = await self.governor.outbox.operator_history(
            states=states, limit=limit, before_id=cursor
        )
        result["items"] = [self._explain(item) for item in result["items"]]
        result.update({"retention_days": self.retention_days, "filter": state_filter})
        return result

    async def history_item(self, item_id: int) -> dict[str, Any] | None:
        rows = await self.database.read(
            """
            SELECT id,state,text,destination,channel,traffic_class,severity,want_ack,
                   length(binary_payload) binary_len,created_at,expires_at,attempts,
                   last_attempt_at,next_attempt_at,packet_id,outcome,last_error,completed_at
            FROM outbound_work WHERE id=?
            """,
            (item_id,),
        )
        return self._explain(dict(rows[0])) if rows else None

    async def cancel(self, item_id: int) -> bool:
        cancelled = await self.governor.cancel_work(item_id)
        if cancelled:
            await self._audit("queue.cancel", f"queue:{item_id}", None)
        return cancelled

    @staticmethod
    def _validate_send(text: str, destination: str, channel: int, traffic_class: str) -> str:
        if not text.strip() or len(text.encode()) > 200:
            raise ValueError("Message must be 1-200 UTF-8 bytes.")
        if destination != "^all" and not MESH_ID.fullmatch(destination):
            raise ValueError("Destination must be ^all or an 8-digit mesh node ID.")
        if not 0 <= channel <= 7:
            raise ValueError("Channel must be 0-7.")
        if traffic_class not in {"reply", "bulletin", "alert"}:
            raise ValueError("Traffic class must be reply, bulletin, or alert.")
        return text.strip()

    def estimate(
        self, text: str, destination: str, channel: int, traffic_class: str
    ) -> dict[str, object]:
        message = self._validate_send(text, destination, channel, traffic_class)
        return self.governor.estimate_text(
            message,
            traffic_class=TrafficClass(traffic_class),
        )

    async def send(self, text: str, destination: str, channel: int, traffic_class: str) -> int:
        message = self._validate_send(text, destination, channel, traffic_class)
        item_id = await self.governor.admit(
            OutboundItem(
                text=message,
                dest=destination,
                channel=channel,
                traffic_class=TrafficClass(traffic_class),
                want_ack=destination != "^all",
            )
        )
        if item_id is None:
            raise ValueError("Message was rejected by airtime or queue policy.")
        await self._audit(
            "mesh.send",
            f"queue:{item_id}",
            {"destination": destination, "channel": channel, "class": traffic_class},
        )
        return item_id

    def airtime(self) -> dict[str, Any]:
        return {
            "used_seconds": self.governor.used_airtime,
            "configured_budget_percent": self.governor.config.budget_percent,
            "configured_reserve_percent": self.governor.config.emergency_reserve_percent,
            "budget_percent": self.governor.budget_percent,
            "reserve_percent": self.governor.reserve_percent,
            "region": self.governor.region,
            "regional_ceiling_percent": self.governor.regional_ceiling_percent,
            "reported_preset": self.governor.reported_preset,
            "costing_preset": self.governor.preset,
            "profile_matches": self.governor.reported_preset == self.governor.preset,
            "warnings": list(self.governor.profile_warnings),
            "by_class_seconds": self.governor.airtime_breakdown(),
        }

    async def power(self) -> dict[str, Any]:
        if self.power_monitor is None:
            return {
                "battery_level": None,
                "reported": False,
                "condition": "unavailable",
                "observed_at": None,
                "trend": {"direction": "unavailable", "sample_count": 0},
                "samples": [],
            }
        return await self.power_monitor.history()

    async def _audit(self, action: str, target: str, detail: object) -> None:
        await write_audit(
            self.database,
            actor_kind="web",
            actor_ref=current_actor_ref(),
            action=action,
            target=target,
            detail=detail,
            created_at=int(self.clock.now().timestamp()),
        )
