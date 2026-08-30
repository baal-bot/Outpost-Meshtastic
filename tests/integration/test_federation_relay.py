import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from outpost.clock import VirtualClock
from outpost.fed import FederationPeerService, FederationRelayService
from outpost.store import Database
from outpost.web.api import create_web_app
from outpost.web.operator_inbox import OperatorInboxService

A = "!aaaaaaaa"
B = "!bbbbbbbb"
C = "!cccccccc"
D = "!dddddddd"


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


async def claimed_origin_wire(
    relay: FederationRelayService,
    claimed_origin: str,
    destination: str,
    *,
    idempotency_key: str,
) -> dict:
    now = int(relay.clock.now().timestamp())
    payload = relay._payload_bytes("incident", {"status": "forged"})
    core = {
        "origin": claimed_origin,
        "destination": destination,
        "scope": "incident",
        "idempotency_key": idempotency_key,
        "created_at": now,
        "expires_at": now + 3600,
        "hop_limit": 3,
        "payload": payload,
    }
    encoded = relay._core_bytes(core)
    private, public, _, _ = await relay._identity()
    return {
        **core,
        "envelope_id": relay._envelope_id(encoded),
        "origin_public_key": public,
        "origin_signature": private.sign(encoded),
        "route": [claimed_origin, relay.peers.local_mesh_id],
    }


def test_relay_key_recovery_migration_demotes_indirect_observed_pins(tmp_path) -> None:
    connection = sqlite3.connect(tmp_path / "legacy.db")
    connection.executescript(
        """
        CREATE TABLE fed_peer(id INTEGER PRIMARY KEY,mesh_id TEXT NOT NULL);
        CREATE TABLE fed_relay_identity(
          id INTEGER PRIMARY KEY,private_key BLOB,public_key BLOB,created_at INTEGER
        );
        CREATE TABLE fed_relay_envelope(
          envelope_id TEXT PRIMARY KEY,origin_node TEXT NOT NULL
        );
        CREATE TABLE fed_relay_origin_key(
          origin_node TEXT PRIMARY KEY,public_key BLOB NOT NULL,fingerprint TEXT NOT NULL,
          state TEXT NOT NULL,observed_from_peer_id INTEGER,first_seen_at INTEGER NOT NULL,
          reviewed_at INTEGER,reviewed_by TEXT
        );
        """
    )
    connection.executemany("INSERT INTO fed_peer(id,mesh_id) VALUES(?,?)", ((1, B), (2, D)))
    connection.executemany(
        "INSERT INTO fed_relay_origin_key(origin_node,public_key,fingerprint,state,"
        "observed_from_peer_id,first_seen_at) VALUES(?,?,?,'observed',?,1)",
        ((A, bytes(range(32)), "a" * 64, 1), (D, bytes(reversed(range(32))), "d" * 64, 2)),
    )
    migration = (
        Path(__file__).parents[2]
        / "src/outpost/store/migrations/0157_relay_origin_key_recovery.sql"
    ).read_text()

    connection.executescript(migration)

    assert connection.execute(
        "SELECT origin_node FROM fed_relay_origin_key ORDER BY origin_node"
    ).fetchall() == [(D,)]
    assert connection.execute(
        "SELECT origin_node,fingerprint,state FROM fed_relay_origin_candidate"
    ).fetchall() == [(A, "a" * 64, "observed")]
    connection.close()


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
    observed = (await c_relay.origins())[0]
    assert observed["state"] == "unverified" and observed["fingerprint"] is None
    assert observed["candidates"][0]["state"] == "observed"
    assert await b_relay.acknowledge(C, envelope_id, received_state) is None

    await c_relay.review_origin(
        A,
        "replace",
        "operator:c",
        fingerprint=observed["candidates"][0]["fingerprint"],
    )
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
async def test_non_origin_peer_cannot_poison_pin_and_genuine_key_remains_recoverable(
    tmp_path,
) -> None:
    a_db, _, a_peers, a_relay = await relay_node(tmp_path, "a", A)
    b_db, _, b_peers, b_relay = await relay_node(tmp_path, "b", B)
    c_db, _, c_peers, c_relay = await relay_node(tmp_path, "c", C)
    await allow_relay(a_db, a_peers, a_relay, B)
    await allow_relay(b_db, b_peers, b_relay, A)
    await allow_relay(b_db, b_peers, b_relay, C)
    await allow_relay(c_db, c_peers, c_relay, B)

    forged = await claimed_origin_wire(b_relay, A, C, idempotency_key="forged-origin-a")
    assert (await c_relay.accept(B, forged))[1] == "quarantined"
    assert await c_db.read("SELECT * FROM fed_relay_origin_key WHERE origin_node=?", (A,)) == []

    genuine_id = await a_relay.create(
        C, "incident", {"status": "genuine"}, idempotency_key="genuine-origin-a"
    )
    await b_relay.accept(A, await a_relay.wire(genuine_id))
    genuine_wire = await b_relay.wire(genuine_id)
    assert (await c_relay.accept(B, genuine_wire))[1] == "quarantined"
    origin = (await c_relay.origins())[0]
    assert origin["fingerprint"] is None
    assert len(origin["candidates"]) == 2
    genuine_fingerprint = c_relay._fingerprint(genuine_wire["origin_public_key"])
    await c_relay.review_origin(A, "replace", "operator:c", fingerprint=genuine_fingerprint)
    pin = (await c_relay.origins())[0]
    assert pin["fingerprint"] == genuine_fingerprint and pin["state"] == "trusted"
    assert (
        next(item for item in await c_relay.queue() if item["envelope_id"] == genuine_id)["state"]
        == "delivered"
    )

    for database in (a_db, b_db, c_db):
        await database.close()


