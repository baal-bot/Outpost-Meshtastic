"""Receiver contract only: temporary databases, authenticated simulated radios."""

import asyncio
import copy
import json
import sqlite3
import sys
from dataclasses import replace

import pytest

from outpost.fed.framing import MessageType, Reassembler
from outpost.fed.incident_events import CAPABILITY, RATE_CURSOR, content_digest
from outpost.fed.sync import FederationSyncService
from outpost.store import Database, Transaction
from outpost.transport.models import InboundMessage
from tests.integration.test_federation_incident_notes import (
    approve,
    exported,
    preview,
    source_note,
)
from tests.integration.test_federation_item_failures import flush_radio, wire
from tests.integration.test_federation_revisions import nodes as nodes
from tests.integration.test_federation_revisions import report

pytestmark = pytest.mark.production_wiring


async def negotiate(app, peer):
    await app.database.write(
        "UPDATE fed_peer SET capabilities=? WHERE id=?",
        (json.dumps({**peer.capabilities, CAPABILITY: 1}), peer.id),
    )
    return await app.federation.by_mesh_id(peer.mesh_id)


async def prepare(nodes, *, note=False):
    source, sp, _ = await nodes("remote", notes=note)
    target, tp, _ = await nodes(notes=note)
    sp, tp = await negotiate(source, sp), await negotiate(target, tp)
    incident = await report(source)
    item = await exported(source, sp, "incidents", incident.uid)
    event = {key: item[key] for key in ("stream", "uid", "epoch", "revision", "payload")}
    return source, sp, target, tp, incident, event


def envelope(event):
    return {"mode": 1, "target_mesh_id": "!local", "event": event}


async def rate(app):
    rows = await app.database.read("SELECT cursor FROM fed_cursor WHERE stream=?", (RATE_CURSOR,))
    return json.loads(rows[0]["cursor"]) if rows else None


async def receipts(app):
    reassembler = Reassembler()
    result = []
    for packet in app.radio.sent:
        fragment = app.federation_codec.decode_fragment(packet.payload, bytes(range(32)))
        assert fragment.msg_type is MessageType.INCIDENT_RECEIPT
        value = reassembler.add(app.radio.local_node_id, fragment)
        if value is not None:
            result.append(value)
    return result


async def test_event_crosses_real_framing_and_governor_without_import_or_human_ack(nodes):
    source, _, target, _, _, event = await prepare(nodes)
    await wire(source, target, MessageType.INCIDENT, envelope(event))
    inbox = (await target.database.read("SELECT * FROM fed_inbox_item"))[0]
    assert inbox["state"] == "pending" and inbox["source_revision"] == event["revision"]
    assert json.loads(inbox["payload_json"]) == event["payload"]
    assert not target.radio.sent  # Admission is not transmission.
    assert not await target.database.read("SELECT * FROM incident")
    assert not await target.database.read("SELECT * FROM incident_update")
    assert not await target.database.read("SELECT * FROM alert")
    await flush_radio(target, source)
    assert await receipts(target) == [
        {
            **{key: event[key] for key in ("stream", "uid", "epoch", "revision")},
            "mode": 1,
            "mesh_id": "!local",
            "target_mesh_id": "!remote",
            "digest": content_digest(event["payload"]),
            "state": "stored",
        }
    ]
    assert not await source.database.read("SELECT * FROM fed_post_delivery")


@pytest.mark.parametrize("review_state", ["pending", "imported", "rejected"])
async def test_lost_receipt_retry_survives_reopen_and_preserves_review(nodes, review_state):
    source, _, target, tp, _, event = await prepare(nodes)
    first = await target.federation_sync.incident_events.receive(tp, event, 100)
    if review_state == "imported":
        await approve(target, await preview(target, event["uid"]))
    elif review_state == "rejected":
        await target.database.write(
            "UPDATE fed_inbox_item SET state='rejected',reviewed_at=101,reviewed_by='test'"
        )
    before = dict((await target.database.read("SELECT * FROM fed_inbox_item"))[0])
    reopened = Database(target.database.path)
    await reopened.open()
    try:
        sync = FederationSyncService(reopened, "!local")
        assert await sync.incident_events.receive(tp, event, 102) == first
        assert dict((await reopened.read("SELECT * FROM fed_inbox_item"))[0]) == before
    finally:
        await reopened.close()
    assert await rate(target) == {"start": 100, "count": 1}
    # Retry with a fresh counter uses the production handler after storage already exists.
    await wire(source, target, MessageType.INCIDENT, envelope(event))
    await flush_radio(target, source)
    assert (await receipts(target))[0]["digest"] == first["digest"]
    assert dict((await target.database.read("SELECT * FROM fed_inbox_item"))[0]) == before


