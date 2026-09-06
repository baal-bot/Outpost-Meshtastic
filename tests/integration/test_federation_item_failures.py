"""Oversized content stays missing: temporary stores and simulated radios only."""

import copy
import json
import random
from dataclasses import replace
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from outpost.fed.framing import FrameError, FrameTooLarge, MessageType
from outpost.fed.item_failures import CODE, CURSOR, LIMIT, validate
from outpost.fed.reconciliation import Reconciliation
from outpost.fed.revisions import RevisionReset
from outpost.store import Database
from outpost.transport.models import InboundMessage
from outpost.web.api import create_web_app
from tests.integration.test_federation_incident_notes import exported, source_note
from tests.integration.test_federation_revisions import checkpoint, report, transfer_request
from tests.integration.test_federation_revisions import nodes as nodes

pytestmark = pytest.mark.production_wiring


def oversized_body(note=False):
    rng = random.Random(162)  # noqa: S311 -- deterministic synthetic content, not secrets
    return (
        "".join(chr(rng.randrange(0x10000, 0x10FFFF)) for _ in range(500))
        if note
        else "".join(chr(rng.randrange(33, 127)) for _ in range(5000))
    )


async def diagnostics(app, peer):
    rows = await app.database.read(
        "SELECT cursor FROM fed_cursor WHERE peer_id=? AND stream=? AND direction='send'",
        (peer.id, CURSOR),
    )
    return json.loads(rows[0]["cursor"]) if rows else []


async def reopen_cursors(app):
    await app.database.close()
    app.database = Database(app.database.path)
    await app.database.open()
    app.federation.database = app.database
    app.federation_sync.database = app.database
    app.federation_sync.revisions.database = app.database
    app.federation_reconciliation = Reconciliation(app)


async def prepare(nodes, *, capable=True, note=False):
    source, sp, _ = await nodes("remote", failures=capable, notes=note)
    target, tp, queued = await nodes(failures=capable, notes=note)
    incident = await report(source)
    if note:
        _, bad = await source_note(source, sp, incident, oversized_body(True))
    else:
        await source.database.write(
            "UPDATE incident SET body=? WHERE id=?", (oversized_body(), incident.id)
        )
        bad = await exported(source, sp, "incidents", incident.uid)
    await report(source, "fire normal neighboring record")
    await target._federation_sync_once()
    await transfer_request(source, sp, target, tp, *queued.pop(0))
    kind, request = queued.pop(0)
    assert kind is MessageType.ITEM_REQ
    items = await source.federation_sync.revisions.export(sp, request)
    bad = next(item for item in items if item["uid"] == bad["uid"])
    return source, sp, target, tp, queued, request, items, bad


async def wire(sender, receiver, kind, value):
    counter = await sender.federation.next_counter(receiver.radio.local_node_id)
    frames = sender.federation_codec.encode(
        kind, {**value, "mesh_id": sender.radio.local_node_id}, counter, bytes(range(32))
    )
    for frame in frames:
        assert len(frame) <= 188
        await receiver._handle_federation_discovery(
            InboundMessage(
                counter,
                sender.radio.local_node_id,
                "^all",
                0,
                260,
                False,
                None,
                frame,
                receiver.clock.now(),
            )
        )


async def flush_radio(sender, receiver):
    delivered = len(sender.radio.sent)
    for _ in range(120):
        sender.clock.advance(3)
        await sender.governor.tick()
        while delivered < len(sender.radio.sent):
            packet = sender.radio.sent[delivered]
            delivered += 1
            assert packet.payload and len(packet.payload) <= 188
            await receiver._handle_federation_discovery(
                InboundMessage(
                    delivered,
                    sender.radio.local_node_id,
                    "^all",
                    0,
                    260,
                    False,
                    None,
                    packet.payload,
                    receiver.clock.now(),
                )
            )
        pending = await sender.database.read(
            "SELECT id FROM outbound_work WHERE state IN ('pending','held','sending')"
        )
        if not pending:
            return
    pytest.fail("simulated governed queue did not drain")


