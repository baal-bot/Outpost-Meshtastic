"""Guarded admission through real app/governor wiring, exclusively temporary stores."""

import asyncio
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest

from outpost.app import OutpostApp
from outpost.fed.framing import MessageType
from outpost.fed.incident_sender import GUARD, IncidentSender
from outpost.store import Database, Transaction
from outpost.store.backups import BackupService
from outpost.store.database import PostCommitError
from tests.integration.test_federation_incident_events import negotiate
from tests.integration.test_federation_incident_notes import source_note
from tests.integration.test_federation_item_failures import flush_radio, oversized_body, wire
from tests.integration.test_federation_revisions import nodes as nodes
from tests.integration.test_federation_revisions import report

pytestmark = pytest.mark.production_wiring


async def prepare(nodes, *, notes=False):
    source, sp, _ = await nodes(notes=notes)
    target, tp, _ = await nodes("remote", notes=notes)
    sp, tp = await negotiate(source, sp), await negotiate(target, tp)
    incident = await report(source)
    await source.federation_sync.incident_handoff.stage(sp.id)
    return source, sp, target, tp, incident


async def saved(app):
    return [dict(row) for row in await app.database.read("SELECT * FROM fed_incident_dispatch")]


async def receipt(app):
    row = (await saved(app))[0]
    return {
        "mode": 1,
        "state": "stored",
        "mesh_id": row["peer_mesh_id"],
        "target_mesh_id": row["producer_mesh_id"],
        "stream": row["stream"],
        "uid": row["producer_mesh_id"] + ":" + row["uid"],
        **{key: row[key] for key in ("epoch", "revision", "digest")},
    }


async def accept_receipt(app, peer, value, now):
    # Direct domain tests carry the same synthetic key used by the wire fixture.
    return await app.incident_sender.receive(
        peer,
        value,
        now,
        authenticated_secret=bytes(range(32)),
    )


async def test_sender_to_receiver_and_exact_receipt_never_imply_human_acceptance(nodes):
    source, sp, target, _, incident = await prepare(nodes)
    result = await source.incident_sender.admit(sp.id, "incidents", incident.uid)
    assert result.state == "queued" and 1 <= len(result.frame_ids) <= 8
    assert not source.radio.sent and not target.radio.sent
    row = (await saved(source))[0]
    assert tuple(json.loads(row["frame_ids"])) == result.frame_ids and row["stored_at"] is None
    assert all(item.guard_kind == GUARD for item in source.governor.queued_items())
    assert await source.incident_sender.admit(sp.id, "incidents", incident.uid) == result
    await flush_radio(source, target)
    assert (
        await source.incident_sender.admit(sp.id, "incidents", incident.uid)
    ).state == "awaiting_receipt"
    assert (await saved(source))[0]["stored_at"] is None
    inbox = (await target.database.read("SELECT * FROM fed_inbox_item"))[0]
    assert inbox["state"] == "pending"
    assert not await target.database.read("SELECT * FROM incident")
    await flush_radio(target, source)
    assert (await saved(source))[0]["stored_at"] is not None
    assert (
        await source.incident_sender.admit(sp.id, "incidents", incident.uid, retry=True)
    ).state == "stored"
    for app in (source, target):
        assert not await app.database.read("SELECT * FROM alert")
        assert not await app.database.read("SELECT * FROM fed_post_delivery")


@pytest.mark.parametrize("fault", ["association", "cancel", "queue_full", "oversized", "counter"])
async def test_admission_failure_rolls_back_counter_frames_and_association(
    nodes, monkeypatch, fault
):
    source, sp, _, _, incident = await prepare(nodes)
    original = Transaction.write
    if fault in {"association", "cancel"}:

        async def fail(tx, sql, params=()):
            if sql.startswith("INSERT INTO fed_incident_dispatch"):
                raise asyncio.CancelledError() if fault == "cancel" else RuntimeError("injected")
            return await original(tx, sql, params)

        monkeypatch.setattr(Transaction, "write", fail)
    elif fault == "queue_full":
        source.governor.config.queue_max_items = 0
    elif fault == "oversized":
        await source.database.write(
            "UPDATE incident SET body=? WHERE id=?", (oversized_body(), incident.id)
        )
        await source.federation_sync.incident_handoff.stage(sp.id)
    else:
        await source.database.write("UPDATE fed_peer SET tx_counter=4294967295")
    before = (await source.database.read("SELECT tx_counter FROM fed_peer"))[0][0]
    with pytest.raises((ValueError, RuntimeError, asyncio.CancelledError)):
        await source.incident_sender.admit(sp.id, "incidents", incident.uid)
    assert not await saved(source) and not source.governor.queued_items()
    assert not await source.database.read("SELECT * FROM outbound_work")
    assert (await source.database.read("SELECT tx_counter FROM fed_peer"))[0][0] == before


