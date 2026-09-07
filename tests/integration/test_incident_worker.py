"""Automatic delivery through production app wiring; synthetic identities/stores only."""

import asyncio
import json
import sqlite3
from contextlib import closing
from datetime import timedelta
from pathlib import Path

import pytest

from outpost.app import OutpostApp
from outpost.fed.framing import MessageType
from outpost.fed.incident_worker import DELIVERY_TTL, IncidentWorker
from outpost.store import Database, Transaction
from outpost.store.backups import BackupService
from outpost.task_supervision import TaskFailureDomain
from outpost.transport.models import InboundMessage, TrafficClass
from tests.integration.test_federation_incident_events import negotiate
from tests.integration.test_federation_incident_notes import approve, preview
from tests.integration.test_federation_item_failures import flush_radio, oversized_body, wire
from tests.integration.test_federation_revisions import nodes as nodes
from tests.integration.test_federation_revisions import report
from tests.integration.test_incident_sender import create_legacy

pytestmark = pytest.mark.production_wiring


async def pair(nodes, *, notes=False):
    source, sp, _ = await nodes(notes=notes)
    target, tp, _ = await nodes("remote", notes=notes)
    return source, await negotiate(source, sp), target, await negotiate(target, tp)


async def test_change_after_completed_sync_automatically_reaches_remote_storage(nodes):
    source, sp, target, _ = await pair(nodes)
    now = int(source.clock.now().timestamp())
    await source.database.write("UPDATE fed_peer SET last_sync_at=?", (now,))
    incident = await report(source)
    await source._incident_delivery_once()
    work = await source.database.read("SELECT * FROM fed_incident_dispatch")
    assert len(work) == 1 and work[0]["uid"] == incident.uid
    assert work[0]["stored_at"] is None and not source.radio.sent
    await flush_radio(source, target)
    await flush_radio(target, source)
    stored = (await source.database.read("SELECT * FROM fed_incident_dispatch"))[0]
    assert stored["stored_at"] is not None
    assert (await target.database.read("SELECT state FROM fed_inbox_item"))[0][0] == "pending"
    assert not await target.database.read("SELECT * FROM incident")
    assert not await target.database.read("SELECT * FROM alert")
    assert (await source.database.read("SELECT last_sync_at FROM fed_peer WHERE id=?", (sp.id,)))[
        0
    ][0] == now


async def intents(app):
    return [dict(row) for row in await app.database.read("SELECT * FROM fed_incident_intent")]


async def pump(source, target, seconds, *, drop=None, until=None, first_poll_delay=0):
    """Both virtual clocks advance together; real framing/governor/receiver paths."""
    offsets = {id(app): len(app.radio.sent) for app in (source, target)}
    for elapsed in range(seconds):
        for app in (source, target):
            if elapsed % 5 == 0 and elapsed >= first_poll_delay:
                await app._incident_delivery_once()
            await app.governor.tick()
        for app, remote in ((source, target), (target, source)):
            while offsets[id(app)] < len(app.radio.sent):
                packet = app.radio.sent[offsets[id(app)]]
                offsets[id(app)] += 1
                assert packet.payload and len(packet.payload) <= 188
                fragment = app.federation_codec.decode_fragment(packet.payload, bytes(range(32)))
                if drop and drop(app, fragment):
                    continue
                await remote._handle_federation_discovery(
                    InboundMessage(
                        offsets[id(app)],
                        app.radio.local_node_id,
                        "^all",
                        0,
                        260,
                        False,
                        None,
                        packet.payload,
                        remote.clock.now(),
                    )
                )
        if until and await until():
            return elapsed
        for app in (source, target):
            app.clock.advance(1)
    return seconds


async def stored(app, uid):
    rows = await app.database.read(
        "SELECT d.stored_at FROM fed_incident_dispatch d JOIN fed_revision r "
        "ON r.stream=d.stream AND r.uid=d.uid AND r.revision=d.revision WHERE d.uid=?",
        (uid,),
    )
    return bool(rows and rows[0][0] is not None)


