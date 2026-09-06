"""Original-producer notes: temporary stores and simulated radios only."""

import asyncio
import copy
import json
import sqlite3
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from outpost.fed.incident_updates import STREAM
from outpost.fed.review import ReviewConflict
from outpost.fed.sync import FederationSyncService
from outpost.store import Database, Transaction
from tests.integration.test_federation_revisions import checkpoint, drain, report
from tests.integration.test_federation_revisions import nodes as nodes

pytestmark = pytest.mark.production_wiring


async def exported(app, peer, stream, local_uid):
    sync = app.federation_sync
    epoch = (await app.database.read("SELECT epoch FROM fed_revision_lineage"))[0]["epoch"]
    revision = (
        await app.database.read(
            "SELECT revision FROM fed_revision WHERE stream=? AND uid=?", (stream, local_uid)
        )
    )[0]["revision"]
    items = await sync.revisions.export(
        peer,
        {
            "cycle": "a" * 32,
            "epoch": epoch,
            "scope": sync.revisions.scope(peer),
            "items": [{"stream": stream, "uid": sync.wire_uid(local_uid), "revision": revision}],
        },
    )
    return items[0]


async def preview(app, uid):
    row = (await app.database.read("SELECT id FROM fed_inbox_item WHERE uid=?", (uid,)))[0]
    return await app.operations_center.federation_item(row["id"])


async def approve(app, item):
    return await app.import_federation_inbox_as(item["id"], "web:test", item["review_token"])


async def source_note(app, peer, incident=None, body="Crew reached bridge; foot crossing open."):
    incident = incident or await report(app)
    await app.incidents.operator_update(incident.id, "update", body, actor="web:coordinator")
    note = (
        await app.database.read(
            "SELECT uid FROM incident_update WHERE incident_id=? ORDER BY seq DESC", (incident.id,)
        )
    )[0]
    return incident, await exported(app, peer, STREAM, note["uid"])


async def import_parent(source, sp, target, tp, incident):
    item = await exported(source, sp, "incidents", incident.uid)
    await target.federation_sync.quarantine(tp, item, 100)
    await approve(target, await preview(target, item["uid"]))
    return (await target.database.read("SELECT id FROM incident WHERE uid=?", (item["uid"],)))[0][
        "id"
    ]


async def test_notes_require_parent_review_preserve_content_and_never_act_for_responders(nodes):
    source, sp, _ = await nodes("remote", notes=True)
    target, tp, _ = await nodes(notes=True)
    incident, item = await source_note(source, sp, body="Crew reached bridge. Café meeting point.")
    assert await target.federation_sync.quarantine(tp, item, 100)
    pending = await preview(target, item["uid"])
    with pytest.raises(ValueError, match="parent incident"):
        await approve(target, pending)
    assert not await target.database.read("SELECT * FROM incident_update")
    assert await target.federation_sync.import_approved_replies("federation:auto", 101) == 0
    imported_id = await import_parent(source, sp, target, tp, incident)
    await target.incidents.operator_update(imported_id, "ack", actor="web:local")
    before = dict(
        (await target.database.read("SELECT * FROM incident WHERE id=?", (imported_id,)))[0]
    )
    await approve(target, pending)
    note = dict(
        (await target.database.read("SELECT * FROM incident_update WHERE uid=?", (item["uid"],)))[0]
    )
    assert note["body"] == item["payload"]["body"]
    assert (
        note["source_node"] == "!remote"
        and note["origin_incident_uid"] == item["payload"]["incident_uid"]
    )
    assert note["source_revision"] == item["revision"] and note["source_epoch"] == item["epoch"]
    assert note["kind"] == "update" and note["author_id"] is None
    assert note["author_label"] == "federation:!remote: web:coordinator"
    assert (
        dict((await target.database.read("SELECT * FROM incident WHERE id=?", (imported_id,)))[0])
        == before
    )
    assert not await target.database.read("SELECT * FROM alert")
    assert not await target.database.read("SELECT * FROM outbound_work")
    assert not await target.database.read("SELECT * FROM fed_revision WHERE stream=?", (STREAM,))
    events = await target.database.read(
        "SELECT * FROM incident_provenance WHERE event_kind='federation_note_imported'"
    )
    assert len(events) == 1 and json.loads(events[0]["payload_json"])["body"] == note["body"]
    assert not await target.federation_sync.quarantine(tp, item, 102)
    assert len(await target.incidents.updates(imported_id, 100)) == 2


