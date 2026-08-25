from __future__ import annotations

import re
from typing import Any

from outpost.clock import Clock
from outpost.store import Database
from outpost.transport.governor import AirtimeGovernor, OutboundItem
from outpost.transport.models import TrafficClass

MESH_ID = re.compile(r"^![0-9a-fA-F]{8}$")


class RadioOperations:
    def __init__(self, database: Database, governor: AirtimeGovernor, clock: Clock) -> None:
        self.database, self.governor, self.clock = database, governor, clock

    def queue(self) -> list[dict[str, Any]]:
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
            }
            for item in self.governor.queued_items()
        ]

    async def cancel(self, item_id: int) -> bool:
        cancelled = self.governor.cancel(item_id)
        if cancelled:
            await self._audit("queue.cancel", f"queue:{item_id}", None)
        return cancelled

    async def send(self, text: str, destination: str, channel: int, traffic_class: str) -> int:
        if not text.strip() or len(text.encode()) > 200:
            raise ValueError("Message must be 1-200 UTF-8 bytes.")
        if destination != "^all" and not MESH_ID.fullmatch(destination):
            raise ValueError("Destination must be ^all or an 8-digit mesh node ID.")
        if not 0 <= channel <= 7:
            raise ValueError("Channel must be 0-7.")
        if traffic_class not in {"reply", "bulletin", "alert"}:
            raise ValueError("Traffic class must be reply, bulletin, or alert.")
        item_id = self.governor.enqueue(
            OutboundItem(
                text=text.strip(),
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
            "budget_percent": self.governor.budget_percent,
            "reserve_percent": self.governor.reserve_percent,
            "by_class_seconds": self.governor.airtime_breakdown(),
        }

    async def _audit(self, action: str, target: str, detail: object) -> None:
        import json

        await self.database.write(
            """
            INSERT INTO audit_log(actor_kind,actor_ref,action,target,detail,created_at)
            VALUES('web','operator',?,?,?,?)
            """,
            (action, target, json.dumps(detail), int(self.clock.now().timestamp())),
        )