@pytest.mark.parametrize("change", ["create", "update", "resolve", "reopen"])
async def test_supported_single_peer_changes_meet_60_second_simulated_storage_target(nodes, change):
    source, _, target, _ = await pair(nodes)
    incident = await report(source)
    if change != "create":
        assert await pump(source, target, 60, until=lambda: stored(source, incident.uid)) < 60
        if change == "reopen":
            await source.incidents.operator_patch(
                incident.id,
                status="resolved",
                severity=None,
                resolution="Road cleared",
                actor="web:test",
            )
            assert await pump(source, target, 60, until=lambda: stored(source, incident.uid)) < 60
        await source.incidents.operator_patch(
            incident.id,
            status={"update": "monitoring", "resolve": "resolved", "reopen": "open"}[change],
            severity=None,
            resolution="Road cleared" if change == "resolve" else None,
            actor="web:test",
        )
    start = source.clock.monotonic()
    elapsed = await pump(source, target, 60, until=lambda: stored(source, incident.uid))
    assert elapsed < 60
    assert source.clock.monotonic() - start < 60
    remote = (await target.database.read("SELECT * FROM fed_inbox_item"))[0]
    assert remote["state"] == "pending"
    assert not await target.database.read("SELECT * FROM incident")
    assert not await target.database.read("SELECT * FROM alert")
    view = (await source.incident_delivery.status())["items"][0]
    assert view["remote_storage"] == "observed" and view["human_review"] == "not_reported"
    assert view["responder_acknowledgement"] == "not_reported"
    print(f"\nG6 synthetic {change}: exact storage receipt in {elapsed}s; human review excluded")


async def test_notes_wait_for_current_parent_then_progress_without_human_import(nodes):
    source, _, target, _ = await pair(nodes, notes=True)
    incident = await report(source)
    await source.incidents.operator_update(incident.id, "update", "Crew at bridge; use footpath.")
    note = (await source.database.read("SELECT uid FROM incident_update"))[0][0]
    elapsed = await pump(source, target, 120, until=lambda: stored(source, note))
    assert elapsed < 60
    print(f"\nG6 synthetic parent plus note: exact receipts in {elapsed}s; human review excluded")
    assert await stored(source, incident.uid)
    rows = await target.database.read("SELECT stream,state,payload_json FROM fed_inbox_item")
    assert {row["stream"] for row in rows} == {"incidents", "incident_updates"}
    assert all(row["state"] == "pending" for row in rows)
    assert not await target.database.read("SELECT * FROM incident_update")


@pytest.mark.parametrize("severity,priority", [("urgent", 20), ("critical", 30)])
async def test_fresh_urgent_head_and_backlog_each_have_a_bounded_lane(nodes, severity, priority):
    source, sp, _, _ = await pair(nodes)
    oldest = await report(source, "road oldest backlog record")
    for index in range(24):
        await report(source, f"road retained history {index}")
    urgent = await report(source, "fire latest urgent incident")
    await source.incidents.operator_patch(
        urgent.id,
        status=None,
        severity=severity,
        resolution=None,
        actor="web:test",
    )
    await source._incident_delivery_once()
    admitted = await source.database.read(
        "SELECT uid FROM fed_incident_dispatch WHERE peer_id=?", (sp.id,)
    )
    assert {row[0] for row in admitted} == {oldest.uid, urgent.uid}
    assert len(await intents(source)) <= 8
    assert {item.traffic_class for item in source.governor.queued_items()} == {
        TrafficClass.FEDERATION
    }
    assert not await source.database.read("SELECT * FROM alert")

    # The governor uses higher numeric priorities first. Admission order alone
    # does not prove that an urgent report actually precedes the backlog on air.
    first = await source.governor.tick()
    assert first is not None and first.priority == priority
    urgent_work = await source.database.read(
        "SELECT frame_ids FROM fed_incident_dispatch WHERE uid=?", (urgent.uid,)
    )
    assert first.item_id in json.loads(urgent_work[0][0])


async def test_continuous_fresh_arrivals_cannot_replenish_ahead_of_admitted_backlog(nodes):
    source, _, _, _ = await pair(nodes)
    oldest = await report(source, "road oldest backlog")
    for index in range(8):
        await report(source, f"road newer {index}")
    await source._incident_delivery_once()
    assert len(await source.database.read("SELECT * FROM fed_incident_dispatch")) == 2

    for elapsed in range(85):
        if elapsed % 5 == 0:
            await report(source, f"fire fresh arrival {elapsed}")
            await source._incident_delivery_once()
        await source.governor.tick()
        source.clock.advance(1)
    backlog = await source.database.read(
        "SELECT w.state FROM outbound_work w JOIN fed_incident_dispatch d "
        "ON d.queue_key=w.queue_key WHERE d.uid=?",
        (oldest.uid,),
    )
    assert backlog and all(row[0] == "sent" for row in backlog)


