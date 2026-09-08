# Session resume point — 2026-09-08 (America/New_York)

## Current scope

- Repository: `baal-bot/Outpost-Meshtastic`; branch `main`.
- The user approved closing #137 on its verified software evidence and requires
  battery backup for both deployment machines. Physical qualification remains #44;
  storage/energy, battery runtime and shutdown behavior remain #148. These hardware
  checks are unperformed and do not block #137's software closure.
- The second node is a separate machine and **the user will update it**. Do not
  infer remote access or deployment.
- No live database, service, radio, WAN or physical power test was changed here.
  Development and benchmarks use new disposable stores with simulated radio.
- Verified software revision: `9c3324a14486bc1933766b9c57bf8c523495b43f`.
  Read `.data/durability-2026-09-08/STATE.json` for its CI and offline-kit evidence.
  `.data/close-137-2026-09-08/STATE.json`, when present, records the later documentation
  commit and GitHub closure. Keep these revisions and their evidence distinct.

## #137 change and evidence

The application now configures WAL/FULL connections and checks the actual writer
before migrations and before serving callers. Startup rejects a weakened writer
and closes its connection. No weaker production configuration is added. Schema,
wire format and identity remain unchanged. The current DATA-002 clause and
operating contract describe this decision; the original requirement snapshot is
preserved and whole-requirement physical acceptance stays pending.

Five new production-wired test cases cover fresh migrations, an upgrade from
schema 184, reopen, restore and rejection/cleanup of weakened writer settings.
Broader transaction, mail, outbox, signed custody and recovery regression results
are under `.data/durability-2026-09-08/` and in final CI.

The maintained `tools/benchmark_commit_policy.py` creates only new temporary
stores and measures real incident/mail/outbox/signed-custody service calls. Six
512-operation trials at concurrency eight retained all 3,072 returned IDs after
ordinary reopen; every reopened writer used FULL and radio sends stayed zero.
FULL elapsed 2.763–3.030 seconds/trial versus NORMAL 1.629–1.835. The exact source
hashes, per-category timings and limits are in
`docs/benchmarks/SQLITE-COMMIT-POLICY-2026-09-08.md` and its JSON record. These are
existing-host workspace-SD measurements, not energy or physical-loss qualification.

All 229 targeted store/transaction/custody/backup/recovery/kit regressions passed;
the database module reached 93.8% line coverage in that run. Package smoke verified
all 38 runtime/radio pins. Lint, strict typing and the unchanged 262-error debt
ratchet passed locally.

The first full CI run found a stale expected lint-tool list in the deployment
tests after the benchmark was added to CI/pre-push. The follow-up updates that
expectation to include the benchmark and offline-kit tool; application code is
unchanged. At `9c3324a`, [CI run 34228844518](https://github.com/baal-bot/Outpost-Meshtastic/actions/runs/34228844518)
passed all four jobs: 2,246 tests per full-suite run and 1,201 production-wiring
tests per Python version, with coverage, package and dependency checks passing.

A new 39-package FULL-policy kit from that exact green revision is retained at
`.data/durability-2026-09-08/kit`. A separate copy installed into an inactive runtime
with the source checkout, wheelhouse and package cache hidden, and networking
unavailable to its process. Synthetic encrypted restore, named-operator login,
identity continuity and the persistent recovery fence passed. The installed writer
reported FULL before and after restart; simulated radio sends stayed zero.
Its manifest SHA-256 is
`7380bda194764c6bfcee5bcf83c33c1fe480f8319425f6ca83f5f6b80b64b1c3`.
This dated kit retains its original documents and is not rebuilt for the later
scope clarification. Its verification does not establish physical replacement,
battery runtime or power-loss acceptance.

## Next actions and second-node update

1. Finish recording the authorized #137 software closure, if the closure state
   file or GitHub issue still shows it pending. Keep #44 and #148 open with their
   hardware checks unchecked; full DATA-002 acceptance also remains pending.
2. The next repository/deployment task is #136: align the installed service with
   the verified software and complete its startup/reboot acceptance. The last
   read-only observation found this host's enabled service failed and selecting
   the August 29 release `ff11ed8dd99b` (schema capacity 153; checkout capacity 185).
   The live database schema was not queried and this observation does not establish
   the failure's cause. No deployment, service restart or reboot occurred here.
3. On their separate second machine, the user should retain a validated off-device
   backup, use a clean checkout, fetch and select verified revision
   `9c3324a14486bc1933766b9c57bf8c523495b43f`, then run
   `./deploy/update.sh 9c3324a14486bc1933766b9c57bf8c523495b43f` as the normal checkout
   owner. The updater requires GitHub CLI for CI verification and invokes sudo for
   installation. `git pull` alone does not update the installed service.
4. The user must verify installed service health, radio/peer state and a fresh
   readiness result. Those remote checks have not happened here. A later
   documentation commit does not change the verified application's deployment pin.

## Previous completed checkpoint

The previous main revision `6fb1909` passed all four CI jobs; the three September 7
commits passed 12 jobs total. #143, #146 and #151 are closed. #145 and #147 remain
open for actual fresh-appliance, permanent-client/reboot and second-operator gates.
The 39-package copied-kit install and synthetic fenced restore succeeded with
networking unavailable to their process and original source/wheelhouse/cache hidden.
See `.data/reboot-2026-09-07/STATE.json` for that dated evidence and retained kit.

#130 remains the resilience tracker. The physical/time/capacity/power/SDR/field/soak
and evaluation tasks keep their own acceptance requirements. Root coverage JSON
files from older sessions must not be cited as current evidence.
