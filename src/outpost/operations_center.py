from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from typing import Any, TypedDict

from outpost.clock import Clock
from outpost.radio_operations import RadioOperations
from outpost.store import Database
from outpost.watch import CheckinService
from outpost.watch.incidents import ACTIVE, IncidentService
from outpost.web.operator_inbox import OperatorInboxService

Importer = Callable[[int, str], Awaitable[str]]
ReplySender = Callable[[dict[str, str], str, str], Awaitable[dict[str, object]]]
SNAPSHOT_LIMIT = 60
COORDINATE_PAIR = re.compile(
    r"(?<![\d.])"
    r"[+-]?(?:90(?:\.0+)?|(?:[0-8]?\d)(?:\.\d+)?)"
    r"\s*,\s*"
    r"[+-]?(?:180(?:\.0+)?|(?:1[0-7]\d|[0-9]?\d)(?:\.\d+)?)"
    r"(?![\d.])"
)


class OperationsSummary(TypedDict):
    action: int
    incidents: int
    incident_action: int
    welfare_help: int
    welfare_missing: int
    event_name: str | None
    inbox: int
    federation: int
    failed: int


def _mesh_safe(value: object) -> str:
    """Remove coordinate pairs from metadata before it reaches a mesh response."""

    return COORDINATE_PAIR.sub("[location withheld]", str(value))


