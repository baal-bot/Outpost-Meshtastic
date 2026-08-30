import sqlite3

import pytest
from fastapi.testclient import TestClient

from outpost.clock import VirtualClock
from outpost.fed import FederationPeerService, FederationRelayService
from outpost.store import Database
from outpost.web.api import create_web_app

A = "!aaaaaaaa"
B = "!bbbbbbbb"
C = "!cccccccc"


async def relay_node(tmp_path, name: str, mesh_id: str):
    database = Database(tmp_path / f"{name}.db")
    await database.open()
    clock = VirtualClock()
    peers = FederationPeerService(database, clock, mesh_id)
    relay = FederationRelayService(database, peers, clock)
    await relay.initialize()
    return database, clock, peers, relay


async def allow_relay(
    database: Database,
    peers: FederationPeerService,
    relay: FederationRelayService,
    mesh_id: str,
    *,
    rate: int = 20,
    paused: bool = False,
) -> None:
    await peers.discover(mesh_id, mesh_id, 1, {}, "radio")
    await database.write("UPDATE fed_peer SET state='active' WHERE mesh_id=?", (mesh_id,))
    await relay.set_policy(
        mesh_id,
        enabled=True,
        paused=paused,
        scopes=["incident", "request"],
        max_stored_items=10,
        max_stored_bytes=16_384,
        rate_per_hour=rate,
        airtime_seconds_per_hour=30,
        actor="operator:test",
    )


@pytest.mark.asyncio
async def test_partition_relay_custody_signature_origin_review_and_receipt(tmp_path) -> None:
    a_db, _, a_peers, a_relay = await relay_node(tmp_path, "a", A)
    b_db, _, b_peers, b_relay = await relay_node(tmp_path, "b", B)
    c_db, _, c_peers, c_relay = await relay_node(tmp_path, "c", C)
    await allow_relay(a_db, a_peers, a_relay, B)
    await allow_relay(b_db, b_peers, b_relay, A)
    await allow_relay(b_db, b_peers, b_relay, C)
    await allow_relay(c_db, c_peers, c_relay, B)

    envelope_id = await a_relay.create(
        C,
        "incident",
        {"origin_uid": "incident-a", "status": "monitoring"},
        idempotency_key="incident-a:20",
        hop_limit=3,
        actor="operator:a",
    )
    assert await a_relay.next_hop(envelope_id) == {"mesh_id": B, "path": "relay"}
    wire = await a_relay.wire(envelope_id)
    await a_relay.reserve_forward(envelope_id, B, 1.0)
    received_id, received_state = await b_relay.accept(A, wire)
    assert (received_id, received_state) == (envelope_id, "queued")
    assert await a_relay.acknowledge(B, envelope_id, received_state) is None

    assert await b_relay.next_hop(envelope_id) == {"mesh_id": C, "path": "direct"}
    relayed_wire = await b_relay.wire(envelope_id)
    assert relayed_wire["route"] == [A, B]
    await b_relay.reserve_forward(envelope_id, C, 1.0)
    received_id, received_state = await c_relay.accept(B, relayed_wire)
    assert (received_id, received_state) == (envelope_id, "quarantined")
    assert (await c_relay.origins())[0]["state"] == "observed"
    assert await b_relay.acknowledge(C, envelope_id, received_state) is None

    await c_relay.review_origin(A, "trusted", "operator:c")
    assert await c_relay.pending_receipts() == [{"envelope_id": envelope_id, "previous_hop": B}]
    duplicate_id, duplicate_state = await c_relay.accept(B, relayed_wire)
    assert (duplicate_id, duplicate_state) == (envelope_id, "delivered")
    previous = await b_relay.acknowledge(C, envelope_id, "delivered")
    assert previous == A
    assert await a_relay.acknowledge(B, envelope_id, "delivered") is None
    assert (await a_relay.queue())[0]["state"] == "delivered"
    assert len(await c_relay.queue()) == 1

    for database in (a_db, b_db, c_db):
        await database.close()


@pytest.mark.asyncio
async def test_direct_delivery_is_preferred_and_policy_pause_forces_relay(tmp_path) -> None:
    database, _, peers, relay = await relay_node(tmp_path, "a", A)
    await allow_relay(database, peers, relay, B)
    await allow_relay(database, peers, relay, C)
    envelope_id = await relay.create(C, "request", {"kind": "status"})

    assert await relay.next_hop(envelope_id) == {"mesh_id": C, "path": "direct"}
    await relay.reserve_forward(envelope_id, C, 1.0)
    assert await relay.recover_stalled(now=int(relay.clock.now().timestamp()) + 301) == 1
    assert (await relay.queue())[0]["state"] == "queued"
    await relay.set_policy(
        C,
        enabled=True,
        paused=True,
        scopes=["request"],
        max_stored_items=10,
        max_stored_bytes=16_384,
        rate_per_hour=20,
        airtime_seconds_per_hour=30,
        actor="operator:test",
    )
    assert await relay.next_hop(envelope_id) == {"mesh_id": B, "path": "relay"}
    assert await relay.expire(now=int(relay.clock.now().timestamp()) + 86_401) == 1
    expired = (await relay.queue())[0]
    assert expired["state"] == "expired" and expired["payload_bytes"] == 0
    await database.close()


