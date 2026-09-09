# Trusted-peer UTC checks: software qualification (#141)

Installed release **`20260909T024744Z-7113902e7793`**, source
`7113902e7793ff217644662d9c21ec1a55ccb2d0`, passed all four exact-source CI jobs
and the live post-update checks. The normal updater completed at 02:48:50 UTC on
September 9. Database schema remains **186**. Peer-time permissions remain off.

This extends the [September 8 clock safeguards](OFFLINE-TIME-QUALIFICATION-2026-09-08.md).
The primary field scenario remains continuous Pi/SDR/LoRa operation on the owner's
shared external battery with solar charging backup. No WAN isolation, power cut,
host-clock adjustment, RTC charging change or second-node configuration was performed.

## Behavior

- Current-key operator permissions separately authorize trusting and serving time.
  Both default off. Only a live authenticated challenge can establish freshness.
- Each exchange uses one frame per direction, the existing HMAC/counters, explicit
  identities, a 32-byte random challenge and a 30-second elapsed deadline. All local
  and remote queue delay contributes to uncertainty.
- Confidence concerns actual running UTC: a fresh agreeing reference can restore
  it; positive/negative six-hour differences remain unsafe and display the offset.
  Outpost does not set or offset the host clock.
- Native source age and conservative error bound limit each reference. Peer-derived
  time is not served as a native source; conflicting sources remain explicit.
- Only owned time frames pass the UTC hold. They retain the sole-egress governor's
  federation share, total/regional limits, quiet hours and channel policy. Cold
  startup with unknown historical airtime waits one silent hour before probing.
- Reservations persist probe costs before RF. A healthy restart charges those
  costs conservatively for one hour without imposing a global hour-long transmit
  hold. Unknown/corrupt accounting retains the silent-hour fallback. Retained
  ordinary work stays untouched until time permits normal recovery.

Final review retained the original wall-step latch: correcting a stepped clock still
requires restarting Outpost before a new peer check may restore confidence. It also
keeps malformed recovery metadata under its owning validator, so the operator
settings/dashboard can start while the governor enforces the conservative hold.
Seven focused corruption, pairing and wall-step regressions passed. Startup
recovery also rechecks source confidence at the actual recovery boundary: losing
UTC after construction or during the store transaction cannot waive the silent
airtime hour. Eight focused startup/corrupt-cost/restart cases passed.

Policy changes write their audit event through the shared audit helper inside the
same transaction as the permission change. The initial full CI run caught a direct
audit-table write; that repository convention is now preserved.

The protocol and operator procedure are in [OFFLINE-TIME.md](OFFLINE-TIME.md).
Database schema remains 186; the new settings use the existing runtime-setting store.
The packaged service still excludes `CAP_SYS_TIME` and retains `NoNewPrivileges`.

## Local evidence

The overlapping local regression groups passed:

- 71 clock/peer-time/browser cases during initial qualification; protocol coverage
  91.94%, clock-evidence coverage 94.5%. Both files have a 90% production coverage gate.
- 101 governor, durable queue, incident worker and maintenance cases.
- 185 compatibility cases covering queue publication, readiness, peer management,
  governor eligibility, time warning UI and peer-time exchanges.
- 113 final clock, peer-time and governor timing cases after restart accounting was
  changed to preserve known costs rather than impose a healthy-restart radio hold.
- Seven operator/API/browser cases, including CSRF/role enforcement, strict explicit
  permissions, truthful queue status and three themes at 320/1280-pixel widths.
- 577 unit and peer-time operator/browser cases after the final shared-audit fix.

Tests use real application/store/governor/dispatcher wiring with simulated radio and
clock sources. Scenarios include three simulated days, cold recovery of a retained
alert, six-hour errors, replay, queue delay, revocation while receiving, key changes,
unauthorized pairing attempts, source expiry/conflict, older peers, radio limits,
restart/corrupt accounting and prevention of peer-derived re-export. Synthetic time
tests do not adjust the host. Lint, formatting, strict type ratchet, requirement
ledger, generated capability/command documentation and static markup checks passed.

## Exact-source CI and live deployment

