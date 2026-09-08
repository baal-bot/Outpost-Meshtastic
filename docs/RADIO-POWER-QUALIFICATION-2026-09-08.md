# External radio power — September 8, 2026

The local Raspberry Pi now recognizes its connected radio's external-power report.
After the normal verified update, a fresh `radio_power` readiness check passed with
`external_power: true`, `battery_level: null` and condition `external`. The Radio
page displays **External power** without requiring a battery percentage.
[Telemetry and compatibility details](RADIO-POWER.md). This completes #171.

## Release and verification

| Item | Verified value |
| --- | --- |
| Installed release | `20260908T175620Z-0dd05985194a` |
| Installed source | `0dd05985194af45d4621dce24d62376ed639c9b2` |
| Database schema / packaged migration capacity | 186 / 186 |
| Deployment | Normal `deploy/update.sh`, native HailoRT wheel, successful health and metrics checks |
| Exact-commit CI | [Run 34254372769](https://github.com/baal-bot/Outpost-Meshtastic/actions/runs/34254372769), all four jobs successful |

Each full CI suite passed **2,286 tests**; each Python version's production-mode
suite passed **1,214 tests**. Local verification passed 254 cases, including adapter,
governor, migration, readiness, recovery and browser checks. Six Chromium cases cover
external/missing/low-battery states at 320px and 1280px in all three themes. Nine
affected Radio-page visual baselines were reviewed across phone, tablet and desktop
sizes and all themes; normal baseline checks passed without relaxing thresholds.

The pre-update observation at **17:56:18 UTC** recorded the old `unknown` radio-power
result at schema 185. The post-update observation at **17:57:54 UTC** recorded a
passing external-power sample, 30 seconds old. The enabled packaged service was
active with zero automatic restarts, web and health returned HTTP 200, radio and
core tasks were healthy, and required native Hailo AI was ready. SQLite integrity
was `ok`, foreign-key errors were zero, and fresh boot-schema readiness passed with
the running and boot package at the same location.

At **17:57:58 UTC**, served Radio HTML, JavaScript and CSS matched the source and
installed package byte for byte. Their `no-cache` response policy permits ordinary
page refresh to load the update. Live readiness and served assets were inspected;
authenticated rendering was exercised in the automated browser cases.

## Retention and recovery

Private primary-key comparisons found no missing incident, incident-update, mail,
federation-mail, outbound-work, relay-envelope or radio-power records. All **1,570**
pre-update outbound-work records survived; normal operation added one record. No
retention exception was needed. All **1,929** historical power records kept their
false external-power flag, and a new external-power record was present. Historical
unknown samples were not reclassified. No older snapshot was restored over live data.

A private schema-185 copy passed a migration rehearsal before deployment. Fresh
schema-185 and post-update schema-186 database/configuration archives were encrypted,
synced to the owner's USB, read back, decrypted, and verified by component hashes,
SQLite integrity and foreign-key checks. Existing archives were preserved and the
USB was safely unmounted. These are encrypted recovery archives, not disk images or
native `.opr` bundles. Recovery-key custody outside the Pi remains the owner's
separate handoff step.

## Observation scope

Hardware context: Raspberry Pi/aarch64, USB serial through an Espressif USB
JTAG/serial debug interface, US region and LONG_FAST preset. The existing diagnostics
did not expose the exact board model or firmware version. No radio configuration or
firmware changes were made. Raw identifiers, locations, configuration and recovery
material remain in private evidence under
`/var/lib/outpost-qualification/171-20260908/` and
`/var/lib/outpost/backups/issue-171-2026-09-08/`.

Overall readiness remains **degraded**: offline maps fail their check; time confidence,
restore, station power, local access, peer path and SAME reception remain unknown.
This observation measures the connected radio's reported power source. Battery
runtime, power interruption, whole-station energy, a new reboot and the separate
second machine retain their own acceptance gates.
