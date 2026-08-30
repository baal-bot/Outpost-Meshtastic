from __future__ import annotations

from enum import StrEnum
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field


class ProviderError(RuntimeError):
    """Normalised inference-provider failure."""


class ProviderUnavailable(ProviderError):
    """The configured backend cannot currently serve a request."""


class ProviderState(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class ProviderModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProviderHealth(ProviderModel):
    state: ProviderState
    detail: str
    latency_ms: int | None = Field(default=None, ge=0)
    models: tuple[str, ...] = ()


class Capabilities(ProviderModel):
    context_tokens: int = Field(ge=1)
    supports_streaming: bool
    max_output_tokens: int = Field(ge=1)
    idle_unloads: bool = False
    source: Literal["discovered", "configured"] = "configured"


class ChatMessage(ProviderModel):
    role: Literal["system", "user", "assistant"]
    content: str


class ChatRequest(ProviderModel):
    messages: tuple[ChatMessage, ...]
    max_output_tokens: int = Field(default=220, ge=1)
    temperature: float = Field(default=0.1, ge=0, le=2)


class ChatResponse(ProviderModel):
    content: str
    prompt_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    ttft_ms: int | None = Field(default=None, ge=0)
    total_ms: int = Field(ge=0)
    generation_tokens_per_s: float | None = Field(default=None, ge=0)
    prompt_tokens_per_s: float | None = Field(default=None, ge=0)
    finish_reason: str | None = None


class InferenceProvider(Protocol):
    name: str
    model: str
    external: bool

    async def health(self) -> ProviderHealth: ...
    async def capabilities(self) -> Capabilities: ...
    async def chat(self, req: ChatRequest) -> ChatResponse: ...
    async def warm(self) -> None: ...
    async def close(self) -> None: ...
