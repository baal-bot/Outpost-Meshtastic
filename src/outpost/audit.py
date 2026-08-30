from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any, Literal, Protocol


class AuditStore(Protocol):
    async def write(self, sql: str, params: Sequence[Any] = ()) -> int: ...


_SECRET_KEY = re.compile(
    r"password|passphrase|secret|token|api[_-]?key|private[_-]?key|psk|credential|"
    r"authorization|cookie",
    re.IGNORECASE,
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(password|passphrase|secret|token|api[_-]?key|private[_-]?key|psk|credential|"
    r"authorization|cookie)(\s*[:=]\s*)([^,;\s}]+)"
)


def redact_audit_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if _SECRET_KEY.search(str(key)) else redact_audit_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_audit_value(item) for item in value]
    return value


def encode_audit_detail(value: object | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            structured = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return _SECRET_ASSIGNMENT.sub(r"\1\2[REDACTED]", value)
    else:
        structured = value
    return json.dumps(
        redact_audit_value(structured), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def display_audit_detail(value: object) -> tuple[str | None, str | None]:
    encoded = encode_audit_detail(value)
    if encoded is None:
        return None, None
    try:
        structured = json.loads(encoded)
    except (json.JSONDecodeError, TypeError):
        return encoded, "text"
    return json.dumps(structured, indent=2, sort_keys=True, ensure_ascii=False), "json"


async def write_audit(
    store: AuditStore,
    *,
    actor_kind: str,
    actor_ref: str,
    action: str,
    target: str | None,
    detail: object | None = None,
    created_at: int | None = None,
    outcome: Literal["success", "denied", "failure"] = "success",
) -> int:
    return await store.write(
        "INSERT INTO audit_log(actor_kind,actor_ref,action,target,detail,created_at,outcome) "
        "VALUES(?,?,?,?,?,COALESCE(?,unixepoch()),?)",
        (
            actor_kind[:32],
            actor_ref[:160],
            action[:160],
            target[:240] if target is not None else None,
            encode_audit_detail(detail),
            created_at,
            outcome,
        ),
    )