@pytest.mark.parametrize("offset", [-21600, 21600])
async def test_same_second_notes_survive_skew_restart_and_a_complete_revision_walk(nodes, offset):
    source, sp, _ = await nodes("remote", notes=True, offset=offset)
    target, tp, queued = await nodes(notes=True)
    incident, first = await source_note(source, sp)
    _, second = await source_note(source, sp, incident, "Second same-second operator note.")
    assert second["revision"] > first["revision"]
    assert second["payload"]["created_at"] == first["payload"]["created_at"]
    source.clock.epoch -= timedelta(days=1)
    _, third = await source_note(source, sp, incident, "Third after backwards source clock step.")
    assert third["revision"] > second["revision"]
    assert third["payload"]["created_at"] < first["payload"]["created_at"]
    await target._federation_sync_once()
    await drain(source, sp, target, tp, queued)
    assert (await checkpoint(target, tp))["status"] == "complete"
    assert len(await target.database.read("SELECT * FROM fed_revision_receipt")) == 4
    reopened = Database(target.database.path)
    await reopened.open()
    try:
        restarted = FederationSyncService(reopened, "!local")
        assert not await restarted.quarantine(tp, first, 105)
        assert len(await reopened.read("SELECT * FROM fed_inbox_item WHERE state='pending'")) == 4
    finally:
        await reopened.close()
    assert not await target.database.read("SELECT * FROM incident")
    assert not await target.database.read("SELECT * FROM incident_update")
    await approve(target, await preview(target, third["payload"]["incident_uid"]))
    for item in (third, first, second):
        await approve(target, await preview(target, item["uid"]))
    assert {r["body"] for r in await target.database.read("SELECT body FROM incident_update")} == {
        item["payload"]["body"] for item in (first, second, third)
    }


@pytest.mark.parametrize(
    "change", ["old_peer", "legacy", "bool_cap", "disabled", "scope", "geographic"]
)
async def test_exports_are_capability_negotiated_and_scope_filtered(nodes, change):
    source, sp, _ = await nodes("remote", notes=True)
    incident, item = await source_note(source, sp)
    old_scope = source.federation_sync.revisions.scope(sp)
    if change == "old_peer":
        sp = replace(sp, capabilities={"reconciliation": 2})
    elif change == "legacy":
        sp = replace(sp, capabilities={"incident_updates": 1})
    elif change == "bool_cap":
        sp = replace(sp, capabilities={"reconciliation": 2, "incident_updates": True})
    elif change == "disabled":
        source.federation_sync.module_enabled = lambda _module: False
    elif change == "scope":
        sp = replace(sp, sync_incidents=False)
    else:
        await source.database.write("UPDATE incident SET lat=40,lon=-75 WHERE id=?", (incident.id,))
    assert not await source.federation_sync.export_items(sp, [item])
    assert all(value.stream != STREAM for value in await source.federation_sync.manifest(sp))
    page = await source.federation_sync.revisions.page(sp, {"cycle": "a" * 32})
    assert not any(value["s"] == STREAM for value in page["items"])
    if change != "geographic":
        assert old_scope != source.federation_sync.revisions.scope(sp)


async def test_enabling_notes_resets_scope_and_backfills_pre_checkpoint_content(nodes):
    source, sp, _ = await nodes("remote", notes=True)
    _, item = await source_note(source, sp)
    old = replace(sp, capabilities={"reconciliation": 2})
    page = await source.federation_sync.revisions.page(old, {"cycle": "a" * 32})
    reset = await source.federation_sync.revisions.page(sp, {**page, "after": page["next"]})
    assert reset["reset"] is True
    fresh = await source.federation_sync.revisions.page(sp, {"cycle": "b" * 32})
    assert item["uid"] in {value["u"] for value in fresh["items"]}


