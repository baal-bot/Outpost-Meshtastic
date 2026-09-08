# Offline time confidence (#141)

Outpost distinguishes usable UTC evidence from **qualified RTC retention**. A valid
timezone, an RTC device, its charging voltage, and a recent internet sync do not prove
that a station retains time when all main power is removed. The physical retention
gate remains open until it has dated measurements on the intended hardware.

## Runtime policy

The packaged `SystemClock` reads Linux's `adjtimex` status with `modes=0`. This is an
unprivileged query; it cannot adjust time. Outpost never sets the OS clock, enables
battery charging, connects to a time server, or takes time from a remote radio.
The administrator remains responsible for choosing the OS time source.

The packaged systemd unit admits `adjtimex` and `clock_adjtime` queries while
explicitly excluding `CAP_SYS_TIME` from its capability bounding set and retaining
`NoNewPrivileges=yes`. Deploy the unit together with the package; an older syscall
allowlist blocks the query. Custom units need the same read access and must not
grant clock-setting capability.

| Evidence | Behavior |
| --- | --- |
| OS reports synchronization, no clock fault, maximum error at most 30 seconds | Timestamp-sensitive operations may proceed. RTC retention remains unqualified. |
| Source disappears during the same process | Estimated holdover lasts at most six hours from the last good observation, allowing 500 ppm drift and at most 30 seconds estimated error. This is a conservative software bound, not measured oscillator accuracy. |
| Source missing at cold boot, implausible date, unsupported probe, clock fault, or exhausted holdover | Signed custody, federation egress, scheduled traffic and retention cleanup wait. Readiness and Federation explain the time-confidence reason. |
| Wall time jumps relative to elapsed time by over five seconds, allowing normal slew | Timestamp confidence latches as uncertain. Correct and verify the OS time source, then restart Outpost. Simply moving the clock back does not clear the latch. |

Clock-source probes are cached for 30 seconds; discontinuity checks run at operation
boundaries. The Linux probe currently supports the 64-bit Linux ABI used by the
qualified ARM64 appliance and x86-64 CI runners. An unavailable probe yields unknown
confidence. Operator readiness observations never unlock time-sensitive operations.

Local replies and alerts continue during **in-process** uncertainty if the governor
started with usable time and has recovered its airtime history. Their deadlines,
retry delays and airtime accounting use an epoch projected from monotonic elapsed
time. Clock changes cannot replenish airtime or renew their lifetimes. Scheduled
traffic waits rather than guessing whether quiet hours apply.

At an uncertain cold start, retained queue deadlines and prior airtime cannot safely
be reconstructed. Radio egress remains held until the source becomes usable; work
is preserved without claiming a send. New durable admissions return `time_uncertain`.
The dashboard and local authoritative records remain available. UTC labels created
while the source is uncertain remain historical claims; they are not silently
rewritten after correction. This release does not add per-record clock provenance.

## Signed transfers, ordering and expiry

Signed custody creation, acceptance, forwarding, destination dispatch and automatic
expiry require usable time. An uncertain receiver does not report successful custody
or delivery. Queued payloads remain available for review and retry. API and dashboard
responses provide the time-confidence reason. A sender whose remote peer cannot
respond still has bounded retry/expiry policy; local queue admission is not delivery.

The existing signed fields, origin key checks, five-minute future-time allowance,
maximum seven-day signed lifetime, expiry rejection and replay/idempotency protections
remain in effect. Six-hour peer offsets do not widen that allowance. Operators must
verify UTC on both stations and retry an unexpired transfer; expired signed data needs
a new explicit operation. Fixing time does not resurrect an expired or rejected item.

Modern incident synchronization still orders changes by producer lineage and durable
revision, including incident notes and human-reviewed updates. Clock changes do not
make an older revision authoritative or imply acceptance by a responder. Automatic
incident delivery keeps its intent and reports `time_uncertain` while blocked.
Legacy timestamp synchronization remains clock-sensitive; upgrade both peers.

