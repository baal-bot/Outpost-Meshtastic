from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

EventHandler = Callable[[Any], Awaitable[None]]


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[type[Any], list[EventHandler]] = defaultdict(list)

    def subscribe(self, event_type: type[Any], handler: EventHandler) -> None:
        self._handlers[event_type].append(handler)

    async def publish(self, event: Any) -> None:
        results = await asyncio.gather(
            *(handler(event) for handler in self._handlers[type(event)]),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                # Subscribers are isolated; composition root supplies structured logging later.
                continue
