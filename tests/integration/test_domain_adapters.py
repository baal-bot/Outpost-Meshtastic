"""Explicit adapter ownership through actual application services and HTTP middleware."""

import ast
from contextlib import AsyncExitStack
from pathlib import Path

import pytest
from fastapi.routing import APIRoute

import outpost.app as application
from outpost.fed.dispatcher import FederationDispatcher
from outpost.fed.framing import FrameError, MessageType
from outpost.transport.models import InboundMessage
from outpost.web.api import create_web_app
from tests.browser.test_incident_g6 import operator_client
from tests.integration.test_federation_incident_events import envelope, prepare
from tests.integration.test_federation_item_failures import flush_radio, wire
from tests.integration.test_federation_revisions import nodes as nodes
from tests.integration.test_outage_readiness import appliance as appliance

pytestmark = pytest.mark.production_wiring


async def test_transient_dispatch_adapters_share_reassembly_counters_and_receipt_owner(
    nodes, monkeypatch
):
    source, _, target, _, _, event = await prepare(nodes)
    adapters = []

    def build(**dependencies):
        adapter = FederationDispatcher(**dependencies)
        adapters.append(adapter)
        return adapter

    monkeypatch.setattr(application, "FederationDispatcher", build)
    await wire(source, target, MessageType.INCIDENT, envelope(event))
    assert len(adapters) >= 2  # One logical event is reassembled across borrowed adapters.
    assert len({id(adapter) for adapter in adapters}) == len(adapters)
    for adapter in adapters:
        assert adapter.database is target.database
        assert adapter.federation is target.federation
        assert adapter.federation_reassembler is target.federation_reassembler
        assert adapter.federation_sync is target.federation_sync
        assert adapter.incident_receipts is target.incident_receipts
        assert adapter.incident_sender is target.incident_sender
    assert len(await target.database.read("SELECT id FROM fed_inbox_item")) == 1
    assert not await target.database.read("SELECT id FROM incident")
    assert (await target.federation.by_mesh_id("!remote")).rx_counter == 1
    assert await target.database.read("SELECT id FROM outbound_work WHERE state='pending'")
    assert not target.radio.sent  # Governed receipt admission remains distinct from RF.


async def test_route_groups_keep_names_methods_and_request_schemas(appliance):
    expected = {
        "/api/v1/diagnostics/readiness": ({"POST"}, "diagnostic_readiness"),
        "/api/v1/readiness": ({"GET"}, "readiness"),
        "/api/v1/readiness/run": ({"POST"}, "run_readiness"),
        "/api/v1/readiness/observations": ({"POST"}, "readiness_observation"),
        "/api/v1/federation/inbox": ({"GET"}, "federation_inbox"),
        "/api/v1/federation/inbox/{item_id}": ({"PATCH"}, "federation_inbox_reject"),
    }
    routes = [route for route in appliance.web.routes if isinstance(route, APIRoute)]
    for path, contract in expected.items():
        matching = [route for route in routes if route.path == path]
        assert len(matching) == 1
        assert (matching[0].methods, matching[0].name) == contract
    schemas = appliance.web.openapi()["components"]["schemas"]
    observation = schemas["ReadinessObservationBody"]
    assert set(observation["required"]) == {"check", "outcome", "observed_at", "review_token"}
    assert observation["additionalProperties"] is False
    review = schemas["FederationInboxBody"]
    assert set(review["required"]) == {"state", "review_token"}
    assert review["properties"]["reason"]["default"] == "Rejected by operator"


async def test_review_routes_still_use_the_shared_live_module_and_role_middleware(appliance):
    async with AsyncExitStack() as stack:
        client = await operator_client(appliance, stack)
        path = "/api/v1/federation/inbox"
        assert (await client.get(path)).status_code == 200
        appliance.config.modules.fed.enabled = False
        disabled = await client.get(path)
        assert disabled.status_code == 409
        assert disabled.json()["error"]["code"] == "module_disabled"
        appliance.config.modules.fed.enabled = True
        assert (await client.get(path)).status_code == 200
        viewer = await operator_client(appliance, stack, role="viewer", username="viewer")
        assert (await viewer.get(path)).status_code == 403
        assert (await viewer.get("/api/v1/readiness")).status_code == 403
        assert not appliance.radio.sent


