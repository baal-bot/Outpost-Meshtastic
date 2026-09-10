import asyncio
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import pytest

from outpost.clock import VirtualClock
from outpost.config import Config
from outpost.fed import bulk_format as fmt
from outpost.fed.bulk_policy import BulkDenied, BulkLedger, BulkReplay, BulkStorageFull
from outpost.fed.peers import FederationPeerService
from outpost.store import Database, StoreError
from outpost.store.members import MemberRepo

SOURCE, DESTINATION = "!00000001", "!00000002"
PAGE = {"cycle": "a" * 32, "epoch": None, "scope": None, "after": 0, "snapshot": None, "limit": 8}


@dataclass
class Side:
    database: Database
    peers: FederationPeerService
    config: Config
    ledger: BulkLedger


async def side(path: Path, local: str, remote: str) -> Side:
    database = Database(path)
    await database.open()
    peers = FederationPeerService(database, VirtualClock(), local)
    config = Config.model_validate(
        {
            "modules": {"fed": {"enabled": True}},
            "fed": {
                "bulk": {
                    "enabled": True,
                    "listen_address": "127.0.0.1",
                    "test_only_loopback": True,
                    "certificate_file": "/synthetic/local.crt",
                    "private_key_file": "/synthetic/local.key",
                    "trust_root_file": "/synthetic/root.crt",
                    "peers": [
                        {
                            "mesh_id": remote,
                            "address": "127.0.0.1",
                            "certificate_identity": "peer.local",
                            "public_key_sha256": "a" * 64,
                        }
                    ],
                }
            },
        }
    )
    await peers.discover(remote, "Synthetic peer", 1, {}, "radio")
    await database.write(
        "INSERT INTO fed_bundle_identity VALUES(1,?,0,'synthetic operator')", (local,)
    )
    return Side(database, peers, config, BulkLedger(database, peers, config))


async def pair(left: Side, right: Side, *, replace: bool = False) -> None:
    if replace:
        await right.peers.set_state(SOURCE, "pending")
    _, request = await left.peers.create_pairing_request(DESTINATION, replace=replace)
    _, acknowledgement, code = await right.peers.accept_pairing_request(
        SOURCE, request["public_key"], request["nonce"]
    )
    _, local_code = await left.peers.accept_pairing_ack(
        DESTINATION, acknowledgement["public_key"], acknowledgement["nonce"]
    )
    assert code == local_code
    await left.peers.approve_local(DESTINATION, "synthetic operator", code)
    await right.peers.approve_local(SOURCE, "synthetic operator", code)
    await left.peers.confirm_remote(DESTINATION)
    await right.peers.confirm_remote(SOURCE)


@pytest.fixture
async def stations(tmp_path):
    left = await side(tmp_path / "left.db", SOURCE, DESTINATION)
    right = await side(tmp_path / "right.db", DESTINATION, SOURCE)
    try:
        await pair(left, right)
        yield left, right
    finally:
        await left.database.close()
        await right.database.close()


async def response_for(right: Side, raw: bytes) -> bytes:
    secret = await right.peers.secret(SOURCE)
    request = fmt.decode_request(raw, secret, SOURCE, DESTINATION)
    return fmt.encode_response(
        secret,
        request,
        {
            "status": "ok",
            "result": {
                "cycle": request.payload["cycle"],
                "epoch": "b" * 32,
                "scope": "c" * 16,
                "after": 0,
                "snapshot": 0,
                "next": 0,
                "done": True,
                "items": [],
            },
        },
    )


async def prepare(left: Side) -> bytes:
    async with left.database.transaction() as tx:
        return await left.ledger.prepare(tx, DESTINATION, "manifest", PAGE)


async def restart(station: Side) -> None:
    path = station.database.path
    await station.database.close()
    station.database = Database(path)
    await station.database.open()
    station.peers = FederationPeerService(
        station.database, VirtualClock(), station.peers.local_mesh_id
    )
    station.ledger = BulkLedger(station.database, station.peers, station.config)


