import pytest

from outpost.clock import VirtualClock
from outpost.store import Database
from outpost.store.members import MemberRepo
from outpost.watch.incidents import IncidentService


@pytest.mark.asyncio
async def test_report_inference_deduplication_and_reactions(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock = VirtualClock()
    members = MemberRepo(database, clock)
    reporter = await members.resolve("!00000001")
    confirmer = await members.resolve("!00000002")
    service = IncidentService(database, clock)

    created, similar = await service.create(
        "tree down blocking cedar ln 40.4406 -79.9959", reporter
    )
    assert similar is None and created is not None
    assert created.type == "hazard" and created.severity == "caution"
    assert created.local_ref == 1 and created.lat == pytest.approx(40.4406)

    duplicate, similar = await service.create("tree down at cedar ln 40.4407 -79.9958", confirmer)
    assert duplicate is None and similar is not None and similar.id == created.id

    confirmed = await service.react(1, confirmer, "confirm")
    assert confirmed.confirm_count == 1
    assert (await service.react(1, confirmer, "confirm")).confirm_count == 1
    disputed = await service.react(1, reporter, "dispute", "road is passable")
    assert disputed.dispute_count == 1
    await database.close()


@pytest.mark.asyncio
async def test_incident_order_and_forced_duplicate(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock = VirtualClock()
    member = await MemberRepo(database, clock).resolve("!00000001")
    service = IncidentService(database, clock)
    info, _ = await service.create("power outage 40.0 -79.0", member)
    urgent, _ = await service.create("fire near barn 40.1 -79.1", member)
    forced, _ = await service.create("fire near barn 40.1 -79.1", member, force=True)
    assert info and urgent and forced
    assert [item.severity for item in await service.list()] == ["urgent", "urgent", "info"]
    assert forced.local_ref == 3
    await database.close()


@pytest.mark.asyncio
async def test_operator_acknowledgement_and_update_are_recorded(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock = VirtualClock()
    member = await MemberRepo(database, clock).resolve("!00000001")
    service = IncidentService(database, clock)
    incident, _ = await service.create("flood at bridge 40.0 -79.0", member)
    assert incident is not None

    acknowledged = await service.operator_update(incident.id, "ack")
    updated = await service.operator_update(incident.id, "update", "Crew checking bridge")

    assert acknowledged.status == "monitoring"
    assert updated.status == "monitoring"
    changes = await service.updates(incident.id, 10)
    assert [change["kind"] for change in changes] == ["update", "ack"]
    assert changes[0]["body"] == "Crew checking bridge"
    await database.close()


@pytest.mark.asyncio
async def test_due_incidents_expire_without_being_deleted(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock = VirtualClock()
    member = await MemberRepo(database, clock).resolve("!00000001")
    service = IncidentService(database, clock)
    incident, _ = await service.create("fire near shed 40.0 -79.0", member)
    assert incident is not None and incident.expires_at is not None

    clock.advance(12 * 3600 + 1)
    expired = await service.expire_due()

    assert [item.id for item in expired] == [incident.id]
    stored = await service.by_id(incident.id)
    assert stored is not None and stored.status == "expired"
    assert await service.list() == []
    changes = await service.updates(incident.id)
    assert changes[0]["kind"] == "status_change"
    assert await service.expire_due() == []
    await database.close()


@pytest.mark.asyncio
async def test_shared_position_becomes_pending_report_location(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock = VirtualClock()
    member = await MemberRepo(database, clock).resolve("!00000001")
    service = IncidentService(database, clock)

    await service.record_position(member, 40.4406, -79.9959, prompt=True)
    result = await service.create_from_pending("tree blocking the road", member)
    assert result is not None
    incident, similar = result
    assert similar is None and incident is not None
    assert incident.type == "hazard"
    assert incident.lat == pytest.approx(40.4406)
    assert await service.pending_position(member) is None
    assert await service.create_from_pending("another message", member) is None
    await database.close()


@pytest.mark.asyncio
async def test_report_can_use_saved_waypoint_without_creating_location_incident(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock = VirtualClock()
    member = await MemberRepo(database, clock).resolve("!00000001")
    await database.write(
        """INSERT INTO waypoint(name,slug,latitude,longitude,category,created_at,updated_at)
           VALUES(?,?,?,?,?,0,0)""",
        ("North Spring", "north-spring", 40.4567, -79.9876, "water"),
    )
    incident, similar = await IncidentService(database, clock).create(
        "-wp north-spring water contamination reported", member
    )
    assert similar is None and incident is not None
    assert incident.lat == pytest.approx(40.4567)
    assert incident.lon == pytest.approx(-79.9876)
    assert incident.location_text == "North Spring"
    assert incident.body == "water contamination reported"
    assert len(await IncidentService(database, clock).list()) == 1
    await database.close()


def test_taxonomy_and_haversine() -> None:
    assert IncidentService.infer("washout on mill road") == "road"
    assert IncidentService.infer("water available at school") == "resource"
    assert IncidentService.infer("unknown situation") == "other"
    distance = IncidentService.distance_m(40.4406, -79.9959, 40.4416, -79.9959)
    assert 110 < distance < 112
    assert IncidentService.is_position_share_notice(
        "📍 Meshtastic 55af has shared their position and requested a response with your position."
    )
    assert not IncidentService.is_position_share_notice("Animal in road")
    assert IncidentService.emergency_keyword("MAYDAY at the bridge", ["sos", "mayday"])
    assert IncidentService.emergency_keyword("please help me now", ["help me"])
    assert not IncidentService.emergency_keyword("social event", ["sos"])


@pytest.mark.asyncio
async def test_emergency_keyword_cooldown_appends_to_one_urgent_incident(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock = VirtualClock()
    member = await MemberRepo(database, clock).resolve("!00000001")
    service = IncidentService(database, clock)

    first, created = await service.emergency_trigger(member, "SOS water rising", 10)
    second, created_again = await service.emergency_trigger(member, "SOS now at porch", 10)

    assert created and not created_again
    assert first.id == second.id
    assert second.type == "other" and second.severity == "urgent"
    assert len(await service.list()) == 1
    assert (await service.updates(first.id))[0]["body"] == "SOS now at porch"
    await database.close()
