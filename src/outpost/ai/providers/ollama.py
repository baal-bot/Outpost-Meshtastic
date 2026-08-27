from __future__ import annotations

import json
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
    ToolCall,
)


class OllamaProvider(HTTPProvider):
    name = "ollama"

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

    @staticmethod
    def _model_names(value: dict[str, Any]) -> tuple[str, ...]:
        models = value.get("models", [])
        if not isinstance(models, list):
            return ()
        names: list[str] = []
        for item in models:
            if isinstance(item, str) and item:
                names.append(item)
            elif isinstance(item, dict) and (item.get("name") or item.get("model")):
                names.append(str(item.get("name") or item.get("model")))
        return tuple(names)

    async def health(self) -> ProviderHealth:
        started = time.perf_counter()
        try:
            value = await self._json("GET", "/api/tags")
        except ProviderUnavailable as exc:
            return ProviderHealth(state=ProviderState.UNAVAILABLE, detail=str(exc))
        models = self._model_names(value)
        state = ProviderState.HEALTHY if self.model in models else ProviderState.DEGRADED
        detail = "ready" if state is ProviderState.HEALTHY else f"model not installed: {self.model}"
        return ProviderHealth(
            state=state, detail=detail, latency_ms=self._elapsed_ms(started), models=models
        )

    async def capabilities(self) -> Capabilities:
        context = self.endpoint.context_tokens
        discovered = False
        try:
            value = await self._json("POST", "/api/show", json={"model": self.model})
            info = value.get("model_info")
            if isinstance(info, dict):
                reported = [
                    item
                    for key, item in info.items()
                    if str(key).endswith(".context_length") and isinstance(item, int)
                ]
                if reported:
                    context = max(reported)
                    discovered = True
        except ProviderUnavailable:
            pass
        return Capabilities(
            context_tokens=context,
            supports_tools=self.endpoint.supports_tools,
            supports_streaming=True,
            max_output_tokens=self.max_output_tokens,
            idle_unloads=True,
            source="discovered" if discovered else "configured",
        )

    async def chat(self, req: ChatRequest) -> ChatResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self._message_payload(req.messages),
            "stream": True,
            "options": {
                "num_predict": min(req.max_output_tokens, self.max_output_tokens),
                "temperature": req.temperature,
            },
        }
        if req.tools:
            payload["tools"] = self._tool_payload(req.tools)
        started = time.perf_counter()
        first_token: int | None = None
        content: list[str] = []
        final: dict[str, Any] = {}
        calls: list[ToolCall] = []
        try:
            async with self.client.stream("POST", "/api/chat", json=payload) as response:
                response.raise_for_status()
                async for item in self._json_lines(response):
                    final = item
                    message = item.get("message", item)
                    if not isinstance(message, dict):
                        continue
                    chunk = message.get("content", "")
                    if isinstance(chunk, str) and chunk:
                        if first_token is None:
                            first_token = self._elapsed_ms(started)
                        content.append(chunk)
                    raw_calls = message.get("tool_calls", [])
                    if isinstance(raw_calls, list):
                        for call in raw_calls:
                            if not isinstance(call, dict):
                                continue
                            function = call.get("function", {})
                            if not isinstance(function, dict) or not function.get("name"):
                                continue
                            arguments = function.get("arguments", {})
                            if isinstance(arguments, str):
                                try:
                                    arguments = json.loads(arguments)
                                except json.JSONDecodeError:
                                    arguments = {}
                            if isinstance(arguments, dict):
                                calls.append(
                                    ToolCall(name=str(function["name"]), arguments=arguments)
                                )
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"{self.name}: {type(exc).__name__}") from exc
        return ChatResponse(
            content="".join(content),
            prompt_tokens=_integer(final.get("prompt_eval_count")),
            output_tokens=_integer(final.get("eval_count")),
            ttft_ms=first_token,
            total_ms=self._elapsed_ms(started),
            generation_tokens_per_s=_per_second(
                final.get("eval_count"), final.get("eval_duration")
            ),
            prompt_tokens_per_s=_per_second(
                final.get("prompt_eval_count"), final.get("prompt_eval_duration")
            ),
            finish_reason=str(final.get("done_reason") or "stop"),
            tool_calls=tuple(calls),
        )

    def _message_payload(self, messages: tuple[ChatMessage, ...]) -> list[dict[str, Any]]:
        return [message.model_dump(exclude_none=True) for message in messages]

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


def _per_second(count: Any, duration_ns: Any) -> float | None:
    if not isinstance(count, int) or not isinstance(duration_ns, int) or duration_ns <= 0:
        return None
    return count / duration_ns * 1_000_000_000