Retention cleanup waits when the clock is uncertain, including checks inside deletion
transactions. Disk headroom remains a separate readiness check. Authentication,
certificate validity, event labels and remote source timestamps retain their existing
time dependencies; this change is not a claim that every feature is clock-independent.

No wire-format change, schema migration, new radio message, GPS write, or additional
radio airtime is introduced. Existing peers retain their validity checks. The OS
synchronization flag is local administrative evidence, not cryptographic proof of UTC.

## Inspect a station

The existing diagnostics now include the OS clock-source state and RTC hints.
For a focused, read-only witness without loading station configuration:

```sh
/opt/outpost/current/bin/python -m outpost.timekeeping
timedatectl show -p NTPSynchronized -p NTP -p LocalRTC -p TimeUSec -p RTCTimeUSec
```

The first command includes a hashed boot identifier, elapsed boot time, UTC, RTC time
and source status. It contains no credentials, station coordinates or member data.
`physical_retention_tested: false` is intentional: a snapshot cannot perform the
physical test. Compare the witnesses with an independent trusted clock.

Pi 5 has a built-in RTC and a dedicated backup-battery connector. It can expose a
working RTC without an attached battery. Charging is disabled by default. Confirm
the installed battery type before following the manufacturer's charging instructions;
Outpost does not modify them. [Raspberry Pi RTC documentation](https://www.raspberrypi.com/documentation/computers/raspberry-pi.html#real-time-clock-rtc).

An independently powered local NTP server disciplined by GNSS/PPS is an optional
source for a community LAN. Configure and verify it at the OS level, then repeat the
WAN-disconnected acceptance on the intended station. A peer radio's timestamp, a
timezone setting, or a local NTP server merely advertising its own unsynchronized
clock is insufficient evidence. No local source has been physically qualified here.

## Physical acceptance and recovery

Prepare a local console/operator, verified recovery copy and independent time
reference. Record battery hardware, source configuration, intended offline duration
and acceptable error. Keep private configuration and exact locations out of evidence.

1. While synchronized, save the focused witness and independent reference UTC.
2. Arrange a controlled stop, then shut the Pi down cleanly. Isolate WAN/NTP access
   while retaining a way to reach the console after boot. Remove **all main power**
   for a measured interval, initially at least ten minutes, leaving only the RTC
   battery attached. A reboot or UPS-backed pause does not test RTC battery retention.
3. Reconnect main power with WAN/NTP still unavailable. Capture a witness before any
   synchronization can mask the result. Record the independently measured off-time,
   changed boot fingerprint, RTC/system UTC and error against the reference.
4. Confirm the dashboard describes cold-start uncertainty truthfully. Verify that
   retained work has not been falsely sent, acknowledged, purged or assigned to a
   responder. Qualifying RTC retention does not by itself unlock the runtime source
   gate in this release; use the configured verified local time source to supply OS
   synchronization during offline cold starts.
5. Restore the selected verified source. If a wall step was latched, restart Outpost
   after the OS reports synchronized time. Review waiting/expired/failed work and
   retry only the intended unexpired operations. Verify web/radio recovery and a
   synthetic signed transfer with the peer operator.
6. Repeat across the intended retention interval and relevant battery conditions.
   Record dated bounds and error; a ten-minute test cannot establish days of retention.

This is a planned hands-on exercise; no power cut, WAN change, battery setting or
clock adjustment runs automatically. #44 keeps its separate abrupt-power-loss gate.
#148 keeps whole-station battery runtime. #141 stays open for the physical evidence.

Implementation references: [clock monitor](../src/outpost/timekeeping.py),
[fault tests](../tests/integration/test_time_confidence.py), and the existing
[revision reconciliation tests](../tests/integration/test_federation_revisions.py).
The read-only kernel ABI is documented in [Linux adjtimex(2)](https://man7.org/linux/man-pages/man2/adjtimex.2.html).
