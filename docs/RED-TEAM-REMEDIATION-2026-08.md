# August 2026 red-team remediation exit audit

This document audits the exit criteria in GitHub issue #44 against the current working tree. It is
an evidence index, not permission to close field-gated work. The original review baseline was
`d6f0307`; the current capability manifest and CI run against the revision under review.

## Tracker status

The P0/P1 implementation findings linked by #44 are closed upstream. The remaining enhancement
issues #34–#37, #40, and #41 are implemented in this working tree with source, migrations, operator
UI, tests, and documentation. They remain open on GitHub until these changes are reviewed, committed,
pushed, and their issue evidence is accepted.

| Remaining issue | Local evidence |
| --- | --- |
| #34 federation topology | `tests/integration/test_federation_topology.py`, topology browser workflow, `FEDERATION-TOPOLOGY.md` |
| #35 signed multi-hop relay | `tests/integration/test_federation_relay.py`, `FEDERATION-RELAY-PROTOCOL.md` |
| #36 incident reconciliation | `tests/integration/test_incident_reconciliation.py`, `INCIDENT-RECONCILIATION.md` |
| #37 appliance onboarding | diagnostics tests, `ONBOARDING.md`, installation/operations guides |
| #40 release supply chain | pinned CI/release workflows, release verification tools, `RELEASES.md` and checklist |
| #41 capability matrix | `docs/capabilities.toml`, generated `FEATURES.md`/README summary, CI drift check |

## Exit criteria

### P0 failure paths

Satisfied in software. Critical background task supervision, truthful watchdog health, critical
alert ordering, bounded repeats, and all-clear audience delivery have automated failure-path
coverage. The current GitHub issue list no longer contains the P0 findings #7–#10.

### Recovery and power loss

Partially satisfied. Durable admission, restart recovery, idempotent delivery, quiesced restore, and
failure injection pass in automated tests, including `test_durable_outbox.py`, backup/restore tests,
and critical orchestration coverage. A destructive power-cut campaign on the deployed storage and
radio hardware is still an explicit capability limitation. Do not mark this criterion fully field
accepted until that exercise records no silent critical-task or admitted-message loss.

### Two-node LoRa/MQTT federation regression

Partially satisfied. The recorded two-Outpost backlog covers pairing, radio sync, recovery, mail,
privacy, rejection probes, and mixed-path discovery/pairing. Automated tests cover authenticated
LoRa/MQTT observations, partition/reconnect reconciliation, stable retry counters, receipts, signed
three-node custody, and topology updates. Physical MQTT-only federation traffic and automatic
fallback remain unchecked in `FEDERATION-ACCEPTANCE-BACKLOG.md`; this blocks the literal field exit
criterion.

### Mobile, theme, and accessibility CI

Satisfied. CI installs Chromium on Python 3.12 and 3.13 and runs the full Pytest suite. The browser
matrix covers every operator page across mobile/tablet/desktop viewports and dark/daylight/night
themes, WCAG axe rules, keyboard/touch behavior, visual baselines, console/page errors, API health,
shared offline maps, and critical operator workflows.

### Truthful maturity documentation

Satisfied. `docs/capabilities.toml` is the source of generated maturity claims. CI rejects stale
`FEATURES.md` or README output and requires every evidence path to exist. Simulation-only relay and
topology work and the remaining destructive/physical acceptance gaps are stated as limitations;
test count is not presented as production readiness.

## Close decision

Keep #44 open as a field-acceptance tracker. Software remediation is ready for review, but its exit
criteria are not all satisfied until both of these are recorded:

1. destructive power interruption and recovery on deployment hardware; and
2. physical two-node MQTT-only traffic plus automatic LoRa/MQTT fallback through a partition,
   reconnect, and application receipt cycle.

After those runs, refresh the capability verification dates/evidence, rerun the release checklist,
and attach the resulting logs to the relevant issues before closing #44.