@pytest.mark.asyncio
async def test_origin_candidates_cannot_bypass_peer_rate_quota(tmp_path) -> None:
    b_db, _, b_peers, b_relay = await relay_node(tmp_path, "b", B)
    c_db, _, c_peers, c_relay = await relay_node(tmp_path, "c", C)
    await allow_relay(b_db, b_peers, b_relay, C)
    await allow_relay(c_db, c_peers, c_relay, B, rate=1)

    assert (
        await c_relay.accept(
            B, await claimed_origin_wire(b_relay, A, C, idempotency_key="candidate-a")
        )
    )[1] == "quarantined"
    with pytest.raises(ValueError, match="hourly rate"):
        await c_relay.accept(
            B, await claimed_origin_wire(b_relay, D, C, idempotency_key="candidate-d")
        )
    candidates = await c_db.read(
        "SELECT origin_node FROM fed_relay_origin_candidate ORDER BY origin_node"
    )
    assert [row["origin_node"] for row in candidates] == [A]
    await b_db.close()
    await c_db.close()


@pytest.mark.asyncio
async def test_identity_regeneration_surfaces_candidate_and_operator_can_replace_pin(
    tmp_path,
) -> None:
    a_db, _, a_peers, a_relay = await relay_node(tmp_path, "a", A)
    b_db, _, b_peers, b_relay = await relay_node(tmp_path, "b", B)
    await allow_relay(a_db, a_peers, a_relay, B)
    await allow_relay(b_db, b_peers, b_relay, A)

    original_id = await a_relay.create(
        B, "incident", {"status": "original"}, idempotency_key="original"
    )
    assert (await b_relay.accept(A, await a_relay.wire(original_id)))[1] == "delivered"
    old_fingerprint = (await b_relay.origins())[0]["fingerprint"]

    await a_db.write("DELETE FROM fed_relay_identity")
    await a_relay.initialize()
    replacement_id = await a_relay.create(
        B, "incident", {"status": "replacement"}, idempotency_key="replacement"
    )
    replacement_wire = await a_relay.wire(replacement_id)
    assert (await b_relay.accept(A, replacement_wire))[1] == "quarantined"
    new_fingerprint = b_relay._fingerprint(replacement_wire["origin_public_key"])
    inbox = await b_db.read(
        "SELECT body FROM mail WHERE conversation_key LIKE 'system:relay-origin-key:%'"
    )
    assert old_fingerprint in inbox[0]["body"]
    assert new_fingerprint in inbox[0]["body"]
    assert A in inbox[0]["body"]
    operator_inbox = await OperatorInboxService(b_db).list(kind="system")
    assert operator_inbox["counts"]["actionable"] == 1
    assert operator_inbox["items"][0]["subject"] == "Federation origin key needs review"

    await b_relay.review_origin(A, "replace", "operator:b", fingerprint=new_fingerprint)
    assert (await b_relay.origins())[0]["fingerprint"] == new_fingerprint
    assert (
        next(item for item in await b_relay.queue() if item["envelope_id"] == replacement_id)[
            "state"
        ]
        == "delivered"
    )
    audit = await b_db.read(
        "SELECT action FROM audit_log WHERE action='federation.relay_origin_review'"
    )
    assert audit
    assert (await OperatorInboxService(b_db).list(kind="system"))["counts"]["actionable"] == 0
    await b_relay.review_origin(A, "forget", "operator:b")
    assert await b_db.read("SELECT 1 FROM fed_relay_origin_key WHERE origin_node=?", (A,)) == []
    await a_db.close()
    await b_db.close()


