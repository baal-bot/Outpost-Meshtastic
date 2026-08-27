from __future__ import annotations

import httpx

from outpost.config import AIConfig

from .hailo import HailoProvider
from .hailo_vlm import HailoVLMProvider
from .models import InferenceProvider
from .null import NullProvider
from .ollama import OllamaProvider
from .openai_compat import LlamaCppProvider, OpenAICompatProvider


def create_provider(
    config: AIConfig, *, client: httpx.AsyncClient | None = None
) -> InferenceProvider:
    common = (
        config.model,
        config.timeout_s,
        config.max_output_tokens,
    )
    if config.provider == "hailo_vlm":
        return HailoVLMProvider(config.hailo_vlm, *common)
    if config.provider == "hailo":
        return HailoProvider(config.hailo, *common, client=client)
    if config.provider == "llamacpp":
        return LlamaCppProvider(config.llamacpp, *common, client=client)
    if config.provider == "ollama":
        return OllamaProvider(config.ollama, *common, client=client)
    if config.provider == "openai_compat":
        return OpenAICompatProvider(config.openai_compat, *common, client=client)
    return NullProvider()


__all__ = ["create_provider"]