@pytest.mark.parametrize("blocked", ["offline", "queue_full", "quiet"])
async def test_waiting_work_has_a_finite_nonrenewing_window(nodes, blocked):
    source, sp, _, _ = await pair(nodes)
    await report(source)
    if blocked == "offline":
        await source.database.write("UPDATE fed_peer SET last_seen_at=NULL WHERE id=?", (sp.id,))
    elif blocked == "queue_full":
        source.governor.config.queue_max_items = 0
    else:
        source.governor.config.quiet_hours.classes = ["federation"]
        source.governor.config.quiet_hours.start = "00:00"
        source.governor.config.quiet_hours.end = "23:59"
    await source._incident_delivery_once()
    first = (await intents(source))[0]
    for _ in range(3):
        source.clock.advance(5)
        await source._incident_delivery_once()
        await source.governor.tick()
    assert (await intents(source))[0]["deadline_at"] == first["deadline_at"]
    assert not source.radio.sent
    source.clock.advance(DELIVERY_TTL)
    await source._incident_delivery_once()
    assert (await intents(source))[0]["delivery_state"] == "expired"
    assert (await source.incident_delivery.status())["items"][0][
        "remote_storage"
    ] == "not_confirmed"
    await source.database.write(
        "UPDATE fed_peer SET last_seen_at=?", (int(source.clock.now().timestamp()),)
    )
    source.governor.config.queue_max_items = 200
    source.governor.config.quiet_hours.classes = []
    source.clock.advance(5)
    await source._incident_delivery_once()
    assert (await intents(source))[0]["delivery_state"] == "expired"
    assert not source.governor.queued_items()


@pytest.mark.parametrize("fault", ["lost_event", "lost_receipt"])
async def test_application_retry_uses_fresh_counter_after_loss(nodes, fault):
    source, _, target, _ = await pair(nodes)
    incident = await report(source)
    counters = set()
    blocked_kind = MessageType.INCIDENT if fault == "lost_event" else MessageType.INCIDENT_RECEIPT

    def drop(app, fragment):
        if fragment.msg_type is MessageType.INCIDENT:
            counters.add(fragment.counter)
        return fragment.msg_type is blocked_kind and len(counters) < 2

    assert (
        await pump(source, target, 240, drop=drop, until=lambda: stored(source, incident.uid)) < 240
    )
    assert len(counters) == 2
    assert (await intents(source))[0]["application_attempts"] == 2
    assert len(await target.database.read("SELECT * FROM fed_inbox_item")) == 1
    assert not await target.database.read("SELECT * FROM incident")


async def test_cancelled_frame_is_not_automatically_revived_and_explicit_retry_is_audited(nodes):
    source, _, _, _ = await pair(nodes)
    await report(source)
    await source._incident_delivery_once()
    item = source.governor.queued_items()[0]
    assert await source.governor.cancel_work(item.item_id)
    source.clock.advance(5)
    await source._incident_delivery_once()
    state = (await intents(source))[0]
    assert state["delivery_state"] == "cancelled"
    assert not source.governor.queued_items()
    source.incident_worker = IncidentWorker(source.incident_sender)
    source.clock.advance(300)
    await source._incident_delivery_once()
    assert (await intents(source))[0]["application_attempts"] == 1
    await source.incident_worker.action(
        *source.incident_worker._key(state),
        "retry",
        (await source.incident_delivery.status())["items"][0]["action_token"],
        "web:test",
    )
    assert source.governor.queued_items()
    assert (await intents(source))[0]["application_attempts"] == 1
    assert (await source.database.read("SELECT action FROM audit_log ORDER BY id DESC LIMIT 1"))[0][
        0
    ] == "federation.incident_delivery.retry"


@pytest.mark.parametrize("fault", ["error", "cancel"])
async def test_worker_admission_and_attempt_counter_rollback_together(nodes, monkeypatch, fault):
    source, _, _, _ = await pair(nodes)
    await report(source)
    original = Transaction.write

    async def fail(tx, sql, params=()):
        if "application_attempts=application_attempts+1" in sql:
            raise asyncio.CancelledError() if fault == "cancel" else RuntimeError("injected")
        return await original(tx, sql, params)

    with monkeypatch.context() as patch:
        patch.setattr(Transaction, "write", fail)
        with pytest.raises((RuntimeError, asyncio.CancelledError)):
            await source._incident_delivery_once()
    assert not await source.database.read("SELECT * FROM fed_incident_dispatch")
    assert not source.governor.queued_items()
    assert (await source.database.read("SELECT tx_counter FROM fed_peer"))[0][0] == 0
    state = (await intents(source))[0]
    assert state["deadline_at"] is not None and state["application_attempts"] == 0
    await source._incident_delivery_once()
    assert (await intents(source))[0]["application_attempts"] == 1


