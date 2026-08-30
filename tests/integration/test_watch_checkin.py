import pytest

from outpost.clock import VirtualClock
from outpost.config import AirtimeConfig
from outpost.store import Database
from outpost.store.members import MemberRepo
from outpost.transport.governor import AirtimeGovernor
from outpost.transport.models import TrafficClass
from outpost.transport.simulated import SimulatedRadioLink
from outpost.watch import CheckinService, IncidentService


@pytest.mark.asyncio
async def test_event_roster_checkins_and_csv(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock = VirtualClock()
    governor = AirtimeGovernor(SimulatedRadioLink(), AirtimeConfig(), clock)
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
async def test_need_help_uses_position_and_notifies_responders(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock = VirtualClock()
    governor = AirtimeGovernor(SimulatedRadioLink(), AirtimeConfig(), clock)
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
    governor = AirtimeGovernor(SimulatedRadioLink(), AirtimeConfig(), clock)
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
    governor = AirtimeGovernor(SimulatedRadioLink(), AirtimeConfig(), clock)
    members = MemberRepo(database, clock)
    member = await members.resolve("!00000001")
    member = await members.claim_handle(member.mesh_id, "member")
    await members.resolve("!00000002")  # Discovered guest must never receive this message.
    service = CheckinService(database, governor, clock)
    event = await service.open_event("Ice storm", "all", "operator")

    result = await service.solicit(event.id)

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
    governor = AirtimeGovernor(SimulatedRadioLink(), AirtimeConfig(), clock)
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