async def test_parent_location_correction_revisits_previously_withheld_notes(nodes):
    source, sp, _ = await nodes("remote", notes=True)
    incident, item = await source_note(source, sp)
    await source.database.write("UPDATE incident SET lat=40,lon=-75 WHERE id=?", (incident.id,))
    hidden = await source.federation_sync.revisions.page(sp, {"cycle": "a" * 32})
    assert hidden["items"] == []
    await source.database.write("UPDATE incident SET lat=NULL,lon=NULL WHERE id=?", (incident.id,))
    fresh = await source.federation_sync.revisions.page(
        sp, {"cycle": "b" * 32, "after": hidden["next"]}
    )
    note = next(value for value in fresh["items"] if value["s"] == STREAM)
    assert note["u"] == item["uid"] and note["r"] > hidden["snapshot"]


async def test_merge_and_unmerge_keep_imported_note_on_its_original_parent(nodes):
    source, sp, _ = await nodes("remote", notes=True)
    target, tp, _ = await nodes(notes=True)
    incident = await report(source, "road bridge closed 40.0000 -79.0000")
    _, item = await source_note(source, sp, incident)
    # Explicit policy is checked again inside the review transaction.
    for app in (source, target):
        await app.database.write(
            "UPDATE fed_peer SET incident_lat=40,incident_lon=-79,incident_radius_km=25"
        )
    sp = await source.federation.by_mesh_id("!local")
    tp = await target.federation.by_mesh_id("!remote")
    # Re-export after granting the source's geographic policy.
    item = await exported(source, sp, STREAM, source.federation_sync._local_uid(item["uid"]))
    original_id = await import_parent(source, sp, target, tp, incident)
    local = await report(target, "road bridge closed 40.0005 -79.0005")
    await target.incidents.merge(original_id, local.id, "web:test")
    await target.federation_sync.quarantine(tp, item, 100)
    await approve(target, await preview(target, item["uid"]))
    note = (await target.database.read("SELECT * FROM incident_update"))[0]
    assert note["incident_id"] == original_id
    event = (
        await target.database.read(
            "SELECT * FROM incident_provenance WHERE event_kind='federation_note_imported'"
        )
    )[0]
    assert (
        event["incident_id"] == local.id and event["origin_uid"] == item["payload"]["incident_uid"]
    )
    await target.incidents.unmerge(original_id, "web:test")
    assert (await target.incidents.updates(original_id))[0]["body"] == item["payload"]["body"]
    # Local commentary on a foreign report cannot become original-producer authority.
    await target.incidents.operator_update(original_id, "update", "Local advisory")
    assert not await target.database.read("SELECT * FROM fed_revision WHERE stream=?", (STREAM,))


@pytest.mark.parametrize("boundary", [1, 2, 3])
async def test_local_operator_note_and_producer_revision_commit_or_rollback_together(
    nodes, monkeypatch, boundary
):
    source, sp, _ = await nodes("remote", notes=True)
    incident = await report(source)
    heads = [dict(row) for row in await source.database.read("SELECT * FROM fed_revision")]
    original = Transaction.write
    calls = 0

    async def fail(tx, sql, params=()):
        nonlocal calls
        result = await original(tx, sql, params)
        calls += 1
        if calls == boundary:
            raise RuntimeError("injected local note failure")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(Transaction, "write", fail)
        with pytest.raises(RuntimeError, match="injected"):
            await source.incidents.operator_update(incident.id, "update", "Must roll back")
    assert [dict(row) for row in await source.database.read("SELECT * FROM fed_revision")] == heads
    assert not await source.database.read("SELECT * FROM incident_update")
    await source_note(source, sp, incident)


async def test_non_plain_or_oversized_history_is_withheld_without_truncation(nodes):
    source, sp, _ = await nodes("remote", notes=True)
    incident, item = await source_note(source, sp)
    uid = source.federation_sync._local_uid(item["uid"])
    for sql, params in (
        ("UPDATE incident_update SET kind='ack' WHERE uid=?", (uid,)),
        ("UPDATE incident_update SET kind='update',body=? WHERE uid=?", ("x" * 501, uid)),
        ("UPDATE incident_update SET body='Coordinates',lat=40,lon=-79 WHERE uid=?", (uid,)),
    ):
        await source.database.write(sql, params)
        assert not await source.federation_sync.export_items(sp, [item])
    assert (await source.database.read("SELECT body FROM incident_update WHERE uid=?", (uid,)))[0][
        "body"
    ] == "Coordinates"
    await source.incidents.operator_update(incident.id, "ack", actor="web:test")
    heads = await source.database.read("SELECT uid FROM fed_revision WHERE stream=?", (STREAM,))
    assert [row["uid"] for row in heads] == [uid]


