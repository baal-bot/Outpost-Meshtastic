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
