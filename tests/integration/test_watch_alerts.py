import asyncio

import pytest

from outpost.clock import VirtualClock
from outpost.config import Config
from outpost.store import Database
from outpost.store.members import MemberRepo
from outpost.transport.governor import OutboundItem
from outpost.transport.models import TrafficClass
from outpost.transport.simulated import SimulatedRadioLink
from outpost.watch import AlertService, IncidentService
from tests.support.application import production_governor

pytestmark = pytest.mark.production_wiring


@pytest.mark.asyncio
async def test_identical_alert_submissions_coalesce_transactionally(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock = VirtualClock()
    config = Config.model_validate(
        {
            "store": {"path": str(tmp_path / "outpost.db")},
            "channels": {3: {"name": "watch"}},
        }
    )
    governor = production_governor(database, clock, airtime=config.airtime)
    responder = await MemberRepo(database, clock).resolve("!00000002")
    await database.write("UPDATE member SET trust='responder' WHERE id=?", (responder.id,))
    service = AlertService(database, governor, clock, config)

    preview = await service.airtime_preview("urgent", "Bridge closed", [3])
    rendered_bytes = len(service.render("urgent", "Bridge closed").encode())
    assert preview["recipient_count"] == 1
    assert preview["channel_count"] == 1
    assert preview["transmission_count"] == 1
    assert preview["per_copy_seconds"] == pytest.approx(governor.estimate_toa(rendered_bytes))
    assert preview["total_seconds"] == pytest.approx(preview["per_copy_seconds"])

    first, duplicate = await asyncio.gather(
        service.raise_alert("urgent", "Bridge closed", "web:operator", channels=[3]),
        service.raise_alert("urgent", "Bridge closed", "web:operator", channels=[3]),
    )

    assert first.id == duplicate.id
    assert {first.coalesced, duplicate.coalesced} == {False, True}
    assert len(await database.read("SELECT 1 FROM alert")) == 1
    assert len(await database.read("SELECT 1 FROM outbound_work")) == 1

    clock.advance(config.watch.alert_submission_dedupe_seconds + 1)
    later = await service.raise_alert("urgent", "Bridge closed", "web:operator", channels=[3])
    assert later.id != first.id and later.coalesced is False
    assert len(await database.read("SELECT 1 FROM alert")) == 2
    await database.close()


@pytest.mark.asyncio
async def test_alert_preempts_acknowledges_and_cancels_with_all_clear(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock, radio = VirtualClock(), SimulatedRadioLink()
    await radio.connect()
    config = Config.model_validate(
        {
            "store": {"path": str(tmp_path / "outpost.db")},
            "channels": {0: {"name": "public"}, 3: {"name": "watch"}},
            "airtime": {"min_gap_s": 0},
        }
    )
    governor = production_governor(database, clock, link=radio, airtime=config.airtime)
    members = MemberRepo(database, clock)
    reporter = await members.resolve("!00000001")
    responder = await members.resolve("!00000002")
    await database.write("UPDATE member SET trust='responder' WHERE id=?", (responder.id,))
    incident, _ = await IncidentService(database, clock).create(
        "fire at barn 40.4406 -79.9959", reporter
    )
    assert incident is not None
    await governor.admit(OutboundItem("digest", "!peer", 0, TrafficClass.DIGEST))
    service = AlertService(database, governor, clock, config)
    alert = await service.raise_alert(
        "urgent", "Barn fire; avoid Mill Road", "responder", incident_ref=incident.local_ref
    )
    assert alert.broadcast_count == 1
    assert alert.lat == pytest.approx(40.4406)
    assert alert.lon == pytest.approx(-79.9959)
    assert alert.radius_m == 1000
    sent = await governor.tick()
    assert sent is not None and sent.traffic_class == TrafficClass.ALERT
    assert sent.text.startswith("⚠URGENT")

    acked = await service.acknowledge(incident.local_ref, responder)
    assert acked.ack_count == 1
    cancelled = await service.cancel(alert.id, "Fire contained", "operator")
    assert cancelled.cancelled_at is not None
    queued = governor.queued_items()
    all_clears = [item for item in queued if item.text.startswith("ALL CLEAR")]
    assert [(item.dest, item.channel) for item in all_clears] == [(responder.mesh_id, 3)]
    assert not any(item.queue_key == f"alert:{alert.id}:repeat" for item in queued)
    assert cancelled.all_clear_queued == 1
    await database.close()


@pytest.mark.asyncio
async def test_escalation_is_durable_and_stops_at_ack_threshold(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock, radio = VirtualClock(), SimulatedRadioLink()
    config = Config.model_validate(
        {
            "store": {"path": str(tmp_path / "outpost.db")},
            "channels": {0: {"name": "public"}, 3: {"name": "watch"}},
            "watch": {
                "escalation": {
                    "urgent": {
                        "ack_threshold": 2,
                        "stages": [
                            {"after_minutes": 0, "notify": "responders", "channels": [3]},
                            {"after_minutes": 1, "notify": "trusted", "channels": [3]},
                            {"after_minutes": 2, "notify": "all", "channels": [0, 3]},
                        ],
                    }
                }
            },
        }
    )
    governor = production_governor(database, clock, link=radio)
    members = MemberRepo(database, clock)
    reporter = await members.resolve("!00000001")
    first = await members.resolve("!00000002")
    second = await members.resolve("!00000003")
    await database.write(
        "UPDATE member SET trust='responder' WHERE id IN (?,?)", (first.id, second.id)
    )
    incident, _ = await IncidentService(database, clock).create("medical at school", reporter)
    assert incident is not None
    service = AlertService(database, governor, clock, config)
    await service.raise_alert(
        "urgent", "Medical response requested", "responder", incident_ref=incident.local_ref
    )
    clock.advance(61)
    assert await service.advance_due() == 1
    await service.acknowledge(incident.local_ref, first)
    stopped = await service.acknowledge(incident.local_ref, second)
    assert stopped.ack_count == 2 and stopped.next_escalation_at is None
    clock.advance(61)
    assert await service.advance_due() == 0
    await database.close()


@pytest.mark.asyncio
async def test_two_alerts_resume_after_restart_and_one_can_be_halted(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock, radio = VirtualClock(), SimulatedRadioLink()
    config = Config.model_validate(
        {
            "store": {"path": str(tmp_path / "outpost.db")},
            "channels": {0: {"name": "public"}, 3: {"name": "watch"}},
        }
    )
    governor = production_governor(database, clock, link=radio, airtime=config.airtime)
    responder = await MemberRepo(database, clock).resolve("!00000009")
    await database.write("UPDATE member SET trust='responder' WHERE id=?", (responder.id,))
    first_service = AlertService(database, governor, clock, config)
    first = await first_service.raise_alert("urgent", "River rising", "operator")
    second = await first_service.raise_alert("urgent", "Road washed out", "operator")
    assert first.next_escalation_at is not None and second.next_escalation_at is not None

    restarted = AlertService(database, governor, clock, config)
    halted = await restarted.halt_escalation(first.id)
    assert halted.next_escalation_at is None
    clock.advance(601)
    assert await restarted.advance_due() == 1
    resumed = await restarted.by_id(second.id)
    assert resumed is not None and resumed.escalation_stage == 2
    detail = await restarted.operational_json(resumed)
    assert detail["stage_total"] == 3
    assert detail["next_action"]["notify"] == "all"
    await database.close()


@pytest.mark.asyncio
async def test_repeat_stage_is_bounded_durable_and_stops_after_ack(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock, radio = VirtualClock(), SimulatedRadioLink()
    config = Config.model_validate(
        {
            "store": {"path": str(tmp_path / "outpost.db")},
            "channels": {0: {"name": "public"}, 3: {"name": "watch"}},
            "airtime": {"dedupe_window_s": 300},
            "watch": {
                "alert_repeat_max": 3,
                "alert_repeat_interval_minutes": 2,
                "escalation": {
                    "critical": {
                        "ack_threshold": 1,
                        "stages": [
                            {"after_minutes": 0, "notify": "all", "channels": [3]},
                            {
                                "after_minutes": 1,
                                "notify": "all",
                                "channels": [3],
                                "repeat": True,
                            },
                        ],
                    }
                },
            },
        }
    )
    governor = production_governor(database, clock, link=radio, airtime=config.airtime)
    members = MemberRepo(database, clock)
    reporter = await members.resolve("!00000001")
    responder = await members.resolve("!00000002")
    incident, _ = await IncidentService(database, clock).create("wildfire near ridge", reporter)
    assert incident is not None
    service = AlertService(database, governor, clock, config)
    alert = await service.raise_alert(
        "critical", "Wildfire evacuation warning", "operator", incident_ref=incident.local_ref
    )
    assert alert.repeat_count == 0

    clock.advance(60)
    assert await service.advance_due() == 1
    repeated = await service.by_id(alert.id)
    assert repeated is not None
    assert repeated.repeat_count == 1
    assert repeated.escalation_stage == 1
    assert repeated.next_escalation_at == int(clock.now().timestamp()) + 120
    assert repeated.broadcast_count == 2

    restarted = AlertService(database, governor, clock, config)
    clock.advance(120)
    assert await restarted.advance_due() == 1
    second = await restarted.by_id(alert.id)
    assert second is not None and second.repeat_count == 2
    detail = await restarted.operational_json(second)
    assert detail["repeat_max"] == 3
    assert detail["repeat_remaining"] == 1
    assert detail["next_action"]["repeat"] is True

    stopped = await restarted.acknowledge(incident.local_ref, responder)
    assert stopped.ack_count == 1 and stopped.next_escalation_at is None
    clock.advance(120)
    assert await restarted.advance_due() == 0
    persisted = await restarted.by_id(alert.id)
    assert persisted is not None and persisted.repeat_count == 2
    await database.close()


@pytest.mark.asyncio
async def test_repeat_stage_stops_at_configured_maximum(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock, radio = VirtualClock(), SimulatedRadioLink()
    config = Config.model_validate(
        {
            "store": {"path": str(tmp_path / "outpost.db")},
            "channels": {3: {"name": "watch"}},
            "watch": {
                "alert_repeat_max": 2,
                "alert_repeat_interval_minutes": 1,
                "escalation": {
                    "critical": {
                        "stages": [
                            {"after_minutes": 0, "notify": "all", "channels": [3]},
                            {
                                "after_minutes": 1,
                                "notify": "all",
                                "channels": [3],
                                "repeat": True,
                            },
                        ]
                    }
                },
            },
        }
    )
    service = AlertService(
        database,
        production_governor(database, clock, link=radio, airtime=config.airtime),
        clock,
        config,
    )
    alert = await service.raise_alert("critical", "Evacuate now", "operator")

    clock.advance(60)
    assert await service.advance_due() == 1
    clock.advance(60)
    assert await service.advance_due() == 1
    finished = await service.by_id(alert.id)
    assert finished is not None
    assert finished.repeat_count == 0
    assert finished.escalation_stage == 2
    assert finished.next_escalation_at is None
    clock.advance(600)
    assert await service.advance_due() == 0
    await database.close()


@pytest.mark.asyncio
async def test_repeat_budget_resets_for_each_repeating_escalation_stage(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock, radio = VirtualClock(), SimulatedRadioLink()
    config = Config.model_validate(
        {
            "store": {"path": str(tmp_path / "outpost.db")},
            "channels": {3: {"name": "watch"}},
            "watch": {
                "alert_repeat_max": 3,
                "alert_repeat_interval_minutes": 1,
                "escalation": {
                    "critical": {
                        "stages": [
                            {
                                "after_minutes": 0,
                                "notify": "responders",
                                "channels": [3],
                                "repeat": True,
                            },
                            {
                                "after_minutes": 0,
                                "notify": "all",
                                "channels": [3],
                                "repeat": True,
                            },
                        ]
                    }
                },
            },
        }
    )
    responder = await MemberRepo(database, clock).resolve("!00000002")
    await database.write("UPDATE member SET trust='responder' WHERE id=?", (responder.id,))
    service = AlertService(
        database,
        production_governor(database, clock, link=radio, airtime=config.airtime),
        clock,
        config,
    )

    alert = await service.raise_alert("critical", "Evacuate now", "operator")
    assert alert.escalation_stage == 0 and alert.repeat_count == 1
    assert (await service.operational_json(alert))["repeat_remaining"] == 2

    for _ in range(2):
        clock.advance(60)
        assert await service.advance_due() == 1
    second_stage = await service.by_id(alert.id)
    assert second_stage is not None
    assert second_stage.escalation_stage == 1 and second_stage.repeat_count == 0
    assert second_stage.broadcast_count == 3
    assert (await service.operational_json(second_stage))["repeat_remaining"] == 3

    for index in range(3):
        if index:
            clock.advance(60)
        assert await service.advance_due() == 1
    finished = await service.by_id(alert.id)
    assert finished is not None
    assert finished.escalation_stage == 2 and finished.repeat_count == 0
    assert finished.broadcast_count == 6
    assert finished.next_escalation_at is None
    assert (await service.operational_json(finished))["repeat_remaining"] == 0
    await database.close()


@pytest.mark.asyncio
async def test_all_clear_reuses_every_distinct_admitted_alert_audience(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock, radio = VirtualClock(), SimulatedRadioLink()
    config = Config.model_validate(
        {
            "store": {"path": str(tmp_path / "outpost.db")},
            "channels": {0: {"name": "public"}, 3: {"name": "watch"}},
            "watch": {
                "escalation": {
                    "urgent": {
                        "stages": [
                            {"after_minutes": 0, "notify": "responders", "channels": [3]},
                            {"after_minutes": 1, "notify": "trusted", "channels": [3]},
                            {"after_minutes": 2, "notify": "all", "channels": [0, 3]},
                        ]
                    }
                }
            },
        }
    )
    governor = production_governor(database, clock, link=radio, airtime=config.airtime)
    members = MemberRepo(database, clock)
    responder = await members.resolve("!00000002")
    trusted = await members.resolve("!00000003")
    await database.write("UPDATE member SET trust='responder' WHERE id=?", (responder.id,))
    await database.write("UPDATE member SET trust='trusted' WHERE id=?", (trusted.id,))
    service = AlertService(database, governor, clock, config)
    alert = await service.raise_alert("urgent", "Bridge closed", "operator")
    clock.advance(60)
    assert await service.advance_due() == 1
    clock.advance(60)
    assert await service.advance_due() == 1

    active = await service.by_id(alert.id)
    assert active is not None
    detail = await service.operational_json(active)
    assert {(item["destination"], item["channel"]) for item in detail["audiences"]} == {
        (responder.mesh_id, 3),
        (trusted.mesh_id, 3),
        ("^all", 0),
        ("^all", 3),
    }

    cancelled = await service.cancel(alert.id, "Bridge reopened", "operator")
    all_clears = [
        (item.dest, item.channel)
        for item in governor.queued_items()
        if item.text.startswith("ALL CLEAR")
    ]
    assert all_clears == [
        ("!00000002", 3),
        ("!00000003", 3),
        ("^all", 0),
        ("^all", 3),
    ]
    assert cancelled.all_clear_queued == 4
    await database.close()


@pytest.mark.asyncio
async def test_zero_recipient_alert_stays_at_stage_and_recovers_when_responder_appears(
    tmp_path,
) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock, radio = VirtualClock(), SimulatedRadioLink()
    config = Config.model_validate(
        {
            "store": {"path": str(tmp_path / "outpost.db")},
            "channels": {3: {"name": "watch"}},
            "watch": {
                "escalation": {
                    "urgent": {
                        "stages": [
                            {"after_minutes": 0, "notify": "responders", "channels": [3]},
                            {"after_minutes": 20, "notify": "all", "channels": [3]},
                        ]
                    }
                }
            },
        }
    )
    governor = production_governor(database, clock, link=radio, airtime=config.airtime)
    service = AlertService(database, governor, clock, config)

    alert = await service.raise_alert("urgent", "Bridge failure", "operator")

    assert alert.escalation_stage == 0
    assert alert.delivery_state == "empty_audience"
    assert alert.last_delivery_count == 0
    assert alert.next_escalation_at == int(clock.now().timestamp()) + 300
    assert governor.queued_items() == []
    failure = await database.read(
        "SELECT state,message_kind,conversation_key FROM mail WHERE state='failed'"
    )
    assert [dict(row) for row in failure] == [
        {
            "state": "failed",
            "message_kind": "system",
            "conversation_key": "system:delivery:alert_escalation:alert:1:stage:0",
        }
    ]
    assert await database.read(
        "SELECT 1 FROM audit_log WHERE action='safety.delivery.zero' AND target='alert:1:stage:0'"
    )

    responder = await MemberRepo(database, clock).resolve("!00000002")
    await database.write("UPDATE member SET trust='responder' WHERE id=?", (responder.id,))
    clock.advance(301)
    assert await service.advance_due() == 1
    recovered = await service.by_id(alert.id)
    assert recovered is not None
    assert recovered.escalation_stage == 1
    assert recovered.delivery_state == "delivered"
    assert recovered.last_delivery_count == 1
    assert (await database.read("SELECT state FROM mail"))[0]["state"] == "delivered"
    await database.close()


@pytest.mark.asyncio
async def test_queue_refused_alert_retries_same_stage_after_capacity_returns(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock, radio = VirtualClock(), SimulatedRadioLink()
    config = Config.model_validate(
        {
            "store": {"path": str(tmp_path / "outpost.db")},
            "channels": {0: {"name": "public"}, 3: {"name": "watch"}},
            "airtime": {"queue_max_items": 1},
        }
    )
    governor = production_governor(database, clock, link=radio, airtime=config.airtime)
    responder = await MemberRepo(database, clock).resolve("!00000002")
    await database.write("UPDATE member SET trust='responder' WHERE id=?", (responder.id,))
    filler = await governor.admit(OutboundItem("busy", "!peer", 3, TrafficClass.DIGEST))
    assert filler is not None
    service = AlertService(database, governor, clock, config)

    alert = await service.raise_alert("urgent", "Road closed", "operator")

    assert alert.delivery_state == "refused"
    assert alert.escalation_stage == 0
    assert alert.next_escalation_at == int(clock.now().timestamp()) + 60
    assert await governor.cancel_work(filler)
    clock.advance(61)
    assert await service.advance_due() == 1
    recovered = await service.by_id(alert.id)
    assert recovered is not None and recovered.escalation_stage == 1
    assert recovered.last_delivery_count == 1
    await database.close()


@pytest.mark.asyncio
async def test_responder_demotion_pauses_the_next_empty_escalation_stage(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock, radio = VirtualClock(), SimulatedRadioLink()
    config = Config.model_validate(
        {
            "store": {"path": str(tmp_path / "outpost.db")},
            "channels": {3: {"name": "watch"}},
            "watch": {
                "escalation": {
                    "urgent": {
                        "stages": [
                            {"after_minutes": 0, "notify": "responders", "channels": [3]},
                            {"after_minutes": 1, "notify": "trusted", "channels": [3]},
                        ]
                    }
                }
            },
        }
    )
    governor = production_governor(database, clock, link=radio, airtime=config.airtime)
    responder = await MemberRepo(database, clock).resolve("!00000002")
    await database.write("UPDATE member SET trust='responder' WHERE id=?", (responder.id,))
    service = AlertService(database, governor, clock, config)
    alert = await service.raise_alert("urgent", "River rising", "operator")
    assert alert.escalation_stage == 1
    await database.write("UPDATE member SET trust='member' WHERE id=?", (responder.id,))

    clock.advance(61)
    assert await service.advance_due() == 0
    paused = await service.by_id(alert.id)
    assert paused is not None
    assert paused.escalation_stage == 1
    assert paused.delivery_state == "empty_audience"
    assert paused.next_escalation_at == int(clock.now().timestamp()) + 300
    await database.close()
