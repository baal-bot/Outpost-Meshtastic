# Safety readiness self-check

Outpost readiness is semantic: a running task is not considered proof that an urgent message
can reach somebody. The checker runs at startup, after scheduled maintenance, from the operator
dashboard, and whenever `outpost-diagnostics` collects a bundle.

A failed safety check creates a persistent dashboard banner and an actionable system conversation
in the operator inbox. Operations and configuration failures make the report degraded without
masking the more urgent safety state. Every result includes impact, remediation, and content-safe
evidence. Prometheus exports `outpost_self_check_state{check,severity}` for each row below.

## Capability checklist

| Check | Severity | Capability proved |
| --- | --- | --- |
| check: `boot_schema` | Operations | The supported, persistently enabled, active boot selection has sufficient packaged migration capacity for the current database. Unknown evidence does not pass; this is not a reboot test. |
| check: `responder_audience` | Safety | At least one active responder or operator radio can receive targeted urgent traffic. |
| check: `escalation_audiences` | Safety | Every configured caution, urgent, and critical escalation stage resolves to a destination. |
| check: `maintenance_freshness` | Operations | Retention maintenance has a valid completion record no more than 48 hours old. |
| check: `backup_rotation` | Operations | The snapshot inventory does not exceed `store.backup.keep`. |
| check: `radio_power` | Operations | The connected radio reports no battery or remains above the configured warning threshold. |
| check: `alert_delivery_history` | Safety | No alert escalation stage recorded zero admitted deliveries during the previous seven days. |
| check: `intent_map` | Configuration | The configured tolerant-intent file exists, loads, and contains no rejected entries or regexes. |
| check: `configured_keys_effective` | Configuration | Explicitly configured keys are not among settings known to have no runtime consumer. |
| check: `timezone` | Configuration | `node.timezone` resolves through the installed IANA timezone database. |

The authenticated report is available at `GET /api/v1/readiness`; an operator can rerun it with
`POST /api/v1/readiness/run`. The diagnostics CLI uses a loopback-only trigger and embeds the
redacted report in `manifest.json` under `runtime.self_check`.

## Boot compatibility is separate from live health

The boot check compares the current database schema with the migration files in the release
selected by `/opt/outpost/current`. It inspects the effective `outpost.service` launch without
executing that release's Python, importing its code, or opening another writable database. A healthy
checkout HTTP process cannot hide a selected release whose schema capacity is too old.

Evidence distinguishes `compatible`, `incompatible`, `failed`, and `unknown`. Every state other than
`compatible` fails this operations check and makes an otherwise-ready report **degraded**. Existing
safety failures still take precedence and retain their inbox notifications. Boot warnings appear
in the existing dashboard readiness banner; they do not send radio messages or create safety mail.

The supported observation is the installer's direct Python launch, ordinary environment, and one
Python 3.12/3.13 non-editable package in its venv. Missing, disabled, runtime-only-enabled, masked,
inactive, failed, unreadable, custom, ambiguous, or changing service/release evidence does not pass.
A unit awaiting daemon reload is unknown. A custom environment, environment file, or drop-in needs
operator review; this checker deliberately does not interpret arbitrary launch wrappers or imports.

`inspector_schema_cap` describes the inspecting source **on disk**, not a queried remote process or
proof of its loaded bytecode. `source_relation` compares source locations, not artifact identity;
different locations may support the same schema. `boot_location_id` is an opaque path hash used to
notice a changed selection, not a release checksum or supply-chain attestation. The exported evidence
contains no raw unit command, environment, configuration, or filesystem paths.

`outpost-diagnostics` also collects independent evidence under `runtime.boot_schema`, even if the
live backend is absent or predates this check. That field compares the CLI's configured database;
it must not be mistaken for a live-backend assertion. System bus work has a three-second deadline and
32 KiB output ceiling per observation (at most two), directory inventories are capped at 1,024
entries, and readiness runs this blocking work off the radio event loop. No new browser poll or
scheduled task is introduced; ordinary dashboard refreshes use the cached report.
The existing bounded reader pool observes schema before and after inspection; a change invalidates
the result. Client cancellation retains the single-flight run until its bounded probe exits.

A pass proves only a static capacity comparison against **this database**, not the boot unit's
configuration/database path, migration success, package integrity/import behavior, radio recovery,
network access, power autonomy, or successful unattended reboot. Results are point-in-time evidence;
rerun after a deployment or unit change. Arrange a verified deployment and validated recovery backup
to resolve incompatibility. Do not bypass the downgrade guard, restore older data over acknowledged
records, or reboot merely to clear a warning. Development database ownership and actual reboot
qualification remain tracked in #136; the broader outage-readiness assessment remains in #149.
