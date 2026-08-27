from __future__ import annotations

from contextvars import ContextVar, Token

_operator_actor: ContextVar[str] = ContextVar("outpost_operator_actor", default="web:operator")


def current_actor() -> str:
    return _operator_actor.get()


def current_actor_ref() -> str:
    return current_actor().removeprefix("web:")


def set_current_actor(actor: str) -> Token[str]:
    return _operator_actor.set(actor)


def reset_current_actor(token: Token[str]) -> None:
    _operator_actor.reset(token)
