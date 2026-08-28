from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from outpost.ai import ChatMessage, ChatRequest, create_provider
from outpost.ai.providers.hailo_vlm import HailoVLMProvider, _HailoRuntime
from outpost.ai.providers.models import ProviderState, ProviderUnavailable
from outpost.config import AIConfig, AIHailoVLMConfig


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
            return httpx.Response(200, json={"models": ["qwen:1.5b"]})
        if request.url.path == "/api/chat":
            body = json.loads(request.content)
            assert body["messages"] == [{"role": "user", "content": "line one line two"}]
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
        ChatRequest(messages=(ChatMessage(role="user", content="line one\nline two"),))
    )
    assert response.content == "Hailo answer"
    await client.aclose()


class FakeVLMRuntime:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[ChatMessage, ...], int, float, int]] = []
        self.closed = False
        self.active = 0
        self.peak_active = 0

    def generate(
        self,
        messages: tuple[ChatMessage, ...],
        *,
        max_output_tokens: int,
        temperature: float,
        timeout_ms: int,
    ) -> tuple[str, int, int]:
        self.active += 1
        self.peak_active = max(self.peak_active, self.active)
        time.sleep(0.01)
        self.calls.append((messages, max_output_tokens, temperature, timeout_ms))
        self.active -= 1
        return "Native Hailo answer", 12, 3

    def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_hailo_vlm_loads_compiled_model_and_serialises_native_chat(tmp_path) -> None:
    model = tmp_path / "Qwen3-VL-2B-Instruct.hef"
    model.write_bytes(b"test HEF")
    runtime = FakeVLMRuntime()
    provider = HailoVLMProvider(
        AIHailoVLMConfig(model_path=model),
        "Qwen3-VL-2B-Instruct",
        45,
        64,
        runtime_factory=lambda _path, _optimize: runtime,
    )

    health = await provider.health()
    capabilities = await provider.capabilities()
    response = await provider.chat(
        ChatRequest(
            messages=(ChatMessage(role="user", content="Is the road open?"),),
            max_output_tokens=80,
            temperature=0,
        )
    )

    assert health.state is ProviderState.HEALTHY
    assert health.models == ("Qwen3-VL-2B-Instruct",)
    assert not capabilities.supports_tools
    assert not capabilities.supports_streaming
    assert response.content == "Native Hailo answer"
    assert response.prompt_tokens == 12
    assert response.output_tokens == 3
    assert runtime.calls[0][1:] == (64, 0, 45_000)
    await asyncio.gather(
        provider.chat(ChatRequest(messages=(ChatMessage(role="user", content="one"),))),
        provider.chat(ChatRequest(messages=(ChatMessage(role="user", content="two"),))),
    )
    assert runtime.peak_active == 1
    await provider.close()
    assert runtime.closed


@pytest.mark.asyncio
async def test_hailo_vlm_reports_missing_hef_without_importing_runtime(tmp_path) -> None:
    provider = HailoVLMProvider(
        AIHailoVLMConfig(model_path=tmp_path / "missing.hef"),
        "Qwen3-VL-2B-Instruct",
        45,
        64,
    )

    health = await provider.health()

    assert health.state is ProviderState.UNAVAILABLE
    assert "HEF model file was not found" in health.detail


@pytest.mark.asyncio
async def test_hailo_vlm_retries_while_prior_process_releases_device(tmp_path) -> None:
    model = tmp_path / "Qwen3-VL-2B-Instruct.hef"
    model.write_bytes(b"test HEF")
    runtime = FakeVLMRuntime()
    attempts = 0
    delays: list[float] = []

    def factory(_path: object, _optimize: object) -> FakeVLMRuntime:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RuntimeError("HAILO_OUT_OF_PHYSICAL_DEVICES")
        return runtime

    async def sleep(delay: float) -> None:
        delays.append(delay)

    provider = HailoVLMProvider(
        AIHailoVLMConfig(model_path=model),
        "Qwen3-VL-2B-Instruct",
        45,
        64,
        runtime_factory=factory,
        load_attempts=5,
        load_retry_initial_s=0.25,
        sleep=sleep,
    )

    health = await provider.health()

    assert health.state is ProviderState.HEALTHY
    assert attempts == 3
    assert delays == [0.25, 0.5]
    await provider.close()


@pytest.mark.asyncio
async def test_hailo_vlm_reports_unavailable_after_bounded_acquisition_retries(tmp_path) -> None:
    model = tmp_path / "Qwen3-VL-2B-Instruct.hef"
    model.write_bytes(b"test HEF")
    attempts = 0
    delays: list[float] = []

    def factory(_path: object, _optimize: object) -> FakeVLMRuntime:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("HAILO_OUT_OF_PHYSICAL_DEVICES")

    async def sleep(delay: float) -> None:
        delays.append(delay)

    provider = HailoVLMProvider(
        AIHailoVLMConfig(model_path=model),
        "Qwen3-VL-2B-Instruct",
        45,
        64,
        runtime_factory=factory,
        load_attempts=3,
        load_retry_initial_s=0.25,
        sleep=sleep,
    )

    health = await provider.health()

    assert health.state is ProviderState.UNAVAILABLE
    assert "RuntimeError" in health.detail
    assert attempts == 3
    assert delays == [0.25, 0.5]


@pytest.mark.asyncio
async def test_hailo_vlm_device_release_has_a_bounded_timeout(tmp_path) -> None:
    class SlowCloseRuntime(FakeVLMRuntime):
        def close(self) -> None:
            time.sleep(0.05)
            super().close()

    model = tmp_path / "Qwen3-VL-2B-Instruct.hef"
    model.write_bytes(b"test HEF")
    runtime = SlowCloseRuntime()
    provider = HailoVLMProvider(
        AIHailoVLMConfig(model_path=model),
        "Qwen3-VL-2B-Instruct",
        45,
        64,
        runtime_factory=lambda _path, _optimize: runtime,
        close_timeout_s=0.01,
    )
    await provider.warm()

    with pytest.raises(ProviderUnavailable, match="timed out releasing"):
        await provider.close()
    await asyncio.sleep(0.06)
    assert runtime.closed


def test_native_hailo_vlm_clears_context_and_builds_structured_prompt() -> None:
    class FakeNativeVLM:
        def __init__(self) -> None:
            self.cleared = 0
            self.prompt: object = None

        def clear_context(self) -> None:
            self.cleared += 1

        def tokenize(self, text: str) -> list[str]:
            return text.split()

        def generate_all(self, **kwargs: object) -> str:
            self.prompt = kwargs["prompt"]
            return "answer<|im_end|>"

    native = FakeNativeVLM()
    runtime = object.__new__(_HailoRuntime)
    runtime._vlm = native
    content, prompt_tokens, output_tokens = runtime.generate(
        (
            ChatMessage(role="system", content="Stay grounded."),
            ChatMessage(role="user", content="Road status?"),
        ),
        max_output_tokens=32,
        temperature=0,
        timeout_ms=1000,
    )

    assert native.cleared == 1
    assert native.prompt == [
        {
            "role": "system",
            "content": [{"type": "text", "text": "Stay grounded."}],
        },
        {
            "role": "user",
            "content": [{"type": "text", "text": "Road status?"}],
        },
    ]
    assert (content, prompt_tokens, output_tokens) == ("answer", 4, 1)


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