def test_optional_domain_routes_are_not_registered_without_their_services():
    web = create_web_app(lambda: {"status": "ok"})
    paths = {route.path for route in web.routes if isinstance(route, APIRoute)}
    assert "/api/v1/readiness" not in paths
    assert "/api/v1/diagnostics/readiness" not in paths
    assert "/api/v1/federation/inbox" not in paths


def test_extracted_adapters_have_no_application_back_reference_or_new_store():
    for name in ("fed/dispatcher.py", "web/routes/readiness.py", "web/routes/federation_review.py"):
        source = Path("src/outpost", name).read_text()
        tree = ast.parse(source)
        assert "outpost.app" not in source and "OutpostApp" not in source
        calls = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert not calls.intersection({"Database", "Reassembler", "AirtimeGovernor", "OutboxStore"})


async def test_pairing_handshake_crosses_dispatch_and_governed_replies_without_duplicate_owners(
    nodes,
):
    source, _, _ = await nodes("remote", capture=False)
    target, _, _ = await nodes(capture=False)
    await source.federation.set_state("!local", "pending")
    await target.federation.set_state("!remote", "pending")
    await source.initiate_federation_pairing("!local")
    await flush_radio(source, target)
    await flush_radio(target, source)
    code = await source.federation.pairing_code("!local")
    assert code == await target.federation.pairing_code("!remote")
    await source.approve_federation_pairing("!local", code)
    await target.approve_federation_pairing("!remote", code)
    await flush_radio(source, target)
    await flush_radio(target, source)
    await flush_radio(source, target)
    assert (await source.federation.by_mesh_id("!local")).state == "active"
    assert (await target.federation.by_mesh_id("!remote")).state == "active"
    assert await source.federation.secret("!local") == await target.federation.secret("!remote")
    assert not await source.database.read("SELECT id FROM incident")
    assert not await target.database.read("SELECT id FROM incident")


async def test_encrypted_mail_and_storage_receipt_keep_the_existing_atomic_state(nodes):
    source, _, _ = await nodes("remote", capture=False)
    target, _, _ = await nodes(capture=False)
    for app in (source, target):
        await app.database.write("UPDATE fed_peer SET relay_mail=1")
    sealed = await source.federation_mail.seal(
        "!local", "operator", "operator", "Coordination", "Synthetic private message"
    )
    await wire(source, target, MessageType.MAIL_RELAY, sealed)
    stored = await target.database.read("SELECT state,body FROM mail")
    assert len(stored) == 1 and stored[0]["body"] == "Synthetic private message"
    assert stored[0]["state"] == "delivered"
    assert (await source.database.read("SELECT state FROM mail"))[0]["state"] == "queued"
    await flush_radio(target, source)
    assert (await source.database.read("SELECT state FROM mail"))[0]["state"] == "delivered"
    assert (await source.database.read("SELECT state FROM fed_mail_delivery"))[0][
        "state"
    ] == "delivered"
    assert not await source.database.read("SELECT id FROM alert")


@pytest.mark.parametrize(
    "kind,value",
    [
        (MessageType.SYNC_REQ, {"before": [1]}),
        (MessageType.ITEM_REQ, {"items": "invalid"}),
        (MessageType.ITEM, {"item": []}),
        (MessageType.ITEM_RECEIPT, {"state": "invalid"}),
        (MessageType.RELAY_PUT, {"envelope": []}),
        (MessageType.RELAY_ACK, {"reason": []}),
        (MessageType.TOPOLOGY_UPDATE, {"topology": []}),
    ],
)
async def test_typed_dispatch_rejects_malformed_values_before_domain_mutation(nodes, kind, value):
    source, _, _ = await nodes("remote", capture=False)
    target, _, _ = await nodes(capture=False)
    await wire(source, target, kind, value)
    assert (await target.federation.by_mesh_id("!remote")).rx_counter == 1
    for table in ("fed_inbox_item", "outbound_work", "mail", "incident", "alert"):
        assert not await target.database.read(f"SELECT 1 FROM {table}")  # noqa: S608 - fixed names


