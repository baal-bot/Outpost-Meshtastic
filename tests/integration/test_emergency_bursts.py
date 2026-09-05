"""Synthetic, no-RF qualification of the production emergency admission path.

These are bounded application workloads, not claims about RF channel capacity,
independent human identities, indefinite storage, or physical power-loss safety.
"""

import asyncio
import json
import sqlite3
import time
import tracemalloc
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from outpost.app import OutpostApp
from outpost.clock import VirtualClock
from outpost.config import Config
from outpost.fed.revisions import CAPABILITY, MODE
from outpost.router.models import CommandSpec, DispatchTrace, TrustLevel
from outpost.transport.governor import OutboundItem
from outpost.transport.models import InboundMessage, Severity, TrafficClass
from outpost.transport.simulated import SimulatedRadioLink

pytestmark = pytest.mark.production_wiring


@pytest.fixture
async def burst_app(tmp_path):
    apps = []

    async def make(*, name="burst", capacity=500, responders=3, restart=None):
        clock = VirtualClock(epoch=datetime(2026, 1, 1, 12, tzinfo=UTC))
        config = Config.model_validate(
            {
                "store": {"path": str(tmp_path / f"{name}.db")},
                "modules": {"watch": {"enabled": True}, "fed": {"enabled": True}},
                "router": {"inbound_queue_max": 16},
                "airtime": {"queue_max_items": capacity, "quiet_hours": {"classes": []}},
            }
        )
        if restart is not None:
            config, clock = restart.config, restart.clock
            await restart.ai_service.close()
            await restart.radio.close()
            await restart.database.close()
            apps.remove(restart)
            responders = 0
        app = OutpostApp(config, clock=clock, radio=SimulatedRadioLink(clock, node_id="!outpost"))
        await app.database.open()
        apps.append(app)
        app.incidents.origin_node = "!outpost"
        app.federation_sync.local_mesh_id = "!outpost"
        for index in range(responders):
            member = await app.router.members.resolve(f"!f000{index:04x}")
            await app.database.write("UPDATE member SET trust='responder' WHERE id=?", (member.id,))
        return app

    yield make
    for app in reversed(apps):
        await app.ai_service.close()
        await app.radio.close()
        await app.database.close()


def inbound(app, packet_id, text, sender="!10000001"):
    return InboundMessage(
        packet_id=packet_id,
        from_id=sender,
        to_id="!outpost",
        channel=0,
        portnum=1,
        is_direct=True,
        text=text,
        payload=None,
        rx_time=app.clock.now(),
    )


async def deliver(app, message):
    accepted = app.inbound_pipeline.process(message)
    if accepted is None:
        return None
    log_id = await app.message_log.record_inbound(accepted)
    trace = DispatchTrace()
    assert await app._handle_inbound_safely(accepted, log_id, trace)
    return trace


async def count(app, table):
    # Call sites are fixed fixture table names, never inbound identifiers.
    return (await app.database.read(f"SELECT COUNT(*) FROM {table}"))[0][0]  # noqa: S608


async def active_peer(app):
    peer = await app.federation.discover(
        "!remote", "Synthetic peer", 1, {CAPABILITY: MODE}, "radio"
    )
    await app.database.write(
        "UPDATE fed_peer SET state='active',sync_incidents=1,local_approved=1,remote_approved=1 "
        "WHERE id=?",
        (peer.id,),
    )
    return await app.federation.by_mesh_id("!remote")