class MeshOperationsCenter:
    """Role-filtered, metadata-only operations views for the mesh TUI."""

    def __init__(
        self,
        database: Database,
        clock: Clock,
        incidents: IncidentService,
        checkins: CheckinService,
        radio: RadioOperations,
        *,
        importer: Importer | None = None,
        reply_sender: ReplySender | None = None,
    ) -> None:
        self.database = database
        self.clock = clock
        self.incidents = incidents
        self.checkins = checkins
        self.radio = radio
        self.inbox = OperatorInboxService(database)
        self.importer = importer
        self.reply_sender = reply_sender

    async def summary(self, *, operator: bool) -> OperationsSummary:
        incident_counts = (
            await self.database.read(
                "SELECT COUNT(*) total,"
                "COALESCE(SUM(severity IN ('urgent','critical') OR flagged_for_review=1),0) action "
                "FROM incident WHERE status IN ('open','monitoring') AND merged_into_id IS NULL"
            )
        )[0]
        failed = int(
            (
                await self.database.read(
                    "SELECT COUNT(*) count FROM outbound_work WHERE state='failed'"
                )
            )[0]["count"]
        )
        event = await self.checkins.current_event()
        welfare_help = welfare_missing = 0
        if event is not None:
            welfare = await self.checkins.summary(event.id)
            welfare_help = int(welfare["counts"]["need_help"])
            welfare_missing = int(welfare["counts"]["unaccounted"])
        inbox_actionable = federation_pending = 0
        if operator:
            inbox = await self.inbox.list(limit=SNAPSHOT_LIMIT)
            inbox_actionable = int(inbox["counts"]["actionable"])
            federation_pending = int(
                (
                    await self.database.read(
                        "SELECT COUNT(*) count FROM fed_inbox_item WHERE state='pending'"
                    )
                )[0]["count"]
            )
        action = (
            int(incident_counts["action"])
            + welfare_help
            + welfare_missing
            + failed
            + inbox_actionable
            + federation_pending
        )
        return {
            "action": action,
            "incidents": int(incident_counts["total"]),
            "incident_action": int(incident_counts["action"]),
            "welfare_help": welfare_help,
            "welfare_missing": welfare_missing,
            "event_name": _mesh_safe(event.name) if event is not None else None,
            "inbox": inbox_actionable,
            "federation": federation_pending,
            "failed": failed,
        }

    async def incident_refs(self) -> list[str]:
        rows = await self.database.read(
            "SELECT id FROM incident WHERE status IN ('open','monitoring') "
            "AND merged_into_id IS NULL ORDER BY CASE severity WHEN 'critical' THEN 4 "
            "WHEN 'urgent' THEN 3 WHEN 'caution' THEN 2 ELSE 1 END DESC,updated_at DESC,id DESC "
            "LIMIT ?",
            (SNAPSHOT_LIMIT,),
        )
        return [str(row["id"]) for row in rows]

    async def incident(self, incident_id: int) -> dict[str, Any] | None:
        value = await self.incidents.by_id(incident_id)
        if value is None or value.merged_into_id is not None:
            return None
        return {
            "id": value.id,
            "local_ref": value.local_ref,
            "type": value.type,
            "severity": value.severity,
            "status": value.status,
            "title": _mesh_safe(value.title),
            "confirm_count": value.confirm_count,
            "dispute_count": value.dispute_count,
            "updated_at": value.updated_at,
        }

    async def welfare_refs(self) -> list[str]:
        event = await self.checkins.current_event()
        if event is None:
            return []
        summary = await self.checkins.summary(event.id)
        rank = {"need_help": 0, "unaccounted": 1, "evacuated": 2, "ok": 3}
        items = sorted(
            summary["items"],
            key=lambda item: (
                rank[str(item["status"])],
                str(item["handle"] or item["mesh_id"]).casefold(),
            ),
        )
        return [str(item["id"]) for item in items[:SNAPSHOT_LIMIT]]

    async def welfare(self) -> dict[str, Any] | None:
        event = await self.checkins.current_event()
        if event is None:
            return None
        value = await self.checkins.summary(event.id)
        safe_items = [
            {
                "id": int(item["id"]),
                "mesh_id": str(item["mesh_id"]),
                "handle": _mesh_safe(item["handle"] or ""),
                "status": str(item["status"]),
                "created_at": item["created_at"],
            }
            for item in value["items"]
        ]
        safe_event = event.json()
        safe_event["name"] = _mesh_safe(safe_event["name"])
        return {"event": safe_event, "counts": value["counts"], "items": safe_items}

    async def conversation_refs(self) -> list[str]:
        result = await self.inbox.list(limit=SNAPSHOT_LIMIT)
        return [str(item["conversation_key"]) for item in result["items"]]

    async def conversation(self, key: str) -> dict[str, Any] | None:
        result = await self.inbox.list(archive="all", limit=200)
        value = next(
            (item for item in result["items"] if str(item["conversation_key"]) == key),
            None,
        )
        if value is None:
            return None
        return {
            "conversation_key": str(value["conversation_key"]),
            "participant_handle": _mesh_safe(value["participant_handle"]),
            "subject": _mesh_safe(value["subject"]),
            "route_kind": str(value["route_kind"]),
            "peer_mesh_id": value["peer_mesh_id"],
            "peer_name": (
                _mesh_safe(value["peer_name"]) if value["peer_name"] is not None else None
            ),
            "message_count": int(value["message_count"]),
            "unread_count": int(value["unread_count"]),
            "failed_count": int(value["failed_count"]),
            "archived_at": value["archived_at"],
            "reply_available": bool(value["reply_available"]),
        }

    async def failure_refs(self) -> list[str]:
        rows = await self.database.read(
            "SELECT id FROM outbound_work WHERE state='failed' ORDER BY id DESC LIMIT ?",
            (SNAPSHOT_LIMIT,),
        )
        return [str(row["id"]) for row in rows]

    async def failure(self, item_id: int) -> dict[str, Any] | None:
        value = await self.radio.history_item(item_id)
        if value is None:
            return None
        return {
            "id": int(value["id"]),
            "state": str(value["state"]),
            "destination": str(value["destination"]),
            "channel": int(value["channel"]),
            "traffic_class": str(value["traffic_class"]),
            "attempts": int(value["attempts"]),
            "reason_code": str(value["reason_code"]),
            "outcome_explanation": str(value["outcome_explanation"]),
            "outcome_at": value["outcome_at"],
        }

    async def federation_refs(self) -> list[str]:
        rows = await self.database.read(
            "SELECT id FROM fed_inbox_item WHERE state='pending' "
            "ORDER BY received_at DESC,id DESC LIMIT ?",
            (SNAPSHOT_LIMIT,),
        )
        return [str(row["id"]) for row in rows]

    async def federation_item(self, item_id: int) -> dict[str, Any] | None:
        rows = await self.database.read(
            "SELECT i.id,i.stream,i.uid,i.received_at,p.mesh_id,p.node_name "
            "FROM fed_inbox_item i JOIN fed_peer p ON p.id=i.peer_id "
            "WHERE i.id=? AND i.state='pending'",
            (item_id,),
        )
        if not rows:
            return None
        value = rows[0]
        return {
            "id": int(value["id"]),
            "stream": str(value["stream"]),
            "received_at": value["received_at"],
            "mesh_id": str(value["mesh_id"]),
            "node_name": (
                _mesh_safe(value["node_name"]) if value["node_name"] is not None else None
            ),
        }

    async def _audit(
        self, actor_ref: str, action: str, target: str, detail: object | None = None
    ) -> None:
        encoded = (
            json.dumps(detail, separators=(",", ":"), sort_keys=True)
            if detail is not None
            else None
        )
        await self.database.write(
            "INSERT INTO audit_log(actor_kind,actor_ref,action,target,detail,created_at) "
            "VALUES('mesh',?,?,?,?,?)",
            (actor_ref, action, target, encoded, int(self.clock.now().timestamp())),
        )

    async def resolve_incident(self, incident_id: int, resolution: str, actor_ref: str) -> str:
        value = await self.incidents.by_id(incident_id)
        if value is None:
            raise ValueError("Incident not found.")
        if value.status not in ACTIVE:
            return f"INC {value.local_ref} is already {value.status}; nothing changed."
        updated = await self.incidents.operator_patch(
            incident_id,
            status="resolved",
            severity=None,
            resolution=resolution,
            actor=f"mesh:{actor_ref}",
        )
        await self._audit(
            actor_ref,
            "incident.update",
            f"incident:{incident_id}",
            {"changes": ["status", "resolution"], "status": "resolved"},
        )
        return f"INC {updated.local_ref} resolved; audit recorded."

    async def close_event(self, event_id: int, actor_ref: str) -> str:
        value = await self.checkins.by_id(event_id)
        if value is None:
            raise ValueError("Welfare event not found.")
        if value.closed_at is not None:
            return f'Event "{value.name}" is already closed; nothing changed.'
        closed = await self.checkins.close_event(event_id)
        await self._audit(actor_ref, "event.close", f"event:{event_id}", closed.name)
        return f'Event "{closed.name}" closed; audit recorded.'

    async def archive_conversation(self, key: str, actor_ref: str) -> str:
        value = await self.conversation(key)
        if value is None:
            raise ValueError("Conversation not found.")
        if value["archived_at"] is not None:
            return "Conversation is already archived; nothing changed."
        changed = await self.inbox.set_state(key, "archive", actor_kind="mesh", actor_ref=actor_ref)
        if not changed:
            raise ValueError("Conversation not found.")
        return "Conversation archived; audit recorded."

    async def import_federation_item(self, item_id: int, actor_ref: str) -> str:
        if self.importer is None:
            raise ValueError("Federation import is unavailable.")
        stream = await self.importer(item_id, f"mesh:{actor_ref}")
        await self._audit(
            actor_ref,
            "federation.inbox.import",
            f"federation-inbox:{item_id}",
            {"stream": stream},
        )
        return f"Federation {stream} item imported; audit recorded."

    async def reply(self, key: str, body: str, actor_ref: str) -> str:
        route = await self.inbox.reply_route(key)
        if route is None or self.reply_sender is None:
            raise ValueError("This conversation has no safe reply route.")
        result = await self.reply_sender(route, body, f"mesh:{actor_ref}")
        await self._audit(
            actor_ref,
            "mail.conversation.reply",
            f"conversation:{key}",
            {
                "peer_mesh_id": route["source_peer_mesh_id"],
                "recipient": route["reply_recipient_handle"],
                "result": str(result.get("state") or "queued"),
            },
        )
        return "Reply queued through governed federation delivery; audit recorded."
