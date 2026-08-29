from __future__ import annotations

import re
import unicodedata

from .models import Line, Response, ResponseKind, TuiScreen
from .session import PendingAction, Session

TUI_PENDING_SECONDS = 10 * 60


def _choice_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold().strip()
    return re.sub(r"\s+", " ", normalized)


class TuiController:
    """Turn structured screens into compact Meshtastic conversations."""

    @staticmethod
    def prepare(
        invoked: str, session: Session, now: float, *, direct: bool
    ) -> tuple[str, Response | None]:
        if not direct:
            return invoked, None
        session.expire_tui_sensitive(now)
        if session.pending is not None and session.pending.expires_at <= now:
            session.pending = None
        answer = _choice_key(invoked)
        if answer == "0":
            session.clear_tui_sensitive()
            return "MENU", None
        pending = session.pending
        if pending is None:
            if answer.isdigit():
                return invoked, Response(
                    ResponseKind.ERROR,
                    [Line("No active menu. Send ? to start again.")],
                )
            return invoked, None
        target = pending.choices.get(answer)
        if target is not None:
            session.pending = None
            return target, None
        if answer.isdigit() and any(key.isdigit() for key in pending.choices):
            return invoked, Response(ResponseKind.ERROR, [Line(pending.prompt)])
        if pending.input_command is not None and answer not in {
            "cancel",
            "never mind",
            "nevermind",
        }:
            session.pending = None
            return f"{pending.input_command} {invoked}".strip(), None
        if answer in {"cancel", "never mind", "nevermind"}:
            session.pending = None
            return "MENU", None
        if answer.isdigit():
            return invoked, Response(ResponseKind.ERROR, [Line(pending.prompt)])
        return invoked, None

    @staticmethod
    def cancel_for_command(session: Session, *, preserve_operations: bool = False) -> None:
        session.pending = None
        if not preserve_operations:
            session.clear_operations_state()

    @staticmethod
    def activate(
        response: Response,
        session: Session,
        now: float,
        *,
        direct: bool,
        fallback_title: str | None = None,
    ) -> Response:
        if not direct:
            return response
        screen = response.screen
        if screen is None and session.tui_active and fallback_title:
            screen = TuiScreen(
                f"result-{fallback_title.casefold()}", fallback_title.replace("_", " ")
            )
            response.screen = screen
        if screen is None:
            return response
        session.tui_active = True
        session.tui_screen = screen.name
        lines = [Line(f"OUTPOST / {screen.title}")]
        lines.extend(response.lines)
        choices: dict[str, str] = {}
        next_number = 1
        for choice in screen.choices:
            key = choice.key or str(next_number)
            if choice.key is None:
                next_number += 1
            lines.append(Line(f"{key} {choice.label}"))
            choices[_choice_key(key)] = choice.command
            choices[_choice_key(choice.label)] = choice.command
            for alias in choice.aliases:
                choices[_choice_key(alias)] = choice.command
        if screen.input_prompt:
            lines.append(Line(screen.input_prompt))
        lines.append(Line("0 Home · ? Menu"))
        if choices or screen.input_command:
            valid = [key for key in choices if key.isdigit()]
            range_hint = f"Reply 1-{max(map(int, valid))}" if valid else "Reply with your answer"
            session.pending = PendingAction(
                action=screen.name,
                partial_args="",
                prompt=f"{range_hint}, or 0 for Home.",
                expires_at=now + TUI_PENDING_SECONDS,
                choices=choices,
                input_command=screen.input_command,
            )
        else:
            session.pending = None
        response.lines = lines
        return response
