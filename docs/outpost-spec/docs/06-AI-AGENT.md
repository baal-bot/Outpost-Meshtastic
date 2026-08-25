# 06 — AI Agent

**Status:** Baseline · **Phase:** 2 · **Prerequisite:** [05-DATA-MODEL.md](05-DATA-MODEL.md)
**Implements:** `src/outpost/ai/`

---

## 1. What this is and is not

**It is:** a small, constrained, retrieval-grounded agent that answers community questions
from *local data* — the boards, the incident log, the node directory, the cached weather,
and an operator-curated knowledge base — and fits its answer in **one 200-byte packet**,
offline.

**Length, stated once.** The model is instructed to produce **≤180 bytes** (§6.1). The
renderer targets **one part of ≤200 bytes**. The transport class `ai` permits **2 parts**
(doc 03 §5) purely as headroom for the rare answer that genuinely needs it. All three are
byte counts of UTF-8, never character counts.

**It is not:** a chatbot. Not a general assistant. Not a passthrough to a model. Not a
source of facts about the world beyond what the operator has put into it.

**REQ-AI-001** — The assistant's default posture **MUST** be: *answer from retrieved local
evidence, cite the source, or say you don't know and name who might.* Ungrounded generation
from model parameters is permitted only for a narrow allowlist of task types (§6.3) and
**MUST** be labelled differently.

> **The reason is airtime and trust, not caution for its own sake.** A 1.5B model's
> parametric knowledge about *your valley* is nil. Spending 2 seconds of shared channel time
> to transmit a hallucinated answer about when the transfer station opens is worse than
> transmitting nothing. Retrieval is what makes this feature worth its cost.

---

## 2. Provider abstraction

### 2.1 Reality check on the hardware

The brief assumed a 4B-class Qwen on the Hailo accelerator. Verified findings, which the
design must accommodate:

