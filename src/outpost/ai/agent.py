from __future__ import annotations

import asyncio
import json
from collections import deque
from dataclasses import dataclass
from typing import Any

from prometheus_client import Counter, Gauge, Histogram

from outpost.ai.budget import BudgetError, EvidencePack, TokenBudgeter
from outpost.ai.prompts import SITUATION_PROMPT, SYSTEM_PROMPT, UNGROUNDED_PROMPT
from outpost.ai.providers.models import (
    ChatMessage,
    ChatRequest,
    InferenceProvider,
    ProviderHealth,
    ProviderState,
)
from outpost.ai.retrieval import RetrievalEngine
from outpost.ai.safety import (
    contains_prompt_leak,
    extractive_fallback,
    fit_bytes,
    postfilter,
    prefilter,
    unsafe_evidence,
)
from outpost.ai.store import AIStore, InteractionRecord
from outpost.config import Config
from outpost.store.members import Member
from outpost.transport.models import TrafficClass

AI_REQUESTS = Counter(
    "outpost_ai_requests_total", "AI requests", ("class", "channel_kind", "outcome")
)
AI_REFUSED = Counter("outpost_ai_refused_total", "AI refusals", ("reason",))
AI_POSTFILTER = Counter("outpost_ai_postfilter_reject_total", "AI output rejects", ("reason",))
AI_GROUNDED = Counter("outpost_ai_grounded_total", "Grounded AI responses")
AI_TTFT = Histogram("outpost_ai_ttft_seconds", "AI time to first token", ("provider", "model"))
AI_TOTAL = Histogram("outpost_ai_total_seconds", "AI provider latency", ("provider", "model"))
AI_PROMPT_TOKENS = Histogram("outpost_ai_prompt_tokens", "AI prompt tokens")
AI_OUTPUT_TOKENS = Histogram("outpost_ai_output_tokens", "AI output tokens")
AI_BUDGET_OVERFLOW = Counter("outpost_ai_budget_overflow_total", "AI budget failures", ("segment",))
AI_PROVIDER_HEALTH = Gauge("outpost_ai_provider_health", "AI provider health", ("provider",))
AI_EVIDENCE_REJECTED = Counter(
    "outpost_ai_evidence_rejected_total", "AI evidence chunks rejected", ("reason",)
)
AI_CIRCUIT_TRANSITIONS = Counter(
    "outpost_ai_circuit_transitions_total",
    "AI generation circuit transitions",
    ("transition", "reason"),
)
SYNTHESIS_OUTPUT_TOKEN_CAP = 96
SITUATION_OUTPUT_TOKEN_CAP = 160


@dataclass(frozen=True)
class AIAnswer:
    text: str
    outcome: str
    question_class: str
    grounded: bool = False
    refused: bool = False
    refusal_reason: str | None = None
    airtime_class: TrafficClass = TrafficClass.AI


