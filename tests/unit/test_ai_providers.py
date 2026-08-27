from __future__ import annotations

import json

import httpx
import pytest

from outpost.ai import ChatMessage, ChatRequest, create_provider
from outpost.ai.providers.models import ProviderState, ProviderUnavailable
from outpost.config import AIConfig


def client_for(handler: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://provider")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_ollama_normalises_health_capabilities_and_stream() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "local:1.5b"}]})
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"model_info": {"qwen2.context_length": 8192}})
        assert request.url.path == "/api/chat"
        body = json.loads(request.content)
        assert body["options"]["num_predict"] == 64
        chunks = (
            '{"message":{"content":"local "},"done":false}\n'
            '{"message":{"content":"answer"},"done":false}\n'
            '{"message":{"content":""},"done":true,"done_reason":"stop",'
            '"prompt_eval_count":12,"eval_count":2}\n'
        )
        return httpx.Response(200, text=chunks)

    client = client_for(handler)
    provider = create_provider(
        AIConfig.model_validate(
            {
                "provider": "ollama",
                "model": "local:1.5b",
                "max_output_tokens": 64,
            }
        ),
        client=client,
    )
    health = await provider.health()
    capabilities = await provider.capabilities()
    response = await provider.chat(
        ChatRequest(messages=(ChatMessage(role="user", content="question"),))
    )

    assert health.state is ProviderState.HEALTHY
    assert capabilities.context_tokens == 8192
    assert capabilities.source == "discovered"
    assert response.content == "local answer"
    assert response.prompt_tokens == 12
    assert response.output_tokens == 2
    assert response.ttft_ms is not None
    await client.aclose()


@pytest.mark.asyncio
async def test_hailo_keeps_its_non_openai_wire_format_isolated() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": []})
        if request.url.path == "/hailo/v1/list":
            return httpx.Response(200, json={"models": [{"name": "qwen:1.5b"}]})
        if request.url.path == "/api/chat":
            return httpx.Response(
                200,
                text=('{"content":"Hailo "}\n{"content":"answer"}\n{"done":true,"eval_count":2}\n'),
            )
        return httpx.Response(404)

    client = client_for(handler)
    provider = create_provider(
        AIConfig.model_validate({"provider": "hailo", "model": "qwen:1.5b"}),
        client=client,
    )
    health = await provider.health()
    capabilities = await provider.capabilities()

    assert health.state is ProviderState.DEGRADED
    assert "not pulled" in health.detail
    assert capabilities.context_tokens == 2048
    assert not capabilities.supports_tools
    response = await provider.chat(
        ChatRequest(messages=(ChatMessage(role="user", content="question"),))
    )
    assert response.content == "Hailo answer"
    await client.aclose()


@pytest.mark.asyncio
async def test_openai_compat_normalises_sse_usage_and_tool_calls() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "edge-model"}]})
        assert request.url.path == "/v1/chat/completions"
        stream = "\n".join(
            [
                'data: {"choices":[{"delta":{"content":"Road "}}]}',
                'data: {"choices":[{"delta":{"content":"open"},"finish_reason":"stop"}]}',
                'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":2}}',
                "data: [DONE]",
                "",
            ]
        )
        return httpx.Response(200, text=stream, headers={"content-type": "text/event-stream"})

    client = client_for(handler)
    provider = create_provider(
        AIConfig.model_validate(
            {
                "provider": "openai_compat",
                "model": "edge-model",
                "openai_compat": {"base_url": "http://provider"},
            }
        ),
        client=client,
    )
    assert provider.external
    assert (await provider.health()).state is ProviderState.HEALTHY
    response = await provider.chat(
        ChatRequest(messages=(ChatMessage(role="user", content="Is the road open?"),))
    )
    assert response.content == "Road open"
    assert response.prompt_tokens == 10
    assert response.output_tokens == 2
    await client.aclose()


@pytest.mark.asyncio
async def test_null_provider_is_an_explicit_unavailable_backend() -> None:
    provider = create_provider(AIConfig(provider="null"))
    assert (await provider.health()).state is ProviderState.UNAVAILABLE
    with pytest.raises(ProviderUnavailable):
        await provider.chat(ChatRequest(messages=()))


@pytest.mark.asyncio
async def test_provider_connection_failures_are_normalised() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    client = client_for(handler)
    provider = create_provider(AIConfig(provider="ollama"), client=client)
    assert (await provider.health()).state is ProviderState.UNAVAILABLE
    with pytest.raises(ProviderUnavailable):
        await provider.chat(ChatRequest(messages=(ChatMessage(role="user", content="question"),)))
    await client.aclose()
