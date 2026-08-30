import csv
import io
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from outpost.clock import VirtualClock
from outpost.store import Database
from outpost.store.members import MemberRepo
from outpost.transport.models import TrafficClass
from outpost.watch import CheckinService, IncidentService
from outpost.web.api import create_web_app
from tests.support.application import production_governor

pytestmark = pytest.mark.production_wiring


@pytest.mark.asyncio
async def test_event_roster_checkins_and_csv(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock = VirtualClock()
    governor = production_governor(database, clock)
    members = MemberRepo(database, clock)
    dana = await members.resolve("!00000001")
    dana = await members.claim_handle(dana.mesh_id, "dana")
    ray = await members.resolve("!00000002")
    ray = await members.claim_handle(ray.mesh_id, "ray")
    service = CheckinService(database, governor, clock)

    event = await service.open_event("Ice storm", "all", "operator")
    initial = await service.summary(event.id)
    assert initial["counts"] == {
        "ok": 0,
        "need_help": 0,
        "evacuated": 0,
        "unaccounted": 2,
    }
    result = await service.checkin(dana, "ok", "Home and warm")
    assert result["checked_in"] == 1 and result["total"] == 2
    summary = await service.summary(event.id)
    assert summary["counts"]["ok"] == 1
    assert summary["counts"]["unaccounted"] == 1
    exported = await service.csv_export(event.id)
    assert "mesh_id,handle,trust,status" in exported
    assert "dana" in exported and "unaccounted" in exported
    closed = await service.close_event(event.id)
    assert closed.closed_at is not None and await service.current_event() is None
    await database.close()


@pytest.mark.asyncio
async def test_roster_csv_neutralizes_every_spreadsheet_formula_prefix(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock = VirtualClock()
    governor = production_governor(database, clock)
    members = MemberRepo(database, clock)
    service = CheckinService(database, governor, clock)
    event = await service.open_event("Export drill", "all", "operator")
    notes = ["=SUM(1,1)", "+cmd", "-2+3", "@link", " \t=hidden"]

    for index, note in enumerate(notes, start=1):
        member = await members.resolve(f"!{index:08x}")
        member = await members.claim_handle(member.mesh_id, f"member{index}")
        await service.checkin(member, "ok", note)

    exported = await service.csv_export(event.id)
    rows = list(csv.DictReader(io.StringIO(exported)))
    assert {row["note"] for row in rows} == {f"'{note}" for note in notes}
    await database.close()


@pytest.mark.asyncio
async def test_need_help_uses_position_and_notifies_responders(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock = VirtualClock()
    governor = production_governor(database, clock)
    members = MemberRepo(database, clock)
    caller = await members.resolve("!00000001")
    caller = await members.claim_handle(caller.mesh_id, "caller")
    responder = await members.resolve("!00000002")
    await database.write("UPDATE member SET trust='responder' WHERE id=?", (responder.id,))
    service = CheckinService(database, governor, clock)
    await service.open_event("Flood", "all", "operator")
    await IncidentService(database, clock).record_position(caller, 40.44, -79.99, prompt=False)

    await service.checkin(caller, "need_help", "Water rising")
    queued = governor.queued_items()
    assert len(queued) == 1
    assert queued[0].traffic_class == TrafficClass.ALERT
    assert queued[0].dest == responder.mesh_id
    assert "Water rising" in queued[0].text
    roster = await service.summary((await service.current_event()).id)  # type: ignore[union-attr]
    caller_row = next(row for row in roster["items"] if row["mesh_id"] == caller.mesh_id)
    assert caller_row["status"] == "need_help" and caller_row["lat"] == pytest.approx(40.44)
    await database.close()


@pytest.mark.asyncio
async def test_discovered_guests_never_enter_roster_or_solicitation_preview(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock = VirtualClock()
    governor = production_governor(database, clock)
    members = MemberRepo(database, clock)
    member = await members.resolve("!00000001")
    member = await members.claim_handle(member.mesh_id, "member")
    for number in range(2, 22):
        await members.resolve(f"!{number:08x}")
    service = CheckinService(database, governor, clock)
    event = await service.open_event("Test", "all", "operator")

    roster = await service.roster(event.id)
    preview = await service.solicitation_preview(event.id)
    assert [row["mesh_id"] for row in roster] == [member.mesh_id]
    assert [row["mesh_id"] for row in preview] == [member.mesh_id]
    assert governor.queued_items() == []
    await database.close()


@pytest.mark.asyncio
async def test_solicitation_is_direct_digest_and_only_once_per_member(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock = VirtualClock()
    governor = production_governor(database, clock)
    members = MemberRepo(database, clock)
    member = await members.resolve("!00000001")
    member = await members.claim_handle(member.mesh_id, "member")
    await members.resolve("!00000002")  # Discovered guest must never receive this message.
    service = CheckinService(database, governor, clock)
    event = await service.open_event("Ice storm", "all", "operator")
    multibyte_message = service.solicitation_message(replace(event, name="🚨" * 80))
    assert len(multibyte_message.encode()) <= 231
    assert multibyte_message.endswith("Reply OK [note] or HELPME [note].")

    preview = await service.solicitation_airtime(event.id)
    assert preview["recipient_count"] == 1
    assert preview["transmission_count"] == 1
    assert preview["total_seconds"] == pytest.approx(preview["per_copy_seconds"])

    client = TestClient(
        create_web_app(lambda: {"radio": "up"}, database=database, checkins=service)
    )
    api_preview = client.get(f"/api/v1/events/{event.id}/solicitation-preview")
    assert api_preview.status_code == 200
    assert api_preview.json()["airtime"]["recipient_count"] == 1
    governor.channel_utilisation = governor.config.utilisation_ceiling
    constrained = client.post(
        f"/api/v1/events/{event.id}/solicit",
        json={"confirmation": f"QUEUE {event.id}"},
    )
    assert constrained.status_code == 409
    assert constrained.json()["airtime"]["breach_codes"] == ["utilisation_ceiling"]
    confirmed = client.post(
        f"/api/v1/events/{event.id}/solicit",
        json={
            "confirmation": f"QUEUE {event.id}",
            "airtime_confirmation": True,
        },
    )
    assert confirmed.status_code == 200
    result = confirmed.json()
    governor.channel_utilisation = None

    assert result["recipient_count"] == 1
    queued = governor.queued_items()
    assert len(queued) == 1
    assert queued[0].dest == member.mesh_id
    assert queued[0].traffic_class == TrafficClass.DIGEST
    assert "Reply OK" in queued[0].text
    assert await service.solicitation_preview(event.id) == []
    with pytest.raises(ValueError, match="No unsolicited"):
        await service.solicit(event.id)
    await database.close()


@pytest.mark.asyncio
async def test_need_help_reports_zero_when_requester_is_only_responder(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock = VirtualClock()
    governor = production_governor(database, clock)
    members = MemberRepo(database, clock)
    caller = await members.resolve("!00000001")
    await database.write("UPDATE member SET trust='responder' WHERE id=?", (caller.id,))
    service = CheckinService(database, governor, clock)

    result = await service.checkin(caller, "need_help", "Trapped")

    assert result["notification"] == {
        "state": "empty_audience",
        "admitted": 0,
        "reason": "empty_audience",
    }
    row = (await database.read("SELECT notification_state,notification_count FROM checkin"))[0]
    assert dict(row) == {"notification_state": "empty_audience", "notification_count": 0}
    assert governor.queued_items() == []
    system_mail = (await database.read("SELECT state,body FROM mail"))[0]
    assert system_mail["state"] == "failed"
    assert "No recipient was reached" in system_mail["body"]
    await database.close()


@pytest.mark.asyncio
async def test_responder_groups_are_role_bounded_and_target_welfare_rosters(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock = VirtualClock()
    members = MemberRepo(database, clock)
    responder = await members.resolve("!00000001")
    responder = await members.claim_handle(responder.mesh_id, "medic")
    ordinary = await members.resolve("!00000002")
    ordinary = await members.claim_handle(ordinary.mesh_id, "neighbor")
    await database.write("UPDATE member SET trust='responder' WHERE id=?", (responder.id,))
    service = CheckinService(database, production_governor(database, clock), clock)

    group = await service.create_group("Medical", "medical", "web:operator")
    with pytest.raises(ValueError, match="only responder or operator"):
        await service.set_group_members(group["id"], [ordinary.id], "web:operator")
    await service.set_group_members(group["id"], [responder.id], "web:operator")
    event = await service.open_event(
        "Medical accountability",
        "responders",
        "web:operator",
        responder_group_id=group["id"],
    )

    assert [row["mesh_id"] for row in await service.roster(event.id)] == [responder.mesh_id]
    await database.close()


@pytest.mark.asyncio
async def test_recurring_drill_enforces_opt_out_ceiling_and_reports_participation(
    tmp_path,
) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock = VirtualClock()
    clock.advance(10 * 60 * 60)
    governor = production_governor(database, clock)
    members = MemberRepo(database, clock)
    participant = await members.resolve("!00000001")
    participant = await members.claim_handle(participant.mesh_id, "participant")
    opted_out = await members.resolve("!00000002")
    opted_out = await members.claim_handle(opted_out.mesh_id, "optedout")
    service = CheckinService(database, governor, clock)
    await service.set_drill_participation(opted_out.id, False)

    preview = await service.schedule_preview("Saturday readiness", "all")
    assert preview["recipient_count"] == 1
    assert preview["message"].startswith("DRILL — Outpost welfare check")
    schedule = await service.create_schedule(
        "Saturday readiness",
        "weekly",
        5,
        "10:00",
        "all",
        "web:operator",
        preview_token=preview["preview_token"],
    )
    now = int(clock.now().timestamp())
    await database.write("UPDATE welfare_schedule SET next_run_at=? WHERE id=?", (now, schedule.id))

    assert await service.run_due_schedules() == [{"schedule_id": schedule.id, "outcome": "started"}]
    event = await service.current_event()
    assert event is not None and event.event_kind == "drill"
    assert event.auto_close_at == now + 120 * 60
    assert [row["mesh_id"] for row in await service.roster(event.id)] == [participant.mesh_id]
    assert len(governor.queued_items()) == 1
    assert governor.queued_items()[0].text.startswith("DRILL —")
    assert "HELPME always reports real need" in governor.queued_items()[0].text

    await service.checkin(participant, "ok", "Practice response")
    report = await service.participation_report()
    assert report["nets"][0]["response_rate"] == 100.0
    assert report["never_responded"] == []
    assert report["not_heard_since_last_net"] == []
    assert report["runs"][0]["outcome"] == "started"

    real = await service.open_event("Actual flood", "all", "web:operator")
    assert real.event_kind == "real"
    assert (await service.by_id(event.id)).closed_at is not None  # type: ignore[union-attr]
    assert opted_out.mesh_id in [row["mesh_id"] for row in await service.roster(real.id)]
    await database.close()


@pytest.mark.asyncio
async def test_scheduled_drill_suppresses_for_real_event_quiet_hours_and_roster_growth(
    tmp_path,
) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock = VirtualClock()
    clock.advance(10 * 60 * 60)
    governor = production_governor(database, clock)
    members = MemberRepo(database, clock)
    first = await members.resolve("!00000001")
    await members.claim_handle(first.mesh_id, "first")
    service = CheckinService(database, governor, clock)
    night_preview = await service.schedule_preview("Night drill", "all")
    with pytest.raises(ValueError, match="quiet hours"):
        await service.create_schedule(
            "Night drill",
            "weekly",
            5,
            "23:00",
            "all",
            "web:operator",
            preview_token=night_preview["preview_token"],
        )
    preview = await service.schedule_preview("Bounded drill", "all")
    schedule = await service.create_schedule(
        "Bounded drill",
        "weekly",
        5,
        "10:00",
        "all",
        "web:operator",
        preview_token=preview["preview_token"],
    )
    real = await service.open_event("Actual incident", "all", "web:operator")
    now = int(clock.now().timestamp())
    await database.write("UPDATE welfare_schedule SET next_run_at=? WHERE id=?", (now, schedule.id))
    result = await service.run_due_schedules()
    assert result == [{"schedule_id": schedule.id, "outcome": "suppressed_real_event"}]
    assert (await service.current_event()).id == real.id  # type: ignore[union-attr]
    await service.close_event(real.id)

    clock.advance(1)
    now = int(clock.now().timestamp())
    governor.config.quiet_hours.start = "09:00"
    governor.config.quiet_hours.end = "11:00"
    await database.write("UPDATE welfare_schedule SET next_run_at=? WHERE id=?", (now, schedule.id))
    assert await service.run_due_schedules() == [
        {"schedule_id": schedule.id, "outcome": "suppressed_quiet_hours"}
    ]
    governor.config.quiet_hours.start = "22:00"
    governor.config.quiet_hours.end = "06:00"

    second = await members.resolve("!00000002")
    await members.claim_handle(second.mesh_id, "second")
    clock.advance(1)
    now = int(clock.now().timestamp())
    await database.write("UPDATE welfare_schedule SET next_run_at=? WHERE id=?", (now, schedule.id))
    result = await service.run_due_schedules()
    assert result == [{"schedule_id": schedule.id, "outcome": "suppressed_airtime_growth"}]
    assert await service.current_event() is None
    run = (
        await database.read(
            "SELECT outcome,recipient_count FROM welfare_schedule_run ORDER BY id DESC LIMIT 1"
        )
    )[0]
    assert dict(run) == {"outcome": "suppressed_airtime_growth", "recipient_count": 2}
    assert governor.queued_items() == []
    await database.close()


@pytest.mark.asyncio
async def test_operator_api_manages_groups_schedules_and_drill_report(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock = VirtualClock()
    clock.advance(10 * 60 * 60)
    members = MemberRepo(database, clock)
    responder = await members.resolve("!00000001")
    responder = await members.claim_handle(responder.mesh_id, "medic")
    await database.write("UPDATE member SET trust='responder' WHERE id=?", (responder.id,))
    service = CheckinService(database, production_governor(database, clock), clock)
    client = TestClient(
        create_web_app(lambda: {"radio": "up"}, database=database, checkins=service)
    )

    created_group = client.post(
        "/api/v1/responder-groups", json={"name": "Medical", "response_type": "medical"}
    )
    assert created_group.status_code == 200
    group_id = created_group.json()["id"]
    assigned = client.put(
        f"/api/v1/responder-groups/{group_id}/members",
        json={"member_ids": [responder.id]},
    )
    assert assigned.status_code == 200
    assert assigned.json()["members"][0]["handle"] == "medic"

    preview = client.post(
        "/api/v1/welfare-schedules/preview",
        json={
            "name": "Medic practice",
            "cadence": "weekly",
            "day_of_period": 5,
            "local_time": "10:00",
            "roster_policy": "responders",
            "responder_group_id": group_id,
            "window_minutes": 120,
        },
    )
    assert preview.status_code == 200
    assert preview.json()["recipient_count"] == 1
    assert preview.json()["message"].startswith("DRILL —")
    created_schedule = client.post(
        "/api/v1/welfare-schedules",
        json={
            "name": "Medic practice",
            "cadence": "weekly",
            "day_of_period": 5,
            "local_time": "10:00",
            "roster_policy": "responders",
            "responder_group_id": group_id,
            "window_minutes": 120,
            "suppress_if_real_event": True,
            "preview_token": preview.json()["preview_token"],
        },
    )
    assert created_schedule.status_code == 200
    schedule = created_schedule.json()
    assert schedule["recipient_limit"] == 1
    assert schedule["airtime_limit_seconds"] > 0
    assert schedule["timezone"] == "UTC" and schedule["next_run_local"].endswith("UTC")
    assert client.get("/api/v1/welfare-schedules").json()["items"][0]["id"] == schedule["id"]
    assert client.get("/api/v1/welfare-report").json()["nets"] == []

    stale_preview = client.post(
        "/api/v1/welfare-schedules/preview",
        json={
            "name": "All-member practice",
            "cadence": "weekly",
            "day_of_period": 5,
            "local_time": "10:00",
            "roster_policy": "all",
            "window_minutes": 120,
        },
    ).json()
    newcomer = await members.resolve("!00000002")
    await members.claim_handle(newcomer.mesh_id, "newcomer")
    changed = client.post(
        "/api/v1/welfare-schedules",
        json={
            "name": "All-member practice",
            "cadence": "weekly",
            "day_of_period": 5,
            "local_time": "10:00",
            "roster_policy": "all",
            "window_minutes": 120,
            "preview_token": stale_preview["preview_token"],
        },
    )
    assert changed.status_code == 409
    assert changed.json()["error"]["code"] == "schedule_preview_changed"

    assert client.delete(f"/api/v1/responder-groups/{group_id}").status_code == 200
    paused = client.get("/api/v1/welfare-schedules").json()["items"][0]
    assert paused["enabled"] is False
    assert paused["responder_group_id"] is None
    assert paused["last_outcome"] == "group_removed"
    denied_resume = client.patch(
        f"/api/v1/welfare-schedules/{schedule['id']}", json={"enabled": True}
    )
    assert denied_resume.status_code == 422
    assert client.delete(f"/api/v1/welfare-schedules/{schedule['id']}").status_code == 200
    assert client.get("/api/v1/welfare-schedules").json()["items"] == []

    audit_actions = {
        row["action"]
        for row in await database.read(
            "SELECT action FROM audit_log WHERE action LIKE 'responder_group.%' "
            "OR action LIKE 'welfare_schedule.%'"
        )
    }
    assert audit_actions == {
        "responder_group.create",
        "responder_group.delete",
        "responder_group.members",
        "welfare_schedule.create",
        "welfare_schedule.delete",
    }
    await database.close()