@pytest.mark.parametrize("note", [False, True])
async def test_oversize_failure_crosses_real_codec_outbox_and_keeps_page_missing(nodes, note):
    source, sp, target, tp, queued, request, items, bad = await prepare(nodes, note=note)
    original = copy.deepcopy(bad["payload"])
    with pytest.raises(FrameTooLarge):
        source.federation_codec.encode(MessageType.ITEM, {"item": bad}, 1, bytes(range(32)))
    await wire(target, source, MessageType.ITEM_REQ, request)
    errors = await diagnostics(source, sp)
    assert len(errors) == 1 and errors[0]["failure"] == CODE
    assert errors[0]["uid"] == bad["uid"] and errors[0]["attempts"] == 1
    assert not {"payload", "body", "cycle", "lat", "lon"} & errors[0].keys()
    await flush_radio(source, target)
    state = await checkpoint(target, tp)
    assert state["status"] == "active" and state["payload_blocked"] and state["pending"]
    assert state["after"] == 0 and state["used"] == len(items) and state["rounds"] == 1
    assert state["page"]["failures"][0]["uid"] == bad["uid"]
    assert (await target.database.read("SELECT last_sync_at FROM fed_peer WHERE id=?", (tp.id,)))[
        0
    ]["last_sync_at"] is None
    assert len(await target.database.read("SELECT * FROM fed_inbox_item")) == len(items) - 1
    assert len(await target.database.read("SELECT * FROM fed_revision_receipt")) == len(items) - 1
    assert not await target.database.read("SELECT * FROM incident")
    # Only successfully quarantined neighbors are acknowledged, never the failed item.
    assert len(await target.database.read("SELECT * FROM outbound_work")) == len(items) - 1
    assert bad["payload"] == original
    await reopen_cursors(source)
    assert await diagnostics(source, sp) == errors
    await reopen_cursors(target)
    assert await checkpoint(target, tp) == state
    client = TestClient(
        create_web_app(
            lambda: {"radio": "up"}, database=target.database, federation=target.federation
        )
    )
    health = client.get("/api/v1/federation/sync-status").json()["items"][0]
    assert health["transfers"]["catch_up"]["failures"] == state["page"]["failures"]
    assert not health["transfers"]["catch_up"]["active"]
    assert health["last_sync_at"] is None
    source_client = TestClient(
        create_web_app(
            lambda: {"radio": "up"}, database=source.database, federation=source.federation
        )
    )
    source_health = source_client.get("/api/v1/federation/sync-status").json()["items"][0]
    assert source_health["transfers"]["encoding_failures"] == errors


@pytest.mark.parametrize("failure_first", [False, True])
async def test_blocked_page_retry_restart_skew_and_newer_payload_recovery(nodes, failure_first):
    source, sp, target, tp, queued, _, items, bad = await prepare(nodes)
    failure = source.federation_sync.item_failures.envelope(bad)
    ordered = [failure, *(item for item in items if item is not bad)]
    if not failure_first:
        ordered.reverse()
    for item in ordered:
        await target.federation_reconciliation.receive(tp, item)
    state = await checkpoint(target, tp)
    assert state["status"] == "active" and state["payload_blocked"]
    queued.clear()
    for _ in range(3):
        await target.federation_reconciliation.receive(tp, failure)
        await target.federation_reconciliation.tick(tp)
    assert not queued
    await reopen_cursors(target)
    target.clock.epoch += timedelta(hours=6)
    await target.federation_reconciliation.tick(tp)
    assert not queued
    target.clock.epoch -= timedelta(hours=12)
    retry = target.config.fed.sync_retry_minutes * 60
    target.clock.advance(retry - 1)
    await target.federation_reconciliation.tick(tp)
    assert not queued
    target.clock.advance(1)
    await target.federation_reconciliation.tick(tp)
    assert len(queued) == 1 and queued[0][0] is MessageType.ITEM_REQ
    assert queued[0][1]["items"] == [
        {"stream": bad["stream"], "uid": bad["uid"], "revision": bad["revision"]}
    ]
    assert await checkpoint(target, tp) == state
    await target.federation_reconciliation.receive(tp, failure)
    queued.clear()
    target.clock.advance(retry - 1)
    await target.federation_reconciliation.tick(tp)
    assert not queued
    await source.database.write(
        "UPDATE incident SET body='Corrected concise report' WHERE uid=?",
        (source.federation_sync._local_uid(bad["uid"]),),
    )
    target.clock.advance(1)
    await target.federation_reconciliation.tick(tp)
    kind, request = queued.pop(0)
    repaired = await source.federation_sync.revisions.export(sp, request)
    assert repaired[0]["revision"] > bad["revision"]
    assert await target.federation_reconciliation.receive(tp, repaired[0])
    complete = await checkpoint(target, tp)
    assert complete["status"] == "complete" and complete["page"] is None
    assert complete["used"] == state["used"] and complete["rounds"] == state["rounds"]
    assert (await target.database.read("SELECT last_sync_at FROM fed_peer WHERE id=?", (tp.id,)))[
        0
    ]["last_sync_at"] is not None


