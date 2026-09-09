# Offline time confidence (#141)

Outpost distinguishes usable UTC evidence from **qualified RTC retention**. A valid
timezone, an RTC device, its charging voltage, and a recent internet sync do not prove
that a station retains time when all main power is removed. The physical retention
gate remains open until it has dated measurements on the intended hardware.

## Runtime policy

The packaged `SystemClock` reads Linux's `adjtimex` status with `modes=0`. This is an
unprivileged query; it cannot adjust time. Outpost never sets the OS clock, enables
battery charging, or connects to a time server. The optional trusted-peer exchange
below checks the running UTC clock without changing it.
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
| Wall time jumps before this process has ever trusted native or peer time | Work stays held until two fresh, good OS synchronization probes 30–60 elapsed seconds apart confirm a stable, plausible clock. The ordinary queue then recovers automatically against corrected UTC. |
| Wall time jumps after this process has trusted time, or elapsed time goes backward | Timestamp confidence latches as uncertain. Correct and verify the OS time source, then restart Outpost. Simply moving the clock back does not clear the latch. |

Clock-source probes are cached for 30 seconds; discontinuity checks run at operation
boundaries. The Linux probe currently supports the 64-bit Linux ABI used by the
qualified ARM64 appliance and x86-64 CI runners. An unavailable probe yields unknown
confidence. Operator readiness observations never unlock time-sensitive operations.

Startup source evidence is sampled when the system clock is constructed, before
slow application initialization. A delayed OS synchronization correction does not
require a manual service restart if the process has never used trusted UTC. During
confirmation, another jump, an invalid date, a failed synchronization probe or a gap
longer than 60 seconds between probes starts confirmation over. Cached probe results
cannot supply the second observation. No usable source means the hold continues;
the service does not wait for WAN before bringing up local access.

Native or accepted peer confidence permanently closes this startup exception for
the process. Steps during normal operation still require controlled recovery. The
diagnostic `startup_recovered` field records automatic startup recovery for the
current process; it does not prove hardware time accuracy or an actual reboot test.

Local replies and alerts continue during **in-process** uncertainty if the governor
started with usable time and has recovered its airtime history. Their deadlines,
retry delays and airtime accounting use an epoch projected from monotonic elapsed
time. Clock changes cannot replenish airtime or renew their lifetimes. Scheduled
traffic waits rather than guessing whether quiet hours apply.

At an uncertain cold start, retained queue deadlines and prior airtime cannot safely
be reconstructed. Ordinary radio egress remains held until the source becomes usable; work
is preserved without claiming a send. New ordinary durable admissions return
`time_uncertain`. Explicitly approved peer time probes can bootstrap confidence
after one full silent airtime hour; see the recovery accounting below.
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

The peer-time extension adds two optional authenticated control messages. It needs
no schema migration, GPS write, or change to existing signed validity checks. The OS
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

## Continuous operation on station backup power

The local owner reports that the Pi, SDR and LoRa radio share an external backup
battery with days of runtime and solar charging backup. Use continued operation
through a WAN outage as the primary acceptance scenario for this installation.
The Pi's clock keeps running while the station supply keeps the Pi powered. The
dedicated RTC battery concerns retention when main power is completely removed;
its presence is not a prerequisite for this continuously powered scenario.

The installed software still limits estimated holdover to six hours from the last
good OS observation and 30 seconds estimated error, using 500 ppm drift. These are
conservative policy bounds, not measurements of this Pi's clock accuracy. They can
hold timestamp-sensitive functions while the battery still has days of energy.
Increasing the duration alone would still encounter the error bound. Multi-day
powered operation therefore needs clock-accuracy and availability qualification;
the installed safeguards do not establish that acceptance.

For the next planned field exercise, retain station power and local access, record
a synchronized baseline, then arrange WAN/time-source isolation with the operator.
Compare UTC against an independent reference over the intended days-long interval,
including beyond the current holdover boundary. Record elapsed time, boot/process
continuity, actual UTC error, reported confidence and the behavior of retained work.
Recovery must preserve signed validity limits, airtime accounting and revision
ordering. Use those measurements to qualify a longer confidence policy or a verified
offline OS time source. Owner-reported battery runtime guides the exercise; measured
whole-station energy and solar performance remain under #148.

This clarification changes the next acceptance scenario. No WAN isolation, power
interruption, clock adjustment or runtime-policy change was performed for it.

## Trusted-peer time fallback

In Federation, open **Time backup** on a paired peer. **Trust this peer’s time**
allows that peer to check the local running UTC clock. The peer operator must
separately enable **Share this station’s time with this peer**. Both permissions
start disabled, bind to the current pairing credential, and need review after
re-pairing. At most four peers may have time permissions. Save permissions before
using **Check time**. The UI distinguishes queue admission, a fresh reply, timeout,
measured offset, and the current station-wide confidence decision.