async def test_admission_uses_writer_only_and_supersession_waits_for_commit(nodes, monkeypatch):
    source, sp, _, _, incident = await prepare(nodes)
    first = await source.incident_sender.admit(sp.id, "incidents", incident.uid)
    await source.incidents.operator_patch(
        incident.id, status="monitoring", severity=None, resolution=None, actor="web:test"
    )
    await source.federation_sync.incident_handoff.stage(sp.id)
    original = Transaction.write

    async def write(tx, sql, params=()):
        if sql.startswith("INSERT INTO fed_incident_dispatch"):
            assert tuple(i.item_id for i in source.governor.queued_items()) == first.frame_ids
        return await original(tx, sql, params)

    async def forbidden(*args, **kwargs):
        pytest.fail("escaped admission writer")

    with monkeypatch.context() as patch:
        patch.setattr(source.database, "read", forbidden)
        patch.setattr(Transaction, "write", write)
        second = await source.incident_sender.admit(sp.id, "incidents", incident.uid)
    assert second.counter == first.counter + 1
    assert tuple(i.item_id for i in source.governor.queued_items()) == second.frame_ids
    assert len(await saved(source)) == 1
    assert all(
        row[0] == "superseded"
        for row in await source.database.read(
            "SELECT state FROM outbound_work WHERE id<?", (second.frame_ids[0],)
        )
    )


@pytest.mark.parametrize(
    "change",
    [
        "source",
        "secret",
        "revoke",
        "offline",
        "scope",
        "epoch",
        "identity",
        "module",
        "head",
        "association",
        "guard",
        "routing",
    ],
)
@pytest.mark.parametrize("recover", [False, True])
async def test_dispatch_rechecks_current_authority_after_admission_and_recovery(
    nodes, change, recover
):
    source, sp, target, _, incident = await prepare(nodes)
    await source.incident_sender.admit(sp.id, "incidents", incident.uid)
    if change == "source":
        await source.incidents.operator_patch(
            incident.id, status="resolved", severity=None, resolution="Checked", actor="web:test"
        )
    elif change == "secret":
        await source.database.write("UPDATE fed_peer SET shared_secret=?", (b"x" * 32,))
    elif change == "revoke":
        await source.database.write("UPDATE fed_peer SET sync_incidents=0")
    elif change == "offline":
        await source.database.write("UPDATE fed_peer SET last_seen_at=0")
    elif change == "scope":
        await source.database.write("UPDATE fed_peer SET incident_radius_km=1")
    elif change == "epoch":
        await source.database.write("UPDATE fed_revision_lineage SET epoch=?", ("f" * 32,))
    elif change == "identity":
        source.federation_sync.local_mesh_id = "!changed"
    elif change == "module":
        source.config.modules.watch.enabled = False
    elif change == "head":
        await source.database.write("DELETE FROM fed_revision WHERE uid=?", (incident.uid,))
    elif change == "association":
        await source.database.write("DELETE FROM fed_incident_dispatch")
    elif change == "guard":
        source.governor.outbox.attempt_guards.clear()
    else:
        source.config.radio.federation_portnum = 261
    if recover:
        await source.governor.recover()
    await flush_radio(source, target)
    assert not source.radio.sent
    assert not await source.database.read("SELECT * FROM outbound_attempt")
    assert all(
        row[0] == "failed" for row in await source.database.read("SELECT state FROM outbound_work")
    )


