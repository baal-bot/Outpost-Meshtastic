# Indexed producer paging — #144

This qualifies negotiated `reconciliation: 2` links from #134. Both endpoints must use
that capability for the producer-revision guarantees. The legacy timestamp adapter
remains a compatibility path: it is still clock-sensitive and does not have this
large-history resource qualification. Pinned modern peers cannot silently downgrade.
No radio transmission, installed upgrade or live-database migration is part of these
measurements.

## Bounded query design

Migration 176 adds `idx_fed_revision_stream(stream,revision,uid)`. Each selected stream
seeks strictly after the receiver checkpoint and through the producer snapshot using
that covering index. Unselected streams, disabled modules and globally disabled or
archived boards are excluded before loading metadata. The existing peer policy allows
at most 20 boards, so the combined input has at most 22 streams including incidents
and alerts. Invalid oversized stored policies fail closed.

Each stream subquery has its own `LIMIT 101` **before** the final ordered `UNION ALL`
merge; the merged result also has `LIMIT 101`. Thus at most 2,222 metadata heads feed
the bounded SQL merge, at most 101 reach Python, and at most 100 undergo per-record
export-policy checks. The extra head determines whether more scoped work remains.
At most eight permitted records enter the manifest. Index seeks still have normal
tree-height cost; this is bounded page work, not literally constant-time storage.

Exact incident-radius checks and hidden-thread/post rules remain shared with export,
so a manifest cannot advertise a record that this same policy would refuse to export.
An all-out-of-area page can be empty and still advance after 100 scoped candidates;
it cannot force an unbounded search for eight matches. Private/unselected history no
longer consumes empty-page rounds. No policy filter deletes source records. Legacy
board manifests now also withhold hidden threads and archived boards, matching export.

The cursor, snapshot, epoch and wire protocol are unchanged. Source edits allocate new
revisions, so edits beyond an existing snapshot appear in the next cycle. Selected
board policy changes reset consumer scope as before; global board enable/unarchive
triggers allocate fresh heads. The consumer retains its local item budget and 16-round
ceiling, including when pages are empty or a peer advertises inflated limits (#111).

## Target-host measurement

Run the synthetic test; no external service is required:

```sh
nice -n 10 .venv/bin/pytest tests/integration/test_federation_page_cost.py \
  -o junit_family=xunit1 --junitxml=/tmp/outpost-producer-page-cost.xml
```

The XML `producer_page_cost` property records every measured page and its complete
`EXPLAIN QUERY PLAN`. History generation runs outside the measurement and uses the
real migrations and revision triggers. The September 5, 2026 Raspberry Pi / Python
3.13 run used equal-timestamp public posts, private posts, incidents (including
locationless and out-of-area reports), and alerts. Four checkpoints per size cover
the beginning, a large private-history gap, incidents and alerts.

| Resource for an eight-item page | 1,200 retained heads | 120,000 retained heads |
| --- | ---: | ---: |
| Metadata rows returned to Python | 101 | 101 |
| SQL calls including payload/policy reads | 12–24 | 12–24 |
| Traced Python peak | 38–44 KiB | 37–42 KiB |
| Page wall time | 5.2–10.9 ms | 6.3–10.6 ms |
| Encoded manifest bytes, excluding LoRa framing | 304–318 | 309–319 |
| Authenticated application fragments | 2 | 2 |

Wire size depends on the actual identities and content; these short synthetic UIDs
are not a general airtime forecast. Every fragment remains at most 188 bytes. Python
tracing excludes SQLite native allocations, mmap and baseline process memory. The CI
guards allow 2 MiB traced allocation and two seconds per measured page for slower or
loaded runners; recorded values, not those generous ceilings, are the host baseline.
Field delivery latency and RF sustainable rate remain separate qualifications.

The recorded plan uses, for each of the three eligible streams:

```text
SEARCH fed_revision USING COVERING INDEX idx_fed_revision_stream
  (stream=? AND revision>? AND revision<?)
```

SQLite reports `MERGE (UNION ALL)` and `USE TEMP B-TREE FOR ORDER BY` on its bounded
subquery outputs. Those sorts are expected: each input is already limited to 101,
not the entire collection. The maximum-20-board regression checks all 22 indexed
branches and all pre-merge limits, not just the final eight-item network limit.

## Correctness and scope

The paging tests check equal timestamps across all streams, unique ascending revision
order, serialized continuation checkpoints, per-stream concurrent edits, unchanged
privacy/radius/board filters at manifest and export, empty geographic pages and
disabled modules. Existing producer-reconciliation tests additionally exercise actual
database close/reopen, persisted pending pages, delayed/duplicate deliveries, clock
steps, source rollback detection and locally enforced cycle bounds. Full federation,
incident reconciliation and maintenance regressions remain release checks.

The index bounds collection discovery. Payload size and merged-origin cardinality
still affect work on an individual record; this benchmark does not qualify arbitrarily
large merged lineages. This change does not add withdrawal messages, turn quarantine
into approval, fix restored identity forks, or prove infinite backlog convergence.
An unsustainable arrival rate can still outpace bounded catch-up. Those limitations
remain #135, #142 and #145/#146 as applicable.

Back up before a planned deployment. Creating the index needs space and work
proportional to retained metadata once during migration; older binaries refuse schema
176. Existing revision/lineage/receipt state is unchanged. This evidence must not be
used to claim the installed appliance was migrated, rebooted or field-qualified.