@pytest.mark.parametrize(
    "change",
    [
        {"failure": "some_other_error"},
        {"payload": {}},
        {"unavailable": False},
        {"digest": None},
        {"digest": "z" * 16},
        {"digest": "0" * 16},
        {"revision": True},
        {"revision": 0},
        {"uid": "!remote:unsolicited"},
        {"stream": "board:private"},
    ],
)
async def test_malformed_conflicting_or_unsolicited_failures_cannot_change_state(nodes, change):
    source, _, target, tp, _, _, _, bad = await prepare(nodes)
    failure = {**source.federation_sync.item_failures.envelope(bad), **change}
    state = await checkpoint(target, tp)
    with pytest.raises(ValueError):
        await target.federation_reconciliation.receive(tp, failure)
    assert await checkpoint(target, tp) == state
    assert not await target.database.read("SELECT * FROM fed_revision_receipt")
    assert not await target.database.read("SELECT * FROM fed_inbox_item")


@pytest.mark.parametrize("change", [{"cycle": "b" * 32}, {"epoch": "b" * 32}])
async def test_delayed_failures_do_not_affect_other_cycles_or_lineages(nodes, change):
    source, _, target, tp, _, _, _, bad = await prepare(nodes)
    state = await checkpoint(target, tp)
    failure = {**source.federation_sync.item_failures.envelope(bad), **change}
    assert not await target.federation_reconciliation.receive(tp, failure)
    assert await checkpoint(target, tp) == state


@pytest.mark.parametrize("capability", [None, False, True, "1", 2])
async def test_failure_requires_explicit_integer_capability(nodes, capability):
    source, _, target, tp, _, _, _, bad = await prepare(nodes)
    tp = replace(tp, capabilities={"reconciliation": 2, "item_failures": capability})
    with pytest.raises(ValueError, match="negotiate"):
        await target.federation_reconciliation.receive(
            tp, source.federation_sync.item_failures.envelope(bad)
        )


async def test_old_peer_gets_no_negative_wire_extension_but_source_error_is_visible(nodes):
    source, sp, target, tp, _, request, items, _ = await prepare(nodes, capable=False)
    await wire(target, source, MessageType.ITEM_REQ, request)
    assert len(await diagnostics(source, sp)) == 1
    await flush_radio(source, target)
    state = await checkpoint(target, tp)
    assert state["status"] == "active" and not state["page"].get("failures")
    assert len(await target.database.read("SELECT * FROM fed_inbox_item")) == len(items) - 1