@pytest.mark.asyncio
async def test_duplicate_clock_skew_loop_and_signature_attacks_fail_closed(tmp_path) -> None:
    a_db, a_clock, a_peers, a_relay = await relay_node(tmp_path, "a", A)
    b_db, _, b_peers, b_relay = await relay_node(tmp_path, "b", B)
    await allow_relay(a_db, a_peers, a_relay, B)
    await allow_relay(b_db, b_peers, b_relay, A)
    envelope_id = await a_relay.create(B, "incident", {"status": "open"})
    wire = await a_relay.wire(envelope_id)
    assert (await b_relay.accept(A, wire))[1] == "delivered"
    assert (await b_relay.accept(A, wire))[1] == "delivered"
    assert len(await b_relay.queue()) == 1

    tampered = {**wire, "payload": b"different"}
    with pytest.raises(ValueError, match="identity|signature"):
        await b_relay.accept(A, tampered)
    looped = {**wire, "route": [A, B, A]}
    with pytest.raises(ValueError, match="route|loop"):
        await b_relay.accept(A, looped)

    a_clock.advance(301)
    future_id = await a_relay.create(B, "request", {"kind": "future"}, idempotency_key="future")
    with pytest.raises(ValueError, match="timestamps"):
        await b_relay.accept(A, await a_relay.wire(future_id))
    summary = await b_relay.summary()
    assert summary["events"] and summary["counts"] == {"delivered": 1}
    await a_db.close()
    await b_db.close()


@pytest.mark.asyncio
async def test_relay_rejects_non_integer_timestamp_without_overflow(tmp_path) -> None:
    a_db, _, a_peers, a_relay = await relay_node(tmp_path, "a", A)
    b_db, _, b_peers, b_relay = await relay_node(tmp_path, "b", B)
    await allow_relay(a_db, a_peers, a_relay, B)
    await allow_relay(b_db, b_peers, b_relay, A)
    envelope_id = await a_relay.create(B, "incident", {"status": "open"})
    malformed = {**(await a_relay.wire(envelope_id)), "created_at": float("inf")}

    with pytest.raises(ValueError, match="created_at must be an integer"):
        await b_relay.accept(A, malformed)

    assert (await b_relay.summary())["counts"] == {}
    await a_db.close()
    await b_db.close()


@pytest.mark.asyncio
async def test_operator_relay_api_exposes_policy_queue_and_controls(tmp_path) -> None:
    database, _, peers, relay = await relay_node(tmp_path, "api", A)
    await peers.discover(B, "Relay B", 1, {}, "radio")
    await database.write("UPDATE fed_peer SET state='active' WHERE mesh_id=?", (B,))
    client = TestClient(
        create_web_app(
            lambda: {"radio": "up"},
            database=database,
            federation=peers,
            federation_relay=relay,
        )
    )

    policy = client.put(
        f"/api/v1/federation/relay/peers/{B}",
        json={
            "enabled": True,
            "scopes": ["incident", "request"],
            "max_stored_items": 10,
            "max_stored_bytes": 16_384,
            "rate_per_hour": 20,
            "airtime_seconds_per_hour": 30,
        },
    )
    assert policy.status_code == 200 and policy.json()["enabled"] is True
    created = client.post(
        "/api/v1/federation/relay",
        json={"destination": C, "scope": "request", "payload": {"kind": "status"}},
    )
    assert created.status_code == 200
    envelope_id = created.json()["envelope_id"]
    view = client.get("/api/v1/federation/relay").json()
    assert view["queue"][0]["envelope_id"] == envelope_id
    assert view["policies"][0]["scopes"] == ["incident", "request"]

    assert (
        client.patch(
            f"/api/v1/federation/relay/{envelope_id}", json={"action": "pause"}
        ).status_code
        == 200
    )
    assert (
        client.patch(
            f"/api/v1/federation/relay/{envelope_id}", json={"action": "resume"}
        ).status_code
        == 200
    )
    assert (
        client.patch(
            f"/api/v1/federation/relay/{envelope_id}", json={"action": "purge"}
        ).status_code
        == 200
    )
    assert client.get("/api/v1/federation/relay").json()["queue"][0]["state"] == "purged"
    await database.close()


@pytest.mark.asyncio
async def test_rate_quota_pause_purge_and_append_only_audit(tmp_path) -> None:
    a_db, _, a_peers, a_relay = await relay_node(tmp_path, "a", A)
    b_db, _, b_peers, b_relay = await relay_node(tmp_path, "b", B)
    await allow_relay(a_db, a_peers, a_relay, B)
    await allow_relay(b_db, b_peers, b_relay, A, rate=1)
    first = await a_relay.create(C, "incident", {"sequence": 1}, idempotency_key="one")
    second = await a_relay.create(C, "incident", {"sequence": 2}, idempotency_key="two")
    assert (await b_relay.accept(A, await a_relay.wire(first)))[1] == "queued"
    with pytest.raises(ValueError, match="hourly rate"):
        await b_relay.accept(A, await a_relay.wire(second))

    await b_relay.item_action(first, "pause", "operator:b")
    assert (await b_relay.queue())[0]["state"] == "paused"
    await b_relay.item_action(first, "resume", "operator:b")
    await b_relay.item_action(first, "purge", "operator:b")
    item = (await b_relay.queue())[0]
    assert item["state"] == "purged" and item["payload_bytes"] == 0
    event = (await b_db.read("SELECT id FROM fed_relay_event ORDER BY id LIMIT 1"))[0]
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        await b_db.write("DELETE FROM fed_relay_event WHERE id=?", (event["id"],))
    await a_db.close()
    await b_db.close()
