# Phase 3 — Community Watch acceptance evidence

Automated on 2026-08-24 with `pytest`, the virtual clock, simulated radio, and a fresh
SQLite database. Hardware-only gates remain explicitly open until the tabletop exercise is
run; this document does not treat simulation as proof of real-mesh timing.

| # | Gate | Evidence | Status |
|---|---|---|---|
| 1 | One-message typed, geotagged report | `test_safety_commands.py`, `test_watch_incidents.py` | Pass |
| 2 | ≥90% inference on 40 phrases | `test_phase3_acceptance.py` | Pass |
| 3 | Duplicate offered as `CONFIRM` | `test_safety_commands.py`, `test_watch_incidents.py` | Pass |
| 4 | Responder alert preempts digest and broadcasts within 5 s | `test_governor.py`; real timing in tabletop | Sim pass / hardware open |
| 5 | Durable escalation and ack threshold | `test_watch_alerts.py` | Pass |
| 6 | All-clear supersedes queued repeat | `test_governor.py`, `test_watch_alerts.py` | Pass |
| 7 | `OK`, roster, derived unaccounted | `test_safety_commands.py`, `test_watch_checkin.py` | Pass |
| 8 | Need-help reaches responders and dashboard within 5 s | `test_safety_commands.py`; real timing in tabletop | Sim pass / hardware open |
| 9 | Severity then haversine ordering | `test_watch_incidents.py` | Pass |
| 10 | Offline map: incidents, nodes, alert areas, roster | Browser check; WAN-down hardware check remains | Sim pass / hardware open |
| 11 | Three-alert storm stays within airtime ceiling | `test_governor.py` | Pass |
| 12 | Emergency keywords default off; responder-only | `test_watch_incidents.py`, configuration tests | Pass |
| 13 | Valid roster CSV | `test_watch_checkin.py` | Pass |
| 14 | AI cannot author or raise alerts | `test_phase3_acceptance.py` | Pass |
| 15 | Cross-Outpost identity, conflict, merge, unmerge, and local-monitoring lock | `test_incident_reconciliation.py` | Pass |

The remaining phase gate is the six-person real-radio tabletop in
`tests/hardware/PHASE3_TABLETOP.md`.

## September 2026 concurrency hardening

[Resilience issue #131](https://github.com/baal-bot/Outpost-Meshtastic/issues/131) adds
`tests/integration/test_incident_transactions.py` to the automated evidence. The initial
50-case suite exposed 34 failures before the transaction fix. It covers distinct concurrent
reports, duplicate review and forced reports, authenticated web/radio intake, actual task
cancellation and write-failure rollback, closing/reopening the store, mixed member/operator
update sequencing, reaction counters, and concurrent expiry. Concurrent BBS creation remains
a positive control for the existing transaction primitive.

A separate `test_transactions.py::test_cancelled_begin_rolls_back_before_releasing_the_writer`
regression covers cancellation after SQLite starts a transaction but before the caller enters
its body. It reproduced a stranded writer transaction; the helper now rolls back that boundary
before releasing the writer lock.

Incident creation now commits duplicate checking/reference allocation, the record, origin, and
initial provenance as one serialized unit. Reactions, operator notes/acknowledgements,
operator corrections, and each expiry transition similarly commit their related state and
history together. Expiry rechecks eligibility after obtaining the writer. The #131 atomicity
change alone does not change schema, radio commands, federation wire format, or airtime policy.

The #131 evidence does not close short-reference reuse (#138), missing-location correction (#140),
federation clock/latency (#134/#135), or SQLite power-loss durability (#137). No physical outage
or real-radio timing qualification is implied by transaction/cancellation tests.

The separate #138 implementation and migration 174 add permanent local reference bindings,
retire ambiguous reused legacy numbers, and include report title/origin in reaction replies.
`tests/integration/test_incident_references.py` covers delayed radio commands, restart,
retention cleanup, legacy migration, import, merge/unmerge, and same-origin reimport. The
transaction tests also assert that reference reservations roll back with failed creations.
See [resilience hardening](RESILIENCE-HARDENING.md) for rollout and database-lineage limits.

The separate #140 implementation supplies verified-reporter `UPD`, guided handheld input,
and authenticated operator location correction without attaching cached GPS. Its location
suite tests the exact acknowledgement journey, ownership/PKI/replay protections, explicit
coordinate consent, stale references, atomic before/after evidence, and federation policy.
Browser intake tests now start without a map position and exercise the correction control
on phone and desktop in Dark, Daylight, and Night Ops. These checks do not establish
prompt multi-radio propagation or erase previously shared history.
