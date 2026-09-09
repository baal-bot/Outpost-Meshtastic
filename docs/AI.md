# Local AI

Outpost's assistant is a constrained, retrieval-grounded radio service, not a general chatbot.
`ASK`, deterministic command help, permission-filtered retrieval, hard safety filters, the local
knowledge base, and the operator review console are implemented. Enable the module only after the
configured provider passes the guarded evaluation on its target hardware.

The server classifies each question and selects authorized, read-only evidence before inference.
The model receives one bounded synthesis request; it cannot initiate database queries or choose a
second retrieval round. This keeps authorization in deterministic application code and gives the
native Hailo path the same behavior as HTTP providers.

## Provider choices

| Provider | Endpoint | Data boundary |
| --- | --- | --- |
| `hailo_vlm` | Direct HailoRT Python VLM API | Local Hailo-10H |
| `hailo` | Hailo-Ollama `/api/chat` | Local Hailo-10H |
| `llamacpp` | llama.cpp `/v1/chat/completions` | Local or operator-managed LAN |
| `ollama` | Ollama `/api/chat` | Local or operator-managed LAN |
| `openai_compat` | OpenAI-compatible `/v1/chat/completions` | External; explicitly disclosed |
| `null` | No endpoint | Deterministic unavailable response |

All adapters return one internal response shape with context capability, prompt/output token
counts, time to first token, total latency, throughput where reported, and finish reason. Provider
wire formats do not leak into the agent. Configured context is used only when the backend cannot
report it; values below 1,600 tokens are rejected.

`openai_compat` is always treated as external even if an operator points it at a private endpoint.
Its credential is read from the configured environment-variable name and never belongs in YAML.

## Hailo AI HAT+ 2

On 64-bit Raspberry Pi OS Trixie, install a matching Hailo-10H runtime, PCIe driver, and GenAI
model-zoo release—not the incompatible Hailo-8 `hailo-all` package. The target node is validated
with the 5.3.0 suite. Raspberry Pi's `hailo-h10-all` repository package may lag that release, so
verify the candidate versions before using the metapackage and obtain the matching suite from
Hailo when necessary. Reboot after replacing the PCIe driver.

Verify the device after reboot:

```sh
hailortcli fw-control identify
ls -l /dev/hailo0 /dev/h1x-0 2>/dev/null
```

The default `hailo_vlm` provider loads `Qwen3-VL-2B-Instruct.hef` directly through HailoRT 5.3.
The installer accepts the vendor wheel and downloaded HEF as explicit inputs, installs the wheel
into the isolated Outpost release, copies the model to `/var/lib/outpost/models`, and verifies the
known-good model SHA-256:

```sh
sudo OUTPOST_HAILORT_WHEEL="$HOME/Downloads/hailo-5.3.0/hailort-5.3.0-cp313-cp313-linux_aarch64.whl" \
  OUTPOST_HAILO_VLM_MODEL="$HOME/Downloads/hailo-5.3.0/Qwen3-VL-2B-Instruct.hef" \
  ./deploy/install.sh
```

The wheel's CPython tag must match the host Python. The native adapter serialises hardware access,
clears the VLM's retained conversation context before every request, and keeps the model loaded.
Outpost currently uses its text capability; image input is a separate future interface change.
With AI enabled, `ai.required_for_readiness` defaults to `true`: a missing model or unavailable
accelerator keeps `/api/v1/health` at HTTP 503 so an upgrade cannot silently accept a broken AI
runtime. Bounded background probes restore readiness without restarting Outpost. Set this option
to `false` only when the operator intentionally wants AI to be best-effort.

The older `hailo` provider remains available as a rollback path. When it is selected,
`deploy/install.sh` installs a hardened
`hailo-ollama.service` and a vendor-native JSON configuration bound only to `127.0.0.1:8000`.
The vendor package defaults to all network interfaces, so do not run an unmodified long-lived
server on an untrusted LAN. Outpost supplies both the JSON setting used by older builds and the
`OLLAMA_HOST` setting honored by 5.3.0.

List available and installed models, then pull the configured candidate if needed:

```sh
curl -fsS http://127.0.0.1:8000/hailo/v1/list
curl -fsS http://127.0.0.1:8000/api/tags
curl -fsS http://127.0.0.1:8000/api/pull \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen2.5:1.5b","stream":false}'
```

Hailo's [official model matrix](https://github.com/hailo-ai/hailo_model_zoo_genai/blob/main/docs/MODELS.rst)
lists the C++ and Python inference APIs for Qwen3-VL rather than Hailo-Ollama. Trying to pull it
through `/api/pull` will not install a usable model. Keep the known-good Qwen 2.5 Hailo-Ollama
path available for rollback.

## Benchmark and release gate

