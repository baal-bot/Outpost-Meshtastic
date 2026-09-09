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

On Linux ARM64 / Python 3.13, **207 clock, peer, queue and governor cases passed**.
The new startup cases cover the observed 259.806-second correction, both six-hour
directions, delayed/absent/faulty sources, bounded fresh confirmation, repeat
jumps, elapsed regression, initial system-clock wiring, queue preservation and
expiry, prior airtime, durable probe accounting and unknown-history silence,
signed expiry/replay, and closing the startup exception after peer trust.
Focused production-path coverage for `timekeeping.py` is **94.8%**.

**Eight authenticated API/Chromium cases passed**, including the existing time
hold across three themes at phone/desktop widths and new startup recovery shown
by the real navigation refresh scheduler at 320 and 1280 pixels. The status changes
to recovered UTC while hardware time qualification remains unknown; no operator
observation is created. Formatting, lint, typing/debt, capability, requirement,
command and static-markup checks passed.

A broader compatibility group passed **695 unit, readiness, maintenance,
incident-worker and durable-outbox cases**. This overlaps the focused governor
group; the counts are reported separately rather than added together.

Source and evidence:
[clock monitor](../src/outpost/timekeeping.py),
[startup regressions](../tests/integration/test_startup_time.py),
[operator browser checks](../tests/browser/test_time_confidence_ui.py), and
[runtime policy](OFFLINE-TIME.md).

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
Exact-source CI and installed-release evidence will be recorded after they pass;
this local verification record alone does not claim deployment.
