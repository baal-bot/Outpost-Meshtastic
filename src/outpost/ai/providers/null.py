from __future__ import annotations

from .models import (
    Capabilities,
    ChatRequest,
    ChatResponse,
    ProviderHealth,
    ProviderState,
    ProviderUnavailable,
)


class NullProvider:
    name = "null"
    model = "none"
    external = False

    async def health(self) -> ProviderHealth:
        return ProviderHealth(state=ProviderState.UNAVAILABLE, detail="no inference provider")

    async def capabilities(self) -> Capabilities:
        return Capabilities(
            context_tokens=2048,
            supports_streaming=False,
            max_output_tokens=220,
        )

    async def chat(self, req: ChatRequest) -> ChatResponse:
        del req
        raise ProviderUnavailable("null: no inference provider")

    async def warm(self) -> None:
        return None

    async def close(self) -> None:
        return None
