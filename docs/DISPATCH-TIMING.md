# Dispatch timing across asynchronous waits

[#170](https://github.com/baal-bot/Outpost-Meshtastic/issues/170) corrects the shared governor's
use of tick-start time after asynchronous work. It changes neither class shares, critical
reserve, quiet-hour configuration nor human approval policy. It does not enable #135's
automatic incident sender or qualify physical RF delivery.

## Eligibility and ownership

`tick()` is serialized through one egress lock, including telemetry, power observation,
reservation, radio I/O and outcome persistence. Other admissions and operator cancellation
are not held under that lock. Recovery still requires quiesced egress and admissions.

Candidate ordering remains severity/priority/FIFO for alerts and round-robin/priority/FIFO
for other classes. Eligibility is evaluated separately using current clock, queue, link,
latest observed utilisation/battery, radio profile and budget evidence. A deferred class
does not consume an attempt or change its queue order; other eligible work remains available.
The expiry sweep also tolerates concurrent cancellation during its database writes.

For durable work, `OutboxStore.start_attempt` reloads pending state through the writer and
runs any retained owner guard. After those awaits, it samples the supplied current-time
callback and runs the governor's synchronous dispatch-policy check. Expired work is terminal;
guard rejection retains its explicit authorization-denied failure. Quiet hours, budgets,
pacing, retry deadlines, link/power or changed costing defer work without consuming an
attempt or dropping the pending item. Missing incident guard wiring still fails closed.

An in-process monotonic TTL cannot be extended by a backward wall-clock step. A forward
wall step can expire durable work earlier. Quiet hours use the current configured local
timezone. This is not trusted-time/RTC qualification, a new clock-confidence policy, or a
hard real-time guarantee. Telemetry remains the latest observation, not continuous RF sensing.

The authorization boundary remains **attempt reservation after current validation**. The
following SQL writes/commit and physical radio call are not atomic with that sample. A
deadline/policy change after that boundary cannot recall an already authorized/in-flight
operation. No radio I/O runs under the SQLite writer, and no physical transmit timestamp or
exactly-once delivery claim is inferred from a database timestamp.

## Outcome, retry and airtime evidence

| Evidence | Time boundary |
| --- | --- |
| Durable attempt start | Current wall time sampled at reservation after awaited validation. |
| Successful outcome | Wall time when the awaited radio result is observed, before logging. |
| Failed outcome/retry | Current wall time after acquiring/reading the failure writer transaction; retry delay starts there. |
| Live rolling airtime and pacing | Monotonic time at radio-call completion, failure or cancellation. |
| Recovered rolling airtime | Later of retained attempt start/completion, converted against fresh recovery clocks. |

A radio call may transmit before returning, raising, or being cancelled. Each such call is
charged once for a full rolling hour after its latest observed end. Failures also establish
the normal minimum/four-times-ToA/multipart gap. Retry conversion samples current clocks
after the persistence wait, so a delayed writer cannot create an immediately overdue retry.
Overlapping ticks cannot reserve a second call while the first outcome remains unresolved.

Cancellation during radio I/O propagates after recording the in-memory conservative charge
and gap. Its durable `started` attempt remains for normal recovery to settle as `uncertain`;
there is no invented success or automatic retransmission inside cancellation handling.
Failure of outcome persistence likewise leaves the reserved record recoverable. Rebuilding
the mirror replaces, rather than adds to, its airtime history and restores the remaining
dispatch gap, including multipart pacing.

Recovery includes future-dated records after a backward wall step and clamps their age to
zero. Long calls remain charged even after their start leaves the one-hour window. This can
conservatively overestimate usage. Restart reconstruction still depends on persisted wall
time; arbitrary forward clock changes across a restart are not solved here (#141). The
existing replay/signature/expiry policies are not weakened.

## Verification and compatibility

`tests/integration/test_governor_timing.py` exercises temporary production-wired stores with
simulated radios, actual writer contention, clock changes at awaited boundaries, policy
deferral, cancellation, overlapping calls, long outcomes, recovery and an upgrade fixture.
Existing incident guards, check-ins/alerts, queue publication, transaction supervision,
power, maintenance/backup and package tests remain compatibility gates.

Migration 181 adds only an expression index for retained attempt accounting time. It keeps
late-completion recovery indexed without changing records, states, payloads or protocol.
Back up before an approved upgrade; older binaries correctly reject a newer schema. No
live database was opened/migrated, package deployed, service/radio restarted, WAN disconnected,
power cut or inactive additional node activated by this work.
