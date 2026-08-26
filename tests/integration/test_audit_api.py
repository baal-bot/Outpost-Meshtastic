from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from outpost.store import Database
from outpost.web.api import create_web_app


@pytest.mark.asyncio
async def test_audit_api_filters_paginates_and_redacts_copyable_detail(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    records = (
        (
            "web",
            "operator",
            "config.update",
            "node:primary",
            json.dumps(
                {
                    "changed": ["name"],
                    "api_key": "do-not-return",
                    "nested": {"accessToken": "also-secret", "safe": "visible"},
                }
            ),
            "success",
            1_700_000_100,
        ),
        (
            "member",
            "!00000001",
            "bbs.remove",
            "thread:2:post:3",
            "password=hunter2; reason=spam",
            "denied",
            1_700_000_200,
        ),
        (
            "system",
            "maintenance",
            "maintenance.run",
            "database",
            '{"deleted": 4}',
            "failure",
            1_700_000_300,
        ),
    )
    for record in records:
        await database.write(
            "INSERT INTO audit_log(actor_kind,actor_ref,action,target,detail,outcome,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            record,
        )
    client = TestClient(create_web_app(lambda: {"radio": "up"}, database=database))

    result = client.get(
        "/api/v1/audit",
        params={
            "actor": "WEB:OPER",
            "action": "CONFIG",
            "target": "PRIMARY",
            "outcome": "success",
            "from_time": datetime.fromtimestamp(1_700_000_000, UTC).isoformat(),
            "until": datetime.fromtimestamp(1_700_000_150, UTC).isoformat(),
        },
    )

    assert result.status_code == 200
    payload = result.json()
    assert payload["total"] == 1 and payload["next_cursor"] is None
    item = payload["items"][0]
    assert item["outcome"] == "success"
    assert item["detail_format"] == "json"
    assert "do-not-return" not in item["detail"]
    assert "also-secret" not in item["detail"]
    assert item["detail"].count("[REDACTED]") == 2
    assert '"safe": "visible"' in item["detail"]

    first_page = client.get("/api/v1/audit", params={"limit": 2}).json()
    assert first_page["total"] == 3
    assert first_page["next_cursor"] == 2
    assert [item["outcome"] for item in first_page["items"]] == ["failure", "denied"]
    second_page = client.get(
        "/api/v1/audit", params={"limit": 2, "cursor": first_page["next_cursor"]}
    ).json()
    assert len(second_page["items"]) == 1 and second_page["next_cursor"] is None

    denied = client.get("/api/v1/audit", params={"outcome": "denied"}).json()["items"][0]
    assert denied["detail_format"] == "text"
    assert denied["detail"] == "password=[REDACTED]; reason=spam"
    await database.close()