async def test_restart_and_backup_preserve_association_and_guarded_work(nodes):
    source, sp, target, _, incident = await prepare(nodes)
    result = await source.incident_sender.admit(sp.id, "incidents", incident.uid)
    before = await saved(source)
    backup = await BackupService(source.database).create()
    recovered = Database(backup)
    await recovered.open()
    try:
        assert [
            dict(r) for r in await recovered.read("SELECT * FROM fed_incident_dispatch")
        ] == before
        assert all(
            r[0] == GUARD for r in await recovered.read("SELECT guard_kind FROM outbound_work")
        )
    finally:
        await recovered.close()
    await source.database.close()
    restarted = OutpostApp(source.config, clock=source.clock, radio=source.radio)
    await restarted.database.open()
    restarted.federation_sync.local_mesh_id = "!local"
    try:
        assert await restarted.governor.recover() == len(result.frame_ids)
        await flush_radio(restarted, target)
        await flush_radio(target, restarted)
        assert (await saved(restarted))[0]["stored_at"] is not None
    finally:
        await restarted.ai_service.close()
        await restarted.database.close()


async def test_lost_receipt_requires_explicit_fresh_counter_and_duplicate_storage_is_idempotent(
    nodes,
):
    source, sp, target, _, incident = await prepare(nodes)
    first = await source.incident_sender.admit(sp.id, "incidents", incident.uid)
    await flush_radio(source, target)
    inbox = dict((await target.database.read("SELECT * FROM fed_inbox_item"))[0])
    for item in target.governor.queued_items():
        await target.governor.cancel_work(item.item_id)  # Simulated dropped application receipt.
    assert (
        await source.incident_sender.admit(sp.id, "incidents", incident.uid)
    ).state == "awaiting_receipt"
    second = await source.incident_sender.admit(sp.id, "incidents", incident.uid, retry=True)
    assert second.counter > first.counter and second.frame_ids != first.frame_ids
    await flush_radio(source, target)
    assert dict((await target.database.read("SELECT * FROM fed_inbox_item"))[0]) == inbox
    await flush_radio(target, source)
    old = await saved(source)
    assert await accept_receipt(source, sp, await receipt(source), 123)
    assert await saved(source) == old


@pytest.mark.parametrize("admit_new", [False, True])
async def test_old_receipt_cannot_clear_newer_intent_or_newer_dispatch(nodes, admit_new):
    source, sp, _, _, incident = await prepare(nodes)
    await source.incident_sender.admit(sp.id, "incidents", incident.uid)
    old_receipt = await receipt(source)
    await source.incidents.operator_patch(
        incident.id, status="monitoring", severity=None, resolution=None, actor="web:test"
    )
    await source.federation_sync.incident_handoff.stage(sp.id)
    intents = await source.federation_sync.incident_handoff.pending(sp.id)
    if admit_new:
        await source.incident_sender.admit(sp.id, "incidents", incident.uid)
    assert await accept_receipt(source, sp, old_receipt, 100) is not admit_new
    assert await source.federation_sync.incident_handoff.pending(sp.id) == intents
    assert (await source.incident_sender.admit(sp.id, "incidents", incident.uid)).state == "queued"
    assert (await saved(source))[0]["stored_at"] is None


@pytest.mark.parametrize(
    "change",
    ["target", "sender", "mode", "epoch", "revision", "digest", "state", "stream", "uid", "fields"],
)
async def test_malformed_receipts_never_change_association(nodes, change):
    source, sp, target, _, incident = await prepare(nodes)
    await source.incident_sender.admit(sp.id, "incidents", incident.uid)
    value = await receipt(source)
    key, replacement = {
        "target": ("target_mesh_id", "!other"),
        "sender": ("mesh_id", "!other"),
        "mode": ("mode", True),
        "epoch": ("epoch", "bad"),
        "revision": ("revision", True),
        "digest": ("digest", "x" * 64),
        "state": ("state", "reviewed"),
        "stream": ("stream", []),
        "uid": ("uid", "!other:wrong"),
        "fields": ("extra", 1),
    }[change]
    value[key] = replacement
    before = await saved(source)
    with pytest.raises(ValueError):
        await accept_receipt(source, sp, value, 100)
    if change != "sender":  # wire() deliberately inserts the real authenticated sender ID.
        await wire(target, source, MessageType.INCIDENT_RECEIPT, value)
    assert await saved(source) == before