async def test_six_minute_mixed_burst_preserves_intake_and_bounds_amplification(
    burst_app, record_property
):
    app = await burst_app()
    peer = await active_peer(app)
    receiver = await burst_app(name="receiver", responders=0)
    receiver_peer = await active_peer(receiver)
    senders, rounds = 12, 6
    packet_id = 0
    accepted = coalesced = 0
    latencies = []
    queue_peak = 0
    after = 0
    received = 0
    before_pages = (await app.database.read("PRAGMA page_count"))[0][0]
    started = time.perf_counter()
    tracemalloc.start()
    try:
        for wave in range(rounds):
            for index in range(senders):
                sender = f"!1{index:07x}"
                commands = (
                    f"HELPME need water at sample shelter {wave}",
                    f"REPORT! road sample obstruction {wave}",
                    f"OK safe at sample shelter {wave}",
                )
                for command in commands:
                    packet_id += 1
                    message = inbound(app, packet_id, command, sender)
                    begin = time.perf_counter()
                    trace = await deliver(app, message)
                    latencies.append(time.perf_counter() - begin)
                    assert trace.response_kind == "ack"
                    assert trace.admission == "admitted"
                    accepted += 1
                    # Same RF packet is rejected before logging/dispatch.
                    assert await deliver(app, message) is None
                    # A new packet carrying an equivalent request is coalesced.
                    packet_id += 1
                    repeat = replace(message, packet_id=packet_id, text="  " + command + "  ")
                    trace = await deliver(app, repeat)
                    assert trace.decision == "safety_repeat_suppressed"
                    coalesced += 1
                queue_peak = max(queue_peak, len(app.governor.queued_items()))
            # Producer catch-up queries share the actual writer with urgent intake.
            page = await app.federation_sync.revisions.page(
                peer,
                {
                    "mode": MODE,
                    "cycle": f"{wave:032x}",
                    "after": after,
                    "snapshot": None,
                    "limit": 8,
                },
            )
            assert len(page["items"]) <= 8
            requested = [
                {"stream": item["s"], "uid": item["u"], "revision": item["r"]}
                for item in page["items"]
            ]
            exports = await app.federation_sync.revisions.export(peer, {**page, "items": requested})
            for item in exports:
                now = int(receiver.clock.now().timestamp())
                assert await receiver.federation_sync.quarantine(receiver_peer, item, now)
                assert not await receiver.federation_sync.quarantine(receiver_peer, item, now)
                received += 1
            after = page["next"]
            app.clock.advance(61)
            receiver.clock.advance(61)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert accepted == coalesced == senders * rounds * 3
    assert await count(app, "incident") == senders * rounds
    assert await count(app, "checkin") == senders * rounds * 2
    assert await count(app, "safety_floor_attempt") == accepted
    totals = (
        await app.database.read(
            "SELECT SUM(attempt_count),SUM(accepted_count),SUM(coalesced_count) "
            "FROM safety_floor_attempt"
        )
    )[0]
    assert tuple(totals) == (accepted * 2, accepted, coalesced)
    assert (
        await app.database.read(
            "SELECT id FROM checkin WHERE status='need_help' AND notification_count<>3"
        )
        == []
    )
    assert queue_peak <= app.config.airtime.queue_max_items
    assert app.inbound_pipeline.dropped["duplicate"] == accepted
    assert app.radio.sent == []  # Delivery admission is not radio receipt.
    assert received == 48  # Eight-item catch-up budget deliberately leaves backlog.
    assert await count(receiver, "fed_revision_receipt") == received
    assert await count(receiver, "fed_inbox_item") == received
    assert await count(receiver, "incident") == 0  # No automatic human approval.
    after_pages = (await app.database.read("PRAGMA page_count"))[0][0]
    page_size = (await app.database.read("PRAGMA page_size"))[0][0]
    report = {
        "synthetic_sender_ids": senders,
        "virtual_seconds": app.clock.monotonic(),
        "packets_including_rf_duplicates": accepted * 3,
        "meaningful_requests": accepted,
        "coalesced_new_packets": coalesced,
        "queue_peak": queue_peak,
        "responder_notifications_admitted": senders * rounds * 3,
        "incident_review_records": senders * rounds,
        "federation_quarantined": received,
        "federation_backlog": senders * rounds - received,
        "database_growth_bytes": (after_pages - before_pages) * page_size,
        "traced_python_peak_bytes": peak,
        "wall_seconds": round(time.perf_counter() - started, 3),
        "intake_p95_seconds": round(sorted(latencies)[int(len(latencies) * 0.95)], 4),
        "intake_max_seconds": round(max(latencies), 4),
    }
    record_property("synthetic_burst", json.dumps(report, sort_keys=True))
    # Resource regressions have generous, portable ceilings; timings are evidence,
    # not flaky host-performance assertions or RF service guarantees.
    assert peak < 32 * 1024 * 1024
    assert report["database_growth_bytes"] < 8 * 1024 * 1024