async def test_discovery_preserves_invalid_empty_and_targeted_message_boundaries(nodes):
    target, _, _ = await nodes(capture=False)
    await target._handle_federation_discovery(object())
    message = InboundMessage(55, "!remote", "^all", 0, 260, False, None, b"x", target.clock.now())
    await target.message_log.record_inbound(message)
    await target._handle_federation_discovery(message)
    row = (
        await target.database.read("SELECT outcome,drop_reason FROM message_log WHERE packet_id=55")
    )[0]
    assert row["outcome"] == "rejected" and row["drop_reason"] == "invalid federation frame"
    for index, value in enumerate(
        (
            {"capabilities": []},
            {"capabilities": {}, "target_mesh_id": "!someone_else"},
            {"capabilities": {}},
        )
    ):
        frame = target.federation_codec.encode(
            MessageType.HELLO, {"mesh_id": "!remote", **value}, index, None
        )[0]
        inbound = InboundMessage(
            60 + index, "!remote", "^all", 0, 260, False, None, frame, target.clock.now()
        )
        await target._handle_federation_discovery(inbound)
        pending = await target.database.read("SELECT id FROM outbound_work")
        assert bool(pending) == (index == 2)
    assert not target.radio.sent


@pytest.mark.parametrize("receipt_failure", [False, True])
async def test_signed_relay_dispatch_and_governed_receipt_survive_reply_admission_failure(
    nodes, monkeypatch, receipt_failure
):
    source, _, _ = await nodes("aaaaaaaa", capture=False)
    target, _, _ = await nodes("bbbbbbbb", capture=False)
    for app, remote in ((source, "!bbbbbbbb"), (target, "!aaaaaaaa")):
        # Adapt only the synthetic fixture's initial peer name to valid relay IDs.
        await app.database.write(
            "UPDATE fed_peer SET mesh_id=?,service_permissions='[\"weather\"]'", (remote,)
        )
        await app.federation_relay.set_policy(
            remote,
            enabled=True,
            paused=False,
            scopes=["request"],
            max_stored_items=10,
            max_stored_bytes=16384,
            rate_per_hour=20,
            airtime_seconds_per_hour=30,
            actor="operator:test",
        )
    envelope_id = await source.federation_relay.create(
        "!bbbbbbbb",
        "request",
        {"request_id": "dispatch-request", "service": "weather", "args": {"lat": 40, "lon": -80}},
    )
    payload = {"envelope": await source.federation_relay.wire(envelope_id)}
    await source.federation_relay.reserve_forward(envelope_id, "!bbbbbbbb", 1.0)
    with monkeypatch.context() as patch:
        if receipt_failure:

            async def reject_reply(*args, **kwargs):
                raise FrameError("synthetic receipt queue refusal")

            patch.setattr(target, "_send_relay_receipt", reject_reply)
        await source._send_federation_value("!bbbbbbbb", MessageType.RELAY_PUT, payload)
        await flush_radio(source, target)
    destination = (await target.federation_relay.queue())[0]
    assert destination["state"] == "delivered" and destination["dispatch_status"] == "dispatched"
    requests = await target.database.read("SELECT status FROM fed_service_request")
    assert len(requests) == 1 and requests[0]["status"] == "pending"
    if receipt_failure:
        assert not target.radio.sent
        assert await target.federation_relay.pending_receipts()
        await source._send_federation_value("!bbbbbbbb", MessageType.RELAY_PUT, payload)
        await flush_radio(source, target)
    await flush_radio(target, source)
    assert (await source.federation_relay.queue())[0]["state"] == "delivered"
    assert not await target.federation_relay.pending_receipts()
    assert len(await target.database.read("SELECT request_id FROM fed_service_request")) == 1
