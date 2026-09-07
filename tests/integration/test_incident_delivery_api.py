"""Production-wired operator delivery status/actions and unchanged access boundaries."""

import json

import httpx
import pytest

from tests.integration.test_federation_incident_notes import approve, preview
from tests.integration.test_federation_revisions import nodes as nodes
from tests.integration.test_federation_revisions import report
from tests.integration.test_incident_location import verified_member
from tests.integration.test_incident_worker import intents, pair, pump, stored
from tests.integration.test_safety_commands import inbound
from tests.integration.test_web_access_policy import _session

pytestmark = pytest.mark.production_wiring
PATH = "/api/v1/federation/incident-delivery"


async def authenticate(app, monkeypatch, role="operator"):
    async def session(_token):
        return _session(role=role) if role else None

    monkeypatch.setattr(app.web_auth, "session", session)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app.web), base_url="http://testserver"
    )


@pytest.mark.parametrize("role,expected", [(None, 401), ("viewer", 403), ("operator", 200)])
async def test_delivery_status_requires_existing_operator_access(
    nodes, monkeypatch, role, expected
):
    source, _, _, _ = await pair(nodes)
    await report(source)
    await source._incident_delivery_once()
    async with await authenticate(source, monkeypatch, role) as client:
        response = await client.get(PATH)
        assert response.status_code == expected
        if expected == 200:
            assert response.headers["cache-control"] == "no-store"
            item = response.json()["items"][0]
            assert item["state"] == "queued"
            assert item["remote_storage"] == "not_confirmed"
            assert item["can_cancel"] and not item["can_retry"]
            assert (
                not {"shared_secret", "secret_digest", "digest", "scope", "payload", "lat", "lon"}
                & item.keys()
            )


async def test_cancel_retry_require_csrf_current_version_and_record_real_queue_outcomes(
    nodes, monkeypatch
):
    source, _, _, _ = await pair(nodes)
    await report(source)
    await source._incident_delivery_once()
    async with await authenticate(source, monkeypatch) as client:
        item = (await client.get(PATH)).json()["items"][0]
        body = {key: item[key] for key in ("peer_id", "stream", "uid", "action_token")}
        body["action"] = "cancel"
        original_cancel = dict(body)
        assert (await client.post(PATH, json=body)).status_code == 403
        headers = {"x-csrf-token": "csrf-token"}
        assert (await client.post(PATH, json=body, headers=headers)).status_code == 200
        assert not source.governor.queued_items()
        assert (await client.post(PATH, json=body, headers=headers)).status_code == 409
        item = (await client.get(PATH)).json()["items"][0]
        assert item["state"] == "cancelled" and item["can_retry"]
        body.update(action="retry", action_token=item["action_token"])
        assert (await client.post(PATH, json=body, headers=headers)).status_code == 200
        item = (await client.get(PATH)).json()["items"][0]
        assert item["state"] == "queued" and item["remote_storage"] == "not_confirmed"
        assert item["application_attempts"] == 1
        assert (await client.post(PATH, json=original_cancel, headers=headers)).status_code == 409
        assert {
            row[0]
            for row in await source.database.read(
                "SELECT action FROM audit_log WHERE action LIKE 'federation.incident_delivery.%'"
            )
        } == {
            "federation.incident_delivery.cancel",
            "federation.incident_delivery.retry",
        }


async def test_delivery_pages_are_bounded_and_do_not_expose_keys_or_payloads(nodes, monkeypatch):
    source, _, _, _ = await pair(nodes)
    for index in range(7):
        await report(source, f"road synthetic private detail {index}")
    await source._incident_delivery_once()
    async with await authenticate(source, monkeypatch) as client:
        first = (await client.get(PATH, params={"limit": 3})).json()
        assert len(first["items"]) == 3 and first["next"]
        second = (await client.get(PATH, params={"limit": 3, **first["next"]})).json()
        assert len(second["items"]) == 3
        assert {item["uid"] for item in first["items"]}.isdisjoint(
            item["uid"] for item in second["items"]
        )
        for value in (0, 101, -1):
            assert (await client.get(PATH, params={"limit": value})).status_code == 422
        assert "synthetic private detail" not in json.dumps(first)
        assert not first["storage_receipt_is_human_ack"]


