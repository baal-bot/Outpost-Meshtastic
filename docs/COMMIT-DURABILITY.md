# Acknowledged-record durability (#137)

Outpost uses WAL with `synchronous=FULL` on application database connections.
The serialized writer verifies its actual setting before migrations and again
before opening the store to callers. A weaker setting aborts startup and closes
the connection. Reopening an existing store applies FULL; an old connection's
NORMAL setting is not a persistent property of its database file. No runtime
configuration, environment switch or automatic low-storage fallback selects a
weaker commit mode.

SQLite documents a WAL synchronization at each FULL commit. NORMAL can lose
recent committed transactions after operating-system failure or power loss.
FULL depends on the operating system, filesystem, storage controller and medium
honoring synchronization requests; it cannot repair dishonest flushes, failed
media or a missing independent backup. See [SQLite's synchronous
contract](https://www.sqlite.org/pragma.html#pragma_synchronous) and
[WAL performance](https://www.sqlite.org/wal.html#performance_considerations).

## What an acknowledgment means

| Boundary | Required committed evidence | What it does not establish |
| --- | --- | --- |
| Incident creation succeeds | Incident, stable reference and associated provenance in the owned transaction | Remote delivery, operator review or a human response |
| Local mail send succeeds | Message identity, body and conversation fields committed together | Recipient receipt/read status |
| Durable outbound admission succeeds | Admitted work persisted; in-memory publication occurs after commit | Radio transmission or destination storage |
| Relay custody is accepted | Validated signed envelope and custody state retained before success returns | Destination dispatch, human action or another node's storage quality |

The policy covers subsequent committed changes to the same authoritative store,
including identity, replay and receipt state. The existing writer settles commit
or rollback before releasing transaction ownership. Failed commits propagate an
error and do not publish newly admitted queue work. A process can die after a
commit but before returning its result; callers must retain an unknown outcome
and use existing identity/idempotency rules instead of assuming rollback.

FULL neither removes private-data retention obligations nor makes historical
backups current. An off-device restore remains fenced under the
[node-loss contract](NODE-LOSS-AND-MOBILITY.md). This change does not alter schema,
wire format, keys, radio policy or acknowledgement vocabulary.

## Verification and operating cost

`tests/integration/test_commit_durability.py` queries the actual writer inside its
owned transaction, exercises fresh migrations, upgrade from schema 184, reopen,
backup restoration and rejection of weakened configuration. Existing transaction,
mail, outbox, signed relay and recovery regressions cover their commit boundaries.
A separate SQLite shell's `PRAGMA synchronous` reports that shell's connection,
not the running Outpost writer; it is not deployment evidence.

Run the bounded comparison from the reviewed checkout's editable development
environment, choosing a **new** output directory on the intended filesystem:

```sh
.venv/bin/python -m tools.benchmark_commit_policy \
  --output /path/to/new-commit-policy-measurement \
  --per-kind 128 --concurrency 8 --rounds 3
```

The tool creates and removes only its own temporary stores beneath that new
directory. It compares alternating NORMAL/FULL trials of real incident, mail,
outbox and signed relay-custody service calls. Only the experimental temporary
writer is weakened. Returned record IDs are compared with retained IDs before
and after ordinary reopen; every reopened writer must return to FULL. It never
connects a radio, transmits, reads live data or changes a service. Setup and
producer signing are outside the timed interval; recipient signature validation,
transaction waiting and normal checkpoint effects are included. Admission waiting
outside the concurrency limit is excluded. Results preserve runtime/source hashes.

The [September 8 measurements](benchmarks/SQLITE-COMMIT-POLICY-2026-09-08.md)
record the observed cost and their limits. They do not establish a sustained-load
ceiling, SD-card endurance, energy consumption or worst-case response time. If a
deployment cannot sustain the chosen workload, reduce discretionary work or improve
storage/power; do not silently downgrade the writer to NORMAL.

## Remaining physical acceptance

#137 remains open for target-storage/energy measurements and #44's controlled
power-loss campaign. Record source/configuration, medium/controller/power details,
the exact acknowledged incident/mail/outbound/custody IDs before interruption,
retained IDs and recovery outcomes afterward, and every loss or intervention.
An `integrity_check` pass or ordinary process restart cannot replace this evidence.
Testing uses backed-up designated hardware and a separately planned interruption;
these instructions do not initiate one.
