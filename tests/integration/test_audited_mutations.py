from __future__ import annotations

from typing import Any, cast

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from outpost.ai.store import AIStore
from outpost.clock import VirtualClock
from outpost.config import Config
from outpost.env import CapAlertService, WaypointService
from outpost.store import Database
from outpost.web.api import create_web_app
from outpost.web.settings import RuntimeSettings

AUDIT_SENSITIVE_MUTATIONS = {
    ("POST", "/api/v1/ai/kb"): "ai.kb.create",
    ("PATCH", "/api/v1/ai/kb/{document_id}"): "ai.kb.update",
    ("DELETE", "/api/v1/ai/kb/{document_id}"): "ai.kb.delete",
    ("POST", "/api/v1/ai/interactions/{interaction_id}/promote"): "ai.kb.promote",
    ("POST", "/api/v1/ai/refusal-rules"): "ai.refusal_rule.create",
    ("DELETE", "/api/v1/ai/refusal-rules/{rule_id}"): "ai.refusal_rule.delete",
    ("POST", "/api/v1/environment/waypoints"): "waypoint.create",
    ("PATCH", "/api/v1/environment/waypoints/{waypoint_id}"): "waypoint.update",
    ("DELETE", "/api/v1/environment/waypoints/{waypoint_id}"): "waypoint.delete",
    ("POST", "/api/v1/environment/alerts/{cap_id}/dismiss"): "cap.dismiss",
}


def _is_audit_sensitive(method: str, path: str) -> bool:
    return (
        path.startswith("/api/v1/ai/kb")
        or path.startswith("/api/v1/ai/refusal-rules")
        or path == "/api/v1/ai/interactions/{interaction_id}/promote"
        or path.startswith("/api/v1/environment/waypoints")
        or path == "/api/v1/environment/alerts/{cap_id}/dismiss"
    ) and method in {"POST", "PUT", "PATCH", "DELETE"}


@pytest.mark.asyncio
async def test_sensitive_mutating_routes_are_enumerated_and_emit_audit_rows(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock = VirtualClock()
    ai_store = AIStore(database)
    settings = RuntimeSettings(database, Config())
    app = create_web_app(
        lambda: {"radio": "up"},
        database,
        settings=settings,
        ai_service=cast(Any, object()),
        ai_store=ai_store,
        weather=cast(Any, object()),
        waypoints=WaypointService(database, clock),
        cap_alerts=CapAlertService(database, clock, settings.config.env),
        alerts=cast(Any, object()),
        module_provider=lambda: {
            "bbs": True,
            "ai": True,
            "watch": True,
            "env": True,
            "fed": True,
        },
    )
    registered = {
        (method, route.path)
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods
        if _is_audit_sensitive(method, route.path)
    }
    assert registered == set(AUDIT_SENSITIVE_MUTATIONS)
    client = TestClient(app)
    observed: list[str] = []

    async def verify(response: Any, expected_action: str) -> None:
        assert response.status_code == 200, response.text
        rows = await database.read(
            "SELECT actor_kind,actor_ref,action FROM audit_log ORDER BY id DESC LIMIT 1"
        )
        assert len(rows) == 1
        assert (rows[0]["actor_kind"], rows[0]["actor_ref"], rows[0]["action"]) == (
            "web",
            "operator",
            expected_action,
        )
        observed.append(str(rows[0]["action"]))

    response = client.post(
        "/api/v1/ai/kb", json={"title": "Shelter", "body": "Use the civic center."}
    )
    await verify(response, "ai.kb.create")
    document_id = response.json()["id"]
    await verify(
        client.patch(
            f"/api/v1/ai/kb/{document_id}",
            json={"title": "Shelter", "body": "Use the civic center after 18:00."},
        ),
        "ai.kb.update",
    )
    interaction_id = await database.write(
        "INSERT INTO ai_interaction(channel,question,question_class,provider,model,answer,outcome,"
        "created_at) VALUES(-1,'backup shelter','general','test','test',"
        "'[AI] Use the library. src: kb:shelter','grounded',unixepoch())"
    )
    await verify(
        client.post(
            f"/api/v1/ai/interactions/{interaction_id}/promote",
            json={"title": "Backup shelter"},
        ),
        "ai.kb.promote",
    )
    await verify(client.delete(f"/api/v1/ai/kb/{document_id}"), "ai.kb.delete")

    response = client.post(
        "/api/v1/ai/refusal-rules",
        json={"phrase": "restricted route", "reason": "operator policy"},
    )
    await verify(response, "ai.refusal_rule.create")
    await verify(
        client.delete(f"/api/v1/ai/refusal-rules/{response.json()['id']}"),
        "ai.refusal_rule.delete",
    )

    response = client.post(
        "/api/v1/environment/waypoints",
        json={
            "name": "Water point",
            "latitude": 40.44,
            "longitude": -79.99,
            "category": "water",
            "notes": "North entrance",
        },
    )
    await verify(response, "waypoint.create")
    waypoint_id = response.json()["id"]
    await verify(
        client.patch(
            f"/api/v1/environment/waypoints/{waypoint_id}",
            json={"notes": "South entrance"},
        ),
        "waypoint.update",
    )
    await verify(client.delete(f"/api/v1/environment/waypoints/{waypoint_id}"), "waypoint.delete")

    cap_id = await database.write(
        "INSERT INTO cap_alert(identifier,msg_type,status,event,headline,area_desc,expires_at,"
        "expires_epoch,decision,gate_reasons,raw_json,first_seen_at,updated_at) "
        "VALUES('cap-audit','Alert','Actual','Tornado Warning','Take shelter',"
        "'Allegheny County','2099-01-01T00:00:00Z',4070908800,'accepted','[]','{}',1,1)"
    )
    await verify(client.post(f"/api/v1/environment/alerts/{cap_id}/dismiss"), "cap.dismiss")

    assert set(observed) == set(AUDIT_SENSITIVE_MUTATIONS.values())
    await database.close()