async def test_oversized_record_does_not_block_other_work_and_correction_rearms_it(nodes):
    source, _, _, _ = await pair(nodes)
    bad = await report(source)
    await source.database.write("UPDATE incident SET body=? WHERE id=?", (oversized_body(), bad.id))
    good = await report(source, "road neighboring valid incident")
    await source._incident_delivery_once()
    states = {row["uid"]: row for row in await intents(source)}
    assert states[bad.uid]["delivery_reason"] == "payload_too_large"
    assert states[good.uid]["delivery_state"] == "queued"
    await source.database.write(
        "UPDATE incident SET body='Corrected short details' WHERE id=?", (bad.id,)
    )
    source.clock.advance(5)
    await source._incident_delivery_once()
    assert (
        next(row for row in await intents(source) if row["uid"] == bad.uid)["delivery_state"]
        != "blocked"
    )


async def test_wall_clock_backstep_does_not_extend_an_armed_in_process_window(nodes):
    source, _, _, _ = await pair(nodes)
    await report(source)
    source.governor.config.queue_max_items = 0
    await source._incident_delivery_once()
    original = (await intents(source))[0]
    source.clock.epoch -= timedelta(hours=6)
    source.clock.advance(DELIVERY_TTL + 1)
    await source._incident_delivery_once()
    state = (await intents(source))[0]
    assert state["deadline_at"] == original["deadline_at"] and state["delivery_state"] == "expired"


async def test_later_authenticated_counter_cannot_strand_an_overtaken_incident(nodes):
    source, _, target, _ = await pair(nodes)
    incident = await report(source)
    await source._incident_delivery_once()  # event counter 1 is retained, not yet sent
    await wire(
        source, target, MessageType.ITEM_RECEIPT, {"uid": "!local:unrelated", "state": "stored"}
    )
    assert (await target.database.read("SELECT rx_counter FROM fed_peer"))[0][0] == 2
    assert await pump(source, target, 240, until=lambda: stored(source, incident.uid)) < 240
    state = (await intents(source))[0]
    assert state["application_attempts"] == 2
    assert (await source.database.read("SELECT counter FROM fed_incident_dispatch"))[0][0] == 3
    assert not await target.database.read("SELECT * FROM incident")


async def test_permanent_loss_stops_after_three_application_attempts(nodes):
    source, _, target, _ = await pair(nodes)
    await report(source)
    await pump(source, target, 620, drop=lambda app, fragment: True)
    state = (await intents(source))[0]
    assert state["application_attempts"] == 3
    assert state["delivery_state"] == "retry_exhausted"
    count = len(source.radio.sent)
    await pump(source, target, 100, drop=lambda app, fragment: True)
    assert len(source.radio.sent) == count
    assert not await target.database.read("SELECT * FROM fed_inbox_item")


@pytest.mark.parametrize("phase", ["pending", "interrupted_send", "awaiting_receipt"])
async def test_new_app_instance_recovers_existing_automatic_work_without_false_delivery(
    nodes, monkeypatch, phase
):
    source, _, target, _ = await pair(nodes)
    incident = await report(source)
    await source._incident_delivery_once()
    before = (await intents(source))[0]
    if phase == "interrupted_send":

        async def interrupt(*args, **kwargs):
            raise asyncio.CancelledError()

        with monkeypatch.context() as patch:
            patch.setattr(source.radio, "_send_data", interrupt)
            with pytest.raises(asyncio.CancelledError):
                await source.governor.tick()
    elif phase == "awaiting_receipt":
        await flush_radio(source, target)
        assert not await stored(source, incident.uid)
    # The original app has no background tasks and is quiesced for this recovery.
    recovered = OutpostApp(source.config, clock=source.clock, radio=source.radio)
    await recovered.database.open()
    recovered.federation_sync.local_mesh_id = source.radio.local_node_id
    try:
        await recovered.governor.recover()
        assert (await intents(recovered))[0]["deadline_at"] == before["deadline_at"]
        if phase == "interrupted_send":
            assert (await recovered.database.read("SELECT state FROM outbound_attempt"))[0][
                0
            ] == "uncertain"
        await recovered._incident_delivery_once()
        assert (
            await pump(recovered, target, 240, until=lambda: stored(recovered, incident.uid)) < 240
        )
        assert (await intents(recovered))[0]["application_attempts"] <= 2
        assert not await target.database.read("SELECT * FROM incident")
    finally:
        await recovered.ai_service.close()
        await recovered.database.close()


