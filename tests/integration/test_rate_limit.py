import pytest

from outpost.clock import VirtualClock
from outpost.security.rate_limit import RateLimiter
from outpost.store import Database


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