@pytest.mark.parametrize(
    "change",
    ["fields", "stream", "uid", "payload_uid", "payload", "epoch", "revision", "nan", "large"],
)
async def test_malformed_events_never_store_or_ack(nodes, change):
    _, _, target, tp, _, event = await prepare(nodes)
    if change == "fields":
        event["unavailable"] = True
    elif change == "stream":
        event["stream"] = "board:gen"
    elif change == "uid":
        event["uid"] = "!third:stolen"
    elif change == "payload_uid":
        event["payload"]["uid"] = "!third:stolen"
    elif change == "payload":
        event["payload"] = []
    elif change == "epoch":
        event["epoch"] = "broken"
    elif change == "revision":
        event["revision"] = True
    elif change == "nan":
        event["payload"]["lat"] = float("nan")
    else:
        event["payload"]["body"] = "x" * 12_001
    with pytest.raises(ValueError):
        await target.federation_sync.incident_events.receive(tp, event, 100)
    assert not await target.database.read("SELECT * FROM fed_inbox_item")
    assert await rate(target) is None
    assert not await target.database.read("SELECT * FROM outbound_work")


@pytest.mark.parametrize(
    "change", ["cap", "bool_cap", "revision_cap", "revoked", "scope", "watch", "fed", "geo"]
)
async def test_current_policy_not_the_callers_stale_peer_controls_admission(nodes, change):
    _, _, target, tp, _, event = await prepare(nodes)
    if change in {"cap", "bool_cap", "revision_cap"}:
        caps = {**tp.capabilities, CAPABILITY: True if change == "bool_cap" else 0}
        if change == "revision_cap":
            caps = {**tp.capabilities, "reconciliation": 1}
        await target.database.write(
            "UPDATE fed_peer SET capabilities=? WHERE id=?", (json.dumps(caps), tp.id)
        )
    elif change == "revoked":
        await target.database.write("UPDATE fed_peer SET state='paused' WHERE id=?", (tp.id,))
    elif change == "scope":
        await target.database.write("UPDATE fed_peer SET sync_incidents=0 WHERE id=?", (tp.id,))
    elif change in {"watch", "fed"}:
        target.federation_sync.module_enabled = lambda name: name != change
    else:
        event["payload"].update(lat=40, lon=-75)
    with pytest.raises(ValueError, match="policy"):
        await target.federation_sync.incident_events.receive(tp, event, 100)
    assert not await target.database.read("SELECT * FROM fed_inbox_item")
    assert await rate(target) is None


async def test_policy_revocation_while_waiting_for_writer_is_observed(nodes):
    _, _, target, tp, _, event = await prepare(nodes)
    async with target.database.transaction() as tx:
        task = asyncio.create_task(target.federation_sync.incident_events.receive(tp, event, 100))
        await asyncio.sleep(0)
        await tx.write("UPDATE fed_peer SET sync_incidents=0 WHERE id=?", (tp.id,))
    with pytest.raises(ValueError, match="policy"):
        await task
    assert not await target.database.read("SELECT * FROM fed_revision_receipt")


@pytest.mark.parametrize("change", ["stale", "conflict", "lineage", "missing", "mismatch"])
async def test_receipts_never_attest_to_a_different_or_deleted_revision(nodes, change):
    _, _, target, tp, _, event = await prepare(nodes)
    event["revision"] += 1
    await target.federation_sync.incident_events.receive(tp, event, 100)
    if change == "stale":
        event["revision"] -= 1
    elif change == "conflict":
        event["payload"]["title"] = "Changed content at the same revision"
    elif change == "lineage":
        event["epoch"] = "b" * 32
    elif change == "missing":
        await target.database.write("DELETE FROM fed_inbox_item")
    else:
        await target.database.write("UPDATE fed_inbox_item SET source_revision=source_revision+1")
    with pytest.raises(ValueError):
        await target.federation_sync.incident_events.receive(tp, event, 101)
    assert await rate(target) == {"start": 100, "count": 1}


