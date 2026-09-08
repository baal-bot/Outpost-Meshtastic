# Checked FULL writer policy — temporary-store measurements, 2026-09-08

Tracking [#137](https://github.com/baal-bot/Outpost-Meshtastic/issues/137);
physical loss qualification remains [#44](https://github.com/baal-bot/Outpost-Meshtastic/issues/44).
The [commit contract](../COMMIT-DURABILITY.md) selects FULL for application connections.

## Source and method

Base revision: `6fb190971c8625f4d0a482d9ca79a1d9a23a0f6a`, with the proposed #137 changes.
The exact measured `database.py` SHA-256 is
`4da4ae11716b825df5c2def88b73a24dcd6af3f2520801daed2b63ac9f269e96`;
the benchmark source SHA-256 is
`dc3af60a13fcb0335b96b8b86879a9d09ddc6d0e3983457b822053ea930c4f95`.
These hashes identify the measured source before its commit; they are not CI evidence.

Host: Raspberry Pi 5 Model B Rev 1.1, aarch64, kernel `6.18.39+rpt-rpi-2712`,
Python 3.13.5, SQLite 3.46.1. New temporary databases were on the workspace ext4
filesystem on `/dev/mmcblk0p2`. Card model/endurance, flush compliance, energy and
whole-station power behavior were not measured. Normal host activity continued;
no other test suite was deliberately run alongside the benchmark.

`python -m tools.benchmark_commit_policy --per-kind 128 --concurrency 8 --rounds 3`
ran six new stores in NORMAL/FULL/FULL/NORMAL/NORMAL/FULL order. Each trial used the
actual current migrations and service classes, then changed only its temporary
writer to the experimental mode. Each interleaved 128 incident creations, 128 mail
sends, 128 governed durable outbox admissions and 128 signed relay custody
admissions: 512 operations at concurrency eight, 3,072 operations overall.

Custody trials validate an actual Ed25519-signed envelope at an intermediary and
persist its queued custody state. They do not transmit an acknowledgment, dispatch
at the destination, or include radio/router timing. Setup, migrations and producer
signing are excluded; recipient validation, service work, serialized-writer waiting
and normal WAL/checkpoint effects are included. Waiting outside the eight-slot
admission semaphore is excluded. Checkpoint timing was not separately measured.

Returned incident/mail/outbox/envelope IDs matched retained IDs before and after
ordinary close/reopen in all trials. Each reopened writer reported FULL (2), even
after an experimental NORMAL trial. Integrity/foreign-key checks passed, and
simulated radio sends stayed zero. This differs from the historical September 5
script's producer-revision quarantine workload and smaller sample/concurrency;
the two reports are not a controlled application-version comparison.

## Observed results

Ranges span the three trials per mode and, for latency, all four operation categories.
Each per-category percentile has 128 samples per trial.

| Measurement | NORMAL | FULL |
| --- | --- | --- |
| 512-operation trial elapsed | 1.629–1.835 s | 2.763–3.030 s |
| Per-category median latency | 7.69–8.51 ms | 37.21–43.25 ms |
| Per-category p95 latency | 206.60–222.04 ms | 86.88–92.29 ms |
| Largest observed single-operation latency | 341.30 ms | 128.42 ms |
| Retained IDs per category per trial | 128, exact match | 128, exact match |
| Main database / WAL at measurement | 1,572,864 / 4,185,952 bytes | Same |

[Raw measurements](sqlite-commit-policy-2026-09-08.json) preserve each category and
trial. FULL costs more elapsed time and median latency in this sample. NORMAL's
larger observed tail does not prove FULL always has better tail latency, nor does
this uninstrumented run identify the cause of those stalls.

The data supports proceeding with the explicit FULL policy and broader regression
testing. It does not establish sustained/aged/full-media performance, a worst-case
bound, RF G6 timing, energy cost or acknowledged-data survival after physical loss.
Those measurements and the #44 retained-ID campaign remain open.