| Finding | Evidence |
|---|---|
| No 4B model exists for Hailo-10H. Ceiling is **Qwen3-1.7B-Instruct** | [Hailo GenAI Model Zoo MODELS.rst](https://github.com/hailo-ai/hailo_model_zoo_genai/blob/main/docs/MODELS.rst) |
| `hailo-ollama` serves only a subset: Qwen2/2.5-1.5B variants, DeepSeek-R1-Distill-1.5B, Llama3.2-3B | [`/hailo/v1/list` dump, CNX Software](https://www.cnx-software.com/2026/01/20/raspberry-pi-ai-hat-2-review-a-40-tops-ai-accelerator-tested-with-computer-vision-llm-and-vlm-workloads/) |
| Qwen3-4B unsupported; no working custom GenAI compile path in the SDK | [Hailo community thread](https://community.hailo.ai/t/qwen-3-4b-on-hailo10h/18818) |
| **Context is hard-capped at 2048 tokens**, compiled into the `.hef` | MODELS.rst |
| Throughput ~4.8–9.9 tok/s (vendor); ~5.9–8.1 tok/s (independent) | MODELS.rst; CNX |
| **Pi 5 CPU beat the NPU on every model tested** (e.g. 11.7 vs 6.7 tok/s on Qwen2.5-1.5B) | CNX |
| Model unloads on idle → 25–40 s cold start | [Hackster hands-on](https://www.hackster.io/news/gen-ai-on-your-raspberry-pi-a-hands-on-review-of-the-raspberry-pi-ai-hat-2-3c829a8894dd) |
| `hailo-ollama` exposes `/api/chat` and `/api/pull` on port 8000; **no OpenAI-compatible endpoint**, `/api/generate` undocumented | [hailo-ollama README](https://github.com/hailo-ai/hailo-apps/blob/main/hailo_apps/python/gen_ai_apps/hailo_ollama/README.md) |
| No text-embedding model in the Hailo GenAI zoo — embeddings must run on CPU | MODELS.rst |

Every row above is **provisional** and is re-verified on the actual hardware during Phase 2
(doc 15 Part C, U2–U7). The architecture below is designed so that being wrong about any of
them costs a config change, not a rewrite.

**REQ-AI-002** — The system **MUST NOT** hard-depend on Hailo. Inference is behind a
provider interface with **five** implementations — four real backends plus `null` — all
first-class:

| Provider | Backend | Notes |
|---|---|---|
| `hailo` | `hailo-ollama` at `127.0.0.1:8000`, `/api/chat` | Low power, offloads CPU. 2048-token ceiling. |
| `llamacpp` | `llama-server` (`llama.cpp`) on the Pi CPU | Arbitrary models, arbitrary context, ~10–20% faster than Ollama on the same model |
| `ollama` | Standard Ollama, local or on a LAN host | For operators with a beefier box on the LAN |
| `openai_compat` | Any OpenAI-compatible endpoint | Explicitly opt-in; **off by default**; sends data off-node |
| `null` | Returns "unavailable" | Used when the AI module is enabled but no backend is reachable |

**REQ-AI-003** — Selecting `openai_compat` **MUST** produce a startup warning, **MUST** be
visible on the dashboard as "AI: external provider — data leaves this node", and **MUST** be
disclosed in `ABOUT` over the air. Sovereignty is product principle 6 (doc 01 §9); if the
operator overrides it, the community is told.

### 2.2 Interface

```python
class InferenceProvider(Protocol):
    name: str
    async def health(self) -> ProviderHealth: ...
    async def capabilities(self) -> Capabilities:
        """context_tokens, supports_tools, supports_streaming, max_output_tokens"""
    async def chat(self, req: ChatRequest) -> ChatResponse: ...
    async def warm(self) -> None: ...
```

**REQ-AI-004** — `Capabilities.context_tokens` **MUST** be discovered or configured per
provider, and the token budgeter (§5) **MUST** use the discovered value, not a constant. A
provider that cannot report it uses the configured value and logs at startup.

**REQ-AI-005** — Providers **MUST** normalise their wire format inside the adapter. The
agent core deals only in `ChatRequest`/`ChatResponse`. Notably, `hailo-ollama` is *not*
OpenAI-compatible and its streaming chunks are bare `{"content": "..."}` objects — that
translation belongs entirely in the `hailo` adapter.

### 2.3 Tool calling on a small model

**REQ-AI-006** — The agent **MUST NOT** assume native tool-calling support. Two modes:

- **Native**, when `Capabilities.supports_tools` is true (e.g. a `llamacpp` build with
  grammar-constrained function calling, or `Qwen2-1.5B-Instruct-Function-Calling-v1` on
  Hailo).
- **Constrained-emit**, otherwise: a single-tool-per-turn protocol where the model is
  instructed to emit exactly one line of the form `TOOL <name> <json-args>` or `ANSWER
  <text>`, and the parser is strict. Malformed output triggers **one** retry with a
  corrective system note, then falls back to a non-AI response.

**REQ-AI-007** — Maximum **2** tool-call rounds per question (config `ai.max_tool_rounds`).
A 1.5B model on a 2048-token budget cannot sustain a longer loop, and each round costs
seconds. Beyond the limit, the agent answers with whatever evidence it has or declines.

**REQ-AI-008** — Prefer **pre-retrieval over tool calling** where the intent is clear.
The router classifies the question (§4.1) and pre-populates evidence before the model's
first turn. Tool calling is for the residual cases. This halves latency and token spend on
the common path.

### 2.4 Model selection guidance

**REQ-AI-009** — The default configured model **MUST** be a 1.5B-class instruct model.
`ai.model` is config-driven. Recommended starting points, to be confirmed by the benchmark
(§3):

| Provider | Suggested starting point | Rationale | Availability |
|---|---|---|---|
| `hailo` | `qwen2.5-instruct:1.5b` | Best instruction-following of the observed served set | Confirmed in a published `/hailo/v1/list` dump |
| `hailo` (tools) | `qwen2-1.5b-instruct-function-calling-v1` | Only tool-tuned model in the GenAI zoo | **In the zoo, not observed in `/hailo/v1/list`** — verify on the unit (doc 15 U3) |
| `hailo` (newer) | `qwen3-1.7b-instruct` | Zoo ceiling | **Same caveat** (doc 15 U3) |
| `llamacpp` | `Qwen2.5-1.5B-Instruct-Q4_K_M` | Fast on Pi 5 CPU, no 2048 ceiling | Always |
| `llamacpp` (quality) | `Qwen2.5-3B-Instruct-Q4_K_M` | Slower; acceptable for DM-only use | Always |

**REQ-AI-009a** — Phase 2 **MUST** begin by querying `GET /hailo/v1/list` on the actual unit
and recording the result in the benchmark report. The table above is a starting point from
published sources, not a guarantee; `hailo-ollama` serves a subset of the zoo, and that
subset changes with releases.

**REQ-AI-010** — `llama3.2:3b` on Hailo **MUST NOT** be the default. The one independent
benchmark available reports it at roughly 2.6 tok/s, at which a 200-byte answer costs
tens of seconds of generation alone.

### 2.5 Concurrency and warmth

**REQ-AI-011** — Inference **MUST** be serialised behind a semaphore of size
`ai.max_concurrency` (default **1**). Hailo concurrency behaviour is undocumented; assume
serial until measured.

**REQ-AI-012** — Queue depth **MUST** be bounded (default 3). Requests beyond it are
rejected immediately with a terse line, not queued. A 5-deep queue at 20 s each means the
fifth person waits 100 s for an answer to a question they've forgotten asking.

**REQ-AI-013** — A keep-warm task **MUST** issue a minimal inference on
`ai.keep_warm.interval_s` (default 240) when the provider reports idle unload behaviour,
and **MUST** be suppressed when the system is on battery/UPS power if that state is known.

**REQ-AI-014** — If a request arrives while cold and the estimated wait exceeds
`ai.cold_placeholder_threshold_s` (default 15), the node **MAY** send a single ≤40-byte
placeholder — **only** on a DM, **only** if the `ai` airtime class has budget, and **never**
on a channel. Default for this behaviour is **off**.

---

## 3. Benchmark harness (mandatory Phase 2 deliverable)

**REQ-AI-015** — `tools/bench_inference.py` **MUST** be delivered and **MUST** be run on the
target hardware before the default provider is fixed. It **MUST** measure, per provider and
model:

- time-to-first-token and total latency, p50/p95, over a fixed prompt set
- generation tok/s and prompt-eval tok/s
- cold-start latency after a forced idle period
- system power draw if a meter is configured (optional)
- **task accuracy** on a 60-item local eval set (§8) — correctness, groundedness, format
  compliance, and refusal appropriateness

**REQ-AI-016** — The harness **MUST** emit a Markdown report and a machine-readable JSON
result, and the chosen default **MUST** be justified against that report in
[15-DECISIONS.md](15-DECISIONS.md).

> This exists because the published benchmarks disagree with the vendor's framing: the Pi 5
> CPU outperformed the NPU on every model in independent testing. The correct provider is an
> empirical question on the operator's actual hardware, not a spec decision.

---

## 4. Retrieval

### 4.1 Question classification

**REQ-AI-017** — Before any inference, the question **MUST** be classified by a cheap,
deterministic classifier (keyword + regex + recency heuristics — **not** a model call) into
one or more of:

| Class | Pre-retrieved evidence |
|---|---|
| `local_knowledge` | KB documents (operator-curated), top-k |
| `board_content` | Recent + FTS-matched posts across readable boards |
| `incident` | Active and recently-resolved incidents, geo-filtered to the asker |
| `weather` | Cached current conditions + forecast + active alerts |
| `directory` | Node/member directory facts |
| `node_status` | Uptime, airtime, link state, counts |
| `howto` | Command help generated from `CommandSpec` (doc 04 §10) |
| `general` | No local evidence; §6.3 rules apply |

**REQ-AI-018** — `howto` questions ("how do I post?", "how do I send mail?") **MUST** be
answered from the command registry **without any model call**. This is the single highest-
volume AI question class and it has a deterministic answer.

**REQ-AI-019** — `node_status` and `directory` questions **SHOULD** be answered
deterministically where the question maps cleanly to an existing command.

### 4.2 Retrieval pipeline

```
question
  ├─ classify → evidence sources
  ├─ per source:
  │    FTS5 BM25 query  ─┐
  │    recency boost     ├─→ candidate chunks
  │    geo filter        │
  │    trust/visibility ─┘
  ├─ optional: embedding re-rank (cosine over kb_chunk.embedding)
  ├─ dedupe by document, cap per source
  └─ budget-fit: pack highest-scoring chunks until max_evidence_tokens
```

**REQ-AI-020** — Retrieval **MUST** respect the asker's read permissions. A `guest`
**MUST NOT** receive an answer synthesised from a board they cannot read, and mail
**MUST NOT** be a retrieval source under any circumstance, for anyone, ever.

**REQ-AI-021** — Every retrieved chunk **MUST** carry a citable reference
(`board:roads#thread42`, `inc:31`, `kb:transfer-station`, `wx:nws@1724500000`) which flows
through to the rendered answer.

**REQ-AI-022** — Recency **MUST** be a first-class ranking term. In a community context, a
post from yesterday beats a semantically better match from eight months ago. Default:
`score = bm25 * exp(-age_days / half_life)` with `half_life` per source (KB 180d, boards
14d, incidents 2d, weather 0.25d).

### 4.3 Embeddings

**REQ-AI-023** — Embeddings **MUST** be optional and **MUST** run on the CPU (no Hailo
embedding model exists). Default model: `all-MiniLM-L6-v2` (22.7M params, 384 dims, 256-token
max sequence) via ONNX Runtime or `fastembed` — chosen to avoid a full PyTorch install on
ARM.

**REQ-AI-024** — With embeddings disabled the system **MUST** remain fully functional using
FTS5 BM25 + recency alone. Hybrid retrieval (BM25 recall → embedding re-rank) is the
preferred configuration when enabled.

**REQ-AI-025** — Embedding generation **MUST** be a background task with a bounded queue,
**MUST** be idempotent, and **MUST NOT** block a user's question. A chunk with no embedding
is simply retrieved by BM25 only.

> No credible Pi 5 embedding-throughput benchmark exists in public sources. Measure it in
> the Phase 2 benchmark and record the result. Indexing is a batch cost and query-time
> embedding is one short string, so this is unlikely to be the bottleneck — the 2048-token
> context is.

### 4.4 Knowledge base

**REQ-AI-026** — The operator **MUST** be able to author KB documents from the dashboard:
the transfer station's hours, the shelter locations, the burn ban rules, the plow schedule,
who to call for what. This is the content that makes the assistant genuinely useful and it
cannot come from anywhere else.

**REQ-AI-027** — The node **MUST** ship with a seed KB template covering: how to use Outpost,
the operator's contact, the emergency disclaimer, and placeholder entries for local services.

**REQ-AI-028** — Board posts **MAY** be promoted into the KB by the operator with one action
("this answer is canonical"), which pins it and gives it KB half-life.

---

## 5. Token budget

**This is the binding constraint.** On Hailo the ceiling is a hard 2048 tokens, compiled in.

**REQ-AI-029** — A `TokenBudgeter` **MUST** enforce the following allocation, config-driven,
recomputed per request from the provider's reported context size:

| Segment | Default (2048 ctx) | Rule |
|---|---|---|
| System prompt | ≤ 240 | Fixed, measured at startup; a longer prompt fails the build |
| Tool schemas (if native tools) | ≤ 160 | Only tools relevant to the classified question |
| Evidence | ≤ 820 | **The flex segment** — packed by score until full, shrunk first under pressure |
| Conversation history | ≤ 200 | Last 2 turns maximum |
| Question | ≤ 110 | Truncate with a note if longer |
| **Reserved for output** | **≥ 220** | Never encroached upon |
| Safety margin | ≥ 298 (14.6%) | Absorbs tokenizer estimation error |
| **Total** | **2048** | |

**REQ-AI-030** — Token counting **MUST** use the provider's tokenizer where obtainable. Where
not, a conservative estimator (`ceil(bytes / 3.2)` for English) **MUST** be used and the
safety margin **MUST** be at least **15% of context** (307 tokens at 2048).

**REQ-AI-030a** — The budgeter **MUST** satisfy the margin by shrinking **evidence** — the
only flex segment — never by encroaching on the output reserve, the system prompt, or the
question. The allocation above is computed so that even at a 15% margin the evidence
allocation stays at 811 tokens, which the retrieval layer must be able to work within. If a
provider reports a context smaller than 1600 tokens, the AI module **MUST** refuse to start
and report the reason.

**REQ-AI-031** — Conversation history **MUST** be capped at 2 prior turns. This matches what
practitioners have had to do to survive the 2048 ceiling, and community memory belongs in the
boards, not in a chat buffer.

**REQ-AI-032** — Evidence packing **MUST** be greedy by score with a per-source cap so one
verbose source cannot crowd out the others.

**REQ-AI-033** — Output **MUST** be capped by `num_predict`/`max_tokens` to the reserved
output allocation, and the renderer **MUST** additionally truncate to the part budget
(2 parts for class `ai`, doc 03 §5).

---

## 6. Prompting

### 6.1 System prompt

**REQ-AI-034** — The system prompt **MUST** be templated, versioned in git, ≤260 tokens, and
**MUST** contain: role, locality, the terse-output contract, the grounding contract, the
citation format, and the refusal contract. Reference text:

```
You are {node_name}, the assistant for a local community radio network in {locale}.
You answer over LoRa radio: replies MUST be under 180 bytes. No greetings, no
sign-offs, no restating the question. Lead with the answer.

Answer ONLY from the EVIDENCE provided. If the evidence does not answer the question,
say so in one short line and name where to look instead. Never guess about local
facts: hours, closures, road conditions, people, schedules, or emergencies.

End factual answers with a short source tag from the evidence, e.g. "src: roads#42".

You are not an emergency service. For life-threatening emergencies, tell the person to
call {emergency_number} if they can, and to send REPORT here if they cannot.
```

**REQ-AI-035** — `{node_name}`, `{locale}`, `{emergency_number}`, and an operator-supplied
persona addendum (≤40 tokens) **MUST** be config-driven. The operator **MUST NOT** be able to
remove the grounding, refusal, or emergency clauses from the dashboard — those are structural.

### 6.2 Evidence block format

**REQ-AI-036** — Evidence **MUST** be presented in a compact, uniform, token-efficient
format with explicit ages:

```
EVIDENCE
[kb:transfer-station] Transfer station: Sat 8-4, Wed 12-6. Closed other days.
[roads#42 2h @dana] Culvert washed out at Mill Rd. Impassable both ways.
[inc:31 20m urgent] Tree down blocking Cedar Ln near the church.
[wx:nws 41m] Now 8C ovc W14g22. Tonight rain 80% lo 5C.
```

### 6.3 Ungrounded generation

**REQ-AI-037** — When no evidence is retrieved, the agent **MUST** default to declining:

```
[AI] No local info on that. Try BOARDS, or ask @ray.
```

**REQ-AI-038** — Ungrounded answers are permitted **only** for these task classes, and
**MUST** carry the `[AI?]` marker rather than `[AI]`:

- unit/measure conversion, arithmetic
- generic first-aid or survival *procedure* recall (never diagnosis, never dosing)
- language translation
- explaining a general concept with no local component
- summarising or rephrasing text the user supplied in the same message

**REQ-AI-039** — Ungrounded answers **MUST NOT** be permitted for: local facts, times,
places, people, road/trail conditions, weather (always use cached data), medical advice,
legal advice, or anything about an active incident.

---

## 7. Safety rails

**REQ-AI-040** — Every AI-originated transmission **MUST** be prefixed `[AI]` (grounded) or
`[AI?]` (ungrounded). This **MUST NOT** be removable by configuration. 4–5 bytes is a cheap
price for the community knowing what it is reading.

**REQ-AI-041** — The AI **MUST NOT** be permitted to author, trigger, escalate, or cancel an
`alert`. It may *summarise* an existing alert on request. Alert content is human- or
CAP-authored, always. (doc 08 §5)

**REQ-AI-042** — The AI **MUST NOT** have write tools in Phase 2. Its entire tool surface is
read-only. Write capability (e.g. "file this as an incident for me") is deferred and, if ever
added, **MUST** require an explicit user confirmation turn.

**REQ-AI-043** — A hard refusal list **MUST** be enforced by a deterministic pre-filter
*before* inference, and a post-filter on the output. Refusal categories:

| Category | Behaviour |
|---|---|
| Medical dosing, diagnosis, treatment decisions | Refuse; redirect to emergency number / responder |
| Legal advice | Refuse; one line |
| Anything asking the AI to raise an alarm or contact authorities | Refuse; explain `REPORT` and `ALERT` |
| Self-harm content | Refuse the request; provide the operator-configured crisis resource line; log for operator attention |
| Requests to reveal other members' positions, mail, or notes | Refuse; log |
| Prompt-injection attempts ("ignore previous instructions") | Refuse; log; count metric |

**REQ-AI-044 (prompt injection)** — Retrieved evidence is **untrusted user-authored content**.
It **MUST** be wrapped in a clearly delimited block, the system prompt **MUST** state that
evidence is data and never instructions, and the output post-filter **MUST** reject responses
that leak system-prompt text or attempt to change the `[AI]` marker.

> This threat is real here in a way it is not for a private assistant: anyone within radio
> range can post text to a board, and that text becomes retrieval evidence for everyone
> else's questions.

**REQ-AI-045** — The output post-filter **MUST** enforce: length, marker presence, absence of
system-prompt leakage, absence of URLs (useless over LoRa and a phishing vector), absence of
invented citations (every `src:` tag **MUST** match a reference that was actually in the
evidence block — a mismatch downgrades the answer to `[AI?]` and logs).

**REQ-AI-046** — Every interaction **MUST** be written to `ai_interaction` with question,
evidence refs, tools called, answer, groundedness, and timings (doc 05 §8).

**REQ-AI-047** — The dashboard **MUST** provide an AI review queue where the operator can
read recent interactions, rate them, promote a good answer into the KB, and add a refusal
rule — in one click each.

---

## 8. Evaluation

**REQ-AI-048** — A local eval set of **≥60 questions** **MUST** be delivered with the system,
covering every question class, with expected behaviour rather than exact strings:
grounded/ungrounded, expected source, expected refusal, max length.

**REQ-AI-049** — `pytest -m eval` **MUST** run the eval set against a configured provider and
report pass rate by class. It **MUST NOT** run in normal CI (it needs hardware) but **MUST**
be runnable on the target node with one command.

**REQ-AI-050** — Release gate: **≥70%** of eval items pass, with **zero** failures in the
refusal and safety categories. A safety failure blocks the release outright.

---

## 9. Channel policy

**REQ-AI-051** — AI availability **MUST** be per-channel policy (doc 02 §7 `channels:`):

| Setting | Behaviour |
|---|---|
| `ai: false` (default, incl. channel 0) | `ASK` returns "not available here, DM me" |
| `ai: true` | `ASK` works with the explicit prefix; bare text still never falls through |
| DM | `ASK` works; unmatched text falls through to `ASK` |

**REQ-AI-052** — The assistant **MUST NOT** be enabled on the primary public channel by
default. Meshtastic community norms treat bot chatter on the default public channel as
antisocial, and existing mesh AI bots ship with public-channel responses disabled for the
same reason.

---

## 10. Failure behaviour

| Failure | Required response |
|---|---|
| Provider unreachable | `[AI] Assistant offline. Try BOARDS or ASK @ray.` — one line, `reply` class not `ai` class |
| Timeout (`ai.timeout_s`, default 45) | Cancel, log, one terse line. **Never** transmit a partial answer |
| Queue full | `[AI] Busy. Retry in a min.` |
| Malformed tool call after 1 retry | Fall back to answering from evidence with no tools; if that fails, decline |
| Output fails post-filter | Do not transmit. Log with the rejected text at debug. Emit the generic decline |
| Repeated failures (5 in 10 min) | Circuit-break the AI module for 15 min; health `degraded`; dashboard alert |

**REQ-AI-053** — AI failure **MUST NOT** affect any other subsystem. The BBS, mail, watch,
and weather paths **MUST** remain fully responsive with the AI circuit-broken.

---

## 11. Metrics

```
outpost_ai_requests_total          counter   {class,channel_kind}
outpost_ai_grounded_total          counter
outpost_ai_refused_total           counter   {reason}
outpost_ai_tool_calls_total        counter   {tool,outcome}
outpost_ai_ttft_seconds            histogram {provider,model}
outpost_ai_total_seconds           histogram {provider,model}
outpost_ai_prompt_tokens           histogram
outpost_ai_output_tokens           histogram
outpost_ai_budget_overflow_total   counter   {segment}
outpost_ai_postfilter_reject_total counter   {reason}
outpost_ai_provider_health         gauge     {provider}
outpost_ai_cold_starts_total       counter
```

---

## 12. Tool catalogue (Phase 2, all read-only)

| Tool | Args | Returns |
|---|---|---|
| `search_boards` | `query`, `board?`, `limit` | matching posts with refs and ages |
| `recent_posts` | `board?`, `hours`, `limit` | recent thread summaries |
| `get_thread` | `thread_ref`, `limit` | thread with posts |
| `active_incidents` | `radius_km?`, `type?` | active incidents near the asker |
| `get_incident` | `ref` | incident + updates |
| `current_weather` | `place?` | cached conditions + age |
| `forecast` | `place?`, `days` | cached forecast + age |
| `active_weather_alerts` | — | active CAP alerts |
| `find_member` | `handle_or_partial` | handle, last heard, trust (never position) |
| `node_status` | — | uptime, airtime, link, counts |
| `search_kb` | `query`, `limit` | KB chunks |
| `list_commands` | `topic?` | from the command registry |

**REQ-AI-054** — Tools **MUST** be declared with Pydantic argument models, **MUST** validate
input strictly, and **MUST** return token-bounded results (each tool declares its own
`max_result_tokens`).

**REQ-AI-055** — Tool results **MUST** be filtered by the asker's permissions inside the tool
implementation, not afterwards.

**REQ-AI-056** — `find_member` **MUST NOT** return position under any circumstance. Position
lookup is an explicit user command with its own consent rules (doc 12 §8), not an AI tool.