async def receive(right: Side, raw: bytes) -> bytes:
    response = await response_for(right, raw)
    async with right.database.transaction() as tx:
        request, cached = await right.ledger.inspect_request(tx, SOURCE, raw)
        assert request.raw == raw and cached is None
        await right.ledger.record_response(tx, SOURCE, raw, response)
    return response


@pytest.mark.asyncio
async def test_ip_counter_two_leaves_delayed_radio_counter_one_valid(stations):
    left, right = stations
    for sequence in (1, 2):
        raw = await prepare(left)
        async with left.database.transaction() as tx:
            assert await left.ledger.reserve_attempt(tx, DESTINATION) == raw
        response = await receive(right, raw)
        async with left.database.transaction() as tx:
            assert (
                await left.ledger.accept_response(tx, DESTINATION, response)
            ).sequence == sequence
    await restart(left)
    await restart(right)
    assert await left.peers.next_counter(DESTINATION) == 1
    assert await right.peers.accept_counter(SOURCE, 1)
    assert not await right.peers.accept_counter(SOURCE, 1)
    assert (await left.database.read("SELECT tx_sequence FROM fed_bulk_state"))[0][0] == 2
    assert (await right.database.read("SELECT rx_sequence FROM fed_bulk_state"))[0][0] == 2
    assert not await left.database.read("SELECT 1 FROM outbound_work")
    assert not await right.database.read("SELECT 1 FROM outbound_work")


@pytest.mark.asyncio
async def test_lost_response_retries_exact_bytes_after_both_restarts(stations):
    left, right = stations
    raw = await prepare(left)
    response = await receive(right, raw)
    await restart(left)
    await restart(right)
    assert await prepare(left) == raw
    async with right.database.transaction() as tx:
        _, cached = await right.ledger.inspect_request(tx, SOURCE, raw)
        assert cached == response
        assert await right.ledger.record_response(tx, SOURCE, raw, response) == response
    async with left.database.transaction() as tx:
        await left.ledger.accept_response(tx, DESTINATION, cached)
    next_raw = await prepare(left)
    assert next_raw != raw
    await receive(right, next_raw)
    with pytest.raises(BulkReplay):
        async with right.database.transaction() as tx:
            await right.ledger.inspect_request(tx, SOURCE, raw)
    rows = await right.database.read("SELECT rx_sequence,rx_response FROM fed_bulk_state")
    assert len(rows) == 1 and rows[0]["rx_sequence"] == 2


@pytest.mark.asyncio
async def test_changed_request_and_sequence_gap_do_not_consume_receive_state(stations):
    left, right = stations
    secret = await left.peers.secret(DESTINATION)
    raw = await prepare(left)
    await receive(right, raw)
    for invalid in (
        fmt.encode_request(secret, SOURCE, DESTINATION, 1, "manifest", {**PAGE, "limit": 1}),
        fmt.encode_request(secret, SOURCE, DESTINATION, 3, "manifest", PAGE),
    ):
        with pytest.raises(BulkReplay):
            async with right.database.transaction() as tx:
                await right.ledger.inspect_request(tx, SOURCE, invalid)
    assert (await right.database.read("SELECT rx_sequence FROM fed_bulk_state"))[0][0] == 1
    with pytest.raises(BulkDenied, match="pending"):
        async with left.database.transaction() as tx:
            await left.ledger.prepare(tx, DESTINATION, "manifest", {**PAGE, "limit": 1})


@pytest.mark.asyncio
async def test_retry_allowance_survives_restart_and_cannot_be_replenished_by_prepare(stations):
    left, _ = stations
    raw = await prepare(left)
    for attempt in range(5):
        async with left.database.transaction() as tx:
            assert await left.ledger.reserve_attempt(tx, DESTINATION) == raw
        if attempt == 1:
            await restart(left)
    assert await prepare(left) == raw
    await restart(left)
    with pytest.raises(BulkDenied, match="paused"):
        async with left.database.transaction() as tx:
            await left.ledger.reserve_attempt(tx, DESTINATION)
    row = (
        await left.database.read("SELECT tx_sequence,tx_attempts,tx_request FROM fed_bulk_state")
    )[0]
    assert tuple(row) == (1, 5, raw)
    assert not await left.database.read("SELECT 1 FROM outbound_work")