@pytest.mark.asyncio
async def test_signed_successor_key_rotates_a_trusted_origin_without_quarantine(tmp_path) -> None:
    a_db, _, a_peers, a_relay = await relay_node(tmp_path, "a", A)
    b_db, _, b_peers, b_relay = await relay_node(tmp_path, "b", B)
    await allow_relay(a_db, a_peers, a_relay, B)
    await allow_relay(b_db, b_peers, b_relay, A)

    original_id = await a_relay.create(
        B, "request", {"kind": "before"}, idempotency_key="before-rotation"
    )
    await b_relay.accept(A, await a_relay.wire(original_id))
    old_fingerprint = (await b_relay.origins())[0]["fingerprint"]
    rotated = await a_relay.rotate_identity("operator:a")
    assert rotated["rotation_from_fingerprint"] == old_fingerprint

    successor_id = await a_relay.create(
        B, "request", {"kind": "after"}, idempotency_key="after-rotation"
    )
    successor_wire = await a_relay.wire(successor_id)
    assert {"rotation_from_public_key", "rotation_signature"} <= set(successor_wire)
    tampered = bytes(successor_wire["rotation_signature"])
    with pytest.raises(ValueError, match="rotation proof"):
        await b_relay.accept(
            A,
            {
                **successor_wire,
                "rotation_signature": bytes([tampered[0] ^ 1]) + tampered[1:],
            },
        )
    assert (await b_relay.accept(A, successor_wire))[1] == "delivered"
    assert (await b_relay.origins())[0]["fingerprint"] == rotated["fingerprint"]
    events = await b_db.read(
        "SELECT event_kind FROM fed_relay_event WHERE event_kind='origin_key_rotated'"
    )
    assert events
    await a_db.close()
    await b_db.close()


@pytest.mark.asyncio
async def test_direct_delivery_is_preferred_and_policy_pause_forces_relay(tmp_path) -> None:
    database, _, peers, relay = await relay_node(tmp_path, "a", A)
    await allow_relay(database, peers, relay, B)
    await allow_relay(database, peers, relay, C)
    envelope_id = await relay.create(C, "request", {"kind": "status"})

    assert await relay.next_hop(envelope_id) == {"mesh_id": C, "path": "direct"}
    await relay.reserve_forward(envelope_id, C, 1.0)
    assert await relay.recover_stalled(now=int(relay.clock.now().timestamp()) + 301) == 1
    retried = (await relay.queue())[0]
    assert retried["state"] == "queued"
    assert await relay.next_hop(envelope_id) is None
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
    assert await relay.next_hop(envelope_id, now=retried["next_attempt_at"]) == {
        "mesh_id": B,
        "path": "relay",
    }
    assert await relay.expire(now=int(relay.clock.now().timestamp()) + 86_401) == 1
    expired = (await relay.queue())[0]
    assert expired["state"] == "expired" and expired["payload_bytes"] == 0
    await database.close()