@pytest.mark.parametrize("phase", ["validation", "admission"])
async def test_expiry_during_awaited_worker_work_cannot_publish_eligible_frames(
    nodes, monkeypatch, phase
):
    source, _, _, _ = await pair(nodes)
    await report(source)
    original = Transaction.write
    advanced = False
    original_current = source.incident_sender._current

    async def delayed_current(*args, **kwargs):
        nonlocal advanced
        result = await original_current(*args, **kwargs)
        if phase == "validation" and not advanced:
            advanced = True
            source.clock.advance(DELIVERY_TTL + 1)
        return result

    async def delayed(tx, sql, params=()):
        nonlocal advanced
        result = await original(tx, sql, params)
        if (
            not advanced
            and phase == "admission"
            and sql.startswith("INSERT INTO fed_incident_dispatch")
        ):
            advanced = True
            source.clock.advance(DELIVERY_TTL + 1)
        return result

    with monkeypatch.context() as patch:
        patch.setattr(Transaction, "write", delayed)
        patch.setattr(source.incident_sender, "_current", delayed_current)
        await source._incident_delivery_once()
    assert advanced
    assert (await intents(source))[0]["delivery_state"] == "expired"
    assert not source.governor.queued_items()
    assert await source.governor.tick() is None and not source.radio.sent


async def test_automatic_remote_resolution_retains_local_monitoring_and_explicit_review(nodes):
    source, _, target, _ = await pair(nodes)
    incident = await report(source)
    assert await pump(source, target, 60, until=lambda: stored(source, incident.uid)) < 60
    uid = source.federation_sync.wire_uid(incident.uid)
    await approve(target, await preview(target, uid))
    local = (await target.database.read("SELECT id FROM incident WHERE uid=?", (uid,)))[0][0]
    await target.incidents.operator_update(local, "ack", actor="web:local")
    await source.incidents.operator_patch(
        incident.id,
        status="resolved",
        severity=None,
        resolution="Producer reports road clear",
        actor="web:source",
    )
    assert await pump(source, target, 90, until=lambda: stored(source, incident.uid)) < 90
    assert (await target.incidents.by_id(local)).status == "monitoring"
    assert (await target.database.read("SELECT state FROM fed_inbox_item WHERE uid=?", (uid,)))[0][
        0
    ] == "pending"
    await approve(target, await preview(target, uid))
    monitored = await target.incidents.by_id(local)
    assert monitored.status == "monitoring" and monitored.reconciliation_review == 1
    assert not await target.database.read("SELECT * FROM alert")


async def test_peer_pages_are_bounded_and_an_ineligible_peer_cannot_starve_others(nodes):
    source, sp, _, _ = await pair(nodes)
    await source.database.write("UPDATE fed_peer SET capabilities='{}' WHERE id=?", (sp.id,))
    others = []
    for index in range(5):
        peer = await source.federation.discover(
            f"!peer{index}",
            f"synthetic peer {index}",
            1,
            {"reconciliation": 2, "incident_events": 1},
            "radio",
        )
        await source.database.write(
            "UPDATE fed_peer SET state='active',sync_incidents=1,shared_secret=? WHERE id=?",
            (bytes(range(32)), peer.id),
        )
        others.append(peer.id)
    await report(source)
    await source._incident_delivery_once()
    first = await source.database.read("SELECT peer_id FROM fed_incident_dispatch")
    assert 0 < len(first) <= 3
    await source._incident_delivery_once()
    assert {
        row[0] for row in await source.database.read("SELECT peer_id FROM fed_incident_dispatch")
    } == set(others)
    status = await source.database.read(
        "SELECT cursor FROM fed_cursor WHERE peer_id=? AND stream='_incident_worker'", (sp.id,)
    )
    assert json.loads(status[0][0])["state"] == "policy_or_lineage_blocked"
    assert not source.radio.sent


