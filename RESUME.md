# Session resume point — 2026-09-08 (America/New_York)

## Completed: #136 controlled reboot acceptance

The user returned after the authorized controlled reboot. The local Pi passed its
automatic boot witness and independent follow-up. Sanitized evidence is recorded
on GitHub, #136 is closed as completed, and its entry is checked in tracker #130.
The second node is a separate machine and the user updates it.

The selected, running packaged release is `20260908T160830Z-045130381360`, source
`045130381360e23aa89e093b8afb0c87613cef6d`, database schema **185**. Exact-commit CI
`34242955400` completed successfully in all four jobs: 2,261 full-suite tests per job,
and 1,209 production-mode tests per Python version. Normal `deploy/update.sh`
completed successfully with the native HailoRT wheel. The working checkout's later
documentation commit is not the installed release's CI identity.

Read `.data/reboot-136-2026-09-08/STATE.json` for the operational checkpoint and
[the dated qualification report](docs/BOOT-RECOVERY-QUALIFICATION-2026-09-08.md)
for public release/schema, timing, retention and scope evidence.

## Verified after the reboot

The automatic systemd witness passed at 16:17:29 UTC, 99.62 seconds after boot.
It observed a changed boot ID, the intended enabled packaged service, green release
evidence, HTTP 200 health/web, radio up, healthy core tasks and required native Hailo
AI, schema-185 integrity with zero foreign-key errors, and a fresh passing boot-schema
check. Its boot journal confirms unattended execution; it did not start Outpost.

Independent follow-up at 16:19:26 UTC passed with the same boot, release and original
service process, with zero restarts. Primary-key comparisons found no missing
pre-repair or pre-reboot records. All 1,558 pre-reboot outbound-work records survived,
including five pending items that remained pending; follow-up had one additional
pending item. Remote delivery and acknowledgement were not measured.

Read these retained local files with sudo; keep raw IDs and configuration private:

- `/var/lib/outpost-qualification/136-20260908/after-reboot-summary.json`
- `/var/lib/outpost-qualification/136-20260908/before-reboot-summary.json`
- `/var/lib/outpost-qualification/136-20260908/resume-verification-summary.json`
- `/var/lib/outpost-qualification/136-20260908/resume-retention-comparison.json`
- `/var/lib/outpost-qualification/136-20260908/cleanup-summary.json`

The temporary `outpost-qualify-reboot-136.service` was disabled and removed after
review. Its armed file is absent, observations/backups are retained, and the original
enabled Outpost service continued running. Preserve the automatic witness rather
than overwriting it with later observations.

## Verified before reboot

The enabled service runs its selected packaged interpreter, web and health return 200,
radio and core tasks are healthy, and required native Hailo AI is ready. Database
integrity and foreign-key checks pass at schema 185. A fresh readiness check reports
`boot_schema: pass` with the running and boot package at the same location. The checkout
was refused access to the installed database before opening a writer.

All original incident, incident-update, mail, federation-mail, outbound-work and relay
envelope IDs were retained. The initial outbox had 1,534 terminal records and no active
pending work; normal operation added work, including the five pending items in the
later pre-reboot baseline. Abrupt power-loss durability remains #44. Other field
readiness remains degraded: offline maps failed their check and several
physical/local-access/peer/restore observations
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

#130 remains the resilience tracker. #136, #143, #146 and #151 are closed; #145 and #147
retain their physical replacement, permanent-client/reboot and second-operator gates.