async def test_note_waits_for_exact_current_parent_and_rechecks_before_transmission(nodes):
    source, sp, target, _, incident = await prepare(nodes, notes=True)
    _, note = await source_note(source, sp, incident)
    note_uid = source.federation_sync._local_uid(note["uid"])
    await source.federation_sync.incident_handoff.stage(sp.id)
    with pytest.raises(ValueError, match="parent storage"):
        await source.incident_sender.admit(sp.id, "incident_updates", note_uid)
    await source.incident_sender.admit(sp.id, "incidents", incident.uid)
    await flush_radio(source, target)
    await flush_radio(target, source)
    await source.incident_sender.admit(sp.id, "incident_updates", note_uid)
    sent_before = len(source.radio.sent)
    await source.incidents.operator_patch(
        incident.id, status="resolved", severity=None, resolution="Checked", actor="web:test"
    )
    await flush_radio(source, target)
    assert len(source.radio.sent) == sent_before


async def test_exact_parent_and_note_can_both_be_stored_without_import(nodes):
    source, sp, target, _, incident = await prepare(nodes, notes=True)
    _, note = await source_note(source, sp, incident)
    await source.federation_sync.incident_handoff.stage(sp.id)
    for stream, uid in (
        ("incidents", incident.uid),
        ("incident_updates", source.federation_sync._local_uid(note["uid"])),
    ):
        await source.incident_sender.admit(sp.id, stream, uid)
        await flush_radio(source, target)
        await flush_radio(target, source)
    assert all(row["stored_at"] is not None for row in await saved(source))
    assert len(await target.database.read("SELECT * FROM fed_inbox_item")) == 2
    assert not await target.database.read("SELECT * FROM incident_update")


@pytest.mark.parametrize("field", ["guard_kind", "binary_payload", "dest", "traffic_class"])
async def test_mutated_queue_candidate_cannot_bypass_durable_authorization(nodes, field):
    from outpost.transport.models import TrafficClass

    source, sp, _, _, incident = await prepare(nodes)
    await source.incident_sender.admit(sp.id, "incidents", incident.uid)
    candidate = source.governor.queued_items()[0]
    setattr(
        candidate,
        field,
        {
            "guard_kind": None,
            "binary_payload": b"bad",
            "dest": "!other",
            "traffic_class": TrafficClass.ALERT,
        }[field],
    )
    # Moving classes behind the scheduler is unsupported; keep the queue consistent
    # here to specifically exercise the durable authorization boundary.
    if field == "traffic_class":
        source.governor.queues[TrafficClass.FEDERATION].remove(candidate)
        source.governor.queues[TrafficClass.ALERT].append(candidate)
    await source.governor.tick()
    assert not source.radio.sent
    assert (
        await source.database.read(
            "SELECT state FROM outbound_work WHERE id=?", (candidate.item_id,)
        )
    )[0][0] == "failed"


async def test_upgrade_adds_guard_and_empty_associations_without_modifying_old_work(tmp_path):
    path = tmp_path / "upgrade.db"
    create_legacy(path)
    database = Database(path)
    await database.open()
    try:
        assert not await database.read("SELECT * FROM fed_incident_dispatch")
        assert "guard_kind" in {
            row["name"] for row in await database.read("PRAGMA table_info(outbound_work)")
        }
        assert (await database.read("PRAGMA integrity_check"))[0][0] == "ok"
        assert not await database.read("PRAGMA foreign_key_check")
        legacy = (await database.read("SELECT uid,state,guard_kind FROM outbound_work"))[0]
        assert tuple(legacy) == ("legacy-work", "pending", None)
    finally:
        await database.close()


def create_legacy(path):
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA auto_vacuum=INCREMENTAL")
        connection.execute("PRAGMA journal_mode=WAL")
        for migration in sorted(Path("src/outpost/store/migrations").glob("*.sql")):
            version = int(migration.name.split("_")[0])
            if version >= 180:
                break
            connection.executescript(migration.read_text())
            connection.execute(
                "INSERT INTO schema_version(version,applied_at) VALUES(?,1)", (version,)
            )
        connection.execute(
            "INSERT INTO outbound_work(uid,state,destination,channel,traffic_class,severity,"
            "want_ack,created_at,expires_at,dedupe_hash) "
            "VALUES('legacy-work','pending','!synthetic',0,'reply','info',1,1,9999999999,'legacy')"
        )
        connection.commit()


