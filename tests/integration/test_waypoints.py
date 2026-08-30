import json

import pytest

from outpost.clock import VirtualClock
from outpost.env import WaypointService
from outpost.store import Database


@pytest.mark.asyncio
async def test_waypoint_crud_slug_and_distance(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    service = WaypointService(database, VirtualClock())

    created = await service.create(
        "Shelter Alpha",
        40.4500,
        -80.0100,
        "shelter",
        "West entrance",
        "field-operator",
    )
    assert created["slug"] == "shelter-alpha"
    assert (await service.by_token("shelter-alpha"))["id"] == created["id"]
    assert (await service.by_token(str(created["id"])))["name"] == "Shelter Alpha"

    distance, bearing = service.distance_bearing(40.4406, -79.9959, created)
    assert 1 < distance < 2
    assert 270 <= bearing <= 360

    updated = await service.update(
        created["id"],
        {"name": "Shelter Bravo", "notes": "Main doors"},
        "field-operator",
    )
    assert updated["slug"] == "shelter-bravo"
    assert updated["notes"] == "Main doors"

    await service.delete(created["id"], "field-operator")
    assert await service.list() == []
    audit = await database.read(
        "SELECT actor_kind,actor_ref,action,target,detail FROM audit_log "
        "WHERE target=? ORDER BY id",
        (f"waypoint:{created['id']}",),
    )
    assert [row["action"] for row in audit] == [
        "waypoint.create",
        "waypoint.update",
        "waypoint.delete",
    ]
    assert {(row["actor_kind"], row["actor_ref"]) for row in audit} == {("web", "field-operator")}
    update_detail = json.loads(audit[1]["detail"])
    assert update_detail["before"]["name"] == "Shelter Alpha"
    assert update_detail["after"]["name"] == "Shelter Bravo"
    with pytest.raises(ValueError, match="not found"):
        await service.get(created["id"])
    await database.close()


@pytest.mark.asyncio
async def test_waypoint_rejects_duplicate_names_and_bad_coordinates(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    service = WaypointService(database, VirtualClock())
    await service.create("Radio Hill", 40.4, -80.0, "radio", "")

    with pytest.raises(ValueError, match="already exists"):
        await service.create("Radio Hill", 40.5, -80.1, "radio", "")
    with pytest.raises(ValueError, match="bounds"):
        await service.create("Invalid", 100, 0, "general", "")
    await database.close()


@pytest.mark.asyncio
async def test_member_can_change_position_privacy(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    service = WaypointService(database, VirtualClock())
    member_id = await database.write(
        "INSERT INTO member(mesh_id,mesh_num,trust,first_seen,last_seen) VALUES(?,?,?,?,?)",
        ("!00000001", 1, "member", 0, 0),
    )
    assert await service.position_privacy(member_id) == "coarse"
    assert await service.set_position_privacy(member_id, "full") == "full"
    assert await service.position_privacy(member_id) == "full"
    assert await service.set_position_privacy(member_id, "off") == "off"
    with pytest.raises(ValueError, match="full, coarse, or off"):
        await service.set_position_privacy(member_id, "public")
    await database.close()


def test_distance_bearing_handles_antimeridian_and_polar_routes() -> None:
    antimeridian = {"latitude": 0.0, "longitude": -179.9}
    distance, bearing = WaypointService.distance_bearing(0.0, 179.9, antimeridian)
    assert distance == pytest.approx(22.24, abs=0.1)
    assert bearing == 90

    polar = {"latitude": 89.0, "longitude": 90.0}
    distance, bearing = WaypointService.distance_bearing(89.0, 0.0, polar)
    assert distance == pytest.approx(157.25, abs=0.2)
    assert bearing == 45


def test_member_position_privacy_full_coarse_and_off() -> None:
    value = {"lat": 40.44061, "lon": -79.99591, "prefs": '{"position":"full"}'}
    assert WaypointService.privacy_position(value, 500) == (40.44061, -79.99591)

    value["prefs"] = '{"position":"coarse"}'
    coarse = WaypointService.privacy_position(value, 500)
    assert coarse is not None and coarse != (value["lat"], value["lon"])
    distance, _ = WaypointService.distance_bearing(
        value["lat"], value["lon"], {"latitude": coarse[0], "longitude": coarse[1]}
    )
    assert distance <= 0.36
    assert WaypointService.privacy_position(value, 500, operator=True) == (
        value["lat"],
        value["lon"],
    )

    value["prefs"] = '{"position":"off"}'
    assert WaypointService.privacy_position(value, 500) is None
    assert WaypointService.privacy_position(value, 500, operator=True) is None
