# Safety readiness self-check

Outpost readiness is semantic: a running task is not considered proof that an urgent message
can reach somebody. The checker runs at startup, after scheduled maintenance, from the operator
dashboard, and whenever `outpost-diagnostics` collects a bundle.

A failed safety check creates a persistent dashboard banner and an actionable system conversation
in the operator inbox. Operations and configuration failures make the report degraded without
masking the more urgent safety state. Every result includes impact, remediation, and content-safe
evidence. Prometheus exports `outpost_self_check_state{check,severity}` for each row below.
The #149 assessment distinguishes a scoped measured pass/fail from stale, unknown,
and operator-attested evidence; none of the latter three means all-ready.

## Capability checklist

| Check | Severity | Capability proved |
| --- | --- | --- |
| check: `boot_schema` | Operations | The supported, persistently enabled boot selection has sufficient packaged migration capacity for the current database. Unknown evidence does not pass; this is not a reboot test. |
| check: `responder_audience` | Safety | At least one active directory radio is eligible for responder traffic. Actual reachability is not proved by its role. |
| check: `escalation_audiences` | Safety | Every configured caution, urgent, and critical escalation stage resolves to a destination. |
| check: `maintenance_freshness` | Operations | Retention maintenance has a valid completion record no more than 48 hours old. |
| check: `backup_rotation` | Operations | The snapshot inventory does not exceed `store.backup.keep`. |
| check: `radio_power` | Operations | A fresh connected-radio sample reports external power or a battery above its warning threshold. Missing/invalid reports and future timestamps remain unknown; old samples are stale. External radio power does not prove station backup power. |
| check: `alert_delivery_history` | Safety | No alert escalation stage recorded zero admitted deliveries during the previous seven days. |
| check: `intent_map` | Configuration | The configured tolerant-intent file exists, loads, and contains no rejected entries or regexes. |
| check: `configured_keys_effective` | Configuration | Explicitly configured keys are not among settings known to have no runtime consumer. |
| check: `timezone` | Configuration | `node.timezone` resolves through the installed IANA timezone database. |
| check: `storage_reserve` | Operations | Current unprivileged byte and inode headroom meet the diagnostic floor: at least the larger of 1 GiB or 5% free bytes, and 5% free inodes. Not write endurance or outage-duration proof. |
| check: `offline_maps` | Operations | Missing/invalid/bounded-file violations fail. A readable manifest is only availability metadata; regional bounds, zooms, integrity and geographic coverage remain unqualified. |
| check: `time_confidence` | Operations | An observed in-process wall step fails. Read-only OS evidence reports synchronization, bounded process holdover or uncertainty, plus RTC hints. Physical RTC retention remains unknown; see [offline time policy](OFFLINE-TIME.md). |
| check: `backup_restore` | Operations | Off-device encrypted recovery and an isolated replacement-node restore remain unknown without qualification. Local rotation cannot pass this check. |
| check: `station_power` | Operations | Whole-station usable energy and measured load remain unknown without qualification; one radio's battery is not the host/AP/storage reserve. |
| check: `local_access` | Operations | Replacement-client access without WAN/cell remains unknown without qualification; developer HTTP is not a substitute. |
| check: `peer_path` | Operations | A bounded sample of retained exact storage receipts supplies partial historical evidence only, not a current WAN-independent community path. Old sampled receipts are stale. |
| check: `same_reception` | Operations | Installed-receiver SAME qualification remains unknown without independent evidence. An SDR connection or audio buffer is not decoding/handling proof. |

The authenticated report is available at `GET /api/v1/readiness`; an operator can rerun it with
`POST /api/v1/readiness/run`. The diagnostics CLI uses a loopback-only trigger and embeds the
redacted report in `manifest.json` under `runtime.self_check`.

## Evidence states and freshness

| State | Meaning |
| --- | --- |
| `pass` | The specific named assertion was measured and passed; not a blanket platform certificate. |
| `fail` | The check failed, or an operator reported a failed qualification observation. |
| `unknown` | Evidence is missing, unsupported, malformed or cannot be evaluated safely. |
| `stale` | The evidence expired or belongs to an earlier process/known loaded-policy context. |
| `attested` | An operator reported a successful qualification observation; not independently measured proof. |

`passed` is true only for `pass`. A measured safety failure retains the `failed`
overall status and existing safety-inbox behavior. Unverified/stale safety evidence
is separately counted as `safety_unverified`; it degrades the assessment without
pretending a fresh failure occurred. Any non-pass prevents `ready`. Missing or old-format
reports are `never_run`, and the operator dashboard warns that readiness is unknown.

