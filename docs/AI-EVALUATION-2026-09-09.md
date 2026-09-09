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

Additional local checks passed 27 provider/store/budget/situation compatibility
cases, two weather-context regressions and 25 deployment checks. These groups
overlap existing suites and are not summed as unique tests.

The final scripted run passed **28/28 guarded-service cases**, **28/28 deterministic
cases** with zero model calls, and the separate **60/60 original G4 guard-harness
cases**, with zero safety-category failures. In controlled disabled/busy/failed
provider scenarios on this Pi, PING, POST and SEND completed in 4.6–24.3 ms through
the local router/store path. This does not measure RF delivery or native inference.
The [public software summary](benchmarks/AI-SOFTWARE-HOLDOUT-2026-09-09.json)
contains the identified results and measurement limits.

## Verified installation

Source **`a68baa8f0adb965afbef9b493e9ef8d91d2897fd`** passed all four jobs in
[CI 34402429284](https://github.com/baal-bot/Outpost-Meshtastic/actions/runs/34402429284):
**2,551 tests passed and one skipped** in each full-suite run, plus **1,439
production-path tests** per Python version. Formatting, strict typing, coverage,
dependency audits and package checks passed.

The normal updater installed **`20260909T214947Z-a68baa8f0adb`**, completing at
**21:50:54 UTC** with a verified pre-upgrade backup. Schema remains **186**.
Live checks at **21:51:38–21:51:49 UTC** passed for the selected package, service,
web, radio, native Hailo provider, clock, supervised tasks, database integrity,
regional maps and served assets. Installed AI source matched the reviewed source;
a fabricated claim with a real citation was rejected, a supported excerpt was
accepted, and generation capacity was three with four inbound workers.

All tracked record IDs survived, including 9 incidents, 11 incident updates,
5 mail records, 1,691 existing outbound-work records, 2,252 power samples,
33 SAME events, 8 knowledge documents, 8 knowledge chunks and 2 AI interactions.
One normal outbound-work record was added. Operator observation values and their
audit count were unchanged. Offline maps remained PASS and absent from unresolved
checks; the selected pack retained 65,884,160 bytes and 6,176 tiles. The SDR process
pair was running with fresh audio and zero restarts. Its fresh process has no new
decode yet; the retained reception evidence under #150 remains valid.

The software work is installed. Independent blinded usefulness scores and a new
native-provider performance/energy comparison remain separate evaluation results;
they do not block this software update or the remaining software work. No host
reboot, physical interruption or new operator attestation was performed.

Private reports and test evidence are under `.data/ai-evaluation-156-2026-09-09/`
and `/var/lib/outpost-qualification/156-20260909/ai-software-update/`.
Later documentation commits do not change the installed source or its CI identity.