async def test_note_lineage_guards_survive_inbox_and_receipt_retention(nodes):
    source, sp, _ = await nodes("remote", notes=True)
    target, tp, _ = await nodes(notes=True)
    incident, item = await source_note(source, sp)
    await import_parent(source, sp, target, tp, incident)
    await target.federation_sync.quarantine(tp, item, 100)
    await approve(target, await preview(target, item["uid"]))
    await target.database.write("DELETE FROM fed_inbox_item WHERE stream=?", (STREAM,))
    await target.database.write("DELETE FROM fed_revision_receipt WHERE stream=?", (STREAM,))
    reset = {**item, "epoch": "b" * 32, "revision": item["revision"] + 1}
    await target.federation_sync.quarantine(tp, reset, 101)
    with pytest.raises(ValueError, match="lineage differs from its parent"):
        await approve(target, await preview(target, item["uid"]))
    # Even a legacy parent without pinned epoch cannot clear the note's own lineage.
    await target.database.write("UPDATE incident_origin SET source_epoch=NULL")
    with pytest.raises(ValueError, match="note producer lineage changed"):
        await approve(target, await preview(target, item["uid"]))


async def test_hello_advertises_notes_only_with_watch_enabled(nodes, monkeypatch):
    app, _, _ = await nodes(notes=True)
    original = app.federation_codec.encode
    hellos = []

    def encode(kind, value, *args, **kwargs):
        hellos.append(value)
        return original(kind, value, *args, **kwargs)

    monkeypatch.setattr(app.federation_codec, "encode", encode)
    await app._queue_federation_hello("^all")
    assert hellos[-1]["capabilities"]["incident_updates"] == 1
    assert hellos[-1]["capabilities"]["reconciliation"] == 2
    app.config.modules.watch.enabled = False
    await app._queue_federation_hello("^all")
    assert "incident_updates" not in hellos[-1]["capabilities"]


@pytest.mark.parametrize("change", ["peer", "capability", "module", "scope", "geographic"])
async def test_review_rechecks_current_policy_transactionally(nodes, change):
    source, sp, _ = await nodes("remote", notes=True)
    target, tp, _ = await nodes(notes=True)
    incident, item = await source_note(source, sp)
    imported = await import_parent(source, sp, target, tp, incident)
    await target.federation_sync.quarantine(tp, item, 100)
    pending = await preview(target, item["uid"])
    if change == "peer":
        await target.database.write("UPDATE fed_peer SET state='rejected'")
    elif change == "capability":
        await target.database.write("UPDATE fed_peer SET capabilities='{}'")
    elif change == "scope":
        await target.database.write("UPDATE fed_peer SET sync_incidents=0")
    elif change == "module":
        target.federation_sync.module_enabled = lambda _module: False
    else:
        await target.database.write("UPDATE incident SET lat=40,lon=-75 WHERE id=?", (imported,))
    with pytest.raises(ValueError):
        await approve(target, pending)
    assert not await target.database.read("SELECT * FROM incident_update")
    assert (await preview(target, item["uid"]))["review_token"] == pending["review_token"]


@pytest.mark.parametrize(
    "change",
    [
        "note_origin",
        "parent_origin",
        "local_parent",
        "uid",
        "kind",
        "long",
        "blank",
        "author",
        "timestamp",
        "legacy",
    ],
)
async def test_malformed_forged_or_action_notes_cannot_enter_quarantine(nodes, change):
    source, sp, _ = await nodes("remote", notes=True)
    target, tp, _ = await nodes(notes=True)
    _, item = await source_note(source, sp)
    if change == "note_origin":
        item["uid"] = item["payload"]["uid"] = "!other:note"
    elif change in {"parent_origin", "local_parent"}:
        item["payload"]["incident_uid"] = (
            "!other:parent" if change == "parent_origin" else "!local:parent"
        )
    elif change == "uid":
        item["payload"]["uid"] = "!remote:different-note"
    elif change == "kind":
        item["payload"]["kind"] = "ack"
    elif change == "long":
        item["payload"]["body"] = "x" * 501
    elif change == "blank":
        item["payload"]["body"] = "  "
    elif change == "author":
        item["payload"]["author_label"] = []
    elif change == "timestamp":
        item["payload"]["created_at"] = True
    else:
        item.pop("revision")
        item.pop("epoch")
    with pytest.raises(ValueError):
        await target.federation_sync.quarantine(tp, item, 100)
    assert not await target.database.read("SELECT * FROM fed_inbox_item")
    assert not await target.database.read("SELECT * FROM fed_revision_receipt")


