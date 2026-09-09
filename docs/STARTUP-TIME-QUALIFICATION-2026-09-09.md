# Startup clock recovery — September 9, 2026

The [post-reboot observation](PEER-TIME-QUALIFICATION-2026-09-09.md) found a
259.806-second OS time correction after Outpost started. The old monitor latched
the clock hold even though the process had not yet established trusted UTC. A
manual service restart restored timestamp-sensitive operation.

## Changed behavior

The system clock now samples its initial source before slow application
construction. A wall correction before any native or accepted peer confidence
keeps work held while two fresh good OS probes, 30–60 elapsed seconds apart,
confirm a stable plausible clock. Cached results cannot complete confirmation.
Another jump, a failed probe, an invalid date or an excessive probe gap resets
confirmation. With no usable source, local access remains available and the hold
continues without an unbounded network wait in service startup.

Once native or peer confidence can authorize work, the startup exception closes
for that process. A later wall step or monotonic regression remains latched for
controlled recovery. Outpost still only queries the kernel; no clock-setting
privilege, host clock change, new network source, peer permission or radio bypass
is introduced. The Linux read-only query contract is described in
[adjtimex(2)](https://man7.org/linux/man-pages/man2/adjtimex.2.html).

The existing governor then recovers the durable ordinary queue against corrected
UTC, rechecks expiry and reconstructs prior airtime. Durable probe costs and the
silent hour for unknown historical airtime remain enforced. Signed lifetime,
replay/idempotency and custody checks are unchanged. There is no schema migration
or protocol change. The diagnostic recovery marker and operator text explain
startup recovery without certifying RTC retention.

## Local evidence

On Linux ARM64 / Python 3.13, **208 clock, peer, queue and governor cases passed**.
The new startup cases cover the observed 259.806-second correction, both six-hour
directions, delayed/absent/faulty sources, bounded fresh confirmation, repeat
jumps, elapsed regression, initial system-clock wiring, queue preservation and
expiry, prior airtime, durable probe accounting and unknown-history silence,
signed expiry/replay, closing the startup exception after peer trust, and refreshing
cached readiness without renewing observations.
Focused production-path coverage for `timekeeping.py` is **94.8%**.

**Eight authenticated API/Chromium cases passed**, including the existing time
hold across three themes at phone/desktop widths and new startup recovery shown
by the real navigation refresh scheduler at 320 and 1280 pixels. No manual readiness
run or backend refresh is injected after the clock recovers. The status changes
to recovered UTC while hardware time qualification remains unknown; no operator
observation is created. Formatting, lint, typing/debt, capability, requirement,
command and static-markup checks passed.

A broader compatibility group passed **695 unit, readiness, maintenance,
incident-worker and durable-outbox cases** before the final cached-report refresh
follow-up. The final follow-up passed **73 readiness/API/browser cases**, including
the eight browser cases above. These groups overlap; their counts are reported
separately rather than added together.

Source and evidence:
[clock monitor](../src/outpost/timekeeping.py),
[startup regressions](../tests/integration/test_startup_time.py),
[operator browser checks](../tests/browser/test_time_confidence_ui.py), and
[runtime policy](OFFLINE-TIME.md).

## Exact-source CI and installed station

Source **`46d2b4b04ed7a34de54e9c9ace24da55c314eeec`** passed all four jobs in
[CI 34368070215](https://github.com/baal-bot/Outpost-Meshtastic/actions/runs/34368070215).
Each full-suite job passed **2,449 tests**, with one skipped, on Python 3.12 and
3.13, including the locked-runtime jobs. Each production-wiring rerun passed
**1,341 tests**. Production coverage was **95.2%** for `timekeeping.py` and **92.0%**
for peer-time handling, both above their 90% floors. All required static and
coverage gates passed.

The normal updater installed **`20260909T160408Z-46d2b4b04ed7`** at
**16:05:16 UTC**, using exact-source CI verification, a verified pre-upgrade
backup and the native HailoRT wheel. Database schema remains **186**.

Live checks at **16:05:20–16:05:28 UTC** passed service/web, radio, required native
AI, core tasks, usable synchronized UTC, peer-time supervision, database integrity
and same-package boot readiness. The new startup-recovery status is available.
The host did not reboot. All tracked record IDs survived, including **1,654
outbound-work records** and **2,183 power samples**; none required a retention
exception. Clock-setting capability remains excluded and peer-time permissions
remain disabled.

Regional maps and overview remain verified at **65,884,160 bytes / 6,176 tiles**.
Offline maps is **PASS** and absent from unresolved checks. Operator observation
values and their audit count survived unchanged; the prior observation is
classified separately as historical after the service restart. Served readiness
code matches the source and installed package. Overall readiness remains degraded
for the outstanding qualification checks; RTC retention is not certified.

## Remaining qualification

These are synthetic clock/source tests through production software. They do not
constitute another host reboot, physical delayed-NTP startup witness, WAN outage,
two-station peer-time test, multi-day powered holdover or RTC retention test.
Those #141 gates remain open. The complete
[issue closeout plan](ISSUE-CLOSEOUT-PLAN-2026-09-09.md) lists preparation and closing
evidence for all 16 currently open GitHub issues. It changes no GitHub issue state.

Private regression and installation evidence is retained under
`.data/startup-time-2026-09-09/` and
`/var/lib/outpost-qualification/141-20260909/startup-time-update/`.
Later documentation commits do not change the installed release's CI identity.