@pytest.mark.parametrize("blocked", [True, False])
async def test_existing_reconciliation_lineage_guard_applies_to_new_event_uids(nodes, blocked):
    _, _, target, tp, _, event = await prepare(nodes)
    state = {
        "epoch": event["epoch"] if blocked else "b" * 32,
        "status": "blocked" if blocked else "complete",
    }
    await target.database.write(
        "INSERT INTO fed_cursor(peer_id,stream,direction,cursor,updated_at) "
        "VALUES(?,'_reconcile','recv',?,1)",
        (tp.id, json.dumps(state)),
    )
    with pytest.raises(ValueError, match="lineage"):
        await target.federation_sync.incident_events.receive(tp, event, 100)
    assert not await target.database.read("SELECT * FROM fed_inbox_item")


async def test_new_revision_storm_quota_is_durable_and_backwards_clock_does_not_refill(nodes):
    _, _, target, tp, _, event = await prepare(nodes)
    await target.database.write("UPDATE fed_peer SET quota_items_per_hour=2 WHERE id=?", (tp.id,))
    service = target.federation_sync.incident_events
    for _ in range(2):
        await service.receive(tp, event, 100)
        event["revision"] += 1
    assert len(await target.database.read("SELECT * FROM fed_inbox_item")) == 1
    for now in (100, 0, 3699):
        with pytest.raises(ValueError, match="quota"):
            await service.receive(tp, event, now)
    reopened = Database(target.database.path)
    await reopened.open()
    try:
        restart = FederationSyncService(reopened, "!local").incident_events
        with pytest.raises(ValueError, match="quota"):
            await restart.receive(tp, event, 101)
        await restart.receive(tp, event, 3700)
    finally:
        await reopened.close()
    assert await rate(target) == {"start": 3700, "count": 1}


@pytest.mark.parametrize("cancel", [True, False])
async def test_failure_after_inbox_write_rolls_back_content_quota_and_receipt(
    nodes, monkeypatch, cancel
):
    _, _, target, tp, _, event = await prepare(nodes)
    reached = asyncio.Event()
    original = Transaction.write

    async def fail(tx, sql, params=()):
        result = await original(tx, sql, params)
        if "INSERT INTO fed_revision_receipt" in sql:
            reached.set()
            if cancel:
                await asyncio.Event().wait()
            raise sqlite3.OperationalError("injected receipt failure")
        return result

    monkeypatch.setattr(Transaction, "write", fail)
    task = asyncio.create_task(target.federation_sync.incident_events.receive(tp, event, 100))
    await asyncio.wait_for(reached.wait(), 5)
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else sqlite3.OperationalError):
        await task
    assert not await target.database.read("SELECT * FROM fed_inbox_item")
    assert not await target.database.read("SELECT * FROM fed_revision_receipt")
    assert await rate(target) is None
    assert not await target.database.read("SELECT * FROM outbound_work")


async def test_failed_reply_admission_leaves_committed_content_for_a_fresh_retry(
    nodes, monkeypatch
):
    source, _, target, _, _, event = await prepare(nodes)
    original = target._send_federation_value

    async def fail(*args, **kwargs):
        assert await target.database.read("SELECT * FROM fed_revision_receipt")
        raise ValueError("injected governor rejection")

    monkeypatch.setattr(target, "_send_federation_value", fail)
    await wire(source, target, MessageType.INCIDENT, envelope(event))
    assert not await target.database.read("SELECT * FROM outbound_work")
    monkeypatch.setattr(target, "_send_federation_value", original)
    await wire(source, target, MessageType.INCIDENT, envelope(event))
    await flush_radio(target, source)
    assert len(await receipts(target)) == 1
    assert (await rate(target))["count"] == 1


@pytest.mark.parametrize(
    "invalid",
    ["target", "absent_target", "mode", "event", "identity", "auth", "replay", "ordinary_item"],
)
async def test_wire_rejections_do_not_open_an_unsolicited_item_bypass(nodes, invalid):
    source, _, target, _, _, event = await prepare(nodes)
    value = {**envelope(event), "mesh_id": "!remote"}
    kind, secret = MessageType.INCIDENT, bytes(range(32))
    if invalid == "target":
        value["target_mesh_id"] = "!third"
    elif invalid == "absent_target":
        del value["target_mesh_id"]
    elif invalid == "mode":
        value["mode"] = True
    elif invalid == "event":
        value["event"] = []
    elif invalid == "identity":
        value["mesh_id"] = "!third"
    elif invalid == "auth":
        secret = b"z" * 32
    elif invalid == "replay":
        await target.federation.accept_counter("!remote", 100)
    else:
        kind, value = MessageType.ITEM, {"mesh_id": "!remote", "item": event}
    frames = source.federation_codec.encode(kind, value, 1, secret)
    for frame in frames:
        await target._handle_federation_discovery(
            InboundMessage(1, "!remote", "^all", 0, 260, False, None, frame, target.clock.now())
        )
    assert not await target.database.read("SELECT * FROM fed_inbox_item")
    assert not await target.database.read("SELECT * FROM outbound_work")