async def test_revised_notes_require_fresh_review_and_reject_stale_conflicting_or_new_lineage(
    nodes,
):
    source, sp, _ = await nodes("remote", notes=True)
    target, tp, _ = await nodes(notes=True)
    incident, item = await source_note(source, sp)
    await import_parent(source, sp, target, tp, incident)
    await target.federation_sync.quarantine(tp, item, 100)
    old = await preview(target, item["uid"])
    local_uid = source.federation_sync._local_uid(item["uid"])
    await source.database.write(
        "UPDATE incident_update SET body='Revised note',created_at=1 WHERE uid=?", (local_uid,)
    )
    revised = await exported(source, sp, STREAM, local_uid)
    assert revised["revision"] > item["revision"]
    await target.federation_sync.quarantine(tp, revised, 100)
    with pytest.raises(ReviewConflict):
        await approve(target, old)
    await approve(target, await preview(target, item["uid"]))
    assert not await target.federation_sync.quarantine(tp, item, 101)
    for conflict in (copy.deepcopy(revised), {**revised, "epoch": "b" * 32}):
        conflict["payload"] = {**conflict["payload"], "body": "Conflicting note"}
        with pytest.raises(ValueError):
            await target.federation_sync.quarantine(tp, conflict, 101)
    await source.database.write(
        "UPDATE incident_update SET body='Second reviewed revision' WHERE uid=?", (local_uid,)
    )
    latest = await exported(source, sp, STREAM, local_uid)
    await target.federation_sync.quarantine(tp, latest, 102)
    await approve(target, await preview(target, latest["uid"]))
    notes = await target.database.read("SELECT body,created_at FROM incident_update")
    assert (
        len(notes) == 1
        and notes[0]["body"] == "Second reviewed revision"
        and notes[0]["created_at"] == 1
    )


async def test_reparenting_and_receipt_loss_cannot_replace_imported_note(nodes):
    source, sp, _ = await nodes("remote", notes=True)
    target, tp, _ = await nodes(notes=True)
    incident, item = await source_note(source, sp)
    await import_parent(source, sp, target, tp, incident)
    await target.federation_sync.quarantine(tp, item, 100)
    await approve(target, await preview(target, item["uid"]))
    second = await report(source, "road second incident")
    await import_parent(source, sp, target, tp, second)
    changed = {
        **item,
        "revision": item["revision"] + 100,
        "payload": {**item["payload"], "incident_uid": source.federation_sync.wire_uid(second.uid)},
    }
    await target.federation_sync.quarantine(tp, changed, 101)
    with pytest.raises(ValueError, match="reparent"):
        await approve(target, await preview(target, item["uid"]))
    await target.database.write("DELETE FROM fed_revision_receipt WHERE stream=?", (STREAM,))
    await target.database.write("DELETE FROM fed_inbox_item WHERE stream=?", (STREAM,))
    await target.federation_sync.quarantine(tp, item, 102)
    with pytest.raises(ValueError, match="already imported or stale"):
        await approve(target, await preview(target, item["uid"]))
    assert len(await target.database.read("SELECT * FROM incident_update")) == 1


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("boundary", [1, 2, 3, 4, 5])
async def test_review_note_provenance_and_both_audits_roll_back_together(
    nodes, monkeypatch, boundary, cancel
):
    source, sp, _ = await nodes("remote", notes=True)
    target, tp, _ = await nodes(notes=True)
    incident, item = await source_note(source, sp)
    await import_parent(source, sp, target, tp, incident)
    await target.federation_sync.quarantine(tp, item, 100)
    pending = await preview(target, item["uid"])
    baseline = [dict(row) for row in await target.database.read("SELECT * FROM audit_log")]
    original = Transaction.write
    calls = 0

    async def fail(tx, sql, params=()):
        nonlocal calls
        result = await original(tx, sql, params)
        calls += 1
        if calls == boundary:
            if cancel:
                asyncio.current_task().cancel()
                await asyncio.sleep(0)
            raise RuntimeError("injected note review failure")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(Transaction, "write", fail)
        with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
            await asyncio.create_task(approve(target, pending))
    assert calls == boundary
    assert not await target.database.read("SELECT * FROM incident_update")
    assert not await target.database.read(
        "SELECT * FROM incident_provenance WHERE event_kind='federation_note_imported'"
    )
    assert [dict(row) for row in await target.database.read("SELECT * FROM audit_log")] == baseline
    assert (await preview(target, item["uid"]))["review_token"] == pending["review_token"]
    await approve(target, pending)