async def test_full_queue_preserves_reports_and_attributes_every_refused_reply(burst_app):
    app = await burst_app(capacity=16)
    for index in range(16):
        assert await app.governor.admit(
            OutboundItem(f"routine {index}", "!ordinary", 0, TrafficClass.REPLY)
        )
    for index in range(24):
        message = inbound(app, index + 1, f"HELPME changing need {index}")
        trace = await deliver(app, message)
        assert trace.response_kind == "ack" and trace.admission == "queue_full"
        assert "No responder was reached" in trace.response_text
        assert len(app.governor.queued_items()) == 16
    assert await count(app, "checkin") == 24
    assert (
        await app.database.read(
            "SELECT COUNT(*) FROM checkin WHERE notification_state='refused' "
            "AND notification_count=0"
        )
    )[0][0] == 24
    rows = await app.database.read(
        "SELECT m.drop_reason,i.packet_id FROM message_log m JOIN message_log i "
        "ON i.id=m.in_reply_to_id WHERE m.direction='out'"
    )
    assert sorted(tuple(row) for row in rows) == [("queue_full", index + 1) for index in range(24)]
    app.clock.advance(301)
    # A disconnected radio still expires stale work, freeing the durable queue.
    assert await app.governor.tick() is None
    assert len(app.governor.queued_items()) == 0
    assert (await app.database.read("SELECT COUNT(*) FROM outbound_work WHERE state='expired'"))[0][
        0
    ] == 16
    trace = await deliver(app, inbound(app, 100, "HELPME new roof damage"))
    assert trace.admission == "admitted"
    assert len(app.governor.queued_items()) == 4
    restarted = await burst_app(restart=app)
    assert await restarted.governor.recover() == 4
    repeat = await deliver(restarted, inbound(restarted, 101, "HELPME new roof damage"))
    assert repeat.decision == "safety_repeat_suppressed"
    assert await count(restarted, "checkin") == 25
    assert len(restarted.governor.queued_items()) == 4


async def test_exhausted_classes_do_not_block_eligible_replies_during_burst(burst_app):
    app = await burst_app(responders=0)
    governor = app.governor
    budget = 3600 * governor.budget_percent / 100
    for cls in (TrafficClass.ALERT, TrafficClass.FEDERATION):
        governor.history.append(
            (
                app.clock.monotonic(),
                budget * app.config.airtime.class_shares[cls.value],
                cls,
                Severity.INFO,
            )
        )
        for index in range(40):
            assert await governor.admit(
                OutboundItem(f"blocked {cls.value} {index}", "!remote", 0, cls)
            )
    await app.radio.connect()
    for index in range(12):
        trace = await deliver(app, inbound(app, index + 1, f"REPORT! road obstruction {index}"))
        assert trace.admission == "admitted"
        sent = await governor.tick()
        assert sent is not None and sent.traffic_class is TrafficClass.REPLY
        app.clock.advance(15)
    assert len(governor.queues[TrafficClass.ALERT]) == 40
    assert len(governor.queues[TrafficClass.FEDERATION]) == 40
    assert await count(app, "incident") == 12
    assert all(item.attempts == 0 for item in governor.queued_items())
    # Exhaust the normal allowance entirely. Only a declared critical alert may
    # use the reserve; do not relabel all welfare replies as critical to pass.
    governor.history.clear()
    governor.history.append((app.clock.monotonic(), budget, TrafficClass.REPLY, Severity.INFO))
    critical = await governor.admit(
        OutboundItem(
            "synthetic critical escalation", "!remote", 0, TrafficClass.ALERT, Severity.CRITICAL
        )
    )
    sent = await governor.tick()
    assert sent is not None and sent.item_id == critical
    assert len(governor.queues[TrafficClass.ALERT]) == 40


async def test_ordinary_backlog_and_failed_optional_handler_leave_safety_fast_path(burst_app):
    app = await burst_app(responders=0)
    started, release = asyncio.Event(), asyncio.Event()

    async def unavailable(context):
        started.set()
        await release.wait()
        raise RuntimeError("synthetic optional provider unavailable")

    app.router.registry.register(
        CommandSpec(
            name="SLOW",
            aliases=(),
            module="test",
            min_trust=TrustLevel.GUEST,
            airtime_class=TrafficClass.REPLY,
            max_parts=1,
            rate_key="commands",
            help_short="Synthetic optional service",
            mutates=False,
            handler=unavailable,
        )
    )
    worker = asyncio.create_task(app._inbound_worker(1))
    try:
        slow = inbound(app, 1, "SLOW", "!40000001")
        await app._route_inbound(slow, await app.message_log.record_inbound(slow))
        await asyncio.wait_for(started.wait(), 5)
        for index in range(40):
            ordinary = inbound(app, index + 2, "PING", f"!2{index:07x}")
            await app._route_inbound(ordinary, await app.message_log.record_inbound(ordinary))
        assert app._inbound_queued == 16 and app._inbound_backlog_dropped == 24
        for index in range(24):
            urgent = inbound(app, 100 + index, f"REPORT! road obstruction {index}")
            await app._route_inbound(urgent, await app.message_log.record_inbound(urgent))
        assert await count(app, "incident") == 24
        assert app._inbound_fast_processed == 24
        assert app._inbound_queued == 16
        release.set()
        async with asyncio.timeout(10):
            while app._inbound_queued or app._inbound_busy:  # noqa: ASYNC110
                await asyncio.sleep(0.01)
        assert not worker.done()
        assert app._inbound_pending == {}
        assert app._inbound_ready.qsize() == 0
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


