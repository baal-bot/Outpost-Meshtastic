"""Exact reply coalescing, retained evidence and policy guards via production app wiring."""

import asyncio
import json

import pytest

from outpost.fed.framing import MessageType
from outpost.fed.incident_receipts import GUARD
from outpost.store import Transaction
from tests.integration.test_federation_incident_events import envelope, prepare, rate
from tests.integration.test_federation_item_failures import flush_radio, wire
from tests.integration.test_federation_revisions import nodes as nodes

pytestmark = pytest.mark.production_wiring


async def test_identical_fresh_counter_events_share_one_pending_reply(nodes):
    source, _, target, _, _, event = await prepare(nodes)
    for _ in range(8):
        await wire(source, target, MessageType.INCIDENT, envelope(event))
    rows = await target.database.read("SELECT * FROM fed_incident_receipt_reply")
    assert len(rows) == 1
    ids = json.loads(rows[0]["frame_ids"])
    assert tuple(item.item_id for item in target.governor.queued_items()) == tuple(ids)
    assert all(item.guard_kind == GUARD for item in target.governor.queued_items())
    assert (await target.database.read("SELECT tx_counter FROM fed_peer"))[0][0] == 1
    assert (await rate(target))["count"] == 1
    await flush_radio(target, source)
    await wire(source, target, MessageType.INCIDENT, envelope(event))
    later = (await target.database.read("SELECT * FROM fed_incident_receipt_reply"))[0]
    assert later["counter"] == rows[0]["counter"] + 1
    assert later["queue_key"] != rows[0]["queue_key"]
    assert len(await target.database.read("SELECT * FROM fed_incident_receipt_reply")) == 1
    assert (await rate(target))["count"] == 1


@pytest.mark.parametrize(
    "change",
    [
        "key",
        "paused",
        "scope",
        "watch",
        "fed",
        "identity",
        "route",
        "inbox",
        "receipt",
        "payload",
        "lineage",
        "missing_guard",
    ],
)
async def test_recovered_receipt_reply_revalidates_current_evidence_and_policy(nodes, change):
    source, _, target, tp, _, event = await prepare(nodes)
    await wire(source, target, MessageType.INCIDENT, envelope(event))
    if change == "key":
        await target.database.write("UPDATE fed_peer SET shared_secret=?", (b"z" * 32,))
    elif change == "paused":
        await target.database.write("UPDATE fed_peer SET state='paused'")
    elif change == "scope":
        await target.database.write("UPDATE fed_peer SET sync_incidents=0")
    elif change in {"watch", "fed"}:
        getattr(target.config.modules, change).enabled = False
    elif change == "identity":
        target.federation_sync.local_mesh_id = "!changed"
    elif change == "route":
        target.config.radio.federation_portnum = 261
    elif change == "inbox":
        await target.database.write("DELETE FROM fed_inbox_item")
    elif change == "receipt":
        await target.database.write("DELETE FROM fed_incident_receipt_reply")
    elif change == "payload":
        await target.database.write("UPDATE fed_inbox_item SET payload_json='{}'")
    elif change == "lineage":
        await target.database.write(
            "INSERT INTO fed_cursor(peer_id,stream,direction,cursor,updated_at) "
            "VALUES(?,'_reconcile','recv',?,1)",
            (tp.id, json.dumps({"status": "blocked"})),
        )
    else:
        del target.governor.outbox.attempt_guards[GUARD]
    await target.governor.recover()
    for _ in range(8):
        target.clock.advance(3)
        await target.governor.tick()
    assert not target.radio.sent
    assert not await target.database.read("SELECT * FROM outbound_attempt")
    assert not target.governor.queued_items()


