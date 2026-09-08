# Session resume point — 2026-09-07 (America/New_York)

## Current work and live-state boundary

- Repository: `baal-bot/Outpost-Meshtastic`; branch `main`.
- #146 was committed and pushed as `a3c557e5f2809ccd1dc76d748b628b9aaf9d4d2b`.
- Its full CI is [run 34172816172](https://github.com/baal-bot/Outpost-Meshtastic/actions/runs/34172816172).
  It was running when this checkpoint was written; check its actual final state.
- This commit adds the #147 offline runtime preparation tooling and permanent
  local access/second-operator guide. Check the latest commit's full CI separately.
- Read ignored `.data/reboot-2026-09-07/STATE.json`, when present, for the latest
  CI results and issue state recorded after this committed checkpoint.
- Work here uses isolated stores, synthetic fixtures and inactive runtime
  directories. No live database, service, radio, WAN or held appliance was changed.
  The August 30 live-service restart instructions are obsolete.

## #146 implementation and local verification

The node-loss contract defines per-data survival, recovery objectives, distinct
fresh replacement identity, fenced archive review and voluntary local resident
consent/provenance/revocation/conflict/retention. BBS adoption requires a current
named operator's preview and explicit confirmation in the existing writer;
association and audit commit together. It cannot transfer keys/private records
or alias incident/alert identities. Ambiguous legacy mappings fail closed.

The recovered work passed 129 recovery/federation/readiness regressions, 21
federation visual/browser checks and the final two keyboard/consent browser cases.
Both adoption modules reached 100% line coverage in the focused run. Package
smoke installation verified all 38 runtime/radio pins. Ruff, strict mypy/ratchet,
capability/requirement/command catalogues and markup gates passed.

The previous `bf01c2a` CI failure was readiness-browser teardown: its HTTP client
closed before pending Chromium routes. The #146 commit corrects that ownership
order without removing assertions. [Run 34163313982](https://github.com/baal-bot/Outpost-Meshtastic/actions/runs/34163313982)
is historical failed evidence, not a successful run.

## #147 preparation tooling

- `deploy/offline_kit.py build` requires a clean application checkout, matching
  successful saved CI evidence and exactly the application plus all locked wheels.
  Application wheel files must match the source bytes; host and schema fingerprint
  are recorded along with a bounded hash inventory, public references and licenses.
- `verify` requires an independently recorded manifest digest. `install` creates
  only a new inactive venv, uses local hash-pinned binaries without indexes/caches,
  checks package consistency and removes its own failed staging directory.
- `docs/OFFLINE-REPLACEMENT.md` inventories boot media, power/LAN, drivers, maps,
  firmware/client apps, optional vendor/models, licensing and separate private
  recovery custody. It gives permanent numeric-LAN/mDNS access and a second-operator
  expiry/reboot/recovery record. The online installer is not claimed offline-safe.
- Local unit regression: 534 tests passed before the final input-size guard case;
  all 32 final kit-specific cases passed afterward. Ruff and strict typing of the
  standalone tool passed. Full CI on this commit remains required.
- Real preparation: the `a3c557e` application wheel matches all 315 tracked package
  files; all 38 locked dependencies are downloaded into the ignored wheelhouse.
  An actual kit build/install using saved green CI awaits that run's success.
- Test fixtures use a small synthetic application/dependency wheel pair. Do not
  describe their successful fresh-venv installation as a physical Outpost recovery.

## GitHub issue state at checkpoint

- #143 (signed physical transfer) and #151 (incident responsibility) are closed.
  Their bodies now record the four successful jobs on `d138e43` in
  [run 34162591706](https://github.com/baal-bot/Outpost-Meshtastic/actions/runs/34162591706).
  GitHub automatically checked both tasks in #130. No new issue/comment was created.
- #146 awaits its new full CI before final closure of the architecture/software task.
- #145 remains open for actual compatible offline-appliance/operator restoration;
  software verification does not satisfy that physical acceptance criterion.
- #147 remains open for actual fresh-target/permanent-client/reboot/second-operator
  evidence even after preparation tooling passes CI.
- #130 remains the resilience tracker. #135/#136/#137/#139/#141/#142/#148/#150/
  #155–#159 and #44 retain their stated operational, hardware or evaluation gates.

## Resume actions

1. Check `git status`, `git log` and the ignored `STATE.json`; preserve any later changes.
2. Inspect final CI for #146 and this #147 preparation commit. Fix failures on their
   actual evidence before closing tasks or claiming full verification.
3. Once exact source CI passes, build/verify the real prepared kit and test its
   inactive install inside a process-only network namespace. This host supports
   `unshare --user --map-root-user --net`; it does not disconnect the live host.
4. Reconcile the existing #146/#145/#147 issue bodies with actual results and limits.
   Actual service activation, physical RF, WAN disconnection and second-person
   testing need their separately scoped field exercises; do not infer completion.

Ignored artifacts are under `.data/reboot-2026-09-07/`, including test XML/coverage,
CI logs, previous issue bodies, screenshots, a detached `kit-source` worktree and
public dependency wheels. Root coverage JSON files predate this work and must not
be cited as current full-suite evidence.
