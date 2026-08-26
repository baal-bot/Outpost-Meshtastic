import pytest

from outpost.clock import VirtualClock
from outpost.security.rate_limit import RateLimiter
from outpost.store import Database
from outpost.store.members import MemberRepo


@pytest.mark.asyncio
async def test_guest_limit_persists_across_restart(tmp_path) -> None:
    path = tmp_path / "outpost.db"
    clock = VirtualClock()
    database = Database(path)
    await database.open()
    limiter = RateLimiter(clock, database=database)
    for _ in range(4):
        assert await limiter.allow("!12345678", "guest", "PING") is True
    assert await limiter.allow("!12345678", "guest", "PING") is False
    await database.close()

    reopened = Database(path)
    await reopened.open()
    restored = RateLimiter(clock, database=reopened)
    assert await restored.allow("!12345678", "guest", "PING") is False
    await reopened.close()


@pytest.mark.asyncio
async def test_global_breaker_keeps_emergency_path_open() -> None:
    limiter = RateLimiter(VirtualClock(), global_per_minute=2)
    assert await limiter.allow("!00000001", "operator", "PING") is True
    assert await limiter.allow("!00000002", "operator", "PING") is True
    assert await limiter.allow("!00000003", "operator", "PING") is False
    assert limiter.defensive is True
    assert await limiter.allow("!00000003", "guest", "REPORT") is True


@pytest.mark.asyncio
async def test_safety_floor_bypasses_exhausted_member_bucket() -> None:
    limiter = RateLimiter(VirtualClock())
    for _ in range(4):
        assert await limiter.allow("!00000001", "guest", "PING") is True
    assert await limiter.allow("!00000001", "guest", "PING") is False
    assert await limiter.allow("!00000001", "guest", "REPORT") is True
    assert await limiter.allow("!00000001", "guest", "OK") is True
    assert await limiter.allow("!00000001", "guest", "HELPME") is True


@pytest.mark.asyncio
async def test_safety_floor_attempts_contribute_to_global_defensive_mode() -> None:
    limiter = RateLimiter(VirtualClock(), global_per_minute=2)
    assert await limiter.allow("!00000001", "guest", "HELPME") is True
    assert await limiter.allow("!00000001", "guest", "HELPME") is True
    assert await limiter.allow("!00000002", "guest", "PING") is False
    assert limiter.defensive is True


@pytest.mark.asyncio
async def test_safety_floor_coalescing_is_durable_and_accepts_changed_details(tmp_path) -> None:
    path = tmp_path / "outpost.db"
    clock = VirtualClock()
    database = Database(path)
    await database.open()
    limiter = RateLimiter(
        clock,
        database=database,
        safety_repeat_window_seconds=120,
    )

    first = await limiter.safety_floor_decision("!00000001", "HELPME", "Water rising")
    repeat = await limiter.safety_floor_decision("!00000001", "HELPME", "  WATER   rising ")
    changed = await limiter.safety_floor_decision("!00000001", "HELPME", "Water at window")
    deescalated = await limiter.safety_floor_decision("!00000001", "OK", "Water rising")
    reported = await limiter.safety_floor_decision("!00000001", "REPORT", "tree down")
    forced = await limiter.safety_floor_decision("!00000001", "REPORT!", "tree down")

    assert first.accepted is True
    assert repeat.accepted is False
    assert changed.accepted is True
    assert deescalated.accepted is True
    assert reported.accepted is True and forced.accepted is True

    member = await MemberRepo(database, clock).resolve("!00000002")
    await database.write(
        "INSERT INTO member_position(member_id,lat,lon,received_at,expires_at) VALUES(?,?,?,?,?)",
        (
            member.id,
            40.4406,
            -79.9959,
            int(clock.now().timestamp()),
            int(clock.now().timestamp()) + 3_600,
        ),
    )
    located = await limiter.safety_floor_decision("!00000002", "REPORT", "road blocked")
    located_repeat = await limiter.safety_floor_decision("!00000002", "REPORT", "road blocked")
    await database.write(
        "UPDATE member_position SET lat=?,lon=? WHERE member_id=?",
        (40.4506, -79.9859, member.id),
    )
    moved = await limiter.safety_floor_decision("!00000002", "REPORT", "road blocked")
    assert located.accepted is True
    assert located_repeat.accepted is False
    assert moved.accepted is True
    await database.close()

    reopened = Database(path)
    await reopened.open()
    restored = RateLimiter(
        clock,
        database=reopened,
        safety_repeat_window_seconds=120,
    )
    after_restart = await restored.safety_floor_decision("!00000001", "HELPME", "Water rising")
    assert after_restart.accepted is False
    clock.advance(121)
    after_window = await restored.safety_floor_decision("!00000001", "HELPME", "Water rising")
    assert after_window.accepted is True
    await reopened.close()