The existing banner's **All readiness checks** disclosure shows every state,
impact and next step, including desktop/phone observation controls. Viewer sessions
remain excluded. Cached passes become stale after 15 minutes, a process restart,
known loaded-policy changes or an observed wall/monotonic discrepancy over five seconds.
Radio evidence expires after twice its configured sample interval plus 60 seconds;
maintenance freshness also expires independently of the report. Reads project those
states without rerunning probes or rewriting the saved report. The original
`last_run` metric stays at the actual evaluation time; the added
`outpost_self_check_evidence_state{check,state}` gauge is one-hot and follows the
returned classifications. Rerun to refresh stale measurements. Existing startup,
post-maintenance, power-change and on-demand triggers remain; no new timer is added.

Meshtastic reports external power with a battery field above 100. Outpost retains
that indication separately from battery percentage and shows **External power** on
the Radio page and in readiness. A fresh external-power sample passes `radio_power`
without a battery reading. A connected USB/serial port alone cannot substitute for
that report; absent or invalid telemetry stays unknown. The separate `station_power`
check still requires whole-station energy evidence. See [radio power](RADIO-POWER.md)
for persistence, compatibility and validation details.

This assessment includes OS time-confidence guards. Physical RTC retention, off-device
restoration and other field checks remain separate. Unknowns remain visible until
those requirements provide suitable evidence. External hardware changes are not
automatically detected, and persisted metadata is not cryptographic proof for a
restored/forked appliance lineage.

## Operator observations, not automatic qualification

An operator may record a passed/failed observation for the seven qualification
checks (`offline_maps`, `time_confidence`, `backup_restore`, `station_power`,
`local_access`, `peer_path`, `same_reception`). The explicit checkbox states that
the operator performed an approved procedure. The UI **does not perform it** or
grant permission to activate held hardware. Observations are valid for at most
24 hours from their stated time, using a stable local clock; they never become
measured passes, and cannot override a measured local failure. Rerunning readiness
does not extend that observation window. Expired/prior-process/prior-policy
observations stay stale; clock uncertainty stays unknown.

`POST /api/v1/readiness/observations` requires an authenticated operator, CSRF,
check name, `pass`/`fail` outcome, strict integer UTC `observed_at`, and the current
row's `review_token`. Unknown fields are refused. Invalid/stale decisions return
409 or input validation errors; the UI does not resubmit automatically. An uncertain
network outcome instructs the operator to refresh before retrying.

One existing `runtime_setting` slot per check retains the structured observation;
there is no free-text payload, credential, geographic content or raw path in these
new observations. Named-actor audit, observation replacement and saved-report
invalidation share one transaction. Post-commit publication invalidates the in-memory
report even if the requester is cancelled. Two operators cannot overwrite an unseen
observation with the same token. The token/generation is an opaque optimistic guard,
not authorization or an evidence signature. No schema migration is needed.

## Bounded, non-disruptive collection

The local probes read at most 64 KiB of a regular, non-symlink map manifest; FIFOs,
oversize and unreadable metadata fail without a directory walk. Even a declared
single tile or a valid image header cannot pass regional coverage. Filesystem
counters describe current free space only. Peer evidence samples at most 100 dispatch
rows from at most 20 active sharing peers; it is not an exhaustive receipt inventory,
current connectivity test, carrier proof or continued-retention guarantee.

Filesystem inspection runs outside the radio event loop, keeping the existing
single-flight cancellation boundary. Readiness sends no RF, queries no WAN service,
tunes/restarts no radio, changes no network, restores/migrates no database and performs
no power cut, reboot, map download or destructive exercise. Its normal writes remain
local report metadata and safety inbox notifications; operator observations add
only the audited metadata transaction described above.

## Boot compatibility is separate from live health

The boot check compares the current database schema with the migration files in the release
selected by `/opt/outpost/current`. It inspects the effective `outpost.service` launch without
executing that release's Python, importing its code, or opening another writable database. A healthy
checkout HTTP process cannot hide a selected release whose schema capacity is too old.

Evidence distinguishes `compatible`, `incompatible`, `failed`, and `unknown`. Every state other than
`compatible` does not pass this operations check and makes an otherwise-ready report **degraded**. Existing
safety failures still take precedence and retain their inbox notifications. Boot warnings appear
in the existing dashboard readiness banner; they do not send radio messages or create safety mail.

The supported observation is the installer's direct Python launch, ordinary environment, and one
Python 3.12/3.13 non-editable package in its venv. Missing, disabled, runtime-only-enabled, masked,
inactive, failed, unreadable, custom, ambiguous, or changing service/release evidence does not pass.
A unit awaiting daemon reload is unknown. A custom environment, environment file, or drop-in needs
operator review; this checker deliberately does not interpret arbitrary launch wrappers or imports.
An active service can pass the static check. During normal `Type=notify` startup, `activating` can
also pass **only** when systemd's main PID is the inspecting process and its package location matches
the selected boot package. Another process cannot use that exception; it does not claim `READY=1`
or change systemd state.

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
records, or reboot merely to clear a warning. Development database ownership and the local Pi's
actual controlled reboot acceptance were completed under #136; see the separate
[September 8 qualification record](BOOT-RECOVERY-QUALIFICATION-2026-09-08.md). #149's static
assessment alone does not establish that result or qualify a later deployment.