@pytest.mark.asyncio
async def test_outbound_saturation_does_not_starve_inbound_and_defers_to_rollover(
    tmp_path,
) -> None:
    a_db, _, a_peers, a_relay = await relay_node(tmp_path, "a", A)
    b_db, _, b_peers, b_relay = await relay_node(tmp_path, "b", B)
    await allow_relay(a_db, a_peers, a_relay, B, rate=1)
    await allow_relay(b_db, b_peers, b_relay, A, rate=1)

    first = await a_relay.create(C, "incident", {"sequence": 1}, idempotency_key="out-one")
    second = await a_relay.create(C, "incident", {"sequence": 2}, idempotency_key="out-two")
    now = int(a_relay.clock.now().timestamp())
    assert await a_relay.reserve_forward(first, B, 1.0, now=now) is True
    assert await a_relay.reserve_forward(second, B, 1.0, now=now) is False
    deferred = next(item for item in await a_relay.queue() if item["envelope_id"] == second)
    assert deferred["state"] == "queued"
    assert deferred["next_attempt_at"] == now - now % 3600 + 3600
    assert "rate exhausted" in deferred["last_error"]
    assert await a_relay.next_hop(second, now=deferred["next_attempt_at"] - 1) is None

    incoming = await b_relay.create(
        A, "request", {"kind": "status"}, idempotency_key="inbound-survives"
    )
    assert (await a_relay.accept(B, await b_relay.wire(incoming)))[1] == "delivered"
    usage = (await a_db.read("SELECT accepted,forwarded FROM fed_relay_usage"))[0]
    assert (usage["accepted"], usage["forwarded"]) == (1, 1)
    assert await a_relay.reserve_forward(second, B, 1.0, now=deferred["next_attempt_at"]) is True
    await a_db.close()
    await b_db.close()


@pytest.mark.asyncio
async def test_forward_failures_back_off_then_become_operator_visible_terminal_failure(
    tmp_path,
) -> None:
    database, _, peers, relay = await relay_node(tmp_path, "a", A)
    await allow_relay(database, peers, relay, B)
    envelope_id = await relay.create(C, "request", {"kind": "status"})
    stamp = int(relay.clock.now().timestamp())
    delays = []

    for attempt in range(1, 7):
        assert await relay.reserve_forward(envelope_id, B, 1.0, now=stamp) is True
        await relay.mark_failed(envelope_id, "radio delivery failed", now=stamp)
        item = (await relay.queue())[0]
        assert item["attempts"] == attempt
        if attempt < 6:
            assert item["state"] == "queued"
            delays.append(item["next_attempt_at"] - stamp)
            assert await relay.next_hop(envelope_id, now=item["next_attempt_at"] - 1) is None
            stamp = item["next_attempt_at"]
        else:
            assert item["state"] == "rejected"
            assert item["next_attempt_at"] is None
            assert item["history"][0]["event_kind"] == "delivery_failed"

    assert delays == sorted(delays)
    assert (
        len(
            await database.read(
                "SELECT 1 FROM mail WHERE conversation_key=?",
                (f"system:relay-failure:{envelope_id}",),
            )
        )
        == 1
    )
    await database.close()


@pytest.mark.asyncio
async def test_negative_custody_receipt_stops_sender_retries_with_peer_reason(tmp_path) -> None:
    database, _, peers, relay = await relay_node(tmp_path, "a", A)
    await allow_relay(database, peers, relay, B)
    envelope_id = await relay.create(C, "incident", {"status": "open"})
    await relay.reserve_forward(envelope_id, B, 1.0)

    assert (
        await relay.acknowledge(
            B, envelope_id, "rejected", "relay content scope is not allowed for this peer"
        )
        is None
    )
    item = (await relay.queue())[0]
    assert item["state"] == "rejected"
    assert item["last_error"] == "relay content scope is not allowed for this peer"
    assert await relay.next_hop(envelope_id) is None
    assert item["history"][0]["event_kind"] == "delivery_failed"
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
    original_fingerprint = view["identity"]["fingerprint"]
    rotated = client.post("/api/v1/federation/relay/identity/rotate")
    assert rotated.status_code == 200
    assert rotated.json()["rotation_from_fingerprint"] == original_fingerprint
    assert rotated.json()["fingerprint"] != original_fingerprint

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
