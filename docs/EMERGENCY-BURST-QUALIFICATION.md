# Synthetic emergency-burst qualification — #152

This is application-level, no-RF evidence, not a sustainable radio-rate rating, a
human usability trial, or a guarantee of service with no remaining storage. All
identities, reports, peers and database contents are synthetic. Tests instantiate
the production application graph with `SimulatedRadioLink` and temporary databases;
they do not start the appliance, use a real channel, or require internet access.

## Reproduce

```sh
nice -n 10 .venv/bin/pytest tests/integration/test_emergency_bursts.py \
  -o junit_family=xunit1 --junitxml=/tmp/outpost-synthetic-burst.xml
```

The XML `synthetic_burst` property records the workload and measured resource use.
Virtual time exercises rate windows without sleeping. Wall time measures processing,
not radio propagation. Run the existing safety-floor, safety-command, inbound-worker,
rate-limit, governor-eligibility, durable-outbox, transaction and maintenance suites
alongside this test before release.

## Declared envelope and observed result

The September 5, 2026 local run used the Raspberry Pi development host, Python 3.13,
12 synthetic sender IDs, three responder IDs and six waves spaced 61 virtual seconds
apart. Each sender supplied changed HELPME, REPORT! and OK information in each wave.
Every request was repeated both as the same RF packet and as a new equivalent packet.
The radio was disconnected: this deliberately accumulated the entire admitted outbox.
The normal 500-item queue and safety-repeat policy remained enabled. Quiet hours were
disabled only in the isolated fixture to separate scheduling from burst admission.

| Measurement | Observed |
| --- | ---: |
| Submitted packets, including RF repeats | 648 |
| Meaningful requests recorded / equivalent new packets coalesced | 216 / 216 |
| Same-packet repeats rejected before dispatch | 216 |
| Incidents requiring review / welfare records | 72 / 144 |
| Responder notifications admitted, not received | 216 |
| Peak outbound queue | 432 of 500 |
| Peer items quarantined / remaining producer backlog | 48 / 24 |
| Producer database page growth | 432 KiB |
| Peak traced Python allocation during the workload | 595 KiB |
| Local intake latency, p95 / maximum | 15 / 17 ms |
| Processing wall time for the virtual six-minute workload | 3.56 s |

These numbers are a reproducible sample, not a statistical capacity guarantee.
Python allocation tracing excludes SQLite native allocations, mmap, OS caches and
the baseline application. The test separately asserts fewer than 32 MiB traced
allocation and 8 MiB producer database growth; timings are recorded rather than
used as load-sensitive CI assertions. Read connections retain the existing two-reader
pool bound. The receiver accepts eight versioned items per wave, records durable
receipts and ignores duplicate deliveries. It does not approve them automatically
or claim its remaining backlog has converged.

## Failure boundaries exercised

- A 16-item queue full of ordinary replies remains at 16 while 24 changed HELPME
  requests are recorded. Each refused acknowledgement is attributed to its inbound
  packet with `queue_full`; each welfare record reports zero admitted responders.
  After the 300-second reply TTL, a disconnected-radio tick expires stale work and
  new intake can queue again. Closing and reopening the database recovers the four
  newly admitted messages and retains repeat suppression without duplicating intake.
- Forty alerts and forty federation items blocked by their class shares do not
  prevent twelve eligible incident acknowledgements from transmitting on the
  simulated link. Skipped items consume no attempts. A critical escalation alone
  can use the emergency reserve after the normal allowance is exhausted.
- A stalled optional command and 40 ordinary arrivals fill a 16-item worker backlog;
  24 excess ordinary arrivals are explicitly dropped. Twenty-four real report
  handlers still complete through the safety fast path. Releasing the optional
  handler into an exception lets the worker drain and removes sender queue state.
- `PRAGMA max_page_count` on the scratch writer produces real `SQLITE_FULL` without
  consuming the host disk. Failed incident transactions leave no partial incident,
  origin or reference binding. Integrity and foreign-key checks pass. Releasing
  scratch space restores intake and producer paging; database reopen preserves it.
- Reactions from twelve synthetic IDs, each repeated four times concurrently, count
  once per ID. They do not promote PKI, trust or incident verification. Radio IDs
  are **not** proof of twelve independent people; raw counts are not consensus.

## Corrections made during qualification

Changed welfare requests can render identical acknowledgements. Text-only outbound
deduplication previously discarded those later acknowledgements even though the new
information was recorded. Accepted safety ACKs now use an inbound-packet/part identity;
equivalent safety requests still coalesce before the handler. Ordinary response and
responder-notification deduplication, airtime ceilings and queue bounds are unchanged.

Handler timeout or cancellation now releases the safety retry marker just as a
handler error does, then propagates cancellation. Tests interrupt before mutation and
verify an immediate new-packet retry records the report. This is an **at-least-once
retry policy**, not exactly-once execution: interruption after a service has committed
can still leave an uncertain acknowledgement and a subsequent explicit forced report
can be a duplicate. Serialized service transactions and retained provenance remain
the record of what actually committed; a cancellation is not proof of non-commit.

## Operating policy and limits

Keep first requests and meaningful changed information admissible; do not defeat the
safety floor to make a flood benchmark look stable. Keep equivalence coalescing, atomic
outbox admission, class budgets and TTLs enabled. An admitted notification means queued
work, not a radio receipt or a person responding. Queue-full results require operator
attention, not a success claim or an assumption that the sender saw the warning.

The measured three-responder workload creates six outbound items per sender/wave
(four for HELPME and one each for REPORT! and OK). Increasing responder fan-out or
continuing without egress exhausts the remaining queue quickly. Reduce discretionary
traffic, use an intentionally enrolled responder audience, and review delivery age,
failed admissions and unresolved needs before increasing fan-out or catch-up quotas.
Do not label ordinary welfare messages critical simply to consume the reserve.

Storage is **not globally capped**: active incidents, retained evidence and permanent
identity/revision metadata intentionally survive cleanup. Time-based retention is not
a bound under unlimited distinct inputs. Review storage headroom and maintenance
health; provision capacity and recovery storage before deployment. Actual full storage
can prevent logging and replying as well as intake and is an infrastructure fault,
not a supported degraded mode with guaranteed radio service. The page-ceiling test
qualifies rollback/recovery, not physical flash exhaustion or sudden power removal.

Remaining capacity work belongs to #142 (seven-day population/airtime/storage model),
#144 (large-history paging cost), and #158 (long-duration soak). The multi-node field
exercise #157, power-loss qualification #137/#44, and human responder load/triage
remain separate gates. Do not extrapolate 15 ms application latency into an RF SLA or
72 review records into a claim that a volunteer team can handle that load.

No live-channel flooding or member data is needed to reproduce this evidence. Handle
suspected vulnerabilities privately under [SECURITY.md](../SECURITY.md); the public
qualification record contains only bounded synthetic reliability tests.