async def test_source_journal_coalesces_bounds_and_guards_revision_clearing(nodes):
    source, sp, _ = await nodes("remote", failures=True)
    service = source.federation_sync.item_failures
    for i in range(LIMIT + 3):
        incident = await report(source, f"road example {i}")
        item = await exported(source, sp, "incidents", incident.uid)
        failure = service.envelope(item)
        await service.record(sp, failure, i)
        await service.record(sp, failure, i)
    errors = await diagnostics(source, sp)
    assert len(errors) == LIMIT and all(error["attempts"] == 2 for error in errors)
    await source.database.write("UPDATE incident SET body='new content' WHERE id=?", (incident.id,))
    newer = await exported(source, sp, "incidents", incident.uid)
    await service.record(sp, service.envelope(newer), 100)
    await service.record(sp, failure, 101)
    await service.admitted(sp, item, 102)
    assert (await diagnostics(source, sp))[0]["revision"] == newer["revision"]
    assert (await diagnostics(source, sp))[0]["attempts"] == 1
    await service.admitted(sp, {**newer, "epoch": "b" * 32}, 103)
    assert len(await diagnostics(source, sp)) == LIMIT
    await service.admitted(sp, newer, 104)
    assert len(await diagnostics(source, sp)) == LIMIT - 1
    # Stale export after a local deletion cannot replace current diagnostics.
    await source.database.write("DELETE FROM fed_revision WHERE uid=?", (incident.uid,))
    await service.record(sp, service.envelope(newer), 105)
    assert len(await diagnostics(source, sp)) == LIMIT - 1


@pytest.mark.parametrize("error", [FrameError("not oversized"), ValueError("queue rejected")])
async def test_other_encoding_and_admission_errors_are_not_payload_failures(
    nodes, monkeypatch, error
):
    source, sp, target, _, _, request, _, _ = await prepare(nodes)

    async def reject(*args, **kwargs):
        raise error

    monkeypatch.setattr(source, "_send_federation_value", reject)
    await wire(target, source, MessageType.ITEM_REQ, request)
    assert not await diagnostics(source, sp)


@pytest.mark.parametrize(
    "change",
    [
        {"stream": ""},
        {"stream": "x" * 81},
        {"uid": None},
        {"uid": "x" * 161},
        {"epoch": None},
        {"revision": None},
    ],
)
async def test_failure_metadata_validation_is_bounded(nodes, change):
    source, sp, _ = await nodes("remote", failures=True)
    incident = await report(source)
    item = await exported(source, sp, "incidents", incident.uid)
    failure = {**source.federation_sync.item_failures.envelope(item), **change}
    with pytest.raises(ValueError):
        validate(failure)


async def test_failure_without_revision_is_invalid():
    with pytest.raises(ValueError, match="requires a producer revision"):
        validate({"stream": "incidents", "uid": "!remote:x", "failure": CODE, "digest": "a" * 16})


async def test_newer_failure_cannot_be_cleared_by_older_content_or_conflicting_digest(nodes):
    source, sp, target, tp, _, _, _, bad = await prepare(nodes)
    service = source.federation_sync.item_failures
    prior = service.envelope(bad)
    await source.database.write(
        "UPDATE incident SET body=body||'new' WHERE uid=?",
        (source.federation_sync._local_uid(bad["uid"]),),
    )
    newer = await exported(source, sp, "incidents", source.federation_sync._local_uid(bad["uid"]))
    newer["cycle"] = bad["cycle"]
    failure = service.envelope(newer)
    await target.federation_reconciliation.receive(tp, failure)
    state = await checkpoint(target, tp)
    assert not await target.federation_reconciliation.receive(tp, prior)
    assert not await target.federation_reconciliation.receive(tp, bad)
    for conflict in ({**failure, "digest": "0" * 16}, {**newer, "payload": {}}):
        with pytest.raises(ValueError, match="producer revision"):
            await target.federation_reconciliation.receive(tp, conflict)
    assert await checkpoint(target, tp) == state
    # A newly withdrawn source does not claim receipt of the failed content.
    unavailable = {key: newer[key] for key in ("uid", "stream", "cycle", "epoch", "revision")}
    unavailable["unavailable"] = True
    await target.federation_reconciliation.receive(tp, unavailable)
    assert not (await checkpoint(target, tp))["page"]["failures"]
    assert not await target.database.read("SELECT * FROM fed_revision_receipt")


@pytest.mark.parametrize("revoked", [True, False])
async def test_current_trust_and_scope_gate_failures(nodes, revoked):
    source, _, target, tp, _, _, _, bad = await prepare(nodes)
    state = await checkpoint(target, tp)
    tp = replace(tp, state="revoked") if revoked else replace(tp, sync_incidents=False)
    assert not await target.federation_reconciliation.receive(
        tp, source.federation_sync.item_failures.envelope(bad)
    )
    assert await checkpoint(target, tp) == state