async def test_transport_retry_rechecks_policy_and_never_claims_storage(nodes, monkeypatch):
    source, sp, target, _, incident = await prepare(nodes)
    await source.incident_sender.admit(sp.id, "incidents", incident.uid)

    async def fail(*args, **kwargs):
        raise OSError("simulated disconnected transport")

    monkeypatch.setattr(source.radio, "_send_data", fail)
    await source.governor.tick()
    assert len(await source.database.read("SELECT * FROM outbound_attempt")) == 1
    await source.database.write("UPDATE fed_peer SET sync_incidents=0")
    await flush_radio(source, target)
    assert len(await source.database.read("SELECT * FROM outbound_attempt")) == 1
    assert (await saved(source))[0]["stored_at"] is None


async def test_selected_attempt_waits_for_writer_and_observes_revocation(nodes, monkeypatch):
    source, sp, _, _, incident = await prepare(nodes)
    await source.incident_sender.admit(sp.id, "incidents", incident.uid)
    selected = asyncio.Event()
    proceed = asyncio.Event()
    original = source.governor.outbox.start_attempt

    async def attempt(*args, **kwargs):
        selected.set()
        await proceed.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(source.governor.outbox, "start_attempt", attempt)
    source.governor._next_outbox_sweep_at = float("inf")
    task = asyncio.create_task(source.governor.tick())
    try:
        await asyncio.wait_for(selected.wait(), 5)
        async with source.database.transaction() as tx:
            proceed.set()
            await asyncio.sleep(0)
            await tx.write("UPDATE fed_peer SET sync_incidents=0")
        await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert not source.radio.sent
    assert not await source.database.read("SELECT * FROM outbound_attempt")


@pytest.mark.parametrize("phase", ["admit", "receipt"])
@pytest.mark.parametrize("change", ["identity", "module"])
async def test_runtime_change_at_final_write_rolls_back(nodes, monkeypatch, phase, change):
    source, sp, _, _, incident = await prepare(nodes)
    if phase == "receipt":
        await source.incident_sender.admit(sp.id, "incidents", incident.uid)
        value = await receipt(source)
    before = await saved(source)
    original = Transaction.write

    async def changing(tx, sql, params=()):
        result = await original(tx, sql, params)
        if (phase == "admit" and sql.startswith("INSERT INTO fed_incident_dispatch")) or (
            phase == "receipt" and sql.startswith("UPDATE outbound_work SET state='cancelled'")
        ):
            if change == "identity":
                source.federation_sync.local_mesh_id = "!changed"
            else:
                source.config.modules.watch.enabled = False
        return result

    monkeypatch.setattr(Transaction, "write", changing)
    with pytest.raises(ValueError, match="runtime policy"):
        if phase == "admit":
            await source.incident_sender.admit(sp.id, "incidents", incident.uid)
        else:
            await accept_receipt(source, sp, value, 100)
    assert await saved(source) == before
    assert bool(source.governor.queued_items()) is (phase == "receipt")


async def test_receipt_cancels_only_unsent_frames_and_keeps_source_intent(nodes):
    source, sp, _, _, incident = await prepare(nodes)
    result = await source.incident_sender.admit(sp.id, "incidents", incident.uid)
    await source.governor.tick()
    assert len(source.radio.sent) == 1
    assert await accept_receipt(source, sp, await receipt(source), 100)
    assert not source.governor.queued_items()
    states = [
        row[0] for row in await source.database.read("SELECT state FROM outbound_work ORDER BY id")
    ]
    assert states == ["sent"] + ["cancelled"] * (len(result.frame_ids) - 1)
    assert len(await source.federation_sync.incident_handoff.pending(sp.id)) == 1


@pytest.mark.parametrize("change", ["peer", "secret", "unknown", "empty_uid", "identity"])
async def test_receipts_bind_current_peer_secret_and_retained_dispatch(nodes, change):
    source, sp, _, _, incident = await prepare(nodes)
    await source.incident_sender.admit(sp.id, "incidents", incident.uid)
    value = await receipt(source)
    if change == "peer":
        await source.database.write("UPDATE fed_peer SET sync_incidents=0")
    elif change == "secret":
        await source.database.write("UPDATE fed_peer SET shared_secret=?", (b"s" * 32,))
    elif change == "unknown":
        value["uid"] += "-unknown"
    elif change == "empty_uid":
        value["uid"] = "!local:"
    else:
        source.federation_sync.local_mesh_id = "!changed"
    if change in {"peer", "empty_uid", "identity"}:
        with pytest.raises(ValueError):
            await accept_receipt(source, sp, value, 100)
    else:
        assert not await accept_receipt(source, sp, value, 100)
    assert (await saved(source))[0]["stored_at"] is None