Run a short provider benchmark before selecting a model:

```sh
.venv/bin/python tools/bench_inference.py \
  --provider hailo_vlm --model Qwen3-VL-2B-Instruct --runs 3
```

For a release-quality run, include a genuine idle interval and the full 60-item corpus:

```sh
.venv/bin/python tools/bench_inference.py \
  --provider hailo_vlm --model Qwen3-VL-2B-Instruct \
  --runs 5 --cold-idle-seconds 300 --eval
```

The tool writes Markdown and JSON under `.data/benchmarks/` by default. It records the actual Pi,
provider inventory, discovered/configured context, p50/p95 latency, time to first token, throughput,
cold-start result, errors, and per-class evaluation results. The release gate is at least 70%
overall with zero refusal or prompt-injection failures. A preflight against an unavailable backend
is useful diagnostic evidence, but it cannot justify a default-provider decision.

The initial 2026-08-27 target-node run selected `qwen2.5-instruct:1.5b` on Hailo-10H. Suite 5.3.0
publishes the same candidate as `qwen2.5:1.5b`; it remains the rollback model. The guarded
60-question evaluation passed 60/60 with zero safety failures on both versions. The post-upgrade
5.3.0 regression measured 741 ms p50 time to first token and 4.89 s p50 / 10.69 s p95 total
latency in a short six-prompt sample. Raw Qwen output failed the strict marker/citation contract in
every factual eval case, so those answers used the evidence-only cited fallback. See
[`benchmarks/HAILO-H10-QWEN-2026-08-27.md`](benchmarks/HAILO-H10-QWEN-2026-08-27.md).

The current configuration selects native `Qwen3-VL-2B-Instruct`. Its direct-provider smoke test
loaded the model in 7.29 seconds and returned a 20-token response in 4.86 seconds on the target
Hailo-10H. At the production 96-token request cap, the six-prompt sample measured 8.98 s p50 and
21.31 s p95. The complete guarded corpus passed 60/60 with zero safety failures and no provider
errors, qualifying the model behind Outpost's deterministic guards and evidence-only fallback.

## Operational boundaries

### Software evaluation and current grounding

The #156 software evaluation exercises the real `AIService`, database retrieval,
permissions, evidence packing, filters and interaction log. A frozen 28-case
synthetic corpus runs against both the guarded service and a deterministic
retrieval baseline. It includes local/imported board permissions, hidden records,
changed reports, stale/future weather, unsupported claims, injected content and
provider failure. The original 60-case G4 harness remains a separate result; it
does not exercise database retrieval. Run the software controls without hardware:

```sh
.venv/bin/python -m tools.eval_ai --output .data/ai-evaluation-156
```

The report identifies its scripted provider, corpus/source digests, per-case
failures, model calls, latency, process CPU, RSS snapshots, reply bytes and modeled
LONG_FAST airtime. Energy is explicitly unavailable. `blinded-review.json` omits
provider/mode labels and the grading result; keep `review-key.json` separate during
independent usefulness scoring. No human scores are fabricated. The manifest
prevents silently changing cases after a run; additional holdout work needs a new
version. Scripted adversarial controls are regression evidence, not model-quality
measurements. `--configured-provider --config PATH` selects a real provider for a
later comparison using the same disposable synthetic store, with no radio or
production database. The configured provider must be available for that session.

For local facts, a valid citation alone is insufficient. Generated answers must
preserve the complete cited record or its deterministic bounded excerpt, including
age, attribution and qualifiers; other wording uses the cited extractive fallback.
This deliberately limits paraphrasing of operational information. Utility tasks
such as translation keep their separate ungrounded path. Evidence is refreshed
after queueing and its source revision/access is checked again after generation;
changed information produces a retry response. Weather retrieval uses the same
configured maximum cache age as the weather service and rejects future fetch times.

AI admission leaves one inbound worker available for non-AI commands. A
single-worker configuration uses deterministic retrieval instead of model
generation. Production-path regressions saturate AI requests and still store a
board post, mail and an urgent report while serving PING, with AI disabled, blocked
or failed. Physical/energy qualification and later model comparisons do not block
completion of this software work. See [the software evidence](AI-EVALUATION-2026-09-09.md).

- AI is off in the example configuration and always off on channel 0 by default.
- AI never receives mail, exact member positions, or operator notes.
- Permission-filtered retrieval is selected by application code; model-controlled tools are not
  exposed. AI cannot create, broadcast, escalate, or cancel alerts.
- Grounded answers use `[AI]`; narrowly allowed ungrounded answers use `[AI?]`.
- Provider failure never stops BBS, mail, Watch, or radio routing. When AI is readiness-required,
  it does mark the deployment health gate degraded until the provider recovers.