async def test_plain_note_needs_stored_original_parent_and_separate_human_review(nodes):
    source, sp, target, tp, incident, parent = await prepare(nodes, note=True)
    _, item = await source_note(source, sp, incident)
    event = {key: item[key] for key in parent}
    service = target.federation_sync.incident_events
    with pytest.raises(ValueError, match="original parent"):
        await service.receive(tp, event, 100)
    await wire(source, target, MessageType.INCIDENT, envelope(parent))
    await wire(source, target, MessageType.INCIDENT, envelope(event))
    assert len(await target.database.read("SELECT * FROM fed_inbox_item")) == 2
    assert not await target.database.read("SELECT * FROM incident_update")
    with pytest.raises(ValueError, match="parent incident"):
        await approve(target, await preview(target, event["uid"]))
    await approve(target, await preview(target, parent["uid"]))
    await approve(target, await preview(target, event["uid"]))
    assert len(await target.database.read("SELECT * FROM incident_update")) == 1
    assert not await target.database.read("SELECT * FROM alert")


async def test_note_capability_and_parent_geography_are_rechecked(nodes):
    source, sp, target, tp, incident, parent = await prepare(nodes, note=True)
    _, item = await source_note(source, sp, incident)
    event = {key: item[key] for key in parent}
    await target.federation_sync.incident_events.receive(tp, parent, 100)
    await target.database.write(
        "UPDATE fed_peer SET capabilities=? WHERE id=?",
        (json.dumps({CAPABILITY: 1, "reconciliation": 2}), tp.id),
    )
    with pytest.raises(ValueError, match="note capability"):
        await target.federation_sync.incident_events.receive(tp, event, 101)
    await negotiate(target, tp)
    altered = {**parent["payload"], "lat": 40, "lon": -75}
    await target.database.write("UPDATE fed_inbox_item SET payload_json=?", (json.dumps(altered),))
    with pytest.raises(ValueError, match="geographic"):
        await target.federation_sync.incident_events.receive(tp, event, 102)


async def test_missing_or_replaced_peer_and_corrupt_quota_fail_closed(nodes):
    _, _, target, tp, _, event = await prepare(nodes)
    for peer in (replace(tp, id=tp.id + 100), replace(tp, mesh_id="!other")):
        adjusted = copy.deepcopy(event)
        adjusted["uid"] = adjusted["payload"]["uid"] = f"{peer.mesh_id}:sample"
        with pytest.raises(ValueError):
            await target.federation_sync.incident_events.receive(peer, adjusted, 100)
    await target.database.write(
        "INSERT INTO fed_cursor(peer_id,stream,direction,cursor,updated_at) VALUES(?,?,'recv',?,1)",
        (tp.id, RATE_CURSOR, '{"start":true,"count":0}'),
    )
    with pytest.raises(ValueError, match="quota start"):
        await target.federation_sync.incident_events.receive(tp, event, 100)


async def test_legacy_short_digest_collision_cannot_produce_a_false_storage_receipt(
    nodes, monkeypatch
):
    _, _, target, tp, _, event = await prepare(nodes)
    monkeypatch.setattr(target.federation_sync, "_payload_digest", lambda _: "a" * 16)
    await target.federation_sync.incident_events.receive(tp, event, 100)
    event["revision"] += 1
    event["payload"]["title"] = "Different content with injected short digest collision"
    with pytest.raises(ValueError, match="not stored exactly"):
        await target.federation_sync.incident_events.receive(tp, event, 101)
    assert (await rate(target))["count"] == 1
    assert (await target.database.read("SELECT source_revision FROM fed_inbox_item"))[0][
        "source_revision"
    ] == event["revision"] - 1


