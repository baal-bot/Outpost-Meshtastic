# #156 software evaluation — September 9, 2026

The owner requested software completion before physical qualification. This work
adds a frozen evaluation through the real service and fixes the failures it can
expose, using synthetic records and provider faults. It does not require a new
battery setup, a physical outage or a thermal/energy campaign.

## Changes and reproducible evidence

- `tools/eval_ai.py` evaluates the actual database/retrieval/guard/logging path and
  an explicit deterministic baseline. The immutable 28-case corpus and manifest
  live in `tests/eval/holdout-v1.*`. The original 60-case corpus is unchanged and
  its legacy guard-harness score is reported separately.
- A real citation previously allowed a contradictory or invented claim. Grounded
  output now preserves a cited record or its bounded extractive fallback. Full
  records retain source qualifiers and attribution; this conservative rule limits
  generated paraphrases of local facts.
- Queued requests refresh evidence. Revoked board/member permissions, hidden or
  edited posts and changed source revisions cannot authorize a later stale answer.
  Normal progression of the displayed record age does not invalidate an unchanged
  source. Imported board/incident evidence retains origin and unverified-report
  context. Weather older than the configured cache limit or fetched in the future
  is excluded before reaching the provider. Observation/forecast kind and provider
  validity time remain in the evidence, so a recent fetch cannot make an older
  observation appear current. Two additional regressions cover those labels.
- A reproduced busy-provider regression occupied all four inbound workers and
  prevented a normal PING from completing. Generation admission now reserves one
  worker for non-AI requests. With one configured worker the service uses
  deterministic retrieval. Tests exercise the real router and durable board,
  mail and incident stores while AI is disabled, blocked or failed, including an
  urgent report from the sender whose AI request is still blocked.

The software command in [AI.md](AI.md) creates an identified JSON report, blinded
answer sheet and separate mode key. It includes latency, CPU time, process RSS,
reply bytes and the existing software airtime estimate. Scripted-provider timing
does not measure native inference speed; RSS snapshots are not isolated model peak
memory. The adversarial provider is deliberately identified as a software control.

## Acceptance boundary

All defined holdout expectations must pass for the automated software gate.
Private-provider-input canaries and user-visible replies are checked independently.
Independent usefulness grading, native-provider performance/energy comparison and
physical qualification retain their own result fields and are not prerequisites
for implementing or testing the software. No live records, radio sends, operator
attestations or hardware tests are fabricated. GitHub's full issue can retain those
later comparison results while the software change is completed and shipped.

Local Python 3.13 verification passed **116 tests**, covering the frozen holdout,
queued/inflight permission and revision changes, concurrent real-service intake,
single-worker fallback, grader/blinding controls and the existing AI and inbound
regressions. Formatting/lint and strict typing are checked before submission to CI.

Local reports and test evidence are under `.data/ai-evaluation-156-2026-09-09/`.
Exact-source CI and installation results will be recorded after verification.
