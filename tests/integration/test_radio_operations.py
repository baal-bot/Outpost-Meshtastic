import pytest

from outpost.clock import VirtualClock
from outpost.config import AirtimeConfig
from outpost.radio_operations import RadioOperations
from outpost.store import Database
from outpost.transport.governor import AirtimeGovernor


@pytest.mark.asyncio
async def test_operator_send_uses_governor_and_queue_can_be_cancelled(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock = VirtualClock()
    governor = AirtimeGovernor(object(), AirtimeConfig(), clock)  # type: ignore[arg-type]
    operations = RadioOperations(database, governor, clock)

    item_id = await operations.send("Road closed", "^all", 0, "bulletin")
    assert operations.queue()[0]["id"] == item_id
    assert operations.queue()[0]["traffic_class"] == "bulletin"
    assert await operations.cancel(item_id) is True
    assert operations.queue() == []
    actions = [row["action"] for row in await database.read("SELECT action FROM audit_log")]
    assert actions == ["mesh.send", "queue.cancel"]
    with pytest.raises(ValueError):
        await operations.send("x" * 201, "^all", 0, "bulletin")
    await database.close()