@pytest.mark.parametrize("phase", ["committed", "uncommitted"])
async def test_killed_receive_process_recovers_only_committed_content_and_quota(nodes, phase):
    _, _, target, tp, _, event = await prepare(nodes)
    script = """
import asyncio, json, sys
from outpost.app import OutpostApp
from outpost.clock import VirtualClock
from outpost.config import Config
from outpost.transport.simulated import SimulatedRadioLink

async def main():
    event = json.loads(sys.stdin.readline())
    clock = VirtualClock()
    config = Config.model_validate({
        "store": {"path": sys.argv[1]},
        "modules": {"fed": {"enabled": True}, "watch": {"enabled": True}},
    })
    app = OutpostApp(config, clock=clock, radio=SimulatedRadioLink(clock, node_id="!local"))
    await app.database.open()
    peer = await app.federation.by_mesh_id("!remote")
    if sys.argv[2] == "uncommitted":
        original = app.federation_sync.quarantine_transaction
        async def pause(*args):
            await original(*args)
            print("uncommitted", flush=True)
            await asyncio.Event().wait()
        app.federation_sync.quarantine_transaction = pause
    await app.federation_sync.incident_events.receive(peer, event, 100)
    print("committed", flush=True)
    await asyncio.Event().wait()

asyncio.run(main())
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        str(target.database.path),
        phase,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        process.stdin.write(json.dumps(event).encode() + b"\n")
        await process.stdin.drain()
        marker = await asyncio.wait_for(process.stdout.readline(), 30)
        assert marker.decode().strip() == phase
        process.kill()  # Only this test-owned simulated process, never the appliance.
        await asyncio.wait_for(process.wait(), 10)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        await process.communicate()
    assert process.returncode == -9
    reopened = Database(target.database.path)
    await reopened.open()
    try:
        assert (await reopened.read("PRAGMA integrity_check"))[0][0] == "ok"
        rows = await reopened.read("SELECT * FROM fed_inbox_item")
        assert len(rows) == (1 if phase == "committed" else 0)
        rates = await reopened.read("SELECT cursor FROM fed_cursor WHERE stream=?", (RATE_CURSOR,))
        assert len(rates) == len(rows)
        assert not await reopened.read("SELECT * FROM outbound_work")
        receipt = await FederationSyncService(reopened, "!local").incident_events.receive(
            tp, event, 101
        )
        assert receipt["digest"] == content_digest(event["payload"])
        rates = await reopened.read("SELECT cursor FROM fed_cursor WHERE stream=?", (RATE_CURSOR,))
        assert json.loads(rates[0]["cursor"])["count"] == 1
    finally:
        await reopened.close()


@pytest.mark.parametrize(
    "field,value", [("title", False), ("body", []), ("created_at", True), ("lat", "40")]
)
async def test_event_payload_types_are_not_silently_coerced(nodes, field, value):
    _, _, target, tp, _, event = await prepare(nodes)
    event["payload"][field] = value
    with pytest.raises(ValueError):
        await target.federation_sync.incident_events.receive(tp, event, 100)
    assert not await target.database.read("SELECT * FROM fed_inbox_item")


@pytest.mark.parametrize("cursor", ["_reconcile", RATE_CURSOR])
async def test_corrupt_structured_state_fails_closed(nodes, cursor):
    _, _, target, tp, _, event = await prepare(nodes)
    await target.database.write(
        "INSERT INTO fed_cursor(peer_id,stream,direction,cursor,updated_at) "
        "VALUES(?,?,'recv','[]',1)",
        (tp.id, cursor),
    )
    with pytest.raises(ValueError, match="invalid incident event"):
        await target.federation_sync.incident_events.receive(tp, event, 100)


async def test_remote_resolution_event_cannot_silently_end_local_monitoring(nodes):
    source, sp, target, tp, incident, event = await prepare(nodes)
    await target.federation_sync.incident_events.receive(tp, event, 100)
    await approve(target, await preview(target, event["uid"]))
    local_id = (await target.database.read("SELECT id FROM incident"))[0]["id"]
    await target.incidents.operator_update(local_id, "ack", actor="web:local")
    before = dict((await target.database.read("SELECT * FROM incident"))[0])
    await source.database.write(
        "UPDATE incident SET status='resolved',resolved_at=updated_at WHERE id=?", (incident.id,)
    )
    item = await exported(source, sp, "incidents", incident.uid)
    update = {key: item[key] for key in event}
    await wire(source, target, MessageType.INCIDENT, envelope(update))
    assert dict((await target.database.read("SELECT * FROM incident"))[0]) == before
    await approve(target, await preview(target, event["uid"]))
    retained = (await target.database.read("SELECT * FROM incident"))[0]
    assert retained["status"] == "monitoring" and retained["reconciliation_review"]
    assert not await target.database.read("SELECT * FROM alert")