@pytest.mark.asyncio
async def test_abort_rolls_back_joint_effect_response_replay_and_sender_completion(stations):
    left, right = stations
    await right.database.write("CREATE TABLE synthetic_effect(id INTEGER PRIMARY KEY, value TEXT)")
    raw = await prepare(left)
    response = await response_for(right, raw)
    with pytest.raises(RuntimeError, match="power cut"):
        async with right.database.transaction() as tx:
            await tx.write("INSERT INTO synthetic_effect VALUES(1,'committed domain witness')")
            await right.ledger.record_response(tx, SOURCE, raw, response)
            raise RuntimeError("power cut before commit")
    await restart(right)
    assert not await right.database.read("SELECT 1 FROM synthetic_effect")
    assert not await right.database.read("SELECT 1 FROM fed_bulk_state")
    await receive(right, raw)
    with pytest.raises(asyncio.CancelledError):
        async with left.database.transaction() as tx:
            await left.ledger.accept_response(tx, DESTINATION, response)
            raise asyncio.CancelledError()
    await restart(left)
    assert await prepare(left) == raw
    async with left.database.transaction() as tx:
        await left.ledger.accept_response(tx, DESTINATION, response)
    with pytest.raises(BulkReplay, match="no pending"):
        async with left.database.transaction() as tx:
            await left.ledger.accept_response(tx, DESTINATION, response)


@pytest.mark.parametrize("status", sorted(fmt.TRANSIENT_STATUSES))
@pytest.mark.asyncio
async def test_transient_responses_preserve_the_operation_and_finite_attempts(stations, status):
    left, right = stations
    raw = await prepare(left)
    secret = await right.peers.secret(SOURCE)
    request = fmt.decode_request(raw, secret, SOURCE, DESTINATION)
    response = fmt.encode_response(secret, request, {"status": status, "result": {}})
    with pytest.raises(BulkDenied, match="transient"):
        async with right.database.transaction() as tx:
            await right.ledger.record_response(tx, SOURCE, raw, response)
    for _ in range(5):
        async with left.database.transaction() as tx:
            await left.ledger.reserve_attempt(tx, DESTINATION)
            result = await left.ledger.accept_response(tx, DESTINATION, response)
            assert result.payload["status"] == status
    assert await prepare(left) == raw
    with pytest.raises(BulkDenied, match="paused"):
        async with left.database.transaction() as tx:
            await left.ledger.reserve_attempt(tx, DESTINATION)
    assert not await right.database.read("SELECT 1 FROM fed_bulk_state")
    # A delayed success after the last attempt can still settle the pending work.
    stored = await receive(right, raw)
    async with left.database.transaction() as tx:
        await left.ledger.accept_response(tx, DESTINATION, stored)


@pytest.mark.asyncio
async def test_repair_revokes_old_work_and_generation_without_reusing_old_authority(stations):
    left, right = stations
    old_request = await prepare(left)
    old_response = await receive(right, old_request)
    old_secret = await left.peers.secret(DESTINATION)
    await pair(left, right, replace=True)
    assert not await left.database.read("SELECT 1 FROM fed_bulk_state")
    assert not await right.database.read("SELECT 1 FROM fed_bulk_state")
    assert old_secret != await left.peers.secret(DESTINATION)
    with pytest.raises(ValueError, match="generation"):
        async with right.database.transaction() as tx:
            await right.ledger.inspect_request(tx, SOURCE, old_request)
    new_request = await prepare(left)
    with pytest.raises(ValueError, match="generation"):
        async with left.database.transaction() as tx:
            await left.ledger.accept_response(tx, DESTINATION, old_response)
    assert new_request != old_request
    assert (await left.database.read("SELECT tx_sequence FROM fed_bulk_state"))[0][0] == 1