async def test_expired_work_requires_explicit_readmission_not_a_delivery_claim(nodes):
    source, sp, _, _, incident = await prepare(nodes)
    first = await source.incident_sender.admit(sp.id, "incidents", incident.uid)
    source.clock.advance(1801)
    await source.governor.tick()
    assert (
        await source.incident_sender.admit(sp.id, "incidents", incident.uid)
    ).state == "retry_required"
    retry = await source.incident_sender.admit(sp.id, "incidents", incident.uid, retry=True)
    assert retry.state == "queued" and retry.counter > first.counter
    assert not source.radio.sent


@pytest.mark.parametrize("phase", ["uncommitted", "committed"])
async def test_killed_sender_recovers_atomic_counter_association_and_frames(nodes, phase):
    source, sp, target, _, incident = await prepare(nodes)
    script = """
import asyncio, sys
from datetime import UTC, datetime
from outpost.app import OutpostApp
from outpost.clock import VirtualClock
from outpost.config import Config
from outpost.store import Transaction
from outpost.transport.simulated import SimulatedRadioLink
async def main():
    clock = VirtualClock(epoch=datetime(2026,1,1,12,tzinfo=UTC))
    config = Config.model_validate({"store":{"path":sys.argv[1]},
        "modules":{"fed":{"enabled":True},"watch":{"enabled":True}}})
    app = OutpostApp(config, clock=clock, radio=SimulatedRadioLink(clock,node_id="!local"))
    await app.database.open()
    app.federation_sync.local_mesh_id = "!local"
    peer = await app.federation.by_mesh_id("!remote")
    if sys.argv[3] == "uncommitted":
        original = Transaction.write
        async def pause(tx,sql,params=()):
            result = await original(tx,sql,params)
            if sql.startswith("INSERT INTO fed_incident_dispatch"):
                print("uncommitted",flush=True)
                await asyncio.Event().wait()
            return result
        Transaction.write = pause
    await app.incident_sender.admit(peer.id,"incidents",sys.argv[2])
    print("committed",flush=True)
    await asyncio.Event().wait()
asyncio.run(main())
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        str(source.database.path),
        incident.uid,
        phase,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        assert (await asyncio.wait_for(process.stdout.readline(), 30)).decode().strip() == phase
        process.kill()  # Only this test-owned process, with an unconnected simulated radio.
        await asyncio.wait_for(process.wait(), 10)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        await process.communicate()
    assert process.returncode == -9
    assert (await source.database.read("PRAGMA integrity_check"))[0][0] == "ok"
    assert len(await saved(source)) == int(phase == "committed")
    assert (await source.database.read("SELECT tx_counter FROM fed_peer"))[0][0] == int(
        phase == "committed"
    )
    await source.governor.recover()
    await source.incident_sender.admit(sp.id, "incidents", incident.uid)
    await flush_radio(source, target)
    await flush_radio(target, source)
    assert (await saved(source))[0]["stored_at"] is not None


@pytest.mark.parametrize("phase", ["admit", "receipt"])
async def test_commit_cancellation_preserves_association_and_publication(nodes, monkeypatch, phase):
    source, sp, _, _, incident = await prepare(nodes)
    if phase == "receipt":
        await source.incident_sender.admit(sp.id, "incidents", incident.uid)
        value = await receipt(source)
    reached, release = asyncio.Event(), asyncio.Event()
    original = source.database._writer_call

    async def pause(operation):
        result = await original(operation)
        if operation == source.database._commit:
            reached.set()
            await release.wait()
        return result

    with monkeypatch.context() as patch:
        patch.setattr(source.database, "_writer_call", pause)
        task = asyncio.create_task(
            source.incident_sender.admit(sp.id, "incidents", incident.uid)
            if phase == "admit"
            else accept_receipt(source, sp, value, 100)
        )
        try:
            await asyncio.wait_for(reached.wait(), 5)
            for _ in range(3):
                task.cancel()
                await asyncio.sleep(0)
            assert not task.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    rows = await saved(source)
    assert len(rows) == 1 and (rows[0]["stored_at"] is not None) is (phase == "receipt")
    assert bool(source.governor.queued_items()) is (phase == "admit")
    assert (await source.incident_sender.admit(sp.id, "incidents", incident.uid)).state == (
        "queued" if phase == "admit" else "stored"
    )


async def test_publication_failure_is_committed_and_recovers_without_duplicate_counter(
    nodes, monkeypatch
):
    source, sp, target, _, incident = await prepare(nodes)

    def fail(*args, **kwargs):
        raise RuntimeError("injected queue mirror failure")

    with monkeypatch.context() as patch:
        patch.setattr(source.governor, "_publish_committed", fail)
        with pytest.raises(PostCommitError):
            await source.incident_sender.admit(sp.id, "incidents", incident.uid)
    assert len(await saved(source)) == 1
    with pytest.raises(RuntimeError, match="publication failed"):
        await source.governor.tick()
    await source.governor.recover()
    assert (await source.incident_sender.admit(sp.id, "incidents", incident.uid)).counter == 1
    await flush_radio(source, target)
    await flush_radio(target, source)
    assert (await saved(source))[0]["stored_at"] is not None


@pytest.mark.parametrize("change", ["missing", "digest", "stream", "uid", "parent", "runtime"])
async def test_missing_conflicting_or_changed_inputs_cannot_be_admitted(nodes, monkeypatch, change):
    source, sp, _, _, incident = await prepare(nodes, notes=True)
    stream, uid = "incidents", incident.uid
    if change == "missing":
        uid = "not-staged"
    elif change == "digest":
        await source.database.write("UPDATE fed_incident_intent SET digest=?", ("0" * 64,))
    elif change == "stream":
        stream = "board:gen"
    elif change == "uid":
        uid = None
    elif change == "parent":
        _, note = await source_note(source, sp, incident)
        await source.federation_sync.incident_handoff.stage(sp.id)
        stream, uid = "incident_updates", source.federation_sync._local_uid(note["uid"])
        await source.database.write(
            "UPDATE fed_incident_intent SET parent_uid='!other:parent' "
            "WHERE stream='incident_updates'"
        )
    else:
        original = source.federation_sync.export_items

        async def change_runtime(*args, **kwargs):
            items = await original(*args, **kwargs)
            source.config.modules.watch.enabled = False
            return items

        monkeypatch.setattr(source.federation_sync, "export_items", change_runtime)
    with pytest.raises(ValueError):
        await source.incident_sender.admit(sp.id, stream, uid)
    assert not await saved(source) and not source.governor.queued_items()


@pytest.mark.parametrize("owner", ["outbox", "peer"])
async def test_sender_refuses_split_transaction_ownership(nodes, owner):
    source, _, _, _, _ = await prepare(nodes)
    if owner == "outbox":
        prior = source.governor.outbox
        source.governor.outbox = None
    else:
        prior = source.federation.database
        source.federation.database = object()
    try:
        with pytest.raises(ValueError, match="shared"):
            IncidentSender(
                source.federation_sync,
                source.federation,
                source.governor,
                source.federation_codec,
                lambda: "!local",
                lambda: (0, 260),
            )
    finally:
        if owner == "outbox":
            source.governor.outbox = prior
        else:
            source.federation.database = prior


async def test_association_lookups_are_indexed_and_peer_deletion_cannot_release_work(nodes):
    source, sp, target, _, incident = await prepare(nodes)
    await source.incident_sender.admit(sp.id, "incidents", incident.uid)
    key = (await saved(source))[0]["queue_key"]
    for table in ("fed_incident_dispatch", "outbound_work"):
        plan = await source.database.read(
            f"EXPLAIN QUERY PLAN SELECT * FROM {table} WHERE queue_key=?",  # noqa: S608 -- fixed tables
            (key,),
        )
        detail = " ".join(row["detail"] for row in plan)
        assert "SEARCH" in detail and "SCAN" not in detail
    await source.database.write("DELETE FROM fed_peer WHERE id=?", (sp.id,))
    assert not await saved(source)
    await source.governor.recover()
    await flush_radio(source, target)
    assert not source.radio.sent


@pytest.mark.parametrize("phase", ["before_guard", "during_guard"])
async def test_queue_deadline_is_current_after_awaited_reservation_validation(
    nodes, monkeypatch, phase
):
    source, sp, _, _, incident = await prepare(nodes)
    result = await source.incident_sender.admit(sp.id, "incidents", incident.uid)
    selected, release = asyncio.Event(), asyncio.Event()
    owner = source.governor.outbox if phase == "before_guard" else source.incident_sender
    method = "start_attempt" if phase == "before_guard" else "_current"
    original = getattr(owner, method)

    async def delayed(*args, **kwargs):
        validated = await original(*args, **kwargs) if phase == "during_guard" else None
        selected.set()
        await release.wait()
        return validated if phase == "during_guard" else await original(*args, **kwargs)

    monkeypatch.setattr(owner, method, delayed)
    task = asyncio.create_task(source.governor.tick())
    try:
        await asyncio.wait_for(selected.wait(), 5)
        source.clock.advance(1801)
        release.set()
        await task
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert not source.radio.sent
    assert not await source.database.read("SELECT * FROM outbound_attempt")
    work = (
        await source.database.read(
            "SELECT state,last_error FROM outbound_work WHERE id=?", (result.frame_ids[0],)
        )
    )[0]
    assert tuple(work) == ("failed", "dispatch authorization denied")


async def test_old_authenticated_key_cannot_credit_new_key_association(nodes, monkeypatch):
    source, sp, target, _, incident = await prepare(nodes)
    await source.incident_sender.admit(sp.id, "incidents", incident.uid)
    value = await receipt(source)
    original = source.federation.accept_counter
    new_key = b"k" * 32

    async def rotate_after_auth(*args, **kwargs):
        accepted = await original(*args, **kwargs)
        await source.database.write("UPDATE fed_peer SET shared_secret=?", (new_key,))
        await source.incident_sender.admit(sp.id, "incidents", incident.uid)
        return accepted

    with monkeypatch.context() as patch:
        patch.setattr(source.federation, "accept_counter", rotate_after_auth)
        await wire(target, source, MessageType.INCIDENT_RECEIPT, value)
    rows = await saved(source)
    assert rows[0]["counter"] == 2 and rows[0]["stored_at"] is None
    assert source.governor.queued_items()  # An old-key receipt did not cancel new work.
    # A governed fresh reply authenticated with the new key still succeeds.
    await target.database.write("UPDATE fed_peer SET shared_secret=?", (new_key,))
    await target._send_federation_value(
        source.radio.local_node_id,
        MessageType.INCIDENT_RECEIPT,
        value,
    )
    await flush_radio(target, source)
    assert (await saved(source))[0]["stored_at"] is not None


@pytest.mark.parametrize("verified_key", [None, b"wrong key"])
async def test_receipt_needs_the_actual_verified_key(nodes, verified_key):
    source, sp, _, _, incident = await prepare(nodes)
    await source.incident_sender.admit(sp.id, "incidents", incident.uid)
    value = await receipt(source)
    if verified_key is None:
        with pytest.raises(ValueError, match="verified authentication key"):
            await source.incident_sender.receive(sp, value, 100, authenticated_secret=None)
    else:
        assert not await source.incident_sender.receive(
            sp,
            value,
            100,
            authenticated_secret=verified_key,
        )
    assert (await saved(source))[0]["stored_at"] is None


async def test_receipt_fragments_cannot_mix_across_key_rotation(nodes):
    from outpost.transport.models import InboundMessage

    source, sp, target, _, incident = await prepare(nodes)
    await source.incident_sender.admit(sp.id, "incidents", incident.uid)
    value = await receipt(source)
    counter = await target.federation.next_counter(source.radio.local_node_id)
    old = target.federation_codec.encode(
        MessageType.INCIDENT_RECEIPT, value, counter, bytes(range(32))
    )
    assert len(old) > 1

    async def deliver(frame):
        await source._handle_federation_discovery(
            InboundMessage(
                counter,
                target.radio.local_node_id,
                "^all",
                0,
                260,
                False,
                None,
                frame,
                source.clock.now(),
            )
        )

    await deliver(old[0])
    new_key = b"k" * 32
    await source.database.write("UPDATE fed_peer SET shared_secret=?", (new_key,))
    await source.incident_sender.admit(sp.id, "incidents", incident.uid)
    new = target.federation_codec.encode(MessageType.INCIDENT_RECEIPT, value, counter, new_key)
    for frame in new[1:]:
        await deliver(frame)
    assert (await saved(source))[0]["stored_at"] is None
    await deliver(new[0])
    assert (await saved(source))[0]["stored_at"] is not None