async def test_scope_reset_can_recover_a_payload_block_without_resetting_budget(nodes):
    source, sp, target, tp, queued, _, items, bad = await prepare(nodes)
    for item in items:
        await target.federation_reconciliation.receive(
            tp, source.federation_sync.item_failures.envelope(item) if item is bad else item
        )
    old = await checkpoint(target, tp)
    assert old["status"] == "active" and old["payload_blocked"]
    queued.clear()
    target.clock.advance(target.config.fed.sync_retry_minutes * 60)
    await target.federation_reconciliation.tick(tp)
    source.config.modules.watch.enabled = False
    _, request = queued.pop(0)
    with pytest.raises(RevisionReset) as caught:
        await source.federation_sync.revisions.export(sp, request)
    await target.federation_reconciliation.manifest(tp, caught.value.manifest)
    current = await checkpoint(target, tp)
    assert current["status"] == "active" and current["page"] is None
    assert current["used"] == old["used"] and current["rounds"] == old["rounds"] + 1
    assert current["cycle"] != old["cycle"]


async def test_failed_item_with_an_old_inbox_version_never_emits_an_item_receipt(nodes):
    source, sp, target, tp, _, _, _, bad = await prepare(nodes)
    # A previously quarantined older revision must not trigger app.py's legacy
    # UID-only positive acknowledgement path for a new negative response.
    prior = {**bad, "revision": bad["revision"] - 1}
    prior["payload"] = {**bad["payload"], "body": "Previously stored content"}
    await target.federation_sync.quarantine(tp, prior, 100)
    await wire(
        source,
        target,
        MessageType.ITEM,
        {"item": source.federation_sync.item_failures.envelope(bad)},
    )
    assert (await checkpoint(target, tp))["page"]["failures"]
    assert not await target.database.read("SELECT * FROM outbound_work")
    stored = (await target.database.read("SELECT source_revision FROM fed_inbox_item"))[0]
    assert stored["source_revision"] == prior["revision"]


async def test_permission_change_after_encoding_stops_negative_and_remaining_exports(
    nodes, monkeypatch
):
    source, sp, target, _, _, request, _, _ = await prepare(nodes)
    original = source.federation_sync.item_failures.record

    async def revoke(peer, failure, now):
        await original(peer, failure, now)
        await source.database.write("UPDATE fed_peer SET sync_incidents=0 WHERE id=?", (peer.id,))

    monkeypatch.setattr(source.federation_sync.item_failures, "record", revoke)
    await wire(target, source, MessageType.ITEM_REQ, request)
    assert len(await diagnostics(source, sp)) == 1
    assert not await source.database.read("SELECT * FROM outbound_work")


async def test_real_queue_admission_clears_only_the_encoding_diagnostic(nodes):
    source, sp, target, tp, _, request, _, bad = await prepare(nodes)
    await wire(target, source, MessageType.ITEM_REQ, request)
    await flush_radio(source, target)
    assert (await checkpoint(target, tp))["payload_blocked"]
    await source.database.write(
        "UPDATE incident SET body='Concise repaired content' WHERE uid=?",
        (source.federation_sync._local_uid(bad["uid"]),),
    )
    await wire(
        target,
        source,
        MessageType.ITEM_REQ,
        {**request, "items": [item for item in request["items"] if item["uid"] == bad["uid"]]},
    )
    assert not await diagnostics(source, sp)
    # Queue admission is not remote receipt. Only actual simulated dispatch repairs it.
    assert (await checkpoint(target, tp))["payload_blocked"]
    await flush_radio(source, target)
    assert (await checkpoint(target, tp))["status"] == "complete"