async def test_idle_scans_do_not_rewrite_unchanged_checkpoints_every_poll(nodes):
    source, _, _, _ = await pair(nodes)
    await source._incident_delivery_once()
    async with source.database.transaction() as tx:
        before = (await tx.read("SELECT total_changes()"))[0][0]
    source.clock.advance(5)
    await source._incident_delivery_once()
    async with source.database.transaction() as tx:
        after = (await tx.read("SELECT total_changes()"))[0][0]
    assert after == before


async def test_worker_loop_failure_uses_core_supervision_and_is_visible(nodes, monkeypatch):
    source, _, _, _ = await pair(nodes)

    async def fail():
        raise RuntimeError("injected incident writer fault")

    monkeypatch.setattr(source.incident_worker, "tick", fail)
    task = source._start_background_task(
        "incident-delivery",
        source._incident_delivery_loop,
        TaskFailureDomain.CORE,
    )
    try:
        reason = await asyncio.wait_for(source.wait_for_task_failure(), timeout=2)
        assert "incident-delivery" in reason and "writer fault" in reason
        assert not source.background_tasks_healthy()
        assert source.status()["tasks"]["incident-delivery"]["required"]
        assert not source.radio.sent
    finally:
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("notes", [False, True])
async def test_change_just_after_a_worker_poll_includes_full_poll_phase_in_latency(nodes, notes):
    source, _, target, _ = await pair(nodes, notes=notes)
    await source._incident_delivery_once()
    incident = await report(source)
    uid = incident.uid
    if notes:
        await source.incidents.operator_update(
            incident.id, "update", "Crew at bridge; use footpath."
        )
        uid = (await source.database.read("SELECT uid FROM incident_update"))[0][0]
    elapsed = await pump(source, target, 61, first_poll_delay=5, until=lambda: stored(source, uid))
    assert elapsed <= 60
    print(
        f"\nFull 5s poll phase, notes={notes}: exact storage receipt in {elapsed}s; "
        "human review excluded"
    )


@pytest.mark.parametrize(
    ("fault", "expected", "reason"),
    [
        ("expired", "expired", "transport_expired"),
        ("denied", "blocked", "dispatch_policy_denied"),
        ("missing", "blocked", "transport_history_missing"),
    ],
)
async def test_terminal_transport_evidence_never_silently_restarts_work(
    nodes, fault, expected, reason
):
    source, _, _, _ = await pair(nodes)
    await report(source)
    await source._incident_delivery_once()
    if fault == "missing":
        await source.database.write("DELETE FROM outbound_work")
    else:
        await source.database.write(
            "UPDATE outbound_work SET state=?,last_error=?,completed_at=?",
            (
                "expired" if fault == "expired" else "failed",
                "dispatch authorization denied" if fault == "denied" else None,
                int(source.clock.now().timestamp()),
            ),
        )
    source.clock.advance(5)
    before = (await source.incident_delivery.status())["items"][0]
    if fault == "expired":
        assert before["state"] == "expired" and before["reason"] == reason
    await source._incident_delivery_once()
    item = (await intents(source))[0]
    assert (item["delivery_state"], item["delivery_reason"]) == (expected, reason)
    assert item["application_attempts"] == 1
    source.clock.advance(600)
    await source._incident_delivery_once()
    assert (await source.database.read("SELECT tx_counter FROM fed_peer"))[0][0] == 1
    assert not source.radio.sent


async def test_key_rotation_cannot_reset_exhausted_application_attempt_budget(nodes):
    source, _, _, _ = await pair(nodes)
    await report(source)
    await source._incident_delivery_once()
    await source.database.write("UPDATE fed_incident_intent SET application_attempts=3")
    await source.database.write(
        "UPDATE fed_peer SET shared_secret=?", (bytes(reversed(range(32))),)
    )
    source.clock.advance(5)
    await source._incident_delivery_once()
    item = (await intents(source))[0]
    assert item["delivery_state"] == "retry_exhausted"
    assert item["delivery_reason"] == "application_attempt_limit"
    assert not source.governor.queued_items()
    assert not source.radio.sent