@pytest.mark.parametrize("fault", ["counter", "association", "cancel", "queue_full"])
async def test_reply_failure_preserves_storage_but_rolls_back_reply_and_counter(
    nodes, monkeypatch, fault
):
    source, _, target, tp, _, event = await prepare(nodes)
    receipt = await target.federation_sync.incident_events.receive(tp, event, 100)
    original = Transaction.write
    original_read = Transaction.read

    async def fail_read(tx, sql, params=()):
        if fault == "counter" and sql.startswith("UPDATE fed_peer SET tx_counter="):
            raise RuntimeError("injected counter writer failure")
        return await original_read(tx, sql, params)

    async def fail(tx, sql, params=()):
        if (fault == "counter" and "tx_counter" in sql and sql.startswith("UPDATE fed_peer")) or (
            fault in {"association", "cancel"} and "INSERT INTO fed_incident_receipt_reply" in sql
        ):
            raise asyncio.CancelledError() if fault == "cancel" else RuntimeError("injected")
        return await original(tx, sql, params)

    if fault == "queue_full":
        target.governor.config.queue_max_items = 0
    with monkeypatch.context() as patch:
        patch.setattr(Transaction, "write", fail)
        patch.setattr(Transaction, "read", fail_read)
        with pytest.raises((RuntimeError, ValueError, asyncio.CancelledError)):
            await target.incident_receipts.admit(tp.id, receipt)
    assert not await target.database.read("SELECT * FROM fed_incident_receipt_reply")
    assert not await target.database.read("SELECT * FROM outbound_work")
    assert not target.governor.queued_items()
    assert (await target.database.read("SELECT tx_counter FROM fed_peer"))[0][0] == 0
    assert len(await target.database.read("SELECT * FROM fed_inbox_item")) == 1
    target.governor.config.queue_max_items = 200
    assert await target.incident_receipts.admit(tp.id, receipt)


async def test_newer_stored_version_replaces_only_unsent_old_reply(nodes):
    source, sp, target, tp, incident, event = await prepare(nodes)
    await wire(source, target, MessageType.INCIDENT, envelope(event))
    old = (await target.database.read("SELECT * FROM fed_incident_receipt_reply"))[0]
    await source.incidents.operator_patch(
        incident.id,
        status="monitoring",
        severity=None,
        resolution=None,
        actor="web:test",
    )
    item = (
        await source.federation_sync.export_items(
            sp, [{"stream": "incidents", "uid": source.federation_sync.wire_uid(incident.uid)}]
        )
    )[0]
    revision = (
        await source.database.read(
            "SELECT revision FROM fed_revision WHERE stream='incidents' AND uid=?", (incident.uid,)
        )
    )[0][0]
    newer = {**event, "payload": item["payload"], "revision": revision}
    await wire(source, target, MessageType.INCIDENT, envelope(newer))
    current = (await target.database.read("SELECT * FROM fed_incident_receipt_reply"))[0]
    assert current["counter"] > old["counter"]
    assert all(
        row[0] == "superseded"
        for row in await target.database.read(
            "SELECT state FROM outbound_work WHERE queue_key=?", (old["queue_key"],)
        )
    )
    assert len(await target.database.read("SELECT * FROM fed_incident_receipt_reply")) == 1
    assert json.loads(current["value_json"])["revision"] == revision
    await flush_radio(target, source)
    assert not await target.database.read("SELECT * FROM incident")


@pytest.mark.parametrize("change", ["mode", "fields", "stream", "uid", "digest", "revision"])
async def test_malformed_internal_reply_is_rejected_without_counter_or_queue_change(nodes, change):
    _, _, target, tp, _, event = await prepare(nodes)
    receipt = await target.federation_sync.incident_events.receive(tp, event, 100)
    if change == "fields":
        receipt["extra"] = True
    else:
        receipt[change] = {
            "mode": True,
            "stream": [],
            "uid": 1,
            "digest": "x" * 64,
            "revision": True,
        }[change]
    with pytest.raises(ValueError):
        await target.incident_receipts.admit(tp.id, receipt)
    assert not await target.database.read("SELECT * FROM outbound_work")
    assert (await target.database.read("SELECT tx_counter FROM fed_peer"))[0][0] == 0
