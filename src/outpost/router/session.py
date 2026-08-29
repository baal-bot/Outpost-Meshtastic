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


@dataclass(frozen=True)
class TuiConfirmation:
    action: str
    target: str
    payload: dict[str, str]
    expires_at: float


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
    tui_snapshots: dict[str, list[str]] = field(default_factory=dict)
    tui_snapshot_expires_at: float = 0.0
    tui_confirmations: dict[str, TuiConfirmation] = field(default_factory=dict)

    def push(self, frame: ContextFrame) -> None:
        if len(self.context) >= 3:
            self.context[-1] = frame
        else:
            self.context.append(frame)

    def expire_tui_sensitive(self, now: float) -> None:
        if self.tui_snapshot_expires_at <= now:
            self.tui_snapshots.clear()
            self.tui_snapshot_expires_at = 0.0
        self.tui_confirmations = {
            token: confirmation
            for token, confirmation in self.tui_confirmations.items()
            if confirmation.expires_at > now
        }

    def clear_operations_state(self) -> None:
        self.tui_snapshots.clear()
        self.tui_snapshot_expires_at = 0.0
        self.tui_confirmations.clear()

    def clear_tui_sensitive(self) -> None:
        self.pending = None
        self.page_refs.clear()
        self.cursor_kind = None
        self.cursor_target = None
        self.cursor_offset = 0
        self.cursor_expires_at = 0.0
        self.last_mail_id = None
        self.last_mail_sender = None
        self.clear_operations_state()


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
            session.clear_tui_sensitive()
            session.tui_active = False
            session.tui_screen = None
        session.expire_tui_sensitive(now)
        session.last_seen = now
        return session

    def clear_sensitive(self) -> None:
        for session in self._sessions.values():
            session.clear_tui_sensitive()
