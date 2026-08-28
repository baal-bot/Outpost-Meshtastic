from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from typing import Any, Protocol

from outpost.transport.models import InboundMessage, TrafficClass


class TrustLevel(IntEnum):
    BLOCKED = -1
    GUEST = 0
    MEMBER = 1
    TRUSTED = 2
    RESPONDER = 3
    OPERATOR = 4

    @classmethod
    def parse(cls, value: str) -> TrustLevel:
        return cls[value.upper()]


class ResponseKind(StrEnum):
    ACK = "ack"
    LISTING = "listing"
    DETAIL = "detail"
    ERROR = "error"
    NONE = "none"


class ChannelUse(StrEnum):
    """The broadcast-channel capability a command requires."""

    GENERAL = "general"
    BBS_READ = "bbs_read"
    BBS_WRITE = "bbs_write"
    REPORT = "report"
    ALERT = "alert"
    AI = "ai"


@dataclass(frozen=True)
class TuiChoice:
    """One low-friction action displayed on a mesh TUI screen."""

    label: str
    command: str
    aliases: tuple[str, ...] = ()
    key: str | None = None


@dataclass(frozen=True)
class TuiScreen:
    """Presentation and continuation metadata for a direct-message screen."""

    name: str
    title: str
    choices: tuple[TuiChoice, ...] = ()
    input_command: str | None = None
    input_prompt: str | None = None
    parent_command: str = "MENU"


@dataclass(frozen=True)
class Line:
    text: str


@dataclass
class Response:
    kind: ResponseKind
    lines: list[Line] = field(default_factory=list)
    airtime_class: TrafficClass = TrafficClass.REPLY
    context_push: ContextFrame | None = None
    context_pop: bool = False
    broadcast: bool = False
    supersedes: str | None = None
    data: dict[str, Any] | None = None
    screen: TuiScreen | None = None


@dataclass(frozen=True)
class ContextFrame:
    kind: str
    ref: str


class CommandHandler(Protocol):
    async def __call__(self, context: CommandContext) -> Response: ...


@dataclass(frozen=True)
class CommandSpec:
    name: str
    aliases: tuple[str, ...]
    module: str
    min_trust: TrustLevel
    airtime_class: TrafficClass
    max_parts: int
    rate_key: str
    help_short: str
    mutates: bool
    handler: CommandHandler
    channel_use: ChannelUse = ChannelUse.GENERAL


@dataclass
class CommandContext:
    message: InboundMessage
    member: Any
    session: Any
    args: str
    registry: Any
    node_name: str
    operator_contact: str
    version: str
    disclaimer: str
    channel_policy: Any | None = None
    attribution: str = ""
