"""Source intents only: isolated stores, production services, no RF deployment."""

import asyncio
import sqlite3
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path

import pytest

from outpost.store import Database
from outpost.watch.change_events import MAX_PENDING, IncidentChangeEvents
from tests.integration.test_federation_incident_notes import (
    approve,
    import_parent,
    preview,
    source_note,
)
from tests.integration.test_federation_revisions import nodes as nodes
from tests.integration.test_federation_revisions import report
from tests.integration.test_incident_transactions import interrupt_after_write

pytestmark = pytest.mark.production_wiring


async def assert_heads(app):
    events = await app.incidents.change_events.pending()
    heads = await app.database.read(
        "SELECT stream,uid,revision FROM fed_revision "
        "WHERE stream IN ('incidents','incident_updates')"
    )
    assert {(e.stream, e.uid, e.revision) for e in events} == {tuple(r) for r in heads}
    assert all(e.state == "pending" for e in events)
    return {(e.stream, e.uid): e for e in events}


async def test_changes_coalesce_atomically_without_radio_or_wall_clock_order(nodes):
    app, _, controls = await nodes()
    first = await report(app, "road first bridge obstruction")
    initial = (await app.incidents.change_events.pending())[0]
    second = await report(app, "road second bridge obstruction")
    for step, state in zip(
        (-21600, 43200, -43200), ("monitoring", "resolved", "open"), strict=True
    ):
        app.clock.epoch += timedelta(seconds=step)
        await app.incidents.operator_patch(
            first.id, status=state, severity=None, resolution="Checked", actor="web:test"
        )
        pending = await app.incidents.change_events.pending()
        assert [e.uid for e in pending] == [first.uid, second.uid]
        assert pending[0].first_revision == initial.first_revision
        assert pending[0].revision > initial.revision
        await assert_heads(app)
    await app.incidents.operator_update(first.id, "update", "Synthetic note")
    note = (await app.database.read("SELECT uid FROM incident_update WHERE kind='update'"))[0]
    await app.incidents.operator_location(first.id, "-share 10.0 20.0", actor="web:test")
    events = await assert_heads(app)
    assert ("incident_updates", note["uid"]) in events
    assert len(events) == 4  # two incidents, plain note, explicit location-change note
    assert set(asdict(initial)) == {"stream", "uid", "epoch", "revision", "first_revision", "state"}
    assert not controls
    assert not await app.database.read("SELECT * FROM outbound_work")
    assert not await app.database.read("SELECT * FROM fed_revision_receipt")


async def test_merge_unmerge_expiry_and_retention_update_the_same_intents(nodes):
    app, _, _ = await nodes()
    first = await report(app, "fire smoke at shed 10.0 20.0")
    second = await report(app, "fire smoke at shed 10.0001 20.0001")
    await app.incidents.operator_update(first.id, "update", "Synthetic crew report")
    before = await assert_heads(app)
    await app.incidents.merge(second.id, first.id, "web:test")
    merged = await assert_heads(app)
    assert all(
        merged[key].revision > value.revision
        for key, value in before.items()
        if key[0] == "incidents"
    )
    await app.incidents.unmerge(second.id, "web:test")
    unmerged = await assert_heads(app)
    assert all(
        unmerged[key].revision > value.revision
        for key, value in merged.items()
        if key[0] == "incidents"
    )
    app.clock.advance(12 * 3600 + 1)
    assert len(await app.incidents.expire_due()) == 2
    expired = await assert_heads(app)
    assert all(
        expired[key].revision > value.revision
        for key, value in unmerged.items()
        if key[0] == "incidents"
    )
    # Retention must leave metadata-only work, not a fabricated remote withdrawal.
    await app.database.write("DELETE FROM incident")
    deleted = await assert_heads(app)
    assert set(deleted) == set(before)
    assert all(deleted[key].revision > value.revision for key, value in expired.items())
    assert all(deleted[key].first_revision == value.first_revision for key, value in before.items())
    assert not await app.database.read("SELECT * FROM incident_update")
    assert len(await app.database.read("SELECT * FROM incident_reference")) == 2


async def test_imports_journal_parent_changes_but_not_foreign_notes_or_review_stages(nodes):
    source, sp, _ = await nodes("remote", notes=True)
    target, tp, _ = await nodes(notes=True)
    parent, note = await source_note(source, sp)
    assert not await target.incidents.change_events.pending()
    await target.federation_sync.quarantine(tp, note, 100)
    assert not await target.incidents.change_events.pending()  # quarantine != import
    imported_id = await import_parent(source, sp, target, tp, parent)
    imported = await assert_heads(target)
    assert len(imported) == 1
    assert next(iter(imported))[1].startswith("!remote:")
    await target.incidents.operator_update(imported_id, "ack", actor="web:local")
    before = await assert_heads(target)
    await approve(target, await preview(target, note["uid"]))
    assert await assert_heads(target) == before
    assert (await target.incidents.by_id(imported_id)).status == "monitoring"
    assert all(e.stream == "incidents" for e in before.values())