async def test_already_stored_revision_ignores_late_negative_response(nodes):
    source, _, target, tp, _, _, items, bad = await prepare(nodes)
    good = next(item for item in items if item is not bad)
    assert await target.federation_reconciliation.receive(tp, good)
    before = await checkpoint(target, tp)
    assert not await target.federation_reconciliation.receive(
        tp, source.federation_sync.item_failures.envelope(good)
    )
    assert await checkpoint(target, tp) == before


@pytest.mark.parametrize("stream", ["board:gen", "alerts"])
async def test_oversized_board_and_alert_content_is_not_auto_imported(nodes, stream):
    source, sp, _ = await nodes("remote", failures=True)
    target, tp, queued = await nodes(failures=True)
    if stream == "board:gen":
        board = (await source.database.read("SELECT id FROM board WHERE slug='gen'"))[0]["id"]
        thread = await source.database.write(
            "INSERT INTO thread(uid,board_id,subject,origin_node,created_at,last_post_at) "
            "VALUES('thread',?,'Synthetic large post','!remote',100,100)",
            (board,),
        )
        await source.database.write(
            "INSERT INTO post(uid,thread_id,seq,author_label,origin_node,body,created_at) "
            "VALUES('post',?,1,'Reporter','!remote',?,100)",
            (thread, oversized_body()),
        )
    else:
        await source.database.write(
            "INSERT INTO alert(uid,severity,headline,body,source,channels,raised_by,raised_at) "
            "VALUES('alert','urgent','Synthetic large advisory',?,'operator','[]','operator',100)",
            (oversized_body(),),
        )
    await target._federation_sync_once()
    await transfer_request(source, sp, target, tp, *queued.pop(0))
    _, request = queued.pop(0)
    await wire(target, source, MessageType.ITEM_REQ, request)
    await flush_radio(source, target)
    assert (await checkpoint(target, tp))["payload_blocked"]
    assert (await diagnostics(source, sp))[0]["stream"] == stream
    for table in ("fed_inbox_item", "fed_revision_receipt", "post", "alert", "outbound_work"):
        assert not await target.database.read(f"SELECT * FROM {table}")  # noqa: S608


@pytest.mark.parametrize("status", ["active", "complete"])
async def test_leftover_payload_flag_does_not_mislabel_a_page_without_failures(nodes, status):
    target, tp, _ = await nodes(failures=True)
    await target._store_reconciliation_checkpoint(
        tp.id,
        {
            "mode": 2,
            "status": status,
            "payload_blocked": True,
            "page": {"failures": []} if status == "active" else None,
        },
        100,
    )
    client = TestClient(
        create_web_app(
            lambda: {"radio": "up"}, database=target.database, federation=target.federation
        )
    )
    health = client.get("/api/v1/federation/sync-status").json()["items"][0]
    assert health["transfers"]["catch_up"]["status"] == status


async def test_newer_failure_revision_floor_exposes_producer_rollback(nodes):
    source, sp, target, tp, queued, _, _, bad = await prepare(nodes)
    local_uid = source.federation_sync._local_uid(bad["uid"])
    await source.database.write("UPDATE incident SET body=body||'new' WHERE uid=?", (local_uid,))
    newer = await exported(source, sp, "incidents", local_uid)
    newer["cycle"] = bad["cycle"]
    await target.federation_reconciliation.receive(
        tp, source.federation_sync.item_failures.envelope(newer)
    )
    target.clock.advance(target.config.fed.sync_retry_minutes * 60)
    await target.federation_reconciliation.tick(tp)
    _, request = queued.pop(0)
    assert (
        next(item for item in request["items"] if item["uid"] == bad["uid"])["revision"]
        == newer["revision"]
    )
    # Synthetic restored head, not an appliance restore or a power-cut test.
    await source.database.write(
        "UPDATE fed_revision SET revision=? WHERE uid=?", (bad["revision"], local_uid)
    )
    with pytest.raises(RevisionReset) as caught:
        await source.federation_sync.revisions.export(sp, request)
    assert caught.value.manifest["rollback"] is True
    await target.federation_reconciliation.manifest(tp, caught.value.manifest)
    assert (await checkpoint(target, tp))["status"] == "blocked"
    assert not await target.database.read("SELECT * FROM fed_revision_receipt")
