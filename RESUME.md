# Session resume point — 2026-09-07

## Repository and active work

- Repository: `baal-bot/Outpost-Meshtastic`.
- Branch: `main`; local HEAD and GitHub `main` both at
  `bf01c2a17266b08c65bd9fc8a93ee57d06a492aa` when this session resumed.
- Last pushed commit: `Add encrypted off-device recovery with fenced offline verification (#145)`.
- The reboot preserved the implementation for
  [#146](https://github.com/baal-bot/Outpost-Meshtastic/issues/146). It is now reviewed
  and locally verified, and is committed together with this handoff. Use `git log`
  and GitHub to check publication and CI state before continuing.
- This handoff replaces the August 30 queue and live-service instructions, which are stale.
  This session used isolated test stores and simulated radios. Current live service,
  database, radio and boot health have not been inspected or qualified.

## Recovered #146 implementation

- `docs/NODE-LOSS-AND-MOBILITY.md` defines per-data survival, recovery objectives,
  fresh replacement identities, restored-copy fencing, and voluntary resident
  consent/provenance/revocation/conflict/retention rules.
- `fed/adoption.py` and `web/routes/adoption.py` replace the old one-step BBS
  adoption endpoint with an explicit preview and current-session confirmation.
  Current authority, predecessor retirement, successor pairing, context expiry,
  one-to-one association and audit are checked in the existing writer transaction.
- BBS namespace aliases no longer affect distinct incident or alert identities;
  ambiguous legacy aliases and chains fail closed.
- The Federation page supports reviewed association for rejected or forgotten
  predecessors, cancellation and changed-key conflicts without automatic retries.
  Keyboard focus returns after cancellation, errors and a successful association.
  The topology audit view accepts both legacy text and structured adoption events.
- New production-wired integration/browser cases cover synthetic source loss,
  stale encrypted checkpoints, unavoidable uncopied losses, fresh guest enrolment,
  stale/revoked authority, replay counters, audit rollback and competing approvals.
- Coverage floors, wheel contents, capability evidence and operational docs are updated.
- No automatic private replication, clone activation, portable account or welfare
  transfer feature is approved or implemented by this contract.

## CI recovered from GitHub

- [Run 34162591706](https://github.com/baal-bot/Outpost-Meshtastic/actions/runs/34162591706)
  passed all four jobs on `d138e43`. This includes the #143 and #151 corrections.
  Their issue bodies still describe that run as pending; both issues remain open.
- [Run 34163313982](https://github.com/baal-bot/Outpost-Meshtastic/actions/runs/34163313982)
  on `bf01c2a` passed Python 3.13 and both locked-runtime jobs. The Python 3.12
  coverage suite had **2,172 passed, one skipped, one failed**.
- The failure was teardown in
  `test_missing_report_warns_until_an_explicit_real_readiness_run`: its HTTP client
  closed before Chromium, while a routed request still needed the client.
  `tests/browser/test_outage_readiness_ui.py` now registers the client before the
  browser resources so teardown closes Chromium first. No assertion was removed.
- The next push contains #146 and the readiness cleanup correction. Full GitHub
  CI for that revision must pass before claiming complete verification.

## Local verification

- The initially recovered #146 suite passed **35 tests** before follow-up fixes.
- Ruff, strict source mypy, capability/requirement/command catalogues and static
  markup checks passed.
- The isolated wheel smoke installation passed with all **38** runtime/radio pins.
- The final recovery/federation/readiness regression run passed **129 tests**.
  Both new adoption modules have **100% line coverage** in that focused run.
- Federation visual/browser checks passed **21 cases** with existing baselines
  and thresholds. The final keyboard regression passed **two browser cases**
  after correcting focus return for the disabled review trigger.
- Isolated dialog checks covered keyboard entry, Escape and focus return at
  320 pixels in Dark, Daylight and Night Ops, plus desktop. Screenshots use
  synthetic records only. These are not physical usability/second-operator evidence.
- Evidence is under ignored `.data/reboot-2026-09-07/`: original failed CI log,
  snapshots of the active GitHub issue bodies, JUnit/coverage results and screenshots.
  Root `coverage.json` and `production-coverage.json` predate this work; do not
  report them as current full-suite evidence.

## Continue from here

1. Inspect `git status` and this handoff, preserving all recovered changes.
2. Check publication and full CI for the #146 commit and its readiness-test
   cleanup correction. No local check was failing when this handoff was committed.
3. Run full CI on the resulting revision before claiming full verification or
   closing #146/#145. Reconcile #143/#151 against their already-passing descendant
   CI evidence when updating the existing GitHub issue bodies.
4. The next recovery preparation issue is
   [#147](https://github.com/baal-bot/Outpost-Meshtastic/issues/147): offline
   replacement kit and persistent local operator access. Its fresh-target,
   disconnected-client and second-operator acceptance needs actual field evidence.
5. [#130](https://github.com/baal-bot/Outpost-Meshtastic/issues/130) is the current
   resilience tracker. #135/#136/#137/#139/#141/#142/#148/#150/#155–#159 and #44
   retain their stated operational, hardware or evaluation gates. Software tests
   do not close physical power-loss, reboot, RTC, maps, RF, usability or soak gates.

Do not use the old August 30 handoff to restart the live service or claim the
live schema/radio is healthy. This session has not performed a deployment,
live migration, service/radio restart, WAN cut or held-node activation.
