from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from typing import Any, Protocol

from prometheus_client import Counter

from outpost.config import AIHailoVLMConfig

from .models import (
    Capabilities,
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ProviderError,
    ProviderHealth,
    ProviderState,
    ProviderUnavailable,
)


class VLMRuntime(Protocol):
    def generate(
        self,
        messages: tuple[ChatMessage, ...],
        *,
        max_output_tokens: int,
        temperature: float,
        timeout_ms: int,
    ) -> tuple[str, int, int]: ...

    def close(self) -> None: ...


RuntimeFactory = Callable[[Path, bool], VLMRuntime]
Sleep = Callable[[float], Awaitable[None]]

HAILO_GENERATIONS_OUTLIVED_CALLER = Counter(
    "outpost_hailo_generations_outlived_caller_total",
    "Hailo VLM generations that kept running after their caller was cancelled",
)


class _HailoRuntime:
    def __init__(self, model_path: Path, optimize_memory_on_device: bool) -> None:
        try:
            from hailo_platform import VDevice
            from hailo_platform.genai import VLM
        except ImportError as exc:
            raise ProviderUnavailable(
                "hailo_vlm: HailoRT Python bindings are not installed"
            ) from exc

        self._vdevice = VDevice()
        try:
            self._vlm = VLM(
                self._vdevice,
                str(model_path),
                optimize_memory_on_device=optimize_memory_on_device,
            )
        except Exception:
            self._vdevice.release()
            raise

    def generate(
        self,
        messages: tuple[ChatMessage, ...],
        *,
        max_output_tokens: int,
        temperature: float,
        timeout_ms: int,
    ) -> tuple[str, int, int]:
        prompt: list[dict[str, Any]] = [
            {
                "role": message.role,
                "content": [{"type": "text", "text": message.content}],
            }
            for message in messages
        ]
        # Hailo's VLM maintains conversation state internally. Outpost requests
        # are independent and may belong to different mesh users, so retaining
        # that state would be a cross-user data leak.
        self._vlm.clear_context()
        prompt_tokens = sum(len(self._vlm.tokenize(message.content)) for message in messages)
        do_sample = temperature > 0
        response = self._vlm.generate_all(
            prompt=prompt,
            frames=[],
            temperature=temperature if do_sample else None,
            do_sample=do_sample,
            max_generated_tokens=max_output_tokens,
            timeout_ms=timeout_ms,
        )
        content = _strip_generation_markers(str(response))
        return content, prompt_tokens, len(self._vlm.tokenize(content))

    def close(self) -> None:
        try:
            self._vlm.release()
        finally:
            self._vdevice.release()


def _default_runtime_factory(model_path: Path, optimize_memory_on_device: bool) -> VLMRuntime:
    return _HailoRuntime(model_path, optimize_memory_on_device)


