from __future__ import annotations

import time
from typing import Any

import httpx

from outpost.config import AIProviderEndpoint

from .models import Capabilities, ChatMessage, ProviderHealth, ProviderState, ProviderUnavailable
from .ollama import OllamaProvider


class HailoProvider(OllamaProvider):
    """Hailo-Ollama adapter; intentionally separate from standard Ollama."""

    name = "hailo"

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
            installed = await self._json("GET", "/api/tags")
            available = await self._json("GET", "/hailo/v1/list")
        except ProviderUnavailable as exc:
            return ProviderHealth(state=ProviderState.UNAVAILABLE, detail=str(exc))
        installed_models = self._model_names(installed)
        available_models = self._model_names(available)
        models = tuple(dict.fromkeys((*installed_models, *available_models)))
        if self.model in installed_models:
            state, detail = ProviderState.HEALTHY, "ready"
        elif self.model in available_models:
            state, detail = ProviderState.DEGRADED, f"model available but not pulled: {self.model}"
        else:
            state, detail = ProviderState.DEGRADED, f"model unavailable: {self.model}"
        return ProviderHealth(
            state=state, detail=detail, latency_ms=self._elapsed_ms(started), models=models
        )

    async def capabilities(self) -> Capabilities:
        return Capabilities(
            context_tokens=self.endpoint.context_tokens,
            supports_streaming=True,
            max_output_tokens=self.max_output_tokens,
            idle_unloads=True,
            source="configured",
        )

    def _message_payload(self, messages: tuple[ChatMessage, ...]) -> list[dict[str, Any]]:
        payload = super()._message_payload(messages)
        # Hailo-Ollama 5.3.0 passes literal control characters into the GenAI
        # JSON renderer, which rejects otherwise valid multiline chat content.
        for message in payload:
            content = message.get("content")
            if isinstance(content, str):
                message["content"] = " ".join(content.split())
        return payload