async def test_migration_backfills_local_notes_only_and_identity_revision_survive_rollback(
    tmp_path,
):
    path = tmp_path / "pre-notes.db"
    migrations = Path(__file__).parents[2] / "src/outpost/store/migrations"
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA auto_vacuum=INCREMENTAL")
        connection.execute("PRAGMA journal_mode=WAL")
        for migration in sorted(migrations.glob("[0-9][0-9][0-9][0-9]_*.sql")):
            version = int(migration.name[:4])
            if version > 176:
                break
            connection.executescript(migration.read_text())
            connection.execute(
                "INSERT INTO schema_version(version,applied_at) VALUES(?,1)", (version,)
            )
        for number, uid in enumerate(("local-parent", "!remote:parent"), 1):
            connection.execute(
                "INSERT INTO incident(id,uid,local_ref,type,severity,title,reporter_label,"
                "origin_node,created_at,updated_at) "
                "VALUES(?,?,?,'road','caution','Test','Test','!local',1,1)",
                (number, uid, number),
            )
            connection.execute(
                "INSERT INTO incident_update(uid,incident_id,seq,author_label,kind,body,"
                "created_at) VALUES(?,?,1,'Test','update','Retained note',1)",
                (f"note{number}", number),
            )
        connection.execute(
            "INSERT INTO incident_update(uid,incident_id,seq,author_label,kind,created_at) "
            "VALUES('ack',1,2,'Test','ack',1)"
        )
        connection.commit()
    finally:
        connection.close()
    database = Database(path)
    await database.open()
    try:
        heads = [
            dict(row)
            for row in await database.read("SELECT * FROM fed_revision WHERE stream=?", (STREAM,))
        ]
        assert [row["uid"] for row in heads] == ["note1"]
        with pytest.raises(RuntimeError):
            async with database.transaction() as tx:
                await tx.write(
                    "INSERT INTO incident_update(uid,incident_id,seq,author_label,kind,body,"
                    "created_at) VALUES('rolledback',1,3,'Test','update','Pending',1)"
                )
                assert await tx.read("SELECT * FROM fed_revision WHERE uid='rolledback'")
                raise RuntimeError("rollback")
        assert not await database.read("SELECT * FROM fed_revision WHERE uid='rolledback'")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            await database.write("UPDATE incident_update SET incident_id=2 WHERE uid='note1'")
        await database.write(
            "UPDATE incident_update SET body='Redacted author',author_label='Deleted member' "
            "WHERE uid='note1'"
        )
        advanced = [
            dict(row)
            for row in await database.read("SELECT * FROM fed_revision WHERE stream=?", (STREAM,))
        ]
        assert advanced[0]["revision"] > heads[0]["revision"]
        await database.write("DELETE FROM incident_update WHERE uid='note1'")
        deleted = [
            dict(row)
            for row in await database.read("SELECT * FROM fed_revision WHERE stream=?", (STREAM,))
        ]
        assert deleted[0]["revision"] > advanced[0]["revision"]
    finally:
        await database.close()
    database = Database(path)
    await database.open()
    try:
        assert [
            dict(row)
            for row in await database.read("SELECT * FROM fed_revision WHERE stream=?", (STREAM,))
        ] == deleted
    finally:
        await database.close()