The automatic worker checks approved peers when the local OS stops reporting
synchronized time. Each process requests at most once per peer per hour and once
globally per 15 minutes; replies have the same separate limits. The existing
rolling federation share, total airtime, radio region, channel-utilization limit,
quiet hours and radio availability still apply. The quiet-hour check uses the
Pi’s displayed local time; elapsed airtime limits are independent of UTC.
Nothing is transmitted when no
peer has explicit time permissions. This feature is a UTC check, not a time setter.
A station six hours wrong stays held and reports the disagreement; the operator
or configured OS time service must correct the clock.

A useful provider must report its own usable native Linux kernel synchronization
or bounded same-process holdover. Outpost does not re-export a peer-derived check,
simulated clocks, or unqualified RTC retention as a native reference. Providers
need a working configured OS source, such as WAN NTP or a local GNSS-disciplined
service. If every station loses all independent references, exchanging the same
aging evidence cannot extend the six-hour source lifetime indefinitely. The
exchange validates the provider's claim and delay bound; a compromised approved
provider can still lie about UTC. Pairing authentication is not proof of accuracy.

The `time_v1: true` HELLO capability advertises support. `TIME_REQUEST` (`0x42`) and
`TIME_RESPONSE` (`0x43`) each fit one frame under the existing 188-byte ceiling,
using the paired HMAC, durable replay counter and explicit sender/target IDs. The
request carries `n`, a fresh random 32-byte challenge. The response echoes `n` and
carries `u` (UTC milliseconds), `e` (error milliseconds rounded upward), `a`
(native reference age in seconds rounded upward), and `s: "kernel"`. Unknown
fields, multiple fragments, wrong targets, changed keys, expired or used challenges,
unsupported references and malformed values are rejected. Ordinary signed-message
timestamps never serve as clock samples. Older peers are not probed until they
advertise support.

A challenge expires 30 monotonic seconds after creation, including local admission,
radio queue delay, peer processing/queue delay and the return path. If round-trip
time is `r`, the UTC interval on receipt is conservatively bounded by
`[provider_utc - provider_error, provider_utc + r + provider_error]`, with an added
500 ppm delay allowance. Confidence requires the midpoint's distance from the
**actual local UTC**, plus the interval half-width, to remain at most 30 seconds.
Fresh peer intervals must overlap; disagreement holds affected work. A good native
OS synchronization observation remains authoritative. Accepted evidence ages by
500 ppm, expires within the provider's remaining six-hour lifetime, disappears on
restart or revocation, and is invalidated by a later wall-clock step. Peer evidence
cannot clear a wall-step hold. After the process has trusted native or peer time,
correcting a stepped clock requires an Outpost restart before validation. This also
reconstructs the durable queue against the corrected UTC epoch. Only the never-trusted
startup case above can recover automatically through fresh native OS evidence.
RTC retention is never certified by this check.

### Recovery accounting

Only broker-owned time frames with current exact-payload and credential checks may
pass the UTC uncertainty hold. They use the same sole-egress governor and federation
budget as other federation traffic, and bypass the wall-time peer-online heuristic.
No ordinary custody, synchronization, scheduled work or cleanup is released merely
because a probe is queued. Valid UTC evidence is required before rebuilding the
retained ordinary queue.

An uncertain cold process first waits a full rolling airtime hour, since old UTC
labels cannot reconstruct recent radio cost. A native source that recovers before
any time frame was reserved can reconstruct history normally and avoid that wait.
Every time-frame reservation records its airtime cost before RF, in the same
transaction as the durable send reservation. On restart, those costs are charged
for a full hour using elapsed time, independently of UTC. Healthy restarts can
therefore preserve their budget without holding unrelated traffic for an hour.
Unknown or corrupt recovery accounting still requires the full silent hour. An
interrupted reservation is charged conservatively because RF completion is unknown.
Same-process recovery preserves elapsed costs and may count overlapping durable
costs twice for their remaining hour. The recovery cost list is refreshed from the
bounded rolling history; old costs do not accumulate indefinitely. Quiet hours can
delay recovery and are never bypassed.

Software tests exercise three simulated days of reference loss, cold startup,
positive/negative six-hour errors, delay/expiry, replay, revocation/key replacement,
conflicting references, source age, restart accounting, quotas/region limits,
older peers and operator controls in three themes at mobile/desktop sizes.
Physical two-station and multi-day powered qualification remain open under #141.
No host time adjustment, WAN isolation or power interruption is part of automated
qualification. Deployment evidence is recorded separately from source tests.

Design references: NTP's offset/delay model in
[RFC 5905, section 8](https://www.rfc-editor.org/rfc/rfc5905.html#section-8), and
fresh authenticated responses and delay limits in
[RFC 8915, sections 5.3 and 8.6](https://www.rfc-editor.org/rfc/rfc8915.html#section-5.3).
These inform this custom LoRa protocol; it is not NTP or NTS.

## Complete power loss: RTC acceptance and recovery

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
   gate in this release; use a configured verified local OS time source or a fresh, approved peer
   check that agrees with actual UTC during offline cold starts.
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
