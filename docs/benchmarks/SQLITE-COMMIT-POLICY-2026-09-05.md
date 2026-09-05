# SQLite commit-policy latency — preliminary, 2026-09-05

Source: `da8118431df1cc0a1f046fc573a74f7bcb1c8605`. Tracking:
[#137](https://github.com/baal-bot/Outpost-Meshtastic/issues/137), with physical loss qualification
still in [#44](https://github.com/baal-bot/Outpost-Meshtastic/issues/44).

This is a temporary-database latency comparison, **not power-loss qualification or a default
policy change**. No live database, radio, model, boot service or installed release was modified.

## Method

Host: Raspberry Pi 5 Model B Rev 1.1, aarch64, kernel `6.18.39+rpt-rpi-2712`, Python 3.13.5,
SQLite 3.46.1. Temporary stores were on the workspace's ext4 filesystem on `/dev/mmcblk0p2`.
Card model/endurance/flush behavior, live-store placement and power-system guarantees were not
qualified. The machine remained in normal use; this was not an isolated laboratory host.

Six fresh databases ran in order NORMAL, FULL, FULL, NORMAL, NORMAL, FULL. Each used real
repository migrations and domain services, then changed **only that temporary writer connection**
to the selected synchronous mode. Setup/migration time was excluded. Existing WAL/checkpoint
behavior was left enabled and not separately instrumented, so tail latency must not be attributed
to a particular checkpoint or card operation without additional evidence.

Each trial interleaved 24 incident creations, 24 local mail sends, 24 governed outbox admissions,
and 24 quarantine-plus-producer-revision receipts: 96 operations at concurrency four. Latency
includes service work and serialized-writer waiting, but excludes waiting outside the four-slot
admission semaphore. These are direct service calls, not an end-to-end router/RF benchmark or
signed-custody-envelope workload. No radio tick/send occurred and `radio.sent` remained empty.

The actual writer PRAGMA was checked (NORMAL=1, FULL=2). After ordinary close/reopen, each trial
retained all 24 rows in each relevant table, with clean integrity/foreign-key checks. The reopened
writer returned to the repository's existing NORMAL default in **both** cases; an experimental
PRAGMA change is not a persisted deployment configuration.

## Observed results

Ranges below span all four operation categories in the three trials per mode. Each per-category
percentile has only 24 samples; these are preliminary observations, not reliable long-tail bounds.

| Measurement | NORMAL | FULL |
| --- | --- | --- |
| 96-operation trial elapsed time | 0.295–0.314 s | 0.633–1.332 s |
| Per-category median latency | 2.47–2.75 ms | 15.71–19.57 ms |
| Per-category p95 latency | 4.31–8.21 ms | 65.01–168.20 ms |
| Largest individual observed latency | 245.94 ms | 693.40 ms |
| Rows after close/reopen, per trial | 24 each: incident, mail, outbox, quarantine, revision receipt | Same |

All trials observed a 1,146,880-byte main database and 4,124,152-byte WAL at the measurement
checkpoint. Across six trials, 576 domain operations completed. These numbers must not be
advertised as supported sustained user throughput, RF capacity, native memory bounds or a
maximum database size.

[Raw per-trial measurements](sqlite-commit-policy-2026-09-05.json) and the
[exact experimental script](../review-artifacts/commit-policy-da81184.py.txt) are preserved.
The `.txt` artifact is not a maintained CI test. To reproduce, copy it into a fresh, writable
scratch directory and run it using a disposable checkout/environment at the named source revision.
It creates temporary databases beside its copy, removes only those temporary stores, prints
results, and writes `commit-policy-measurements.json` beside the copied script. Do not overwrite
an earlier result or switch the running appliance's checkout to reproduce historical measurements.

## What remains open

FULL had a measurable latency cost in this limited workload. This does not justify calling
NORMAL power-safe, nor does it prove FULL meets the platform's combined-load service objectives.
The chosen default and any weaker mode still need an explicit decision, sustained/aged/full-media
measurements, end-to-end emergency/receipt workloads, energy measurements, and production reopen/
migration setting tests. Most importantly, #44 must verify retained acknowledged IDs and custody
admissions across actual power loss on the intended storage/power hardware. An integrity check
or ordinary close/reopen cannot establish that outcome; a device that dishonors flushes remains
outside any software-only durability guarantee.