@pytest.mark.parametrize("boundary", [1, 2, 3])
@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("operation", ["create", "note"])
async def test_interrupted_mutation_rolls_back_intent_and_recovers_after_reopen(
    nodes, monkeypatch, boundary, cancel, operation
):
    app, _, _ = await nodes()
    incident = await report(app) if operation == "note" else None
    before = await app.incidents.change_events.pending()
    with monkeypatch.context() as patch:
        interrupt_after_write(patch, app.database, boundary, cancel)
        work = (
            app.incidents.operator_update(incident.id, "update", "Interrupted")
            if incident
            else app.incidents.create("road interrupted obstruction", None)
        )
        with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
            await asyncio.create_task(work)
    assert await app.incidents.change_events.pending() == before
    reopened = Database(app.database.path)
    await reopened.open()
    try:
        assert await IncidentChangeEvents(reopened).pending() == before
        assert len(await reopened.read("SELECT * FROM incident")) == int(incident is not None)
        assert not await reopened.read("SELECT * FROM incident_update")
    finally:
        await reopened.close()
    await report(app, "road recovered report")
    await assert_heads(app)


async def test_journal_write_failure_rolls_back_source_and_reader_cannot_see_uncommitted_work(
    nodes,
):
    app, _, _ = await nodes()
    await app.database.write(
        "CREATE TRIGGER fail_intent BEFORE INSERT ON incident_change_event BEGIN "
        "SELECT RAISE(ABORT,'injected intent failure'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="injected intent failure"):
        await report(app)
    assert not await app.database.read("SELECT * FROM incident")
    assert not await app.database.read("SELECT * FROM fed_revision")
    assert not await app.incidents.change_events.pending()
    await app.database.write("DROP TRIGGER fail_intent")
    incident = await report(app)
    before = await app.incidents.change_events.pending()
    with pytest.raises(RuntimeError, match="rollback"):
        async with app.database.transaction() as tx:
            await tx.write("UPDATE incident SET title='Uncommitted' WHERE id=?", (incident.id,))
            inside = await tx.read("SELECT revision FROM incident_change_event")
            assert inside[0]["revision"] > before[0].revision
            assert await app.incidents.change_events.pending() == before
            raise RuntimeError("rollback")
    assert await app.incidents.change_events.pending() == before


async def test_concurrent_notes_and_reactions_keep_one_current_intent_per_identity(nodes):
    app, _, _ = await nodes()
    incident = await report(app)
    initial = (await app.incidents.change_events.pending())[0]
    members = [await app.router.members.resolve(f"!{i + 1:08x}") for i in range(8)]
    await asyncio.gather(
        *(app.incidents.react(incident.local_ref, member, "confirm") for member in members),
        *(
            app.incidents.operator_update(incident.id, "update", f"Synthetic note {i}")
            for i in range(8)
        ),
    )
    before = await assert_heads(app)
    assert len(before) == 9
    assert before[("incidents", incident.uid)].first_revision == initial.first_revision
    await app.database.write("UPDATE incident_update SET body='Corrected' WHERE kind='update'")
    after = await assert_heads(app)
    for key, event in before.items():
        assert after[key].first_revision == event.first_revision
        if key[0] == "incident_updates":
            assert after[key].revision > event.revision
    assert (await app.incidents.by_id(incident.id)).confirm_count == 8


@pytest.mark.parametrize(
    "damage", ["missing_head", "revision_mismatch", "lineage_mismatch", "missing_lineage"]
)
async def test_inspection_surfaces_inconsistent_heads_without_consuming_or_repairing(nodes, damage):
    app, _, _ = await nodes()
    incident = await report(app)
    before = (await app.incidents.change_events.pending())[0]
    if damage == "missing_head":
        await app.database.write("DELETE FROM fed_revision WHERE uid=?", (incident.uid,))
    elif damage == "revision_mismatch":
        await app.database.write(
            "UPDATE fed_revision SET revision=revision+100 WHERE uid=?", (incident.uid,)
        )
    elif damage == "missing_lineage":
        await app.database.write("DELETE FROM fed_revision_lineage")
    else:
        await app.database.write("UPDATE fed_revision_lineage SET epoch=?", ("f" * 32,))
    after = (await app.incidents.change_events.pending())[0]
    assert after.state == ("lineage_mismatch" if damage == "missing_lineage" else damage)
    assert {k: v for k, v in asdict(after).items() if k != "state"} == {
        k: v for k, v in asdict(before).items() if k != "state"
    }
    assert (await app.incidents.by_id(incident.id)) == incident


async def test_pending_pages_are_indexed_bounded_and_do_not_journal_other_streams(
    nodes, monkeypatch
):
    app, _, _ = await nodes()
    async with app.database.transaction() as tx:
        for i in range(MAX_PENDING + 7):
            await tx.write(
                "INSERT INTO incident(uid,local_ref,type,severity,title,reporter_label,"
                "origin_node,created_at,updated_at) "
                "VALUES(?,?,'road','caution','Synthetic','Test','!local',1,1)",
                (f"synthetic-{i}", i + 1),
            )
            await tx.write(
                "INSERT INTO fed_revision(stream,uid) VALUES('board:gen',?)", (f"p-{i}",)
            )
            await tx.write("INSERT INTO fed_revision(stream,uid) VALUES('alerts',?)", (f"a-{i}",))
    page = await app.incidents.change_events.pending()
    assert len(page) == MAX_PENDING and all(e.stream == "incidents" for e in page)
    tail = await app.incidents.change_events.pending(after=page[-1].first_revision)
    assert len(tail) == 7
    assert not await app.incidents.change_events.pending(after=tail[-1].first_revision)
    assert len(await app.incidents.change_events.pending(limit=1)) == 1
    # Repeated early-head changes keep their initial position; a fresh pass sees them.
    for _ in range(20):
        await app.database.write(
            "UPDATE incident SET updated_at=updated_at-1 WHERE uid='synthetic-0'"
        )
    first = (await app.incidents.change_events.pending(limit=1))[0]
    assert first.first_revision == page[0].first_revision
    assert first.revision > tail[-1].revision
    assert (await app.database.read("SELECT COUNT(*) n FROM incident_change_event"))[0][
        "n"
    ] == MAX_PENDING + 7
    # Inspect the actual service query, not a simplified stand-in.
    original_read = app.database.read
    plans = []

    async def explain(sql, params=()):
        plans.extend(await original_read("EXPLAIN QUERY PLAN " + sql, params))
        return await original_read(sql, params)

    with monkeypatch.context() as patch:
        patch.setattr(app.database, "read", explain)
        await app.incidents.change_events.pending(after=10, limit=2)
    details = " ".join(str(r["detail"]) for r in plans)
    assert "idx_incident_change_pending (first_revision>?)" in details
    assert "SCAN" not in details and "TEMP B-TREE" not in details


@pytest.mark.parametrize(
    "kwargs",
    [
        {"limit": 0},
        {"limit": 101},
        {"limit": True},
        {"limit": 1.5},
        {"after": -1},
        {"after": 2**63},
        {"after": True},
        {"after": "1"},
    ],
)
async def test_invalid_inspection_bounds_fail_before_query(nodes, kwargs):
    app, _, _ = await nodes()
    with pytest.raises(ValueError, match="invalid incident change"):
        await app.incidents.change_events.pending(**kwargs)


async def test_upgrade_backfills_current_heads_once_and_full_backup_preserves_intents(tmp_path):
    path = tmp_path / "upgrade.db"
    migrations = Path(__file__).parents[2] / "src/outpost/store/migrations"
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA auto_vacuum=INCREMENTAL")
        connection.execute("PRAGMA journal_mode=WAL")
        for migration in sorted(migrations.glob("[0-9][0-9][0-9][0-9]_*.sql")):
            version = int(migration.name[:4])
            if version > 177:
                break
            connection.executescript(migration.read_text())
            connection.execute(
                "INSERT INTO schema_version(version,applied_at) VALUES(?,1)", (version,)
            )
        for stream, uid in (
            ("incidents", "deleted-parent"),
            ("incident_updates", "deleted-note"),
            ("alerts", "alert"),
        ):
            connection.execute("INSERT INTO fed_revision(stream,uid) VALUES(?,?)", (stream, uid))
        connection.execute(
            "INSERT INTO incident(uid,local_ref,type,severity,title,reporter_label,origin_node,"
            "created_at,updated_at) VALUES('retained-parent',1,'road','caution','Test','Test',"
            "'!local',1,1)"
        )
        connection.execute(
            "INSERT INTO incident_update(uid,incident_id,seq,author_label,kind,body,created_at) "
            "VALUES('retained-note',1,1,'Test','update','Original',1)"
        )
        connection.execute("UPDATE incident_update SET body='Latest' WHERE uid='retained-note'")
        connection.commit()
    finally:
        connection.close()
    database = Database(path)
    await database.open()
    try:
        events = await IncidentChangeEvents(database).pending()
        assert [e.uid for e in events] == [
            "deleted-parent",
            "deleted-note",
            "retained-parent",
            "retained-note",
        ]
        assert all(e.state == "pending" and e.revision == e.first_revision for e in events)
        assert len(await database.read("SELECT * FROM incident")) == 1
        assert (await database.read("SELECT body FROM incident_update"))[0]["body"] == "Latest"
        backup = tmp_path / "restored.db"
        await database.backup(backup)
    finally:
        await database.close()
    for restored_path in (path, backup):
        reopened = Database(restored_path)
        await reopened.open()
        try:
            assert await IncidentChangeEvents(reopened).pending() == events
            assert len(await reopened.read("SELECT * FROM schema_version WHERE version=178")) == 1
        finally:
            await reopened.close()
