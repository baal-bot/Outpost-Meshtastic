# Session resume point — 2026-09-08 (America/New_York)

## Current task: #136

The user authorized continuing with installed-service alignment and unattended reboot
recovery on this machine. The second node is a separate machine; the user updates it.
No local or remote service restart, deployment, radio test or reboot has occurred in
this task yet. GitHub CLI was installed as an updater prerequisite.

Read `.data/reboot-136-2026-09-08/STATE.json` for the final repair commit, CI state,
checks and deployment checkpoint recorded after this commit. Preserve the distinction
between a prepared repair and a verified live deployment.

The independent-backup prerequisite is pending: the user was asked where a validated
off-device copy exists, or which external destination should receive one. No external
filesystem was mounted at inspection. Do not treat the verified local snapshot as an
off-device copy. Continue independent preparation while awaiting that information.

## Observed failure and prepared repair

Read-only inspection found `outpost.service` enabled and failed, selecting release
`20260829T012425Z-ff11ed8dd99b`, whose migration capacity is 153. The live database is
schema 172 and passed `quick_check`. The source supports schema 185. No checkout
Outpost process was running at inspection.

The repair adds:

- Explicit `OUTPOST_RECOVER_INCOMPATIBLE_BOOT=1` support in the normal verified
  updater. The target must support current data; backups and exact-commit CI remain
  required. A failed repair preserves live data, captures a forensic snapshot where
  possible and leaves the new release selected but stopped. Incompatible automatic
  and manual rollback are refused. Compatible ordinary upgrades retain their existing
  rollback behavior, including failure handling when `systemctl start` fails.
- Store ownership checked before SQLite opens. Standard `/var/lib/outpost` state and
  installer-marked custom databases require the selected packaged interpreter and
  package. Development uses separate state; historical binaries lack this guard.
- Clearing the old systemd failure limit before startup, updated operating guidance,
  and the repair guide included in future offline kits.

See `docs/BOOT-RECOVERY.md`. No migration, wire-format or identity change is introduced
by this repair; it safely deploys the already-existing forward migrations.

## Recovery copies and local evidence

A private, integrity-checked schema-172 snapshot and configuration copies are retained
under `/var/lib/outpost/backups/issue-136-2026-09-08/`. They have not been copied off-device.
Do not publish their contents or identifiers.

A separate private copy migrated from 172 to 185 successfully with the real Database
opening path, FULL on its actual writer, clean integrity and zero foreign-key errors.
All preexisting incident, incident-update, mail, federation-mail and outbound-work IDs
were retained. The live database was unchanged. The sanitized result and private
rehearsal location are in `.data/reboot-136-2026-09-08/migration-rehearsal.json`.

Initial installer/ownership checks passed 32 cases. The broader store, transaction,
backup, boot-readiness, ownership and offline-kit run passed 129 tests. Store ownership
reached 100% line coverage. Strict mypy passed 155 source files; the debt ratchet
remains 262. Required pre-push checks passed. Consult the state file and final logs
for exact-commit CI. Final focused reruns passed nine recovery cases and eight
ownership cases. Package smoke verified all 38 runtime/radio pins and exercised
ownership, FULL commits, reopen and obsolete-release rejection in the installed
wheel's actual interpreter. These local results do not establish a real reboot.

## Next deployment steps

1. Confirm and validate the independent recovery copy, and verify green CI for the
   new #136 repair commit. The previous `9c3324a` deployment pin lacks forward-repair
   support and cannot repair this incompatible predecessor through the old updater.
2. Use the normal updater with `OUTPOST_RECOVER_INCOMPATIBLE_BOOT=1` and the existing
   native Hailo wheel at
   `/var/lib/outpost/installers/hailort-5.3.0-cp313-cp313-linux_aarch64.whl`.
   The current Hailo device was present; model and native inputs must pass the normal
   installer checks. Preserve configuration and authoritative data.
3. Verify actual packaged service health, web access, radio state, schema, retained
   data/durable work and fresh boot-schema readiness. Record exact commit and service
   process evidence privately, with sanitized issue evidence.
4. Record the pre-reboot boot ID and recovery checkpoint, then complete the authorized
   controlled reboot and compare post-boot evidence without starting a checkout server.
   Keep #136 open until the physical reboot criterion is satisfied.

## Completed durability checkpoint: #137

#137 is closed on verified software revision
`9c3324a14486bc1933766b9c57bf8c523495b43f`. CI run `34228844518` passed all four jobs:
2,246 tests per full-suite run and 1,201 production-wiring tests per Python version.
The checked WAL/FULL policy remains enabled. The owner requires battery backup for
both machines; #44 owns physical power-loss qualification and #148 owns storage/energy,
battery runtime and shutdown qualification. Those unperformed hardware checks remain open.

The scope clarification was pushed in `73d098cf249d2206b44e8deb58538b42f8eb46ce`.
See `.data/close-137-2026-09-08/STATE.json` and
`.data/durability-2026-09-08/STATE.json` for their separate evidence.
The retained 39-package kit at `.data/durability-2026-09-08/kit` remains an unchanged
`9c3324a` artifact; its manifest SHA-256 is
`7380bda194764c6bfcee5bcf83c33c1fe480f8319425f6ca83f5f6b80b64b1c3`.
Its inactive installation and synthetic fenced restore do not qualify a physical
replacement or reboot. Do not reuse old coverage files as current evidence.

#130 remains the resilience tracker. #143, #146 and #151 are closed; #145 and #147
retain their physical replacement, permanent-client/reboot and second-operator gates.
