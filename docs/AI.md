# Local AI

Outpost's assistant is designed as a constrained, retrieval-grounded radio service, not a general
chatbot. The provider and benchmark foundation is implemented; retrieval, safety enforcement, the
`ASK` command, knowledge-base editor, and operator review console remain Phase 2 work. Keep
`modules.ai.enabled: false` until those layers and the evaluation gate are complete.

## Provider choices

| Provider | Endpoint | Data boundary |
| --- | --- | --- |
| `hailo` | Hailo-Ollama `/api/chat` | Local Hailo-10H |
| `llamacpp` | llama.cpp `/v1/chat/completions` | Local or operator-managed LAN |
| `ollama` | Ollama `/api/chat` | Local or operator-managed LAN |
| `openai_compat` | OpenAI-compatible `/v1/chat/completions` | External; explicitly disclosed |
| `null` | No endpoint | Deterministic unavailable response |

All adapters return one internal response shape with context capability, prompt/output token
counts, time to first token, total latency, throughput where reported, finish reason, and tool
calls. Provider wire formats do not leak into the agent. Configured context is used only when the
backend cannot report it; values below 1,600 tokens are rejected.

`openai_compat` is always treated as external even if an operator points it at a private endpoint.
Its credential is read from the configured environment-variable name and never belongs in YAML.

## Hailo AI HAT+ 2

On 64-bit Raspberry Pi OS Trixie, install the Hailo-10H stack—not the incompatible Hailo-8
`hailo-all` package:

```sh
sudo apt update
sudo apt install dkms hailo-h10-all
curl -fLO https://dev-public.hailo.ai/2025_12/Hailo10/hailo_gen_ai_model_zoo_5.1.1_arm64.deb
echo "17d5b476320b72ec199032e7a7b87ba72cb51311d56a9f0604213b7a9056deb9  hailo_gen_ai_model_zoo_5.1.1_arm64.deb" | sha256sum -c -
sudo apt install ./hailo_gen_ai_model_zoo_5.1.1_arm64.deb
sudo reboot
```

Verify the device after reboot:

```sh
hailortcli fw-control identify
ls -l /dev/hailo0
```

When AI is enabled with the Hailo provider, `deploy/install.sh` installs a hardened
`hailo-ollama.service` bound only to `127.0.0.1:8000`. The vendor package defaults to all network
interfaces, so do not run an unmodified long-lived server on an untrusted LAN.

List available and installed models, then pull the configured candidate if needed:

```sh
curl -fsS http://127.0.0.1:8000/hailo/v1/list
curl -fsS http://127.0.0.1:8000/api/tags
curl -fsS http://127.0.0.1:8000/api/pull \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen2.5-instruct:1.5b","stream":false}'
```

## Benchmark before enabling

The default model remains a candidate until measured on the target Outpost. Run a short provider
benchmark first:

```sh
.venv/bin/python tools/bench_inference.py \
  --provider hailo --model qwen2.5-instruct:1.5b --runs 3
```

For a release-quality run, include a genuine idle interval and the full 60-item corpus:

```sh
.venv/bin/python tools/bench_inference.py \
  --provider hailo --model qwen2.5-instruct:1.5b \
  --runs 5 --cold-idle-seconds 300 --eval
```

The tool writes Markdown and JSON under `.data/benchmarks/` by default. It records the actual Pi,
provider inventory, discovered/configured context, p50/p95 latency, time to first token, throughput,
cold-start result, errors, and per-class evaluation results. The release gate is at least 70%
overall with zero refusal or prompt-injection failures. A preflight against an unavailable backend
is useful diagnostic evidence, but it cannot justify a default-provider decision.

## Operational boundaries

- AI is off globally and on channel 0 by default.
- AI never receives mail, exact member positions, or operator notes.
- It has read-only tools and cannot create, broadcast, escalate, or cancel alerts.
- Grounded answers use `[AI]`; narrowly allowed ungrounded answers use `[AI?]`.
- Provider failure must degrade only the assistant, never BBS, mail, Watch, or radio routing.
