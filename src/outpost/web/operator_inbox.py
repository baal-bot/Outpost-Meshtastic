from __future__ import annotations

import json
import time
from typing import Any, Literal

from outpost.store import Database


class OperatorInboxService:
    """Web-operator-only conversation views over local and federated mail."""

    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _transports(value: object) -> list[str]:
        try:
            parsed = json.loads(str(value or "[]"))
        except json.JSONDecodeError:
            return []
        return [str(item) for item in parsed if str(item) in {"radio", "mqtt"}]

    @staticmethod
    def _matches(rows: list[dict[str, Any]], query: str) -> bool:
        if not query:
            return True
        needle = query.casefold()
        fields = (
            "subject",
            "body",
            "from_label",
            "to_label",
            "participant_handle",
            "operator_actor",
            "node_name",
            "source_peer_mesh_id",
        )
        return any(
            needle in str(row.get(field) or "").casefold() for row in rows for field in fields
        )

    @staticmethod
    def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
        latest = rows[0]
        oldest = rows[-1]
        unread = sum(
            row["operator_read_at"] is None and row["mail_direction"] != "out" for row in rows
        )
        failed = sum(row["state"] in {"failed", "undeliverable"} for row in rows)
        peer_id = latest.get("source_peer_mesh_id")
        subject = next((str(row.get("subject") or "") for row in rows if row.get("subject")), "")
        participant = str(latest.get("participant_handle") or "").removeprefix("@")
        if not participant:
            participant = str(
                latest["from_label"]
                if str(latest["to_label"]).lower() == "operator"
                else latest["to_label"]
            ).removeprefix("@")
        return {
            "conversation_key": latest["conversation_key"],
            "subject": subject or "No subject",
            "message_kind": latest["message_kind"],
            "participant_handle": participant,
            "operator_actor": next(
                (row.get("operator_actor") for row in rows if row.get("operator_actor")), None
            ),
            "route_kind": "federated" if peer_id else "local",
            "peer_mesh_id": peer_id,
            "peer_name": latest.get("node_name"),
            "transports": OperatorInboxService._transports(latest.get("discovery_transports")),
            "latest_from": latest["from_label"],
            "latest_to": latest["to_label"],
            "latest_direction": latest["mail_direction"],
            "latest_state": latest["state"],
            "created_at": int(oldest["created_at"]),
            "updated_at": int(latest["created_at"]),
            "message_count": len(rows),
            "unread_count": unread,
            "failed_count": failed,
            "action_required": bool(unread or failed),
            "archived_at": latest.get("archived_at"),
            "reply_available": bool(
                peer_id
                and latest.get("federation_conversation_id")
                and latest.get("reply_recipient_handle")
            ),
            "reply_address": latest.get("reply_recipient_handle"),
        }

    async def list(
        self,
        *,
        query: str = "",
        status: Literal["all", "unread", "read", "failed"] = "all",
        archive: Literal["active", "archived", "all"] = "active",
        route: Literal["all", "local", "federated"] = "all",
        kind: Literal["all", "member", "system"] = "all",
        limit: int = 100,
    ) -> dict[str, Any]:
        rows = await self.database.read(
            "SELECT m.id,m.conversation_key,m.federation_conversation_id,m.from_label,"
            "m.to_label,m.subject,m.body,m.created_at,m.delivered_at,m.operator_read_at,"
            "m.archived_at,m.state,m.message_kind,m.mail_direction,m.source_peer_mesh_id,"
            "m.reply_recipient_handle,m.participant_handle,m.operator_actor,p.node_name,"
            "p.discovery_transports FROM mail m LEFT JOIN fed_peer p "
            "ON p.mesh_id=m.source_peer_mesh_id WHERE m.conversation_key IS NOT NULL "
            "AND (?='all' OR (?='active' AND m.archived_at IS NULL) OR "
            "(?='archived' AND m.archived_at IS NOT NULL)) "
            "AND (?='all' OR (?='local' AND m.source_peer_mesh_id IS NULL) OR "
            "(?='federated' AND m.source_peer_mesh_id IS NOT NULL)) "
            "AND (?='all' OR m.message_kind=?) "
            "ORDER BY m.created_at DESC,m.id DESC LIMIT 2000",
            (archive, archive, archive, route, route, route, kind, kind),
        )
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            value = dict(row)
            grouped.setdefault(str(value["conversation_key"]), []).append(value)
        summaries = [
            self._summary(messages)
            for messages in grouped.values()
            if self._matches(messages, query.strip())
        ]
        if status == "unread":
            summaries = [item for item in summaries if item["unread_count"]]
        elif status == "read":
            summaries = [item for item in summaries if not item["unread_count"]]
        elif status == "failed":
            summaries = [item for item in summaries if item["failed_count"]]
        summaries.sort(
            key=lambda item: (item["updated_at"], item["conversation_key"]), reverse=True
        )
        visible = summaries[:limit]
        return {
            "items": visible,
            "total": len(summaries),
            "counts": {
                "unread": sum(bool(item["unread_count"]) for item in summaries),
                "actionable": sum(bool(item["action_required"]) for item in summaries),
                "failed": sum(bool(item["failed_count"]) for item in summaries),
            },
        }

    async def open(self, conversation_key: str) -> dict[str, Any] | None:
        rows = await self.database.read(
            "SELECT m.id,m.uid,m.conversation_key,m.federation_conversation_id,m.from_label,"
            "m.to_label,m.subject,m.body,m.created_at,m.delivered_at,m.operator_read_at,"
            "m.archived_at,m.state,m.message_kind,m.mail_direction,m.source_peer_mesh_id,"
            "m.reply_recipient_handle,m.participant_handle,m.operator_actor,p.node_name,"
            "p.discovery_transports FROM mail m LEFT JOIN fed_peer p "
            "ON p.mesh_id=m.source_peer_mesh_id WHERE m.conversation_key=? "
            "ORDER BY m.created_at,m.id",
            (conversation_key,),
        )
        if not rows:
            return None
        values = [dict(row) for row in rows]
        now = int(time.time())
        async with self.database.transaction() as transaction:
            await transaction.write(
                "UPDATE mail SET operator_read_at=? WHERE conversation_key=? "
                "AND mail_direction<>'out'",
                (now, conversation_key),
            )
            await transaction.write(
                "INSERT INTO audit_log(actor_kind,actor_ref,action,target,detail,created_at) "
                "VALUES('web','operator','mail.conversation.view',?,?,?)",
                (
                    f"conversation:{conversation_key}",
                    json.dumps({"message_count": len(values)}, separators=(",", ":")),
                    now,
                ),
            )
        summary = self._summary(list(reversed(values)))
        summary["unread_count"] = 0
        for item in values:
            item["operator_read_at"] = item["operator_read_at"] or (
                now if item["mail_direction"] != "out" else None
            )
            item["transports"] = self._transports(item.pop("discovery_transports"))
        return {"conversation": summary, "messages": values}

    async def set_state(
        self, conversation_key: str, state: Literal["read", "unread", "archive", "active"]
    ) -> bool:
        rows = await self.database.read(
            "SELECT 1 FROM mail WHERE conversation_key=? LIMIT 1", (conversation_key,)
        )
        if not rows:
            return False
        now = int(time.time())
        params: list[object]
        if state == "read":
            assignment, params = "operator_read_at=?", [now]
        elif state == "unread":
            assignment, params = "operator_read_at=NULL", []
        elif state == "archive":
            assignment, params = "archived_at=?", [now]
        else:
            assignment, params = "archived_at=NULL", []
        params.append(conversation_key)
        async with self.database.transaction() as transaction:
            await transaction.write(
                f"UPDATE mail SET {assignment} WHERE conversation_key=? "  # noqa: S608
                "AND (? <> 'unread' OR mail_direction <> 'out')",
                (*params, state),
            )
            await transaction.write(
                "INSERT INTO audit_log(actor_kind,actor_ref,action,target,detail,created_at) "
                "VALUES('web','operator',?,?,NULL,?)",
                (f"mail.conversation.{state}", f"conversation:{conversation_key}", now),
            )
        return True

    async def reply_route(self, conversation_key: str) -> dict[str, str] | None:
        rows = await self.database.read(
            "SELECT source_peer_mesh_id,reply_recipient_handle,federation_conversation_id,"
            "message_kind,participant_handle,subject FROM mail WHERE conversation_key=? "
            "AND source_peer_mesh_id IS NOT NULL AND reply_recipient_handle IS NOT NULL "
            "ORDER BY created_at DESC,id DESC LIMIT 1",
            (conversation_key,),
        )
        if not rows:
            return None
        return {key: str(value or "") for key, value in dict(rows[0]).items()}
