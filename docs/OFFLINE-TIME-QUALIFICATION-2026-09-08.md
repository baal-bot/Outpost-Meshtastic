# Offline time software qualification — September 8, 2026

Issue #141 now has explicit clock-confidence handling and a physical acceptance
procedure. The software distinguishes usable OS time evidence from RTC retention.
RTC battery retention and the intended offline cold-start source remain unqualified.
See [Offline time confidence](OFFLINE-TIME.md).

## Release verification

The selected, running release is `20260908T235451Z-5a7de3241e4c`, source
`5a7de3241e4cfaa7cb4829ae6fbb01fe714455ef`.
[Exact-source CI](https://github.com/baal-bot/Outpost-Meshtastic/actions/runs/34288628754)
passed all four Python 3.12/3.13 jobs: 2,371 full-suite tests per job and 1,263
production-mode tests per version, with coverage, packaging and configured dependency
audits passing. The normal local updater completed at 23:55:58 UTC with the native
HailoRT wheel. Database schema remains 186; no migration or wire-format change was
introduced.

At 23:56:03 UTC the enabled packaged service, web, radio, core tasks, required native
AI and same-package boot readiness passed. Database integrity and foreign keys were
valid. Every protected pre-update record ID remained present, including all 1,609
outbound-work and 2,001 power-history records. The host did not reboot.

The running service reported usable synchronized UTC with an estimated error of
about 0.4 seconds. Its capability bounding set excludes clock setting and
`NoNewPrivileges` remains enabled. RTC retention correctly remains unverified.
USB external radio power passes readiness without a battery percentage.

The served Federation page and script match the selected package and source. The
selected regional pack and world overview, their served tile and map assets also
verified successfully. The pack is preserved at 65,884,160 bytes with 6,176 tiles.
An earlier operator map observation is stale after the process/policy change; this
is separate from the passing local inventory and requires operator review. No
physical WAN acceptance or replacement operator attestation was performed.

## Fault coverage

The new clock suite covers unavailable cold-start evidence, bounded process holdover,
positive and negative six-hour wall jumps, clock hardware faults, normal slew,
retained queues, dispatch waits, signed custody and automatic incident delivery.
A step during custody dispatch rolls back destination work and its delivery event.
Local replies and alerts retain elapsed deadlines and airtime limits after an
in-process wall step. Recovery under unknown cold-start time holds retained work.

Existing production regressions retain signature/tamper/expiry checks and exercise
revision reconciliation and incident updates under all combinations of positive and
negative six-hour offsets and steps. These are synthetic fault tests, not physical
radio or battery-retention measurements.

Local results included 329 initial regressions, 64 final custody/browser cases,
181 final queue/worker/maintenance/diagnostics cases, 99 clock/readiness follow-up
cases and 25 installer cases. These overlapping suites are reported separately.
The dedicated browser cases cover three themes at phone and desktop widths.
Phone dark and desktop daylight warnings were also visually inspected.

The new production `timekeeping.py` achieved 94% coverage against its new 90% floor.
Formatting, lint, types, strict-debt ratchet, capability/command/requirements
catalogues, markup and package smoke passed without relaxing existing gates.

## Service and dashboard checks

An isolated real systemd service on the target Pi read kernel synchronization
successfully under the packaged syscall allowlist. Its capability bounding set
excluded clock setting, and `NoNewPrivileges` remained enabled. No clock adjustment
was attempted. The package and matching service-unit changes are installed together.

The five-minute visible and five-minute hidden dashboard probe passed every
[performance budget](PERFORMANCE.md):

| Measurement | Visible | Hidden |
| --- | ---: | ---: |
| API requests | 118 | 0 |
| Outpost CPU, share of one core | 0.40% | 0.17% |
| RSS growth | 2.75 MiB | 0.00 MiB |
| New database read connections | 0 | 0 |
| External provider requests | 0 | 0 |

The browser reported no page errors. This probe used an isolated temporary database
and did not interrupt the production radio.

## Owner's power configuration and next acceptance

After deployment, the owner clarified that the Pi, SDR and LoRa radio share one
external backup supply with days of runtime and solar charging backup. The owner
expects the Pi's clock to stay accurate while powered. This establishes the intended
operating scenario: prioritize multi-day clock accuracy and continued service with
WAN time sources unavailable and station power maintained.

The installed six-hour holdover and 30-second estimated-error bounds are conservative
software policy. They do not measure this Pi's actual drift and can pause functions
before the station battery runs out. Multi-day availability is still an open #141
acceptance item. The updated [procedure](OFFLINE-TIME.md#continuous-operation-on-station-backup-power)
records the powered exercise; the runtime and its existing CI identity are unchanged.
Measured whole-station autonomy and charging behavior remain under #148.
The owner subsequently proposed a federated time fallback. Its authentication,
freshness, delay, provenance and recovery requirements are recorded in the operating
guide as a planned extension. No peer-time synchronization was installed by this
qualification, and the existing six-hour holdover bound remains in effect.

## Remaining complete-power-loss acceptance

Read-only inspection found an exposed RTC used at boot and a synchronized OS clock.
The RTC battery input measured 0V and charging voltage was zero. Neither a device
entry nor a voltage snapshot establishes battery presence or retention. Owner
confirmation of the separate RTC backup battery remains unspecified. It concerns
the complete-power-loss fallback rather than the primary continuously powered test.

No physical power interruption, WAN/NTP isolation, clock change, RTC charging
change, recovery-USB operation or second-node operation was performed. A controlled
shutdown, removal of all main power, and cold-start witness before resynchronization
are still required. A working battery alone does not satisfy the runtime source
gate at an offline cold start; the intended verified OS time source also needs
qualification. #141 remains open for that evidence. #44 and #148 keep their separate
abrupt-power-loss and whole-station battery tests.

Private evidence remains under `.data/clock-141-2026-09-08/` and
`/var/lib/outpost-qualification/141-20260908/clock-update/`. Public evidence omits
credentials, exact station locations, raw boot identifiers and record IDs.
