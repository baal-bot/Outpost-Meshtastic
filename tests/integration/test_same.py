import pytest

from outpost.clock import VirtualClock
from outpost.config import SameConfig
from outpost.env import SameService
from outpost.store import Database


@pytest.mark.asyncio
async def test_same_test_message_is_relevant_logged_once_and_never_broadcastable(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    service = SameService(database, VirtualClock(), SameConfig(county_codes=["042003"]))
    header = "ZCZC-WXR-RWT-042003+0030-2361200-KPBZ/NWS-"
    message, created = await service.ingest(header)
    duplicate, created_again = await service.ingest(header)
    assert created and not created_again
    assert message.is_test and message.relevant
    assert duplicate.header == message.header
    assert not (message.relevant and not message.is_test and created)
    rows = await database.read("SELECT * FROM same_event")
    assert len(rows) == 1 and rows[0]["is_test"] == 1
    await database.close()


@pytest.mark.asyncio
async def test_same_live_warning_filters_county_and_exposes_silence_health(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock = VirtualClock()
    service = SameService(
        database,
        clock,
        SameConfig(enabled=True, county_codes=["042003"], silence_alarm_minutes=10),
    )
    assert service.health()["status"] == "no_signal"
    unrelated, _ = await service.ingest("ZCZC-WXR-TOR-039001+0015-2361200-KCLE/NWS-")
    assert not unrelated.relevant and not unrelated.is_test
    relevant, _ = await service.ingest("ZCZC-WXR-TOR-042003+0015-2361200-KPBZ/NWS-")
    assert relevant.relevant and not relevant.is_test
    assert service.health()["status"] == "up"
    clock.advance(601)
    assert service.health()["status"] == "no_signal"
    await database.close()


def test_same_rejects_malformed_header() -> None:
    service = SameService(None, VirtualClock(), SameConfig())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="invalid SAME"):
        service.parse("not a SAME message")