[CI run 34300732257](https://github.com/baal-bot/Outpost-Meshtastic/actions/runs/34300732257)
passed for the installed source:

| Job | Full suite | Production-wiring rerun | Minutes |
| --- | --- | --- | --- |
| Python 3.12 | 2,410 passed, 1 skipped | 1,302 passed | 57.15 |
| Python 3.13 | 2,410 passed, 1 skipped | 1,302 passed | 51.57 |
| Locked runtime, Python 3.12 | 2,410 passed, 1 skipped | — | 27.23 |
| Locked runtime, Python 3.13 | 2,410 passed, 1 skipped | — | 28.95 |

Production coverage was **92.0%** for `fed/time.py` and **94.5%** for
`timekeeping.py` on both Python versions, above their individual 90% floors.
The remaining coverage, package, dependency-audit and repository checks passed.
The production reruns and local groups overlap the full suite; these are not
counts of distinct additional tests.

The normal updater retained a verified pre-upgrade backup and used the native
HailoRT wheel. At 02:49:22 UTC, the packaged service, web, radio, required native AI,
database integrity/foreign keys, and same-package boot readiness passed. All
protected pre-update record IDs survived. The post-update store held 1,625 outbound
work records and 2,036 power samples. The host did not reboot.

The service reports usable synchronized native UTC, excludes `CAP_SYS_TIME`, and
retains `NoNewPrivileges`. The `federation-time` task was running with recent
progress, zero failures/restarts, and healthy core/optional tasks at 02:49:58 UTC.
Its peer API requires authentication; no live peer-time permission was enabled.
Served Federation assets match both the installed package and source.

USB external power passes without a battery percentage. The selected regional map
and world overview remain verified: **65,884,160 bytes and 6,176 tiles**. The served
world tile and map assets match the verified pack/package. The prior map operator
observation remains stale; no replacement field attestation was recorded.

Private test/CI/deployment records are under `.data/peer-time-141-2026-09-09/` and
`/var/lib/outpost-qualification/141-20260909/peer-time-update/`. Later documentation
commits do not change the installed runtime's source/CI identity.

## September 9 reboot follow-up and clock-hold recovery

The owner returned after a reboot of the installed appliance. The boot ID changed
from the installation witness, and the same enabled packaged release started
automatically. Its process began **52.02 seconds** into boot. The OS time service
reported initial synchronization at **72.96 seconds**, before Outpost completed
startup at **95.92 seconds**. These timings use the monotonic boot journal because
wall-clock labels crossed a time correction.

At **11:59:51 UTC**, Outpost reported a latched **259.806-second forward clock step**
and unsafe timestamp confidence, while the OS independently reported synchronized
time. Radio, required native AI, core and optional tasks remained healthy, with
zero automatic service restarts or supervised task failures. All tracked records
from the installation witness were retained, including all 1,625 outbound-work
records and 2,036 power samples. The configured regional map was unchanged.

After preserving that evidence and checking the OS source, an Outpost service
restart ran at **12:00:08–12:00:20 UTC**. The host was not rebooted again, and the
clock, release and configuration were not manually changed. At **12:02:20 UTC**,
the recovered process still had synchronized, usable UTC with no clock-step latch.
Web, radio, native AI, all supervised tasks, schema-186 database integrity and
foreign keys, and a fresh same-package boot readiness check passed. The service
retained its privilege restrictions; peer-time permissions remained disabled and
the peer API still required authentication. Map and Federation assets matched the
installed package; the regional map and overview still verified at 65,884,160 bytes
and 6,176 tiles.

Recovery also released scheduled maintenance. It created a pre-cleanup snapshot
and removed **two expired mail records** at 12:00:20 UTC. Their expiry metadata in
that integrity-checked snapshot, absence of pending data requests, and the exact
maintenance audit count establish policy-driven expiry. The original strict
retention comparison remains preserved with its two missing mail IDs; a separate
assessment accounts for them. All other tracked records were retained. One pending
outbound item expired with its durable row retained. Three new normal sends were
recorded after recovery, leaving no pending or held work at the final observation;
remote receipt or acknowledgement was not tested.

Final counts were 9 incidents, 11 incident updates, 5 mail, 1,629 outbound-work
records and 2,134 power samples. Private observations and the separate retention
assessment are in `/var/lib/outpost-qualification/141-20260909/post-reboot/`, with
a local checkpoint in `.data/post-reboot-2026-09-09/`. Earlier installation evidence
was not overwritten.

This reboot exposed a remaining startup limitation: the service can start before
the OS time source synchronizes, and a subsequent clock step requires an operator
restart to clear the deliberate latch. This observation establishes recovery after
that intervention, not unattended recovery of usable time. Overall readiness still
has outstanding field checks; this follow-up does not qualify offline holdover,
two-station time exchange, RTC retention or physical WAN-disconnected map access.

## Remaining physical evidence

#141 stays open for physical two-station and multi-day powered qualification, plus
the separate RTC complete-power-loss scenario. A group with no remaining independent
time reference cannot create fresh UTC by repeatedly exchanging aging references.
No physical or battery qualification is claimed by the software tests.

#130 remains the overall open resilience tracker. #139's regional-map software and
selected station region are complete; its physical WAN-disconnected browser exercise
and current-process operator attestation remain open. The map checklist was updated
to distinguish that completed software/selection from the remaining field exercise.
