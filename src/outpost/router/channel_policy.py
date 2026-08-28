from __future__ import annotations

from dataclasses import dataclass

from prometheus_client import Counter

from outpost.config import ChannelConfig

from .models import ChannelUse, CommandSpec

CHANNEL_POLICY_REJECTIONS = Counter(
    "outpost_router_channel_policy_rejections_total",
    "Mesh commands rejected by broadcast channel policy",
    ("channel", "family", "reason"),
)


@dataclass(frozen=True)
class ChannelDecision:
    allowed: bool
    reason: str = ""
    message: str = ""


def decide(
    spec: CommandSpec,
    *,
    direct: bool,
    policy: ChannelConfig | None,
) -> ChannelDecision:
    """Authorize a resolved command without inspecting its arguments or payload."""
    if direct:
        return ChannelDecision(True)
    if policy is None:
        return ChannelDecision(
            False,
            "unconfigured",
            "Channel unavailable · DM ? for available actions.",
        )
    if spec.channel_use == ChannelUse.BBS_READ and policy.bbs == "none":
        return ChannelDecision(False, "bbs_disabled", "BBS is off here · DM ? for options.")
    if spec.channel_use == ChannelUse.BBS_WRITE and policy.bbs != "full":
        reason = "bbs_disabled" if policy.bbs == "none" else "bbs_read_only"
        text = (
            "BBS is off here · DM ? for options."
            if policy.bbs == "none"
            else "BBS is read-only here · DM ? for options."
        )
        return ChannelDecision(False, reason, text)
    if spec.channel_use == ChannelUse.REPORT and not policy.accept_reports:
        return ChannelDecision(
            False,
            "reports_disabled",
            "Incident reports are off here · DM ? for options.",
        )
    if spec.channel_use == ChannelUse.ALERT and not policy.alerts:
        return ChannelDecision(False, "alerts_disabled", "Alerts are off here · DM ? for options.")
    if spec.channel_use == ChannelUse.AI and not policy.ai:
        return ChannelDecision(
            False,
            "ai_disabled",
            "AI is off on this channel. DM ASK <question> instead.",
        )
    return ChannelDecision(True)


def available(spec: CommandSpec, *, direct: bool, policy: ChannelConfig | None) -> bool:
    return decide(spec, direct=direct, policy=policy).allowed
