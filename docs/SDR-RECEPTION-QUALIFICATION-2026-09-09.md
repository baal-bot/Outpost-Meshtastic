# SDR reception software qualification — September 9, 2026

Issue [#150](https://github.com/baal-bot/Outpost-Meshtastic/issues/150) identified
fresh receiver audio without proof of intelligible station reception or SAME
decoding. The Environment console now separates those observations and their age.
Read [SDR reception and station qualification](SDR-RECEPTION.md) for the operator
procedure, thresholds and remaining physical evidence.

## Software behavior and local evidence

The pipeline, fresh PCM, latest audio level, dated station/antenna statement and
verified decoder header are independent indicators. Decoder evidence is accepted
only from the supervised decoder, with complete parsing, relevant context and
clock/elapsed checks. Restart, configuration change, noncurrent header timing and
expiry remain visible. Historical records persist through restart without being
reclassified as current reception. Operator statements retain their original date,
one-day expiry, process/policy scope and audit record.

The receiver panel updates when WAN weather/forecast is unavailable, and clears
current status after a receiver-fetch failure. Tests/demos show **DRILL / TEST · log
only** and cannot be approved for broadcast. Existing county filtering, live
warning review, CAP deduplication, expiry and governed delivery remain enforced.
No schema migration, RF transmission or clock-setting privilege is introduced.

Local Linux ARM64 / Python 3.13 verification passed:

- **195** receiver, SAME review/deduplication, outage-readiness and configuration
  tests. Actual child-process tests repeat both `rtl_fm` and `samedec` failure,
  recover through bounded backoff, retain one drill and one pending warning, and
  preserve the reviewed warning without creating another actionable alert.
- **33** authenticated API/browser tests, including the new reception indicators
  at 320/1280 pixels in dark/daylight/night themes, lost/weak/stale evidence,
  observation freshness and existing readiness/clock behavior.
- **Four** existing Environment workflow and web-access compatibility tests.
- Ruff formatting/lint, mypy, the unchanged strict typing debt ratchet, command and
  requirement ledgers, static markup and capability validation.

Focused coverage is **89%** for `same.py` and **87%** for `same_receiver.py`.
Screenshots were inspected at phone/desktop sizes; no horizontal overflow or
browser script errors were observed in the new cases.

The installed `samedec` decoded the pinned public NPT audio fixture from an offline
copy with the expected SHA-256 and exact header. This opened no SDR, populated no
live application records and sent nothing. The hardware observation helper now
labels its default result as a pipeline check; requiring a test decode also checks
current, relevant test evidence and emits a timestamped summary for retention.

## Exact-source CI and installed station

Source **`2d53fcff88c564a69c63600b2c1d31b7c66e7e8d`** passed all four jobs in
[CI 34381137053](https://github.com/baal-bot/Outpost-Meshtastic/actions/runs/34381137053).
Each full-suite job passed **2,480 tests**, with one skipped, across Python 3.12
and 3.13 and their locked-runtime environments. Each production-path rerun passed
**1,372 tests**. All required coverage, static and package checks passed.

The normal updater installed **`20260909T180927Z-2d53fcff88c5`** at
**18:10:33 UTC**, using exact-source CI verification, a verified pre-upgrade backup
and the native HailoRT wheel. Database schema remains **186**.

Live checks at **18:10:36–18:10:40 UTC** passed service/web, radio, native AI, core
tasks, synchronized UTC, peer-time supervision, database integrity and same-package
boot readiness. The SDR process pair was running with fresh, above-threshold
audio and **zero receiver restarts**. The new current-process decode state was
**never verified**, correctly distinct from audio activity. RF quality was not
certified. Served Environment and readiness assets matched source/package.

All tracked record IDs survived, including **32 historical SAME events**, **1,668
outbound-work records** and **2,208 power samples**. No alert records were present
after the update. Operator observation values and their audit count survived
unchanged. Offline maps remained **PASS**, absent from unresolved checks, with the
same verified **65,884,160 bytes / 6,176 tiles**. Overall readiness remains degraded
for outstanding physical qualification checks. The host did not reboot, peer-time
permissions remain disabled and clock-setting capability remains excluded.

## Existing field evidence recovered during closeout review

The owner confirmed that the antenna is installed and weather reception has been
running since initial setup. A read-only database and service-journal review at
**19:34 UTC on September 9** recovered evidence overlooked in the initial update
summary:

- The durable log contains **33 SAME events**, including relevant Required Weekly
  Tests on **September 2 and September 9 at 15:00:42 UTC** (11:00:42 a.m. EDT).
  Both are classified as tests, remain `log_only` / `logged`, and have no linked
  actionable alert. The September 9 receiver journal independently corroborates
  that day's test decode.
- A further SVA header arrived at **18:50:09 UTC**, after this release was
  installed. Its receiver-journal entry matches the durable record; it was
  withheld because its areas do not match the configured counties.
- [The August 26 hardware record](PHASE4-ACCEPTANCE.md) already documents forced
  USB-driver loss, watchdog detection, bounded retry and automatic recovery after
  rebind. The new repeated child-process regressions add duplicate-warning and
  drill-handling coverage.

The legitimate broadcast-test requirement therefore has installed-hardware
evidence alongside the recorded regression fixture. The initial **never** state
was a snapshot of the new process, not a statement that the station had never
decoded a broadcast. Historical evidence must be credited without presenting it
as a fresh measurement of the current antenna configuration.

## Closeout assessment

**#150 is ready to close using the existing evidence.** The owner confirms the
antenna is connected and the station receives alerts; the receiver journal and
durable decoded messages corroborate this. Requiring a new antenna record or
separate speech-listening session would add a gate to an already demonstrated
SAME reception path. The process/audio indicators alone were never the only
available evidence.

| Issue acceptance criterion | Closing evidence |
| --- | --- |
| Legitimate test decode plus recorded regression fixture | September 9 receiver-journal and durable RWT record, September 2 retained RWT, and the checksum-pinned public NPT recording decoded by the installed decoder. |
| Weak/absent signal and never-verified decoding do not qualify reception | Installed independent reception indicators and passing receiver/API/browser regressions for absent, weak, stale and invalidated evidence. |
| Repeated USB/process interruptions recover without duplicate actionable warnings or unintended transmissions | August 26 physical USB-driver-loss/rebind recovery plus `test_repeated_process_loss_recovers_without_duplicate_actionable_warnings`, run for both child processes. Each case recovers after three failures, retains one drill and one pending warning, creates no alert/outbound work before approval, refuses drill approval and preserves one alert after warning approval and repeat ingestion. |
| Document broadcast dependence and separate offline forecast generation | [SDR reception procedure](SDR-RECEPTION.md), including station/test scheduling, indicators and the limits of SAME. |

This credits the physical USB check and automated repeated-process checks for
what each observed; it does not claim a new physical interruption campaign.
No additional reception work is required from the owner. Publishing the closing
evidence and closing the GitHub issue remain administrative steps. No GitHub issue
state or live readiness observation was changed during this review. The separate
long-duration and WAN-outage acceptance tasks keep their own requirements.

Private evidence is retained under `.data/sdr-reception-2026-09-09/`; installed
preservation evidence is under
`/var/lib/outpost-qualification/150-20260909/sdr-reception-update/`.
Later documentation commits do not change the installed release's CI identity.
