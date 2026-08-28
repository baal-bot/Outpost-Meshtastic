from __future__ import annotations

from dataclasses import dataclass, field

from outpost.clock import Clock

from .models import ContextFrame


@dataclass
class PendingAction:
    """A bounded menu selection or one-shot guided input continuation."""

    action: str
    partial_args: str
    prompt: str
    expires_at: float
    on_timeout: str = "discard"
    choices: dict[str, str] = field(default_factory=dict)
    input_command: str | None = None


@dataclass
class Session:
    member_id: str
    channel: int
    context: list[ContextFrame] = field(default_factory=list)
    pending: PendingAction | None = None
    last_seen: float = 0.0
    page_refs: list[int] = field(default_factory=list)
    cursor_kind: str | None = None
    cursor_target: str | None = None
    cursor_offset: int = 0
    cursor_expires_at: float = 0.0
    last_mail_id: int | None = None
    last_mail_sender: str | None = None
    tui_active: bool = False
    tui_screen: str | None = None

    def push(self, frame: ContextFrame) -> None:
        if len(self.context) >= 3:
            self.context[-1] = frame
        else:
            self.context.append(frame)


class SessionStore:
    def __init__(self, clock: Clock, idle_minutes: int = 30) -> None:
        self.clock, self.idle_seconds = clock, idle_minutes * 60
        self._sessions: dict[tuple[str, int], Session] = {}

    def get(self, member_id: str, channel: int) -> Session:
        key = (member_id, channel)
        now = self.clock.monotonic()
        session = self._sessions.get(key)
        if session is None:
            session = Session(member_id, channel, last_seen=now)
            self._sessions[key] = session
        elif session.last_seen + self.idle_seconds < now:
            session.context.clear()
            session.pending = None
            session.tui_active = False
            session.tui_screen = None
        session.last_seen = now
        return session
