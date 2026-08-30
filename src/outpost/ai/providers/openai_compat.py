from __future__ import annotations

import time
from typing import Any

import httpx

from outpost.config import AIProviderEndpoint

from .http import HTTPProvider
from .models import (
    Capabilities,
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ProviderHealth,
    ProviderState,
    ProviderUnavailable,
)


class OpenAICompatProvider(HTTPProvider):
    name = "openai_compat"
    external = True

    def __init__(
        self,
        endpoint: AIProviderEndpoint,
        model: str,
        timeout_s: float,
        max_output_tokens: int,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(endpoint, model, timeout_s, max_output_tokens, client=client)

    async def health(self) -> ProviderHealth:
        started = time.perf_counter()
        try:
            value = await self._json("GET", "/v1/models")
        except ProviderUnavailable as exc:
            return ProviderHealth(state=ProviderState.UNAVAILABLE, detail=str(exc))
        data = value.get("data", [])
        models = (
            tuple(
                str(item["id"])
                for item in data
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            )
            if isinstance(data, list)
            else ()
        )
        state = (
            ProviderState.HEALTHY if not models or self.model in models else ProviderState.DEGRADED
        )
        detail = "ready" if state is ProviderState.HEALTHY else f"model not listed: {self.model}"
        return ProviderHealth(
            state=state, detail=detail, latency_ms=self._elapsed_ms(started), models=models
        )

    async def capabilities(self) -> Capabilities:
        return Capabilities(
            context_tokens=self.endpoint.context_tokens,
            supports_streaming=True,
            max_output_tokens=self.max_output_tokens,
            source="configured",
        )

    async def chat(self, req: ChatRequest) -> ChatResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [message.model_dump(exclude_none=True) for message in req.messages],
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_tokens": min(req.max_output_tokens, self.max_output_tokens),
            "temperature": req.temperature,
        }
        started = time.perf_counter()
        first_token: int | None = None
        content: list[str] = []
        usage: dict[str, Any] = {}
        finish_reason: str | None = None
        timings: dict[str, Any] = {}
        try:
            async with self.client.stream("POST", "/v1/chat/completions", json=payload) as response:
                response.raise_for_status()
                async for item in self._json_lines(response):
                    if isinstance(item.get("timings"), dict):
                        timings = item["timings"]
                    if isinstance(item.get("usage"), dict):
                        usage = item["usage"]
                    choices = item.get("choices", [])
                    if not isinstance(choices, list) or not choices:
                        continue
                    choice = choices[0]
                    if not isinstance(choice, dict):
                        continue
                    if choice.get("finish_reason"):
                        finish_reason = str(choice["finish_reason"])
                    delta = choice.get("delta", {})
                    if not isinstance(delta, dict):
                        continue
                    chunk = delta.get("content")
                    if isinstance(chunk, str) and chunk:
                        if first_token is None:
                            first_token = self._elapsed_ms(started)
                        content.append(chunk)
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"{self.name}: {type(exc).__name__}") from exc
        return ChatResponse(
            content="".join(content),
            prompt_tokens=_integer(usage.get("prompt_tokens")),
            output_tokens=_integer(usage.get("completion_tokens")),
            ttft_ms=first_token,
            total_ms=self._elapsed_ms(started),
            generation_tokens_per_s=_number(timings.get("predicted_per_second")),
            prompt_tokens_per_s=_number(timings.get("prompt_per_second")),
            finish_reason=finish_reason,
        )

    async def warm(self) -> None:
        await self.chat(
            ChatRequest(
                messages=(ChatMessage(role="user", content="."),),
                max_output_tokens=1,
                temperature=0,
            )
        )


def _integer(value: Any) -> int | None:
    return value if isinstance(value, int) and value >= 0 else None


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and value >= 0 else None


class LlamaCppProvider(OpenAICompatProvider):
    name = "llamacpp"
    external = False