@pytest.mark.parametrize("change", ["source", "key", "policy", "lineage", "checkpoint", "identity"])
async def test_old_storage_observation_never_credits_changed_binding(nodes, change):
    source, _, target, _ = await pair(nodes)
    incident = await report(source)
    assert await pump(source, target, 60, until=lambda: stored(source, incident.uid)) < 60
    source.clock.advance(5)
    await source._incident_delivery_once()
    assert (await intents(source))[0]["delivery_state"] == "stored"
    if change == "source":
        await source.database.write(
            "UPDATE incident SET title='Corrected' WHERE id=?", (incident.id,)
        )
    elif change == "key":
        await source.database.write("UPDATE fed_peer SET shared_secret=?", (b"z" * 32,))
    elif change == "policy":
        await source.database.write("UPDATE fed_peer SET sync_incidents=0")
    elif change == "lineage":
        await source.database.write("UPDATE fed_revision_lineage SET epoch=?", ("a" * 32,))
    elif change == "checkpoint":
        await source.database.write("DELETE FROM fed_incident_handoff")
    else:
        source.radio._local_id = "!changed"
    status = (await source.incident_delivery.status())["items"][0]
    assert status["remote_storage"] == "not_confirmed" and status["stored_at"] is None
    assert status["state"] != "stored"
    assert status["human_review"] == status["responder_notification"] == "not_reported"


async def test_policy_disable_and_invalid_actions_do_not_mutate_queue(nodes, monkeypatch):
    source, _, _, _ = await pair(nodes)
    await report(source)
    await source._incident_delivery_once()
    before = await intents(source)
    item = (await source.incident_delivery.status())["items"][0]
    async with await authenticate(source, monkeypatch) as client:
        body = {key: item[key] for key in ("peer_id", "stream", "uid", "action_token")}
        body["action"] = "retry"
        headers = {"x-csrf-token": "csrf-token"}
        assert (await client.post(PATH, json=body, headers=headers)).status_code == 409
        assert await intents(source) == before
        source.config.modules.fed.enabled = False
        disabled = await client.get(PATH)
        assert disabled.status_code == 409 and disabled.json()["error"]["code"] == "module_disabled"
        assert (await client.post(PATH, json=body, headers=headers)).status_code == 409
        assert await intents(source) == before


async def test_field_report_to_reviewed_map_feed_measures_human_delay_separately(
    nodes, monkeypatch
):
    source, _, target, _ = await pair(nodes)
    for app in (source, target):
        await app.database.write(
            "UPDATE fed_peer SET incident_lat=40,incident_lon=-79,incident_radius_km=25"
        )
    key = b"\x01" * 32
    await verified_member(source, "!00000001", key)
    await source.router.dispatch(
        inbound(135, "REPORT fallen tree 40.0 -79.0", "!00000001", key=key)
    )
    incident = (await source.incidents.list())[0]
    transport_seconds = await pump(source, target, 60, until=lambda: stored(source, incident.uid))
    assert transport_seconds < 60
    async with await authenticate(target, monkeypatch) as client:
        assert (await client.get("/api/v1/incidents")).json()["items"] == []
        human_seconds = 10  # Explicit simulation assumption, not an automatic approval timer.
        source.clock.advance(human_seconds)
        target.clock.advance(human_seconds)
        uid = source.federation_sync.wire_uid(incident.uid)
        await approve(target, await preview(target, uid))
        feed = (await client.get("/api/v1/incidents")).json()["items"]
        assert len(feed) == 1 and feed[0]["uid"] == uid
        assert feed[0]["lat"] == 40 and feed[0]["lon"] == -79
        assert feed[0]["remote"] and feed[0]["origins"]
        assert transport_seconds + human_seconds < 60
        assert not await target.database.read("SELECT * FROM alert")
        print(
            f"\nField REPORT to map feed: {transport_seconds}s transport + "
            f"{human_seconds}s simulated human review; browser refresh/RF not qualified"
        )