async def test_sqlite_page_ceiling_rolls_back_intake_and_recovers_without_filling_disk(burst_app):
    app = await burst_app(responders=0)
    peer = await active_peer(app)
    await app.database.write("CREATE TABLE synthetic_storage_padding(payload BLOB)")
    current_pages = (await app.database.read("PRAGMA page_count"))[0][0]
    # Apply the limit on the exact scratch writer, never the host filesystem.
    async with app.database.transaction() as transaction:
        await transaction.read(f"PRAGMA max_page_count={current_pages + 16}")
    for _ in range(32):
        try:
            await app.database.write("INSERT INTO synthetic_storage_padding VALUES(zeroblob(4096))")
        except sqlite3.OperationalError as error:
            assert error.sqlite_errorcode == sqlite3.SQLITE_FULL
            break
    else:
        pytest.fail("scratch SQLite page ceiling was not reached")
    for index in range(256):
        before = await count(app, "incident")
        try:
            await app.incidents.create(f"road sample obstruction {index}", None, force=True)
        except sqlite3.OperationalError as error:
            assert error.sqlite_errorcode == sqlite3.SQLITE_FULL
            assert await count(app, "incident") == before
            break
    else:
        pytest.fail("bounded scratch database did not refuse new incident storage")
    assert await count(app, "incident_origin") == before
    assert await count(app, "incident_reference") == before
    assert (await app.database.read("PRAGMA integrity_check"))[0][0] == "ok"
    assert await app.database.read("PRAGMA foreign_key_check") == []
    async with app.database.transaction() as transaction:
        await transaction.read(f"PRAGMA max_page_count={current_pages + 1024}")
        await transaction.write("DELETE FROM synthetic_storage_padding")
    incident, _ = await app.incidents.create("road recovered intake", None, force=True)
    assert incident is not None and await count(app, "incident") == before + 1
    page = await app.federation_sync.revisions.page(
        peer, {"mode": MODE, "cycle": "a" * 32, "after": 0, "snapshot": None, "limit": 8}
    )
    assert page["items"]
    restarted = await burst_app(restart=app)
    assert await count(restarted, "incident") == before + 1
    assert (await restarted.database.read("PRAGMA integrity_check"))[0][0] == "ok"


async def test_repeated_device_identity_is_not_independent_human_consensus(burst_app):
    app = await burst_app(responders=0)
    incident, _ = await app.incidents.create("road synthetic confirmation target", None, force=True)
    members = [await app.router.members.resolve(f"!3{index:07x}") for index in range(12)]
    for _ in range(4):
        await asyncio.gather(
            *(app.incidents.react(incident.local_ref, member, "confirm") for member in members)
        )
    current = await app.incidents.by_id(incident.id)
    assert current.confirm_count == len(members)
    assert await count(app, "incident_update") == len(members)
    # Synthetic distinct radio IDs are not proof of distinct people or verified PKI.
    assert all(member.pki_state != "verified" for member in members)
    assert current.unverified == incident.unverified
    assert current.status == incident.status


@pytest.mark.parametrize("command", ["HELPME", "OK"])
async def test_changed_safety_notes_receive_distinct_acks_but_repeats_do_not(burst_app, command):
    app = await burst_app(responders=0)
    for index in range(8):
        request = inbound(app, index * 2 + 1, f"{command} sample conditions {index}")
        trace = await deliver(app, request)
        assert trace.admission == "admitted"
        repeat = await deliver(app, replace(request, packet_id=index * 2 + 2))
        assert repeat.decision == "safety_repeat_suppressed"
    assert await count(app, "checkin") == 8
    assert len(app.governor.queued_items()) == 8
    assert len({item.dedupe_token for item in app.governor.queued_items()}) == 8


@pytest.mark.parametrize("cancel", [False, True])
async def test_interrupted_safety_handler_does_not_suppress_unrecorded_retry(
    burst_app, monkeypatch, cancel
):
    app = await burst_app(responders=0)
    entered = asyncio.Event()
    create = app.incidents.create

    async def paused_before_mutation(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(app.incidents, "create", paused_before_mutation)
    app.config.router.member_lock_timeout_s = 0.1
    request = inbound(app, 1, "REPORT! road sample retry")
    task = asyncio.create_task(deliver(app, request))
    await asyncio.wait_for(entered.wait(), 5)
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        first = await task
        assert first.response_kind == "error"
    assert await count(app, "incident") == 0
    monkeypatch.setattr(app.incidents, "create", create)
    app.config.router.member_lock_timeout_s = 5
    retry = await deliver(app, replace(request, packet_id=2))
    assert retry.response_kind == "ack" and retry.admission == "admitted"
    assert await count(app, "incident") == 1
