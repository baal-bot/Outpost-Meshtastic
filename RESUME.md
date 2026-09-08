# Session resume point — 2026-09-08 (America/New_York)

## Current task: #136 controlled reboot acceptance

The local repair is deployed and passed pre-reboot checks. The user authorized the
controlled reboot; do not ask for approval again. The second node is a separate
machine and the user updates it.

The selected, running packaged release is `20260908T160830Z-045130381360`, source
`045130381360e23aa89e093b8afb0c87613cef6d`, database schema **185**. Exact-commit CI
`34242955400` completed successfully in all four jobs: 2,261 full-suite tests per job,
and 1,209 production-mode tests per Python version. Normal `deploy/update.sh`
completed successfully with the native HailoRT wheel. The working checkout's later
documentation commit is not the installed release's CI identity.

Read `.data/reboot-136-2026-09-08/STATE.json` for the latest operational checkpoint.
After reboot, the persistent witness below takes precedence over its pre-boot state.
Keep #136 open until the actual post-boot evidence passes and is recorded on GitHub.

## Resume after the reboot

A temporary, enabled systemd unit `outpost-qualify-reboot-136.service` runs after the
enabled Outpost boot service, only when its private `armed` file exists. It waits up
to five minutes for recovery, then records observations without starting Outpost or
a checkout server. It must see a new boot ID and the intended packaged process,
green release evidence, live health/web/radio/core tasks/required AI, schema integrity,
a fresh boot-schema check and retained authoritative records/durable work.

Read these local files with sudo; never publish raw record IDs or configuration:

- `/var/lib/outpost-qualification/136-20260908/after-reboot-summary.json`
- `/var/lib/outpost-qualification/136-20260908/before-reboot-summary.json`
- `/var/lib/outpost-qualification/136-20260908/collector-error.json`, if present.

If collection is still running, inspect the qualification unit and let its bounded
observation finish. A missing or failed result is not successful reboot evidence.
After reviewing a passing result and actual service state, record sanitized evidence,
close #136, and check it off in tracker #130. Disable and remove only this temporary
qualification unit; retain the private observations and backups. Update this resume
point and the private state file with the actual outcome.

## Verified before reboot

The enabled service runs its selected packaged interpreter, web and health return 200,
radio and core tasks are healthy, and required native Hailo AI is ready. Database
integrity and foreign-key checks pass at schema 185. A fresh readiness check reports
`boot_schema: pass` with the running and boot package at the same location. The checkout
was refused access to the installed database before opening a writer.

All original incident, incident-update, mail, federation-mail, outbound-work and relay
envelope IDs were retained. The initial outbox had 1,534 terminal records and no active
pending work; normal operation added sent work. This does not establish a nonempty
pending-queue physical outage campaign. Other field readiness remains degraded: offline
maps failed their check and several physical/local-access/peer/restore observations
remain unknown. Those do not become qualified by the #136 boot-schema repair.

Two local deployment issues were resolved before the successful update. A logging
wrapper's restrictive umask initially prevented the service account from executing the
staged package; no migration ran on that attempt. Package read/execute permissions and
the updater child umask were corrected. The next attempt started a healthy service but
the installer's unauthenticated metrics probe received 401 under the inherited default.
Its code-only rollback retained the migrated data and the same verified source revision.
The local config now explicitly uses the documented `web.metrics_access: loopback`:
local metrics return 200 and non-loopback requests return 403. The final normal updater
passed both health and metrics checks. Private logs preserve all attempts.

## USB recovery copies

The owner supplied USB storage. Both encrypted archives were synced, read back from the
USB, decrypted and checked against their source archive and per-file SHA-256 hashes.
Each database passed integrity and foreign-key checks. Existing USB files were preserved;
the filesystem was safely unmounted after the final verification.

- Original schema 172: `Outpost-Recovery/20260908T152005Z/pre-upgrade-schema-172.tar.gpg`.
  SHA-256 `dfd57b936bc6d6505235059b260a05150ad0ef497e6e72e5c20fde9baf7f881f`.
- Post-repair schema 185, current configuration and CI evidence:
  `Outpost-Recovery/20260908T161006Z/post-repair-schema-185.tar.gpg`.
  SHA-256 `367eb2d4c095ad11d17b05f01d19ebf1321ddfa398e26e3266fd912d2424e12f`.

These are encrypted database/configuration recovery archives, not native `.opr` bundles
or disk images. They share a strong recovery key retained separately from the USB. The
owner handoff file is identified in the private state file; the owner must retain the
key outside the Pi for loss-of-machine recovery. That custody step and whole-appliance
replacement qualification have not been claimed complete.

Private local snapshots are retained under
`/var/lib/outpost/backups/issue-136-2026-09-08/`. Preserve newer acknowledged live data;
never restore an older snapshot over it. See `docs/BOOT-RECOVERY.md` for the explicit
forward-repair workflow and development-store ownership rule.

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
