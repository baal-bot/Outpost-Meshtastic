from __future__ import annotations

import json
import os
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from outpost.config import AIProviderEndpoint

from .models import ProviderError, ProviderUnavailable


class HTTPProvider:
    name = "http"
    external = False

    def __init__(
        self,
        endpoint: AIProviderEndpoint,
        model: str,
        timeout_s: float,
        max_output_tokens: int,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.model = model
        self.max_output_tokens = max_output_tokens
        self._owned_client = client is None
        headers: dict[str, str] = {"Accept": "application/json"}
        if endpoint.api_key_env:
            value = os.getenv(endpoint.api_key_env)
            if value:
                headers["Authorization"] = f"Bearer {value}"
        self.client = client or httpx.AsyncClient(
            base_url=endpoint.base_url.rstrip("/"),
            headers=headers,
            timeout=httpx.Timeout(timeout_s),
        )

    async def close(self) -> None:
        if self._owned_client:
            await self.client.aclose()

    async def _json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = await self.client.request(method, path, **kwargs)
            response.raise_for_status()
            value = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderUnavailable(f"{self.name}: {type(exc).__name__}") from exc
        if not isinstance(value, dict):
            raise ProviderError(f"{self.name}: response was not a JSON object")
        return value

    @staticmethod
    async def _json_lines(response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
        async for line in response.aiter_lines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("data:"):
                line = line[5:].strip()
            if line == "[DONE]":
                break
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ProviderError("provider returned malformed streaming JSON") from exc
            if not isinstance(value, dict):
                raise ProviderError("provider stream item was not an object")
            yield value

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return max(0, round((time.perf_counter() - started) * 1000))
