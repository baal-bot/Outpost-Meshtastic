# Session resume point — 2026-09-08 (America/New_York)

## Current scope

- Repository: `baal-bot/Outpost-Meshtastic`; branch `main`.
- The user authorized #137's durability work. The second node is a separate
  machine and **the user will update it**. Do not infer remote access or deployment.
- No live database, service, radio, WAN or physical power test was changed here.
  Development and benchmarks use new disposable stores with simulated radio.
- Read `.data/durability-2026-09-08/STATE.json`, when present, for final commit/CI
  evidence recorded after this committed checkpoint. Also inspect current GitHub
  #137 and the actual latest CI result before calling this work fully verified.

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
ratchet passed locally. Inspect the actual full CI outcome; this file was written
before the commit's run.

## Next actions and second-node update

1. Finish/check full CI for the #137 commit and resolve any actual failures.
2. Update the existing #137 body with exact commit/run/results. Keep #137 open for
   energy/storage qualification and #44's physical acknowledged-ID power-cut gate.
3. Give the user the exact green revision. On their second machine, they should
   first retain a validated off-device backup, use a clean checkout, fetch and
   select that revision, then run `./deploy/update.sh <exact-commit>` as the normal
   checkout owner. The updater requires GitHub CLI for CI verification and invokes
   sudo for installation. `git pull` alone does not update the installed service.
4. Have the user verify installed service health, radio/peer state and a fresh
   readiness result. Do not claim that those remote checks have already happened.
5. The previous verified offline kit uses a3c557e/NORMAL. Retain it as a previous
   generation; a FULL-policy kit must be rebuilt from the new green source and
   independently verified. New offline kits include the durability references.

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
