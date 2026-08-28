from __future__ import annotations

import pytest

from outpost.config import ChannelConfig
from outpost.router.channel_policy import decide
from outpost.router.models import (
    ChannelUse,
    CommandContext,
    CommandSpec,
    Response,
    ResponseKind,
    TrustLevel,
)
from outpost.transport.models import TrafficClass


async def unused_handler(_context: CommandContext) -> Response:
    return Response(ResponseKind.NONE)


def command(use: ChannelUse) -> CommandSpec:
    return CommandSpec(
        name=use.value.upper(),
        aliases=(),
        module="test",
        min_trust=TrustLevel.GUEST,
        airtime_class=TrafficClass.REPLY,
        max_parts=1,
        rate_key="commands",
        help_short="test",
        mutates=False,
        handler=unused_handler,
        channel_use=use,
    )


@pytest.mark.parametrize("channel", range(8))
@pytest.mark.parametrize("use", list(ChannelUse))
def test_every_configured_channel_allows_a_fully_enabled_policy(
    channel: int, use: ChannelUse
) -> None:
    policy = ChannelConfig(
        name=f"channel-{channel}",
        bbs="full",
        accept_reports=True,
        alerts=True,
        ai=True,
    )

    assert decide(command(use), direct=False, policy=policy).allowed


@pytest.mark.parametrize("use", list(ChannelUse))
def test_unconfigured_broadcast_channels_fail_closed(use: ChannelUse) -> None:
    decision = decide(command(use), direct=False, policy=None)

    assert not decision.allowed
    assert decision.reason == "unconfigured"


@pytest.mark.parametrize("use", list(ChannelUse))
def test_direct_messages_bypass_broadcast_policy(use: ChannelUse) -> None:
    assert decide(command(use), direct=True, policy=None).allowed


@pytest.mark.parametrize(
    ("bbs", "read_allowed", "write_allowed"),
    (("none", False, False), ("read_only", True, False), ("full", True, True)),
)
def test_bbs_policy_values(bbs: str, read_allowed: bool, write_allowed: bool) -> None:
    policy = ChannelConfig(name="test", bbs=bbs)  # type: ignore[arg-type]

    assert decide(command(ChannelUse.BBS_READ), direct=False, policy=policy).allowed is read_allowed
    assert (
        decide(command(ChannelUse.BBS_WRITE), direct=False, policy=policy).allowed is write_allowed
    )


@pytest.mark.parametrize(
    ("use", "field", "reason"),
    (
        (ChannelUse.REPORT, "accept_reports", "reports_disabled"),
        (ChannelUse.ALERT, "alerts", "alerts_disabled"),
        (ChannelUse.AI, "ai", "ai_disabled"),
    ),
)
def test_independent_channel_capabilities(use: ChannelUse, field: str, reason: str) -> None:
    values = {"name": "test", field: False}
    decision = decide(command(use), direct=False, policy=ChannelConfig.model_validate(values))

    assert not decision.allowed
    assert decision.reason == reason