class AIService:
    def __init__(
        self,
        config: Config,
        provider: InferenceProvider,
        retrieval: RetrievalEngine,
        store: AIStore,
        *,
        now: Any,
    ) -> None:
        self.config = config
        self.provider = provider
        self.retrieval = retrieval
        self.store = store
        self.now = now
        self._semaphore = asyncio.Semaphore(config.ai.max_concurrency)
        self._count_lock = asyncio.Lock()
        self._pending = 0
        self._failures: deque[int] = deque()
        self._circuit_open_until = 0
        self._circuit_open_count = 0
        self._circuit_last_opened_at: int | None = None
        self._circuit_last_closed_at: int | None = None
        self._circuit_last_close_reason: str | None = None
        self._generation_last_success_at: int | None = None
        self._generation_last_failure_at: int | None = None
        self._capabilities: Any = None
        self._provider_health = ProviderHealth(
            state=ProviderState.UNAVAILABLE, detail="provider has not been checked"
        )
        self._provider_health_checked_at: int | None = None

    async def initialize(self) -> None:
        self._capabilities = await self.provider.capabilities()
        budgeter = TokenBudgeter(self.config.ai.budget, self._capabilities.context_tokens)
        system = SYSTEM_PROMPT.format(
            node_name=self.config.node.name,
            locale=self.config.node.locale,
            emergency_number=self.config.node.emergency_number,
            persona=self.config.ai.persona_addendum,
        )
        budgeter.plan(system=system, question="startup validation")
        budgeter.plan(system=UNGROUNDED_PROMPT, question="startup validation")
        await self.check_health()

    async def close(self) -> None:
        await self.provider.close()

    def _remember_health(self, health: ProviderHealth) -> ProviderHealth:
        self._provider_health = health
        self._provider_health_checked_at = int(self.now())
        AI_PROVIDER_HEALTH.labels(self.provider.name).set(
            1 if health.state is ProviderState.HEALTHY else 0
        )
        return health

    @property
    def provider_ready(self) -> bool:
        return self._provider_health.state is ProviderState.HEALTHY

    async def check_health(self) -> ProviderHealth:
        try:
            health = await asyncio.wait_for(
                self.provider.health(), timeout=self.config.ai.timeout_s
            )
        except Exception as error:
            health = ProviderHealth(
                state=ProviderState.UNAVAILABLE,
                detail=f"health check failed ({type(error).__name__})",
            )
        return self._remember_health(health)

    async def warm(self) -> bool:
        try:
            if self.provider_ready and self.config.ai.keep_warm.enabled:
                await asyncio.wait_for(self.provider.warm(), timeout=self.config.ai.timeout_s)
            health = await self.check_health()
        except Exception as error:
            health = self._remember_health(
                ProviderHealth(
                    state=ProviderState.UNAVAILABLE,
                    detail=f"warmup failed ({type(error).__name__})",
                )
            )
        return health.state is ProviderState.HEALTHY

    @property
    def circuit_open(self) -> bool:
        now = int(self.now())
        self._age_failures(now)
        return now < self._circuit_open_until

    @property
    def generation_working(self) -> bool | None:
        if self._generation_last_failure_at is None:
            return True if self._generation_last_success_at is not None else None
        return bool(
            self._generation_last_success_at is not None
            and self._generation_last_success_at >= self._generation_last_failure_at
        )

    async def status(self) -> dict[str, Any]:
        health = await self.check_health()
        capabilities = self._capabilities or await self.provider.capabilities()
        return {
            "provider": self.provider.name,
            "model": self.provider.model,
            "external": self.provider.external,
            "health": health.model_dump(mode="json"),
            "capabilities": capabilities.model_dump(mode="json"),
            "queue": {
                "active_and_waiting": self._pending,
                "capacity": self.config.ai.max_concurrency + self.config.ai.queue_depth,
            },
            "generation": {
                "working": self.generation_working,
                "last_success_at": self._generation_last_success_at,
                "last_failure_at": self._generation_last_failure_at,
            },
            "circuit": {
                "open": self.circuit_open,
                "open_until": self._circuit_open_until or None,
                "recent_failures": len(self._failures),
                "open_count": self._circuit_open_count,
                "last_opened_at": self._circuit_last_opened_at,
                "last_closed_at": self._circuit_last_closed_at,
                "last_close_reason": self._circuit_last_close_reason,
            },
        }

    def snapshot(self) -> dict[str, Any]:
        module_enabled = self.config.modules.ai.enabled
        return {
            "provider": self.provider.name,
            "model": self.provider.model,
            "external": self.provider.external,
            "pending": self._pending,
            "circuit_open": self.circuit_open,
            "circuit_open_until": self._circuit_open_until or None,
            "ready": (self.provider_ready and not self.circuit_open) if module_enabled else None,
            "provider_reachable": self.provider_ready if module_enabled else None,
            "generation_working": self.generation_working if module_enabled else None,
            "generation_last_success_at": self._generation_last_success_at,
            "generation_last_failure_at": self._generation_last_failure_at,
            "health_state": (self._provider_health.state.value if module_enabled else "disabled"),
            "health_detail": (
                self._provider_health.detail if module_enabled else "AI module disabled"
            ),
            "health_checked_at": self._provider_health_checked_at,
            "required_for_readiness": (module_enabled and self.config.ai.required_for_readiness),
        }

    async def narrate_situation(
        self, snapshot: dict[str, Any], required_refs: tuple[str, ...]
    ) -> tuple[str | None, str]:
        """Phrase an already-authorized snapshot without doing retrieval or fact selection."""

        if not self.config.modules.ai.enabled:
            return None, "disabled"
        if not self.provider_ready or self.circuit_open:
            return None, "unavailable"
        async with self._count_lock:
            capacity = self.config.ai.max_concurrency + self.config.ai.queue_depth
            if self._pending >= capacity:
                return None, "queue_full"
            self._pending += 1
        try:
            async with self._semaphore:
                prompt = json.dumps(
                    {"required_refs": required_refs, "snapshot": snapshot},
                    sort_keys=True,
                    separators=(",", ":"),
                )
                response = await asyncio.wait_for(
                    self.provider.chat(
                        ChatRequest(
                            messages=(
                                ChatMessage(role="system", content=SITUATION_PROMPT),
                                ChatMessage(role="user", content=prompt),
                            ),
                            max_output_tokens=min(
                                self.config.ai.max_output_tokens, SITUATION_OUTPUT_TOKEN_CAP
                            ),
                            temperature=0,
                        )
                    ),
                    timeout=self.config.ai.timeout_s,
                )
        except Exception:
            self._remember_health(
                ProviderHealth(state=ProviderState.UNAVAILABLE, detail="briefing inference failed")
            )
            self._record_failure()
            AI_REQUESTS.labels("situation", "web", "provider_error").inc()
            return None, "provider_error"
        finally:
            async with self._count_lock:
                self._pending -= 1
        self._record_generation_success()
        if response.ttft_ms is not None:
            AI_TTFT.labels(self.provider.name, self.provider.model).observe(response.ttft_ms / 1000)
        AI_TOTAL.labels(self.provider.name, self.provider.model).observe(response.total_ms / 1000)
        if response.prompt_tokens is not None:
            AI_PROMPT_TOKENS.observe(response.prompt_tokens)
        if response.output_tokens is not None:
            AI_OUTPUT_TOKENS.observe(response.output_tokens)
        content = response.content.strip()
        if contains_prompt_leak(content):
            AI_POSTFILTER.labels("system_prompt_leak").inc()
            AI_REQUESTS.labels("situation", "web", "rejected").inc()
            return None, "rejected"
        AI_REQUESTS.labels("situation", "web", "answered").inc()
        return content, "answered"

    async def answer(self, question: str, member: Member, channel: int, registry: Any) -> AIAnswer:
        question = " ".join(question.split()).strip()
        if not question:
            answer = AIAnswer("[AI] ASK needs a question.", "invalid", "general")
            await self._log(answer, question, member, channel, ())
            self._record_request(answer, channel)
            return answer
        custom_reason = await self.store.matching_rule(question)
        refusal = prefilter(question, self.config.node.emergency_number)
        if custom_reason and refusal is None:
            answer = AIAnswer(
                "[AI] I can't help with that request. Ask the operator.",
                "refused",
                "refusal",
                refused=True,
                refusal_reason=f"operator:{custom_reason}",
            )
            AI_REFUSED.labels(f"operator:{custom_reason}").inc()
            await self._log(answer, question, member, channel, ())
            self._record_request(answer, channel)
            return answer
        if refusal is not None:
            answer = AIAnswer(
                refusal.text,
                "refused",
                "refusal",
                refused=True,
                refusal_reason=refusal.reason,
            )
            AI_REFUSED.labels(refusal.reason).inc()
            await self._log(answer, question, member, channel, ())
            self._record_request(answer, channel)
            return answer

        result = await self.retrieval.retrieve(question, member, registry)
        primary = result.classes[0].value
        if result.deterministic_answer is not None:
            answer = AIAnswer(result.deterministic_answer, "deterministic", primary, grounded=True)
            await self._log(answer, question, member, channel, ())
            self._record_request(answer, channel)
            return answer
        if not result.chunks and not result.allow_ungrounded:
            answer = AIAnswer(
                "[AI] No local info on that. Try BOARDS or ask the operator.",
                "no_evidence",
                primary,
            )
            await self._log(answer, question, member, channel, ())
            self._record_request(answer, channel)
            return answer
        if self.circuit_open:
            answer = self._offline(primary, "circuit_open")
            await self._log(answer, question, member, channel, ())
            self._record_request(answer, channel)
            return answer
        async with self._count_lock:
            capacity = self.config.ai.max_concurrency + self.config.ai.queue_depth
            if self._pending >= capacity:
                answer = AIAnswer(
                    "[AI] Busy. Retry in a min.",
                    "queue_full",
                    primary,
                    airtime_class=TrafficClass.REPLY,
                )
                await self._log(answer, question, member, channel, ())
                self._record_request(answer, channel)
                return answer
            self._pending += 1
        try:
            async with self._semaphore:
                return await self._infer(question, member, channel, primary, result)
        finally:
            async with self._count_lock:
                self._pending -= 1

    async def _infer(
        self,
        question: str,
        member: Member,
        channel: int,
        primary: str,
        retrieval: Any,
    ) -> AIAnswer:
        grounded = bool(retrieval.chunks)
        capabilities = self._capabilities or await self.provider.capabilities()
        system = (
            SYSTEM_PROMPT.format(
                node_name=self.config.node.name,
                locale=self.config.node.locale,
                emergency_number=self.config.node.emergency_number,
                persona=self.config.ai.persona_addendum,
            )
            if grounded
            else UNGROUNDED_PROMPT
        )
        try:
            budgeter = TokenBudgeter(self.config.ai.budget, capabilities.context_tokens)
            plan = budgeter.plan(system=system, question=question)
            pack: EvidencePack = budgeter.pack_evidence(plan, retrieval.chunks)
        except BudgetError:
            AI_BUDGET_OVERFLOW.labels("context").inc()
            answer = self._offline(primary, "budget_error")
            await self._log(answer, question, member, channel, ())
            self._record_request(answer, channel)
            return answer
        if grounded and not pack.chunks:
            answer = AIAnswer(
                "[AI] Local info is indexed but unavailable within the current AI budget. "
                "Ask the operator.",
                "evidence_budget_empty",
                primary,
            )
            await self._log(answer, question, member, channel, ())
            self._record_request(answer, channel)
            return answer
        rejected_chunks = tuple(chunk for chunk in pack.chunks if unsafe_evidence((chunk,)))
        rejected_refs = tuple(chunk.ref for chunk in rejected_chunks)
        evidence_rejection_reason = "evidence_injection" if rejected_refs else None
        if rejected_refs:
            AI_EVIDENCE_REJECTED.labels("prompt_injection").inc(len(rejected_refs))
            rejected = set(rejected_refs)
            pack = budgeter.pack_evidence(
                plan, tuple(chunk for chunk in pack.chunks if chunk.ref not in rejected)
            )
        if grounded and not pack.chunks:
            answer = AIAnswer(
                "[AI] No local info on that. Try BOARDS or ask the operator.",
                "no_evidence",
                primary,
            )
            await self._log(
                answer,
                question,
                member,
                channel,
                (),
                rejected_evidence_refs=rejected_refs,
                evidence_rejection_reason=evidence_rejection_reason,
            )
            self._record_request(answer, channel)
            return answer
        prompt = f"{pack.text}\n\nQUESTION\n{plan.question}" if pack.text else plan.question
        try:
            response = await asyncio.wait_for(
                self.provider.chat(
                    ChatRequest(
                        messages=(
                            ChatMessage(role="system", content=system),
                            ChatMessage(role="user", content=prompt),
                        ),
                        max_output_tokens=min(
                            self.config.ai.max_output_tokens, SYNTHESIS_OUTPUT_TOKEN_CAP
                        ),
                        temperature=0,
                    )
                ),
                timeout=self.config.ai.timeout_s,
            )
        except Exception:
            self._remember_health(
                ProviderHealth(
                    state=ProviderState.UNAVAILABLE,
                    detail="inference request failed",
                )
            )
            self._record_failure()
            answer = self._offline(primary, "provider_error")
            await self._log(
                answer,
                question,
                member,
                channel,
                tuple(c.ref for c in pack.chunks),
                rejected_evidence_refs=rejected_refs,
                evidence_rejection_reason=evidence_rejection_reason,
            )
            self._record_request(answer, channel)
            return answer
        self._record_generation_success()
        if response.ttft_ms is not None:
            AI_TTFT.labels(self.provider.name, self.provider.model).observe(response.ttft_ms / 1000)
        AI_TOTAL.labels(self.provider.name, self.provider.model).observe(response.total_ms / 1000)
        if response.prompt_tokens is not None:
            AI_PROMPT_TOKENS.observe(response.prompt_tokens)
        if response.output_tokens is not None:
            AI_OUTPUT_TOKENS.observe(response.output_tokens)
        filtered = postfilter(
            response.content,
            evidence_refs=tuple(chunk.ref for chunk in pack.chunks),
            grounded=grounded,
        )
        if filtered.accepted:
            text, outcome = filtered.text or "", "answered"
        elif grounded:
            AI_POSTFILTER.labels(filtered.reason or "unknown").inc()
            text, outcome = extractive_fallback(pack.chunks), "extractive_fallback"
        else:
            AI_POSTFILTER.labels(filtered.reason or "unknown").inc()
            text, outcome = "[AI] No safe local answer. Try BOARDS or ask the operator.", "rejected"
        answer = AIAnswer(text, outcome, primary, grounded=grounded)
        await self._log(
            answer,
            question,
            member,
            channel,
            tuple(chunk.ref for chunk in pack.chunks),
            response=response,
            rejected_evidence_refs=rejected_refs,
            evidence_rejection_reason=evidence_rejection_reason,
        )
        self._record_request(answer, channel)
        return answer

    def _record_failure(self) -> None:
        now = int(self.now())
        self._generation_last_failure_at = now
        self._failures.append(now)
        self._age_failures(now)
        if len(self._failures) >= self.config.ai.circuit_breaker.failures and not self.circuit_open:
            self._circuit_open_until = now + self.config.ai.circuit_breaker.open_minutes * 60
            self._circuit_open_count += 1
            self._circuit_last_opened_at = now
            AI_CIRCUIT_TRANSITIONS.labels("opened", "generation_failures").inc()

    def _age_failures(self, now: int) -> None:
        cutoff = now - self.config.ai.circuit_breaker.window_minutes * 60
        while self._failures and self._failures[0] < cutoff:
            self._failures.popleft()

    def _record_generation_success(self) -> None:
        now = int(self.now())
        self._remember_health(ProviderHealth(state=ProviderState.HEALTHY, detail="ready"))
        circuit_was_opened = bool(
            self._circuit_last_opened_at is not None
            and (
                self._circuit_last_closed_at is None
                or self._circuit_last_closed_at < self._circuit_last_opened_at
            )
        )
        self._generation_last_success_at = now
        self._age_failures(now)
        self._circuit_open_until = 0
        if circuit_was_opened:
            self._circuit_last_closed_at = now
            self._circuit_last_close_reason = "successful_inference"
            AI_CIRCUIT_TRANSITIONS.labels("closed", "successful_inference").inc()

    def _offline(self, primary: str, outcome: str) -> AIAnswer:
        operator = self.config.node.operator_contact
        text = fit_bytes(f"[AI] Assistant offline. Try BOARDS or ask {operator}.")
        return AIAnswer(text, outcome, primary, airtime_class=TrafficClass.REPLY)

    async def _log(
        self,
        answer: AIAnswer,
        question: str,
        member: Member,
        channel: int,
        evidence_refs: tuple[str, ...],
        *,
        response: Any = None,
        rejected_evidence_refs: tuple[str, ...] = (),
        evidence_rejection_reason: str | None = None,
    ) -> None:
        await self.store.log(
            InteractionRecord(
                member_id=member.id if member.id > 0 else None,
                channel=channel,
                question=question,
                question_class=answer.question_class,
                provider=self.provider.name,
                model=self.provider.model,
                evidence_refs=evidence_refs,
                answer=answer.text,
                grounded=answer.grounded,
                refused=answer.refused,
                refusal_reason=answer.refusal_reason,
                outcome=answer.outcome,
                rejected_evidence_refs=rejected_evidence_refs,
                evidence_rejection_reason=evidence_rejection_reason,
                prompt_tokens=getattr(response, "prompt_tokens", None),
                output_tokens=getattr(response, "output_tokens", None),
                ttft_ms=getattr(response, "ttft_ms", None),
                total_ms=getattr(response, "total_ms", None),
            )
        )

    @staticmethod
    def _record_request(answer: AIAnswer, channel: int) -> None:
        AI_REQUESTS.labels(
            answer.question_class,
            AIService._channel_kind(channel),
            answer.outcome,
        ).inc()
        if answer.grounded:
            AI_GROUNDED.inc()

    @staticmethod
    def _channel_kind(channel: int) -> str:
        return "dm" if channel < 0 else "channel"
