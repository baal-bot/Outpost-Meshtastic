# August 2026 red-team remediation exit audit

This document audits the exit criteria in GitHub issue #44 against the current working tree. It is
an evidence index, not permission to close field-gated work. The original review baseline was
`d6f0307`; the current capability manifest and CI run against the revision under review.

## Tracker status

All software findings linked by #44 are closed upstream. This includes the original review issues
#2 and #7–#43 and the August 28 follow-up issues #47–#60. Their implementations, tests, operational
evidence, and issue-specific closeout notes are in the repository and GitHub history. The latest
follow-up snapshot passed 656 tests with one environment-dependent skip, every declared critical
coverage floor, both supported Python versions in hosted CI, the browser regression suite, package
smoke installation, and dependency audit.

The tracker remains open only for the field evidence below. Closing linked software findings does
not convert simulated or automated coverage into evidence from a destructive power cut or a
physical two-node transport transition.

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

Keep #44 open as a field-acceptance tracker. Software remediation is complete, but its exit criteria
are not all satisfied until both of these are recorded:

1. destructive power interruption and recovery on deployment hardware; and
2. physical two-node MQTT-only traffic plus automatic LoRa/MQTT fallback through a partition,
   reconnect, and application receipt cycle.

After those runs, refresh the capability verification dates/evidence, rerun the release checklist,
and attach the resulting logs to the relevant issues before closing #44.
