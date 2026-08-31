import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from outpost.clock import SystemClock, VirtualClock
from outpost.env import WaypointService
from outpost.security.rate_limit import RateLimiter
from outpost.store import Database
from outpost.store.backups import BackupService
from outpost.store.members import MemberRepo
from outpost.watch import CheckinService, IncidentService
from outpost.web.api import create_web_app
from tests.support.application import production_governor

pytestmark = pytest.mark.production_wiring


def test_expiry_migration_suppresses_legacy_exact_positions(tmp_path: Path) -> None:
    connection = sqlite3.connect(tmp_path / "legacy.db")
    connection.executescript(
        """CREATE TABLE member_position (
             member_id INTEGER PRIMARY KEY,
             lat REAL NOT NULL,
             lon REAL NOT NULL,
             received_at INTEGER NOT NULL,
             source TEXT NOT NULL DEFAULT 'position_app'
           );
           INSERT INTO member_position VALUES(1,40,-80,123,'position_app');"""
    )
    migration = (
        Path(__file__).parents[2] / "src/outpost/store/migrations/0138_member_position_expiry.sql"
    ).read_text()
    connection.executescript(migration)

    assert connection.execute("SELECT expires_at=received_at FROM member_position").fetchone()[0]
    connection.close()


@pytest.mark.asyncio
async def test_expired_position_is_rejected_by_member_and_safety_consumers(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock = VirtualClock()
    member = await MemberRepo(database, clock).resolve("!00000001")
    member = await MemberRepo(database, clock).claim_handle(member.mesh_id, "dana")
    incidents = IncidentService(database, clock, position_retention_hours=1)
    waypoints = WaypointService(database, clock)
    await incidents.record_position(member, 40.4406, -79.9959, prompt=False)

    stored = await database.read(
        "SELECT received_at,expires_at FROM member_position WHERE member_id=?", (member.id,)
    )
    assert stored[0]["expires_at"] - stored[0]["received_at"] == 3_600
    assert await waypoints.member_position(member_id=member.id) is not None

    clock.advance(3_601)
    assert await waypoints.member_position(member_id=member.id) is None
    governor = production_governor(database, clock)
    checkins = CheckinService(database, governor, clock)
    await checkins.checkin(member, "ok")
    checkin = await database.read("SELECT lat,lon FROM checkin WHERE member_id=?", (member.id,))
    assert checkin[0]["lat"] is None and checkin[0]["lon"] is None
    emergency, _ = await incidents.emergency_trigger(member, "SOS need help", 10)
    assert emergency.lat is None and emergency.lon is None

    limiter = RateLimiter(clock, database=database)
    first = await limiter.safety_floor_decision(member.mesh_id, "REPORT", "road blocked")
    await database.write(
        "UPDATE member_position SET lat=41,lon=-81 WHERE member_id=?", (member.id,)
    )
    repeated = await limiter.safety_floor_decision(member.mesh_id, "REPORT", "road blocked")
    assert first.accepted is True and repeated.accepted is False
    await database.close()


@pytest.mark.asyncio
async def test_operator_position_lifecycle_and_sensitive_export_disclosure(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock = SystemClock()
    member = await MemberRepo(database, clock).resolve("!00000001")
    member = await MemberRepo(database, clock).claim_handle(member.mesh_id, "dana")
    discovered = await MemberRepo(database, clock).resolve("!00000002")
    now = int(time.time())
    await database.write(
        "INSERT INTO member_position(member_id,lat,lon,received_at,source,expires_at) "
        "VALUES(?,40.4406,-79.9959,?,'position_app',?)",
        (member.id, now - 60, now + 7_140),
    )
    await database.write("UPDATE member SET long_name='Field Radio' WHERE id=?", (discovered.id,))
    await database.write(
        "INSERT INTO member_position(member_id,lat,lon,received_at,source,expires_at) "
        "VALUES(?,40.45,-80.01,?,'position_app',?)",
        (discovered.id, now - 30, now + 7_170),
    )
    await database.write(
        "INSERT INTO pending_incident_location(member_id,lat,lon,created_at,expires_at) "
        "VALUES(?,40.4406,-79.9959,?,?)",
        (member.id, now, now + 600),
    )
    governor = production_governor(database, clock)
    checkins = CheckinService(database, governor, clock)
    await database.write("UPDATE member SET trust='responder' WHERE id=?", (member.id,))
    group = await checkins.create_group("Medical", "medical", "web:operator")
    await checkins.set_group_members(group["id"], [member.id], "web:operator")
    event = await checkins.open_event("Privacy exercise", "all", "operator")
    await checkins.checkin(member, "ok")
    backups = BackupService(database)
    backup = await backups.create()
    incidents = IncidentService(database, clock)
    client = TestClient(
        create_web_app(
            lambda: {"radio": "up"},
            database=database,
            backups=backups,
            incidents=incidents,
            checkins=checkins,
        )
    )

    item = client.get("/api/v1/members/map").json()["items"][0]
    assert item["category"] == "approved"
    assert item["source"] == "position_app"
    assert item["age_seconds"] >= 60
    assert item["visibility"] == "operator exact; member coarse"
    assert item["retention_hours"] == 2
    assert item["expires_at"] > item["received_at"]
    assert item["responder_groups"] == [
        {"id": group["id"], "name": "Medical", "response_type": "medical"}
    ]
    discovered_items = client.get("/api/v1/members/map?view=discovered").json()["items"]
    assert len(discovered_items) == 1
    assert discovered_items[0]["mesh_id"] == discovered.mesh_id
    assert discovered_items[0]["long_name"] == "Field Radio"
    assert discovered_items[0]["category"] == "discovered"
    assert discovered_items[0]["responder_groups"] == []
    assert "mesh broadcast" in discovered_items[0]["visibility"]
    all_items = client.get("/api/v1/members/map?view=all").json()["items"]
    assert {value["category"] for value in all_items} == {"approved", "discovered"}
    assert client.get("/api/v1/members/map?view=unknown").status_code == 422
    situational = client.get("/api/v1/watch/map").json()
    assert situational["nodes"][0]["mesh_id"] == member.mesh_id
    download = client.get(f"/api/v1/backups/{backup.name}")
    assert download.headers["x-outpost-data-classification"] == ("sensitive-includes-location-data")
    roster = client.get(f"/api/v1/events/{event.id}/roster.csv")
    assert "lat,lon" in roster.text
    assert roster.headers["x-outpost-data-classification"] == (
        "sensitive-may-include-member-location-data"
    )

    deleted = client.delete(f"/api/v1/members/{member.id}/position")
    assert deleted.status_code == 200
    assert not await database.read("SELECT 1 FROM member_position WHERE member_id=?", (member.id,))
    assert not await database.read(
        "SELECT 1 FROM pending_incident_location WHERE member_id=?", (member.id,)
    )
    audit = await database.read(
        "SELECT detail FROM audit_log WHERE action='member.position_delete'"
    )
    assert audit and "40.4406" not in str(audit[0]["detail"])

    await database.write(
        "INSERT INTO member_position(member_id,lat,lon,received_at,expires_at) "
        "VALUES(?,40,-80,?,?)",
        (member.id, now - 7_201, now - 1),
    )
    assert client.get("/api/v1/members/map").json()["items"] == []
    assert client.get("/api/v1/watch/map").json()["nodes"] == []
    rejected = client.post(
        "/api/v1/members/positions/purge-expired", json={"confirmation": "PURGE"}
    )
    assert rejected.status_code == 422
    purged = client.post(
        "/api/v1/members/positions/purge-expired",
        json={"confirmation": "PURGE EXPIRED POSITIONS"},
    )
    assert purged.status_code == 200 and purged.json()["deleted"] == 1
    assert await database.read("SELECT 1 FROM audit_log WHERE action='member.position_purge'")
    script = client.get("/member-map.js").text
    assert "Scheduled deletion" in script and "Delete exact position" in script
    await database.close()
