from __future__ import annotations

import json
import time
from typing import Any

from outpost.audit import write_audit
from outpost.channel_profile import (
    CHANNEL_BINDINGS_SETTING,
    apply_channel_bindings,
)
from outpost.config import Config
from outpost.operator_context import current_actor_ref
from outpost.store import Database

EDITABLE_NODE_FIELDS = {
    "name",
    "short_name",
    "operator_contact",
    "emergency_number",
    "timezone",
    "locale",
    "units",
    "disclaimer",
    "location",
}


class RuntimeSettings:
    def __init__(self, database: Database, config: Config) -> None:
        self.database, self.config = database, config

    async def load(self) -> None:
        rows = await self.database.read("SELECT key,value FROM runtime_setting")
        values = {row["key"]: json.loads(row["value"]) for row in rows}
        node_values = {
            key.removeprefix("node."): value
            for key, value in values.items()
            if key.startswith("node.")
        }
        watch_values = {
            key.removeprefix("watch."): value
            for key, value in values.items()
            if key.startswith("watch.")
        }
        if node_values:
            node_candidate = self.config.node.model_dump()
            node_candidate.update(node_values)
            validated = type(self.config.node).model_validate(node_candidate)
            self.config.node = validated
        if watch_values:
            watch_candidate = self.config.watch.model_dump()
            watch_candidate.update(watch_values)
            self.config.watch = type(self.config.watch).model_validate(watch_candidate)
        channel_bindings = values.get(CHANNEL_BINDINGS_SETTING)
        if channel_bindings is not None:
            if not isinstance(channel_bindings, dict):
                raise ValueError("stored Outpost channel bindings must be a mapping")
            apply_channel_bindings(self.config, channel_bindings)

    def redacted(self) -> dict[str, Any]:
        return {
            "product": "Outpost",
            "node": self.config.node.model_dump(),
            "radio": {
                "transport": self.config.radio.transport,
                "liveness_timeout_s": self.config.radio.liveness_timeout_s,
            },
            "airtime": {
                "budget_percent": self.config.airtime.budget_percent,
                "utilisation_ceiling": self.config.airtime.utilisation_ceiling,
                "quiet_hours": self.config.airtime.quiet_hours.model_dump(),
            },
            "modules": self.config.modules.model_dump(),
            "ai": {
                "enabled": self.config.modules.ai.enabled,
                "provider": self.config.ai.provider,
                "model": self.config.ai.model,
            },
            "watch": {
                "emergency_keywords_enabled": self.config.watch.emergency_keywords_enabled,
                "emergency_keywords": self.config.watch.emergency_keywords,
                "emergency_cooldown_minutes": self.config.watch.emergency_cooldown_minutes,
                "escalation": self.config.watch.escalation.model_dump(),
            },
        }

    async def update_node(self, values: dict[str, Any]) -> dict[str, Any]:
        unknown = set(values) - EDITABLE_NODE_FIELDS
        if unknown:
            raise ValueError(f"unsupported settings: {', '.join(sorted(unknown))}")
        candidate = self.config.node.model_dump()
        candidate.update(values)
        validated = type(self.config.node).model_validate(candidate)
        now = int(time.time())
        for key, value in values.items():
            await self.database.write(
                """
                INSERT INTO runtime_setting(key,value,updated_at) VALUES(?,?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at
                """,
                (f"node.{key}", json.dumps(value), now),
            )
        self.config.node = validated
        await write_audit(
            self.database,
            actor_kind="web",
            actor_ref=current_actor_ref(),
            action="config.update",
            target="node",
            detail=sorted(values),
            created_at=now,
        )
        node = self.redacted()["node"]
        assert isinstance(node, dict)
        return node

    async def bind_outpost_channels(self, bindings: dict[str, int]) -> dict[str, int]:
        candidate = self.config.model_copy(deep=True)
        apply_channel_bindings(candidate, bindings)
        normalized = {str(name).lower(): int(index) for name, index in bindings.items()}
        await self.database.write(
            """
            INSERT INTO runtime_setting(key,value,updated_at) VALUES(?,?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at
            """,
            (CHANNEL_BINDINGS_SETTING, json.dumps(normalized, sort_keys=True), int(time.time())),
        )
        apply_channel_bindings(self.config, normalized)
        return normalized

    async def update_watch(self, values: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "emergency_keywords_enabled",
            "emergency_keywords",
            "emergency_cooldown_minutes",
            "escalation",
        }
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"unsupported watch settings: {', '.join(sorted(unknown))}")
        keywords = values.get("emergency_keywords", self.config.watch.emergency_keywords)
        if not isinstance(keywords, list) or not 1 <= len(keywords) <= 20:
            raise ValueError("Emergency keywords must contain 1-20 entries.")
        normalized = [str(value).strip().lower() for value in keywords]
        if any(not value or len(value) > 32 for value in normalized):
            raise ValueError("Each emergency keyword must be 1-32 characters.")
        values["emergency_keywords"] = list(dict.fromkeys(normalized))
        candidate = self.config.watch.model_dump()
        candidate.update(values)
        validated = type(self.config.watch).model_validate(candidate)
        now = int(time.time())
        for key, value in values.items():
            await self.database.write(
                """
                INSERT INTO runtime_setting(key,value,updated_at) VALUES(?,?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at
                """,
                (f"watch.{key}", json.dumps(value), now),
            )
        self.config.watch = validated
        await write_audit(
            self.database,
            actor_kind="web",
            actor_ref=current_actor_ref(),
            action="config.update",
            target="watch",
            detail=sorted(values),
            created_at=now,
        )
        watch = self.redacted()["watch"]
        assert isinstance(watch, dict)
        return watch