@pytest.mark.parametrize("fault", ["error", "cancel"])
async def test_explicit_retry_audit_failure_rolls_back_new_window_counter_and_frames(
    nodes, monkeypatch, fault
):
    source, _, _, _ = await pair(nodes)
    await report(source)
    await source._incident_delivery_once()
    item = (await source.incident_delivery.status())["items"][0]
    worker = source.incident_worker
    await worker.action(*worker._key(item), "cancel", item["action_token"], "web:test")
    item = (await source.incident_delivery.status())["items"][0]
    before = await intents(source)
    counter = (await source.database.read("SELECT tx_counter FROM fed_peer"))[0][0]

    async def fail(*args, **kwargs):
        raise (
            asyncio.CancelledError() if fault == "cancel" else RuntimeError("injected audit fault")
        )

    monkeypatch.setattr("outpost.fed.incident_worker.write_audit", fail)
    with pytest.raises((RuntimeError, asyncio.CancelledError)):
        await worker.action(*worker._key(item), "retry", item["action_token"], "web:test")
    assert await intents(source) == before
    assert (await source.database.read("SELECT tx_counter FROM fed_peer"))[0][0] == counter
    assert not source.governor.queued_items()
    assert not source.radio.sent


async def test_disabled_worker_does_not_stage_and_stale_peer_page_wraps(nodes):
    source, sp, _, _ = await pair(nodes)
    await report(source)
    source.config.modules.fed.enabled = False
    await source._incident_delivery_once()
    assert not await intents(source)
    source.config.modules.fed.enabled = True
    source.incident_worker._after_peer = sp.id + 100
    await source._incident_delivery_once()
    assert (await intents(source))[0]["delivery_state"] == "queued"
    assert not source.radio.sent


def create_staged_181(path):
    create_legacy(path)
    with closing(sqlite3.connect(path)) as connection:
        for migration in sorted(Path("src/outpost/store/migrations").glob("*.sql")):
            version = int(migration.name.split("_")[0])
            if version not in (180, 181):
                continue
            connection.executescript(migration.read_text())
            connection.execute("INSERT INTO schema_version VALUES(?,1)", (version,))
        connection.execute("INSERT INTO fed_peer(id,mesh_id,created_at) VALUES(1,'!synthetic',1)")
        connection.execute(
            "INSERT INTO fed_incident_handoff VALUES(1,'!local','!synthetic','epoch','scope',3,3)"
        )
        connection.execute(
            "INSERT INTO fed_incident_intent VALUES"
            "(1,'incidents','!local:retained','epoch',3,2,'scope','digest',NULL,'pending')"
        )
        before = connection.execute("SELECT * FROM fed_incident_intent").fetchone()
        connection.commit()
    return before


async def test_migration_preserves_existing_staging_without_inventing_delivery(tmp_path):
    path = tmp_path / "upgrade-181.db"
    before = create_staged_181(path)
    database = Database(path)
    await database.open()
    try:
        row = (await database.read("SELECT * FROM fed_incident_intent"))[0]
        assert tuple(row)[:10] == before
        assert row["lane"] == "backfill" and row["application_attempts"] == 0
        assert row["scheduled_at"] is row["deadline_at"] is None
        assert row["delivery_state"] == "pending" and row["next_attempt_at"] == 0
        assert (await database.read("SELECT fresh_revision FROM fed_incident_handoff"))[0][
            0
        ] is None
        assert not await database.read("SELECT * FROM fed_incident_receipt_reply")
        assert (await database.read("PRAGMA integrity_check"))[0][0] == "ok"
        assert not await database.read("PRAGMA foreign_key_check")
    finally:
        await database.close()


async def test_full_backups_preserve_automatic_windows_and_pending_reply_associations(nodes):
    source, _, target, _ = await pair(nodes)
    await report(source)
    await source._incident_delivery_once()
    await flush_radio(source, target)
    assert (await intents(source))[0]["scheduled_at"] is not None
    assert await target.database.read("SELECT * FROM fed_incident_receipt_reply")
    for app in (source, target):
        recovered = Database(await BackupService(app.database).create())
        await recovered.open()
        try:
            for table in (
                "fed_incident_handoff",
                "fed_incident_intent",
                "fed_incident_dispatch",
                "fed_incident_receipt_reply",
                "outbound_work",
            ):
                query = f"SELECT * FROM {table}"  # noqa: S608 -- fixed internal table names
                assert [dict(row) for row in await recovered.read(query)] == [
                    dict(row) for row in await app.database.read(query)
                ]
            assert not await recovered.read("PRAGMA foreign_key_check")
        finally:
            await recovered.close()
