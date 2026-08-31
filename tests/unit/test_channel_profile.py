import pytest

from outpost.channel_profile import apply_channel_bindings, outpost_display_name
from outpost.config import Config


def test_outpost_display_name_adds_radio_suffix_without_duplicating_it() -> None:
    assert outpost_display_name("Pittsburgh Outpost", "!699c2f30") == ("Pittsburgh Outpost 2f30")
    assert outpost_display_name("Pittsburgh Outpost 2F30", "!699c2f30") == (
        "Pittsburgh Outpost 2F30"
    )
    assert outpost_display_name("Outpost", "not-a-mesh-id") == "Outpost"
    assert len(outpost_display_name("é" * 40, "!699c2f30").encode()) <= 40


def test_channel_bindings_move_semantic_policy_and_escalation_channels() -> None:
    config = Config.model_validate(
        {
            "channels": {
                0: {"name": "public", "bbs": "read_only"},
                2: {"name": "outpost", "bbs": "full", "ai": True},
                3: {"name": "watch"},
                6: {"name": "local-logistics"},
            }
        }
    )

    apply_channel_bindings(config, {"public": 2, "outpost": 3, "watch": 4})

    assert {index: policy.name for index, policy in config.channels.items()} == {
        2: "public",
        3: "outpost",
        4: "watch",
        6: "local-logistics",
    }
    assert config.watch.escalation.urgent.stages[0].channels == [4]
    assert config.watch.escalation.critical.stages[0].channels == [2, 4]

    with pytest.raises(ValueError, match="custom Outpost policy"):
        apply_channel_bindings(config, {"public": 2, "outpost": 3, "watch": 6})