@pytest.mark.parametrize(
    "reason",
    [
        "disabled",
        "module_disabled",
        "endpoint_removed",
        "unpaired",
        "unapproved",
        "fenced",
        "identity_changed",
        "uncommissioned",
    ],
)
@pytest.mark.asyncio
async def test_every_attempt_rechecks_current_authority(stations, reason):
    left, _ = stations
    await prepare(left)
    if reason == "disabled":
        left.config.fed.bulk.enabled = False
    elif reason == "module_disabled":
        left.config.modules.fed.enabled = False
    elif reason == "endpoint_removed":
        left.config.fed.bulk.peers.clear()
    elif reason == "unpaired":
        await left.peers.set_state(DESTINATION, "pending")
    elif reason == "unapproved":
        await left.database.write("UPDATE fed_peer SET remote_approved=0")
    elif reason == "fenced":
        await left.database.write(
            "INSERT INTO recovery_fence VALUES(1,?,0,'review_required')", ("f" * 64,)
        )
    elif reason == "identity_changed":
        left.peers.local_mesh_id = "!00000003"
    else:
        await left.database.write("DELETE FROM fed_bundle_identity")
    with pytest.raises(BulkDenied):
        async with left.database.transaction() as tx:
            await left.ledger.reserve_attempt(tx, DESTINATION)
    if reason == "unpaired":
        assert not await left.database.read("SELECT 1 FROM fed_bulk_state")


@pytest.mark.asyncio
async def test_concurrent_prepare_is_single_flight_and_transactions_have_one_owner(stations):
    left, right = stations
    requests = await asyncio.gather(*(prepare(left) for _ in range(8)))
    assert len(set(requests)) == 1
    assert (await left.database.read("SELECT tx_sequence FROM fed_bulk_state"))[0][0] == 1
    async with right.database.transaction() as wrong:
        with pytest.raises(StoreError, match="different database"):
            await left.ledger.prepare(wrong, DESTINATION, "manifest", PAGE)


@pytest.mark.parametrize("hold", ["paused", "unapproved"])
@pytest.mark.asyncio
async def test_temporary_trust_hold_keeps_same_key_replay_history(stations, hold):
    left, right = stations
    raw = await prepare(left)
    response = await receive(right, raw)
    if hold == "paused":
        await right.peers.set_state(SOURCE, "paused")
    else:
        await right.database.write("UPDATE fed_peer SET remote_approved=0")
    with pytest.raises(BulkDenied):
        async with right.database.transaction() as tx:
            await right.ledger.inspect_request(tx, SOURCE, raw)
    assert (await right.database.read("SELECT rx_sequence FROM fed_bulk_state"))[0][0] == 1
    # Exercise any future same-key resumption. Only replacing the key may reset replay state.
    await right.database.write("UPDATE fed_peer SET state='active',remote_approved=1")
    await restart(right)
    async with right.database.transaction() as tx:
        _, cached = await right.ledger.inspect_request(tx, SOURCE, raw)
        assert cached == response