class HailoVLMProvider:
    """Direct HailoRT adapter for a compiled vision-language HEF."""

    name = "hailo_vlm"
    external = False

    def __init__(
        self,
        endpoint: AIHailoVLMConfig,
        model: str,
        timeout_s: float,
        max_output_tokens: int,
        *,
        runtime_factory: RuntimeFactory = _default_runtime_factory,
        load_attempts: int = 5,
        load_retry_initial_s: float = 0.5,
        close_timeout_s: float = 10,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.endpoint = endpoint
        self.model = model
        self.timeout_s = timeout_s
        self.max_output_tokens = max_output_tokens
        self._runtime_factory = runtime_factory
        self._load_attempts = max(1, load_attempts)
        self._load_retry_initial_s = max(0, load_retry_initial_s)
        self._close_timeout_s = max(0.001, close_timeout_s)
        self._sleep = sleep
        self._runtime: VLMRuntime | None = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="outpost-hailo-vlm")
        self._closed = False
        self._load_lock = asyncio.Lock()
        self._request_lock = asyncio.Lock()

    def _load_runtime(self) -> VLMRuntime:
        """Construct and publish the device handle on the serialized hardware worker."""
        if self._closed:
            raise ProviderUnavailable("hailo_vlm: provider is closed")
        if self._runtime is None:
            self._runtime = self._runtime_factory(
                self.endpoint.model_path,
                self.endpoint.optimize_memory_on_device,
            )
        return self._runtime

    async def _ensure_runtime(self) -> VLMRuntime:
        async with self._load_lock:
            if self._closed:
                raise ProviderUnavailable("hailo_vlm: provider is closed")
            if self._runtime is not None:
                return self._runtime
            if not self.endpoint.model_path.is_file():
                raise ProviderUnavailable("hailo_vlm: configured HEF model file was not found")
            last_error: Exception | None = None
            for attempt in range(self._load_attempts):
                try:
                    # Assignment happens inside the worker. If this coroutine is
                    # cancelled during construction, the handle is still published
                    # for the next request (or queued close) instead of being leaked.
                    return await asyncio.wrap_future(self._executor.submit(self._load_runtime))
                except ProviderUnavailable:
                    raise
                except Exception as exc:
                    last_error = exc
                    if attempt + 1 < self._load_attempts:
                        await self._sleep(min(self._load_retry_initial_s * (2**attempt), 4.0))
            assert last_error is not None
            raise ProviderUnavailable(f"hailo_vlm: {type(last_error).__name__}") from last_error

    async def health(self) -> ProviderHealth:
        started = time.perf_counter()
        try:
            await self._ensure_runtime()
        except ProviderUnavailable as exc:
            return ProviderHealth(state=ProviderState.UNAVAILABLE, detail=str(exc))
        return ProviderHealth(
            state=ProviderState.HEALTHY,
            detail="ready",
            latency_ms=max(0, round((time.perf_counter() - started) * 1000)),
            models=(self.model,),
        )

    async def capabilities(self) -> Capabilities:
        return Capabilities(
            context_tokens=self.endpoint.context_tokens,
            supports_tools=False,
            supports_streaming=False,
            max_output_tokens=self.max_output_tokens,
            idle_unloads=False,
            source="configured",
        )

    async def chat(self, req: ChatRequest) -> ChatResponse:
        if req.tools:
            raise ProviderError("hailo_vlm does not support tool calls")
        started = time.perf_counter()
        async with self._request_lock:
            runtime = await self._ensure_runtime()
            worker = self._executor.submit(
                partial(
                    runtime.generate,
                    req.messages,
                    max_output_tokens=min(req.max_output_tokens, self.max_output_tokens),
                    temperature=req.temperature,
                    timeout_ms=max(1, round(self.timeout_s * 1000)),
                )
            )
            try:
                content, prompt_tokens, output_tokens = await asyncio.wrap_future(worker)
            except asyncio.CancelledError:
                # A queued operation can still be abandoned. A running native
                # generation cannot be interrupted, but the one-worker executor
                # keeps every subsequent request behind it until it finishes.
                if not worker.cancel() and not worker.done():
                    HAILO_GENERATIONS_OUTLIVED_CALLER.inc()
                raise
            except Exception as exc:
                raise ProviderUnavailable(f"hailo_vlm: {type(exc).__name__}") from exc
        return ChatResponse(
            content=content,
            prompt_tokens=prompt_tokens,
            output_tokens=output_tokens,
            ttft_ms=None,
            total_ms=max(0, round((time.perf_counter() - started) * 1000)),
            finish_reason="stop",
        )

    async def warm(self) -> None:
        await self._ensure_runtime()

    def _close_runtime(self) -> None:
        runtime, self._runtime = self._runtime, None
        if runtime is not None:
            runtime.close()

    async def close(self) -> None:
        async with self._request_lock:
            async with self._load_lock:
                if self._closed:
                    return
                self._closed = True
                try:
                    worker = self._executor.submit(self._close_runtime)
                    await asyncio.wait_for(
                        asyncio.shield(asyncio.wrap_future(worker)),
                        timeout=self._close_timeout_s,
                    )
                except TimeoutError as exc:
                    raise ProviderUnavailable(
                        "hailo_vlm: timed out releasing the Hailo device"
                    ) from exc
                except Exception as exc:
                    raise ProviderUnavailable(
                        f"hailo_vlm: device release failed ({type(exc).__name__})"
                    ) from exc
                finally:
                    # wait=False keeps shutdown bounded; already-submitted device
                    # work and the queued release still complete in order.
                    self._executor.shutdown(wait=False, cancel_futures=False)


def _strip_generation_markers(content: str) -> str:
    value = content.strip()
    markers = ("<|im_end|>", "<|endoftext|>")
    changed = True
    while changed:
        changed = False
        for marker in markers:
            if value.endswith(marker):
                value = value[: -len(marker)].rstrip()
                changed = True
    return value
