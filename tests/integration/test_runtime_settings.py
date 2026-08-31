import pytest

from outpost.config import Config
from outpost.store import Database
from outpost.web.settings import RuntimeSettings


@pytest.mark.asyncio
async def test_runtime_node_settings_are_validated_persisted_and_audited(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    config = Config.model_validate({"store": {"path": str(tmp_path / "outpost.db")}})
    settings = RuntimeSettings(database, config)

    updated = await settings.update_node(
        {
            "name": "Pittsburgh Outpost",
            "short_name": "PGH",
            "location": {"lat": 40.4406, "lon": -79.9959},
        }
    )
    assert updated["name"] == "Pittsburgh Outpost"
    fresh = Config.model_validate({"store": {"path": str(tmp_path / "outpost.db")}})
    restored = RuntimeSettings(database, fresh)
    await restored.load()
    assert fresh.node.name == "Pittsburgh Outpost" and fresh.node.short_name == "PGH"
    assert fresh.node.location is not None and fresh.node.location.lat == pytest.approx(40.4406)
    assert await database.read("SELECT 1 FROM audit_log WHERE action='config.update'")
    with pytest.raises(ValueError):
        await settings.update_node({"short_name": "TOO-LONG"})
    watch = await settings.update_watch(
        {
            "emergency_keywords_enabled": True,
            "emergency_keywords": ["SOS", "help me", "sos"],
            "emergency_cooldown_minutes": 15,
            "escalation": {
                "urgent": {
                    "ack_threshold": 1,
                    "stages": [{"after_minutes": 0, "notify": "responders", "channels": [3]}],
                }
            },
        }
    )
    assert watch["emergency_keywords_enabled"] is True
    assert watch["emergency_keywords"] == ["sos", "help me"]
    await restored.load()
    assert fresh.watch.emergency_keywords_enabled is True
    assert fresh.watch.emergency_cooldown_minutes == 15
    assert fresh.watch.escalation.urgent.ack_threshold == 1
    assert fresh.watch.escalation.urgent.stages[0].proximity == "any"
    footprint = await settings.update_watch(
        {
            "escalation": {
                "urgent": {
                    "ack_threshold": 1,
                    "stages": [
                        {
                            "after_minutes": 0,
                            "notify": "responders",
                            "channels": [3],
                            "proximity": "footprint",
                        }
                    ],
                }
            }
        }
    )
    assert footprint["escalation"]["urgent"]["stages"][0]["proximity"] == "footprint"
    await restored.load()
    assert fresh.watch.escalation.urgent.stages[0].proximity == "footprint"
    await settings.bind_outpost_channels({"public": 1, "outpost": 2, "watch": 3})
    rebound = Config.model_validate(
        {
            "store": {"path": str(tmp_path / "outpost.db")},
            "channels": {
                0: {"name": "public"},
                2: {"name": "outpost"},
                3: {"name": "watch"},
            },
        }
    )
    await RuntimeSettings(database, rebound).load()
    assert {index: policy.name for index, policy in rebound.channels.items()} == {
        1: "public",
        2: "outpost",
        3: "watch",
    }
    assert rebound.watch.escalation.critical.stages[0].channels == [1, 3]
    await database.close()
