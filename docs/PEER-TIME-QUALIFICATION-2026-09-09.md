# Trusted-peer UTC checks: software qualification (#141)

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
- 110 final clock, peer-time and governor timing cases after restart accounting was
  changed to preserve known costs rather than impose a healthy-restart radio hold.
- Seven operator/API/browser cases, including CSRF/role enforcement, strict explicit
  permissions, truthful queue status and three themes at 320/1280-pixel widths.

Tests use real application/store/governor/dispatcher wiring with simulated radio and
clock sources. Scenarios include three simulated days, cold recovery of a retained
alert, six-hour errors, replay, queue delay, revocation while receiving, key changes,
unauthorized pairing attempts, source expiry/conflict, older peers, radio limits,
restart/corrupt accounting and prevention of peer-derived re-export. Synthetic time
tests do not adjust the host. Lint, formatting, strict type ratchet, requirement
ledger, generated capability/command documentation and static markup checks passed.

## Deployment and remaining evidence

Exact-source CI and local deployment are pending at this source checkpoint. The
running release remains `20260908T235451Z-5a7de3241e4c`. Peer time permissions have
not been enabled on the live station. Private test/CI/deployment records are under
`.data/peer-time-141-2026-09-09/`.

#141 stays open for physical two-station and multi-day powered qualification, plus
the separate RTC complete-power-loss scenario. A group with no remaining independent
time reference cannot create fresh UTC by repeatedly exchanging aging references.
No physical or battery qualification is claimed by the software tests.

#130 remains the overall open resilience tracker. #139's regional-map software and
selected station region are complete; its physical WAN-disconnected browser exercise
and current-process operator attestation remain open. The map checklist was updated
to distinguish that completed software/selection from the remaining field exercise.
