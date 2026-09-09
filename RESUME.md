# Session resume point — 2026-09-09 (America/New_York)

## Current direction: software work and corrected power inventory

The owner asked for the next software issue and reiterated that the Pi runs from
a solar battery capable of powering it for many days. The LoRa radio has its own
battery and remains powered and receiving if the Pi goes down. Use that existing
arrangement as the baseline; the earlier shared-battery description omitted the
radio's independent battery. Do not propose installing backup power again.

The owner authorized **#156 guarded-AI evaluation and service isolation** and
explicitly directed software completion before physical tests. That software is
now installed as **`20260909T214947Z-a68baa8f0adb`**, source
`a68baa8f0adb965afbef9b493e9ef8d91d2897fd`, schema **186**.
[CI 34402429284](https://github.com/baal-bot/Outpost-Meshtastic/actions/runs/34402429284)
passed all four jobs: 2,551 passing tests and one skip per full suite, plus 1,439
production-path tests per Python version. Local focused, compatibility, weather
and deployment regressions pass.

The change adds frozen 28-case real-service evaluation in two modes, a separate
original G4 score and blinded review exports. It strengthens local-fact grounding,
permission/revision revalidation and weather context, and reserves capacity for
non-AI commands. The reproduced all-four-worker blockage is fixed; installed
generation capacity is three with four inbound workers. Single-worker
configurations use deterministic retrieval. Scripted controls pass 28/28 in each
mode and 60/60 in the separate legacy harness; they are not raw-model quality or
independent human usefulness measurements.

The normal updater completed at **21:50:54 UTC** with a verified backup. Live
checks at 21:51:38–21:51:49 UTC passed for service/web/radio/native AI, clock, tasks,
database, boot package, maps/assets and installed AI guards. All tracked IDs
survived, including 1,691 existing outbound-work records, 2,252 power samples,
33 SAME events, 8 knowledge documents, 8 chunks and 2 AI interactions. Maps remained
PASS; operator observation values/audit count were unchanged. No host reboot or
physical qualification was performed.

Details are in `docs/AI-EVALUATION-2026-09-09.md`, its public benchmark summary,
private `.data/ai-evaluation-156-2026-09-09/`, and
`/var/lib/outpost-qualification/156-20260909/ai-software-update/`.
Native-provider comparative measurements and independent blinded scoring remain
later evaluation work, without blocking software completion. #148 stays deferred;
#150 is ready to close using existing evidence. Continue software work before
scheduling the later field sessions. No new power/reception exercise is requested.
No GitHub issue was closed. Later documentation commits retain the installed
release's source and CI identity. The sections below record prior installations.

## Installed: #150 SDR reception evidence

The user authorized the next software task from the closeout plan. The selected,
running release is **`20260909T180927Z-2d53fcff88c5`**, source
`2d53fcff88c564a69c63600b2c1d31b7c66e7e8d`, schema **186**. Exact-source
[CI 34381137053](https://github.com/baal-bot/Outpost-Meshtastic/actions/runs/34381137053)
passed all four jobs: 2,480 full-suite cases per job, one skipped, and 1,372
production-path cases per Python version. Local verification passed 195 receiver,
SAME, readiness and configuration cases; 33 API/browser cases; and four existing
Environment/web compatibility cases. Static/typing/debt and repository gates pass.
The installed decoder also passed the pinned public NPT audio fixture offline.

Environment now separates the process pair, fresh PCM, current audio level, dated
station/antenna observation and verified decoder header. Audio/noise and direct
fixture ingestion cannot qualify reception. Decoder evidence uses elapsed age,
receiver context, clock confidence and pipeline/process provenance. Historical
records survive restart without becoming current reception evidence. Station
statements retain their one-day expiry and process/policy/clock checks; reads do
not renew them. Tests/demos are visibly DRILL / TEST and remain non-broadcastable.
The receiver panel remains useful when weather/forecast fails, and clears current
claims after a receiver-fetch failure. The hardware helper distinguishes pipeline
checks from required test decodes and provides a timestamped summary.

The normal updater completed at **18:10:33 UTC** with a verified backup and native
HailoRT wheel. At 18:10:36–18:10:40 UTC, live service/web/radio/native AI, time, tasks,
database and boot-package checks passed. SDR was running with fresh above-threshold
audio, zero restarts, RF quality unverified and decode state **never** for this new
process. Served Environment/readiness assets matched source/package. All tracked
IDs survived, including 32 SAME events, 1,668 outbound-work records and 2,208 power
samples; no alert records were present. Operator observation values/audit count
were unchanged. Maps remained PASS and absent from unresolved checks.

Read [the qualification record](docs/SDR-RECEPTION-QUALIFICATION-2026-09-09.md),
[the station procedure](docs/SDR-RECEPTION.md) and
[the closeout plan](docs/ISSUE-CLOSEOUT-PLAN-2026-09-09.md). The owner then confirmed
the existing antenna and reception. Read-only review at 19:34 UTC recovered two
relevant log-only weekly tests (September 2 and 9, 15:00:42 UTC), neither linked to
an actionable alert; the September 9 receiver journal corroborates its test. A
33rd SAME event arrived at 18:50:09 UTC after the update, also journal-corroborated.
The initial current-process **never** snapshot must not be treated as lifetime
reception history. August 26 already records physical USB-driver-loss recovery.
#150 is ready to close: existing connected-antenna reception and real decoded
tests satisfy reception, and the August hardware recovery plus passing repeated
process-failure/deduplication regressions cover the recovery criterion. The owner
correctly rejected requests to re-document or re-prove the working antenna path.
No additional station record, listening session or reception test is required.
The qualification record now maps all four criteria to the existing evidence;
publishing that evidence and closing GitHub remain administrative steps.
No GitHub issue was closed, no operator statement was recorded and no physical USB
cut or host reboot was performed during this update. Next software is
**#156 guarded-AI evaluation and service isolation**; #139 is another small
remaining field task. Other field/equipment prerequisites are in the plan.

Private evidence and updater checks are in `.data/sdr-reception-2026-09-09/` and
`/var/lib/outpost-qualification/150-20260909/sdr-reception-update/`.
Later documentation commits do not change the installed release's CI identity.

## Installed: startup time recovery and open-issue closeout plan

The user authorized the startup clock fix under #141 and requested a plan for
closing every open GitHub issue. The implementation allows automatic recovery
only before this process has ever trusted native or peer UTC, after two fresh
stable native synchronization probes 30–60 seconds apart. Later steps remain
latched. Existing governed recovery preserves expiry, replay checks and airtime.

The selected, running release is **`20260909T160408Z-46d2b4b04ed7`**, source
`46d2b4b04ed7a34de54e9c9ace24da55c314eeec`, database schema **186**. Exact-source
[CI 34368070215](https://github.com/baal-bot/Outpost-Meshtastic/actions/runs/34368070215)
passed all four jobs: 2,449 full-suite tests per job with one skipped, and 1,341
production-wiring tests per Python version. Production coverage passed at 95.2%
for the clock monitor and 92.0% for peer-time handling.

Local clock/queue/governor verification passed 208 cases. The cached startup-hold
report now refreshes through the existing navigation poll without renewing
observations; 73 readiness/API/browser cases passed, including eight browser cases.
Read
[the qualification record](docs/STARTUP-TIME-QUALIFICATION-2026-09-09.md) and
[the 16-issue closeout plan](docs/ISSUE-CLOSEOUT-PLAN-2026-09-09.md).

The normal updater completed at **16:05:16 UTC** with a verified backup and native
HailoRT wheel. At 16:05:20–16:05:28 UTC, live service/web/radio/native AI, synchronized
UTC, peer-time task, database integrity and same-package boot checks passed. All
tracked record IDs survived, including 1,654 outbound-work records and 2,183 power
samples. Maps/assets remain verified, offline maps is PASS and absent from the
unresolved list, and operator observation values/audit count remain unchanged.
Overall readiness still reflects the remaining physical qualification gaps.

No GitHub issue was closed, no operator attestation was recorded and no physical
reboot/time-source test was performed. Peer-time permissions remain disabled.
Private evidence is in `.data/startup-time-2026-09-09/STATE.json` and
`/var/lib/outpost-qualification/141-20260909/startup-time-update/`.
Later documentation commits do not change the installed release's CI identity.

The live GitHub inventory still has 16 open issues. #150's subsequent installation
and recovered field evidence are recorded above. The next software recommendation
is now #156; #148 is deferred in the work order and #139 maps remains a small field
task. Share #145/#147 recovery/access setup and #135/#157 multi-station work,
then run the seven-day #142 and thirty-day #158 gates on a qualified fixed candidate.
The plan details every issue, prerequisite and closing witness. Third-station,
separate restore-target and second-operator availability remain unconfirmed; no
field dates are assigned. #61 stays parked until core qualification is complete.

## Installed: readiness observation status and completed forms

The user reported that recording a passed offline-map observation left the check
unresolved and its date, checkbox and save buttons active. The fix keeps independent
map verification at **PASS**, records observation status separately, and collapses
recorded observations to a summary with **Update observation**. Current failed
observations and measured failures still require attention. A previous observation
can need review after restart without downgrading freshly verified maps. Other
unresolved checks continue to keep the overall readiness banner degraded.

The selected, running release is **`20260909T131238Z-693cbf3cc04b`**, source
`693cbf3cc04b2cf2a7276d9bae00654f084794aa`, database schema **186**. Exact-source
[CI 34349701170](https://github.com/baal-bot/Outpost-Meshtastic/actions/runs/34349701170)
passed all four jobs: 2,423 tests per full-suite run and 1,315 production-wiring tests
per Python version. Local verification passed 140 readiness/map regression cases
and 14 authenticated API/browser cases, including phone/desktop and all three themes.

The normal updater completed at **13:13:50 UTC** with a verified backup and native
HailoRT wheel. At 13:14:00–13:14:07 UTC, service/web/radio/native AI, synchronized UTC,
peer-time task, database integrity, same-package boot readiness, map tiles and assets
passed. Offline maps is **PASS** and absent from the unresolved-check list. Served
readiness code matches source/package. All operator observation values and their
audit count survived unchanged; no new operator attestation was recorded. Existing
observations remain historical after the service restart, with an explicit update
button and closed form. A browser refresh loads the new controls.

All tracked record IDs were retained, including 1,636 outbound-work records and
2,149 power samples. Regional maps and overview remain verified at 65,884,160 bytes
and 6,176 tiles. Physical WAN/time/peer gates remain open; peer-time permissions
remain disabled. The startup clock-hold finding is addressed by the installation
above; its physical qualification gates remain open.
See [the readiness guide](docs/SAFETY-READINESS.md). Private evidence is under
`.data/readiness-observation-2026-09-09/` and
`/var/lib/outpost-qualification/149-20260909/readiness-observation-update/`.
Later documentation commits do not change the installed release's CI identity.

## Post-reboot: startup clock hold recovered

The user returned after reboot. The selected release remains
**`20260909T024744Z-7113902e7793`**, schema **186**. The enabled service started
automatically, but OS time synchronization during startup moved UTC forward by
**259.806 seconds** and latched Outpost's time-confidence hold. The process began
52.02 seconds into boot, the OS time service synchronized at 72.96 seconds, and
Outpost became active at 95.92 seconds. All tracked installation records survived
the reboot; radio, native AI and supervised tasks were healthy before recovery.

After confirming synchronized OS time and preserving the original evidence, Outpost
was restarted at **12:00:08–12:00:20 UTC** on September 9. No host clock write or
runtime/configuration update was made. At 12:02:20 UTC, the same recovered process
had usable synchronized UTC, healthy radio/native AI/tasks, passing schema/integrity
and boot-package checks, and verified regional maps/assets. Three new normal sends
were recorded; no work was pending or held. Remote delivery was not measured.

Scheduled maintenance resumed and removed two expired mail records. Their metadata
in its pre-cleanup backup and the maintenance audit establish expected expiry;
all other tracked records survived. One pending work item expired with its durable
record retained. The store then held 9 incidents, 11 incident updates, 5 mail,
1,629 outbound-work records and 2,134 power samples.

This exposed a #141 startup limitation: a time correction after process start
required an operator restart even when the OS subsequently synchronized. The
September 9 startup-recovery installation above addresses that software limitation;
physical offline qualification gates remain open. Peer-time permissions remain
disabled. See the updated
[qualification record](docs/PEER-TIME-QUALIFICATION-2026-09-09.md).
Private evidence is under `.data/post-reboot-2026-09-09/` and
`/var/lib/outpost-qualification/141-20260909/post-reboot/`. Original installation,
pre-recovery and strict retention observations remain preserved.

## Installed: trusted-peer UTC checks under #141

The user said to continue and asked whether offline maps closed #130. #130 remains
an open umbrella tracker. Map software is complete and installed under #139; its
selected-region checkbox is now corrected, while physical WAN-disconnected browser
acceptance remains open. Do not close either issue for software evidence alone.

The selected, running release is **`20260909T024744Z-7113902e7793`**, source
`7113902e7793ff217644662d9c21ec1a55ccb2d0`, database schema **186**. Exact-source
[CI 34300732257](https://github.com/baal-bot/Outpost-Meshtastic/actions/runs/34300732257)
passed all four jobs: 2,410 full-suite tests per job and 1,302 production-wiring tests
per Python version. The normal updater completed at 02:48:50 UTC with the native
HailoRT wheel and a verified pre-upgrade backup.

Peer-time checks use optional current-key permissions, one-frame live challenges,
30-second elapsed deadlines, source-age/error bounds, actual UTC comparison,
no peer-derived re-export, governor-only recovery and durable restart accounting.
Federation → Time backup exposes authenticated operator controls. A fresh agreeing
reference can restore confidence in actual UTC; a large offset stays held. This
does not set the host clock. A detected wall step remains latched until Outpost is
restarted after clock correction. Healthy restarts retain probe costs; only unknown
historical airtime requires the full silent hour.

At 02:49:22–02:49:58 UTC, web, radio, native AI, database integrity, same-package boot
readiness and all protected-record retention checks passed. The new optional
`federation-time` task is healthy with no failures/restarts. Native UTC is synchronized,
the peer API requires authentication, and all peer-time permissions remain disabled.
The service still excludes `CAP_SYS_TIME` and retains `NoNewPrivileges`. The host did
not reboot. Served Federation assets match the installed package/source. USB power
passes without a radio battery percentage. Regional maps plus the overview remain
verified at 65,884,160 bytes and 6,176 tiles; the old map operator observation is stale.

Local overlapping groups passed: 113 final clock/peer/governor cases, 185 compatibility
cases, 101 queue/worker/maintenance cases, seven operator/browser cases, and 577 final
unit/operator cases. CI production coverage is 92.0% for the peer broker and 94.5%
for clock evidence, both above their 90% floors. See the
[peer-time qualification record](docs/PEER-TIME-QUALIFICATION-2026-09-09.md) and
[OFFLINE-TIME.md](docs/OFFLINE-TIME.md).

#141 remains open for physical two-station and multi-day powered qualification and
the separate RTC complete-power-loss scenario. No WAN isolation, power interruption,
host clock write, RTC charging change, second-node change or replacement operator
attestation was performed. Private evidence is in `.data/peer-time-141-2026-09-09/`
and `/var/lib/outpost-qualification/141-20260909/peer-time-update/`. Documentation
commits after the installed source do not require another runtime update.

## Prior installation: #141 native clock safeguards

The owner confirmed a solar backup battery with days of runtime and expects the
powered Pi to retain accurate time. The later correction above records the LoRa
radio's own battery, which was omitted from the earlier shared-supply description.
Prioritize continuous operation through a days-long
WAN outage. Dedicated RTC battery presence remains unspecified and concerns the
complete-power-loss fallback; it does not block this primary scenario.
The installed six-hour/30-second estimated holdover policy is conservative and
can pause timestamp-sensitive functions before the station battery is depleted.
Qualify actual drift and multi-day availability before claiming that requirement
is met. This clarification did not change the installed runtime or run a field test.
The owner also proposed pulling time from peer Outposts when needed. The authenticated
peer-time extension was planned at the prior checkpoint below and is now installed
as described above. Physical peer and multi-day powered evidence remains outstanding.

The owner authorized #141. The previous installed release was
**`20260908T235451Z-5a7de3241e4c`**, source
`5a7de3241e4cfaa7cb4829ae6fbb01fe714455ef`, database schema **186**.
Exact-source CI `34288628754` passed all four jobs: 2,371 full-suite tests per job
and 1,263 production-mode tests per Python version. The normal updater completed
at 23:55:58 UTC with the native HailoRT wheel.

The implementation adds read-only OS time evidence, bounded process holdover,
latched wall-step detection, guarded custody/retention/recovery, elapsed queue
accounting, and visible time holds. Local replies and alerts continue within their
budgets during in-process uncertainty; uncertain cold-start radio recovery needs
a usable OS source. See [OFFLINE-TIME.md](docs/OFFLINE-TIME.md) and the
[dated qualification report](docs/OFFLINE-TIME-QUALIFICATION-2026-09-08.md).

At 23:56:03 UTC, web, radio, core tasks, required native AI, schema integrity and
same-package boot readiness passed. All protected record IDs survived, including
1,609 outbound-work and 2,001 power-history records. The running service reports
usable synchronized UTC, excludes `CAP_SYS_TIME`, and retains `NoNewPrivileges`.
USB external power passes without a battery percentage. Served Federation assets
match the installed package/source. The regional pack, overview, tile and map assets
verify; an earlier operator map observation is stale after the restart and needs
operator review. No replacement attestation was recorded.

Local checks include 329 initial regressions, 64 final custody/browser cases, 181
queue/worker/maintenance/diagnostics cases, 99 clock/readiness cases and 25 installer
cases. The new clock/browser coverage includes all three themes at phone and desktop
widths; `timekeeping.py` has 94% production coverage against a new 90% floor.
The 300s visible + 300s hidden dashboard probe passed every budget. These overlapping
suites are reported separately. Package smoke, formatting, lint, types, catalogues,
requirements and markup also passed. The isolated real service-sandbox probe passed
before deployment; no clock writes were used.

RTC boot use, a zero charging voltage and a 0V battery-input reading were observed.
These do not establish separate RTC battery presence or retention. That question
belongs to the complete-power-loss fallback. #141 remains open for multi-day powered
clock accuracy/availability and the separate RTC/cold-start exercise. No clock, charging, network,
power, recovery USB or second-node change was made. The host did not reboot.
Private evidence: `.data/clock-141-2026-09-08/STATE.json` and
`/var/lib/outpost-qualification/141-20260908/clock-update/`. Later documentation
commits are separate from the installed release's CI identity.

## Installed: #139 regional maps with world overview; field acceptance remains open

The owner selected regional detail plus a small world overview and explicitly excluded
full-planet downloads. The implementation and operating guide are in
[WORLDWIDE-MAP-SETUP.md](docs/WORLDWIDE-MAP-SETUP.md): authenticated `/maps.html`, recent
local-radio GPS suggestions/manual fallback, bounded PMTiles ranges, verified immutable
SQLite vector packs, bundled local MapLibre assets, and `outpost-maps` USB import/export.
The installer now points to regional setup after the radio starts instead of seeding USGS.

At the map qualification, the release was **`20260908T215537Z-df4c206dc5f6`**, source
`df4c206dc5f6ef6ee3bf550305a850ecaf1752e4`, database schema **186**. Exact-source CI
`34278060752` passed all four jobs. The normal local updater completed successfully
with the native HailoRT wheel. Web, radio, required native AI, database integrity,
same-package boot readiness and protected-record retention passed. USB external power
still passes readiness without a battery percentage.

The initial qualification installed a verified world overview (z0–6, 5,461 tiles,
**49,823,744 bytes**) at the configured service tile path. Live tiles and map assets
matched the package; a fresh browser rendered the overview and labels with outside requests blocked.
The public manifest excludes precise setup coordinates. A separate public Nairobi
sample (2 km, z12 + overview) measured 50.8 MB and passed export/import verification.

The owner subsequently selected the actual station region. The #141 update above
preserves its verified regional pack and world overview (65,884,160 bytes, 6,176 tiles)
and supersedes the earlier map runtime. The local inventory reports
`selected_region_verified`; the dashboard's earlier operator observation became stale
at the service restart. Keep #139 open for the actual physical WAN-disconnected test
and review the operator observation after performing the relevant acceptance.
Read [the qualification report](docs/REGIONAL-MAP-QUALIFICATION-2026-09-08.md) and private
`.data/regional-maps-2026-09-08/STATE.json`. Documentation commits after the installed
source are separate from its release identity.

## Completed: #171 external radio power

The user reported that the local dashboard incorrectly warns about missing battery
readings from the radio powered by the Pi's USB supply. The fix preserves Meshtastic's
external-power indication through the adapter, governor, stored samples, readiness,
Radio page and situation briefing. Missing/invalid telemetry remains unknown,
stale/future samples retain their boundaries, and station-power qualification remains
separate. Migration 186 preserves old NULL samples as unknown.

The #171 qualification used release **`20260908T175620Z-0dd05985194a`**, source
`0dd05985194af45d4621dce24d62376ed639c9b2`, database schema **186**. Exact-commit CI
`34254372769` passed all four jobs: 2,286 full-suite tests per job and 1,214
production-mode tests per Python version. All 254 local cases passed. The normal
updater completed successfully with the native HailoRT wheel. The subsequent map and
clock releases preserve the external-power fix.

At 17:57:54 UTC, fresh readiness reported `radio_power: pass`, external power true
and no battery percentage. Web, radio, required native AI, schema integrity and
same-package boot readiness passed. All protected pre-update IDs survived, including
1,570 outbound-work and 1,929 power-history records. Old unknown power samples kept
their false flag; a new external-power record was present. Served Radio assets match
the installed package and source. At that qualification, the overall readiness banner
was degraded for offline maps and the other outstanding observations.

Read `.data/radio-external-power-2026-09-08/STATE.json` and
[the dated radio-power report](docs/RADIO-POWER-QUALIFICATION-2026-09-08.md) for
evidence. Fresh pre-update schema-185 and post-update schema-186 encrypted recovery
archives were verified on USB, with existing files preserved; the USB was safely
unmounted. Owner recovery-key custody outside the Pi remains outstanding. The second
machine remains with its owner. Later documentation commits are separate from the
installed release's CI identity.

The initial read-only #139 investigation found the configured service tile path missing and
an existing checkout pack containing all 737 declared, decodable tiles. Intended
coverage was unconfirmed at that point. Subsequent regional-map implementation and
world-overview installation are recorded above. Private evidence remains under
`/var/lib/outpost-qualification/139-20260908/`; preserve the actual WAN-down browser gate.

## Completed: #136 controlled reboot acceptance

The user returned after the authorized controlled reboot. The local Pi passed its
automatic boot witness and independent follow-up. Sanitized evidence is recorded
on GitHub, #136 is closed as completed, and its entry is checked in tracker #130.
The second node is a separate machine and the user updates it.

At the #136 qualification, the selected packaged release was
`20260908T160830Z-045130381360`, source
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