@pytest.mark.parametrize("limit", ["rows", "bytes"])
@pytest.mark.asyncio
async def test_full_ledger_refuses_new_work_without_evicting_replay_state(stations, limit):
    left, _ = stations
    async with left.database.transaction() as tx:
        for index in range(32 if limit == "rows" else 22):
            peer_id = await tx.write(
                "INSERT INTO fed_peer(mesh_id,state,created_at) VALUES(?,'pending',0)",
                (f"!000001{index:02x}",),
            )
            if limit == "rows":
                await tx.write(
                    "INSERT INTO fed_bulk_state(peer_id,local_mesh_id,generation,"
                    "configuration_digest) "
                    "VALUES(?,?,?,?)",
                    (peer_id, SOURCE, "a" * 64, "c" * 64),
                )
            else:
                size = fmt.MAX_MESSAGE_BYTES if index < 21 else 131071
                await tx.write(
                    "INSERT INTO fed_bulk_state(peer_id,local_mesh_id,generation,"
                    "configuration_digest,"
                    "tx_sequence,tx_request,rx_sequence,rx_request_digest,rx_response) "
                    "VALUES(?,?,?,?,1,?,1,?,?)",
                    (
                        peer_id,
                        SOURCE,
                        "a" * 64,
                        "c" * 64,
                        b"x" * size,
                        "b" * 64,
                        b"y" * fmt.MAX_MESSAGE_BYTES if index < 21 else b"y",
                    ),
                )
    before = [
        tuple(row)
        for row in await left.database.read("SELECT * FROM fed_bulk_state ORDER BY peer_id")
    ]
    with pytest.raises(BulkStorageFull):
        await prepare(left)
    assert [
        tuple(row)
        for row in await left.database.read("SELECT * FROM fed_bulk_state ORDER BY peer_id")
    ] == before
    assert not await left.database.read("SELECT 1 FROM outbound_work")


@pytest.mark.parametrize(
    "field,value",
    [
        ("listen_address", "127.0.0.2"),
        ("listen_port", 8445),
        ("certificate_file", Path("/synthetic/new.crt")),
        ("private_key_file", Path("/synthetic/new.key")),
        ("trust_root_file", Path("/synthetic/new-root.crt")),
        ("test_only_loopback", False),
        ("address", "127.0.0.2"),
        ("port", 8445),
        ("certificate_identity", "new-peer.local"),
        ("public_key_sha256", "b" * 64),
    ],
)
@pytest.mark.asyncio
async def test_endpoint_and_tls_changes_cannot_redirect_pending_work(stations, field, value):
    left, right = stations
    raw = await prepare(left)
    response = await response_for(right, raw)
    original = left.config.fed.bulk.model_copy(deep=True)
    target = (
        left.config.fed.bulk.peers[0]
        if field in {"address", "port", "certificate_identity", "public_key_sha256"}
        else left.config.fed.bulk
    )
    setattr(target, field, value)
    with pytest.raises(BulkDenied):
        async with left.database.transaction() as tx:
            await left.ledger.reserve_attempt(tx, DESTINATION)
    with pytest.raises(BulkDenied):
        async with left.database.transaction() as tx:
            await left.ledger.accept_response(tx, DESTINATION, response)
    left.config.fed.bulk = original
    await restart(left)
    assert await prepare(left) == raw
    assert (await left.database.read("SELECT tx_sequence,tx_attempts FROM fed_bulk_state"))[0][
        :
    ] == (1, 0)


@pytest.mark.asyncio
async def test_forward_migration_preserves_every_existing_table_and_record(stations):
    left, _ = stations
    await MemberRepo(left.database, VirtualClock()).resolve("!12345678")
    await left.peers.next_counter(DESTINATION)
    await left.database.close()
    with closing(sqlite3.connect(left.database.path)) as legacy, legacy:
        legacy.execute("DROP TRIGGER fed_bulk_revoke")
        legacy.execute("DROP TABLE fed_bulk_state")
        legacy.execute("DELETE FROM schema_version WHERE version=187")
        names = [
            row[0]
            for row in legacy.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT IN ('schema_version','sqlite_sequence')"
            )
        ]
        before = {name: legacy.execute(f'SELECT * FROM "{name}"').fetchall() for name in names}  # noqa: S608
    left.database = Database(left.database.path)
    await left.database.open()
    for name, expected in before.items():
        rows = await left.database.read(f'SELECT * FROM "{name}"')  # noqa: S608
        assert [tuple(row) for row in rows] == expected, name
    assert (await left.database.read("SELECT max(version) FROM schema_version"))[0][0] == 187
    assert not await left.database.read("SELECT 1 FROM fed_bulk_state")
    assert (await left.database.read("PRAGMA integrity_check"))[0][0] == "ok"
    assert not await left.database.read("PRAGMA foreign_key_check")
