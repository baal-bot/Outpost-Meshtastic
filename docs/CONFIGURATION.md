# Configuration

Outpost reads YAML from `OUTPOST_CONFIG`, defaulting to `config/config.yaml` for source runs. The
system service sets `/etc/outpost/config.yaml`. Models reject unknown keys and invalid ranges.

## Safe editing

```sh
sudo cp /etc/outpost/config.yaml /etc/outpost/config.yaml.before-edit
sudoedit /etc/outpost/config.yaml
sudo systemctl restart outpost
sudo journalctl -u outpost -n 80 --no-pager
```

The dashboard exposes selected settings; YAML remains the complete configuration surface. Runtime
settings saved in the dashboard should be reviewed alongside the file.

## Sections

### `node`

- `name`: instance name such as `Pittsburgh Outpost`.
- `short_name`: 1–4 UTF-8 bytes.
- `operator_contact`: public operational contact.
- `timezone`: installed IANA zone such as `America/New_York`; the appliance requires the operating
  system `tzdata` package and rejects unknown zones at startup.
- `locale`: `ll` or `ll_CC` form such as `en` or `en_US`.
- `units`: `metric` or `imperial`.
- `location`: optional `{lat, lon}` for maps and providers.
- `disclaimer`: safety text included where appropriate.

Avoid publishing a private residential coordinate for a personal installation.

### `radio`

Select `serial`, `tcp`, or `ble` and configure the matching subsection. Serial is preferred for a
fixed node. `federation_portnum` is a private Meshtastic application port from 256–511. Only
explicitly trusted bridge node IDs belong in `bridge_node_ids`.

### `channels`

Keys are Meshtastic channel indices 0–7. Each controls display name, AI access, BBS policy (`none`,
`read_only`, `full`), alerts, and report acceptance. Broadcast commands received on an index not
listed here are rejected. Direct messages are governed by module and member-trust policy instead
of the broadcast channel map.

- `bbs`: `none` blocks board access, `read_only` permits browsing, and `full` permits authorized
  writes. Actions such as `NEW` that change read state count as writes.
- `accept_reports`: permits incident creation and member updates (`REPORT`, `CONFIRM`, `DISPUTE`,
  and `ACK`) from the channel.
- `alerts`: permits official-warning and incident views plus responder alert/event operations from
  the channel. It does not independently enable report intake.
- `ai`: permits AI commands from the channel; AI remains available by DM when its module is enabled.

The radio configurator shows these effective rules per slot and warns when an active radio slot has
no policy or a configured policy slot is inactive. Its guarded workflow can change the radio's
region, modem preset, frequency slot, channel settings/PSK, identity, position, and MQTT state. It
does not edit Outpost's YAML channel policy; change that policy first before disabling a referenced
radio slot.

### `modules`

Disable unused modules. Environment and AI have additional validation: environment needs a real
user agent, and an enabled non-null AI provider needs a base URL.

Module flags are startup policy, not live switches. Change `modules.<name>.enabled` in the YAML (or
the matching environment override) and restart `outpost.service`. The dashboard marks disabled
navigation and capability cards, direct page visits explain the disabled state, and module APIs
return `409 module_disabled` with `restart_required_to_change: true`.

| Module | Disabled behavior |
|---|---|
| `bbs` | Board commands, moderation, digests, board APIs, and federated board exchange stop. |
| `watch` | Incident/check-in/alert commands, schedulers, APIs, and federated safety records stop. |
| `env` | Weather/forecast/geo commands, provider polling, APIs, and peer provider service stop. |
| `fed` | Discovery, pairing, sync, relay mail/services, background work, and federation APIs stop. |
| `ai` | AI navigation/API capability is unavailable; core deterministic features are unaffected. |

Mail, identity, member directory, radio safety/governance, backups, and system operations are core
surfaces and do not depend on the BBS flag.

### `airtime`

The governor enforces an Outpost-originated budget, reserve, channel-utilization ceiling, minimum
gap, multipart pacing, queue capacity, and traffic-class shares. Budget plus reserve may not exceed
20% and must stay below the utilization ceiling. Conservative settings protect other mesh users.

### `router`

`intents_file` is the operator-authored tolerant-language map. Missing or unparseable files and
malformed entries start the service visibly degraded; diagnostics report `ready`, `empty`,
`partial`, `rejected_all`, `error`, or `missing` with content-safe entry reasons. Failed reads are
retried without requiring a timestamp change. Correct the YAML or regex and rerun readiness.

### `ai`

The provider is one of `hailo_vlm`, `hailo`, `llamacpp`, `ollama`, `openai_compat`, or `null`.
`hailo_vlm` loads its compiled HEF from `ai.hailo_vlm.model_path`; production models belong under
`/var/lib/outpost/models`. HTTP providers have a base URL, configured context fallback,
tool-capability flag, and optional API-key environment-variable name. A selected provider context
below 1,600 tokens is invalid.

Runtime bounds include a 45-second timeout, concurrency of one, queue depth of three, at most two
tool rounds, a 220-token output ceiling, keep-warm policy, and circuit breaker.
The token budget reserves system, tool, evidence, history, question, output, and at least 15%
safety-margin allocations. The future dashboard may tune bounded values but cannot remove safety,
grounding, attribution, or emergency clauses.

`openai_compat` is an explicit external-data choice. Put its secret only in the environment named
by `api_key_env`; never place a credential in `base_url` or commit it. See [Local AI](AI.md) for
hardware setup and the mandatory benchmark.

### `env`

Set a descriptive `user_agent` with operator contact. Refresh, cache age, timeout, earthquake
radius, and review magnitude are bounded.

`env.same` controls the receive-only NOAA Weather Radio decoder:

| Key | Meaning |
|---|---|
| `enabled` | Starts the receiver only when this and `modules.env.enabled` are true. |
| `frequency_mhz` | One of the seven NWR channels from 162.400 through 162.550 MHz. |
| `county_codes` | Six-digit SAME location codes; required when enabled. `000000` national messages are always relevant. |
| `device` | Prefer the RTL-SDR serial printed by `rtl_eeprom`, not an unstable device index. |
| `sample_rate` | Decoder PCM rate; 48,000 is the tested default. |
| `oversampling` | `rtl_fm` demodulation oversampling; 4 is the tested default. |
| `gain_db` | `null` uses automatic gain. Set measured dB only when field testing requires it. |
| `ppm` | Tuner frequency correction, from -200 to 200. |
| `signal_rms_threshold` | Audio level that counts as a received signal. |
| `audio_stall_seconds` | Restarts a hung pipeline after this many seconds without PCM audio. |
| `silence_alarm_minutes` | Marks health `no_signal` after sustained below-threshold audio. |
| `restart_initial_seconds`, `restart_max_seconds` | Bounded exponential restart backoff. |
| `rtl_fm_path`, `samedec_path` | Decoder executable names or explicit paths. |

The installer supplies `rtl_fm` and a checksum-pinned `samedec` when SAME is enabled. Required
tests and demo messages are log-only. A live county-matched warning remains pending until an
operator approves it in Environment; approval then uses the normal alert policy and airtime gate.

### `watch`

Controls position freshness, duplicate radius/window, alert repetition, and escalation stages.
Terminal incident state changes require a trusted operator or responder. Emergency keyword
matching is off by default. Enabling it requires a response policy, false-positive review, and
operator training.

### `fed`

Controls frame limits, discovery/sync cadence, batch size, stale peers, incident radius, and
whether the Meshtastic radio's MQTT module may be used. Broker, topic, and MQTT discovery policy
are managed on the radio rather than duplicated in Outpost YAML. Discovery never grants trust.
See [Federation](FEDERATION.md).

### `web`

Named-account password authentication is always enabled; account roles, MFA, and sessions are
managed in the Access workspace rather than YAML. `auth.session_hours` sets the absolute browser
session lifetime. The remaining `auth` keys bound failed sign-ins by source, account name, and
global rate within `failure_window_seconds`; delays grow from `throttle_base_seconds` to
`throttle_max_seconds`, while correct credentials remain usable to prevent hostile account
lockout. `metrics_access` defaults to `authenticated`; `loopback` supports a local
Prometheus process, while `disabled` returns 404. `transport.mode` explicitly selects offline
`trusted_http`, Outpost-terminated `direct_https`, or `trusted_proxy`. HTTPS is optional and never
required for radio/mesh operation. Forwarded client/scheme headers are accepted only from
configured proxy networks. See [Web transport and network boundary](WEB-TRANSPORT.md).

### `store`

Controls SQLite path, offline tiles, maintenance hour, retention, and backup count. The service
account needs write access to the database parent directory. `store.tiles_path` is the absolute
directory containing `manifest.json` and the bounded raster hierarchy; it defaults to
`/var/lib/outpost/.data/tiles` and may point at separate storage. Relative tile paths are rejected
so service working-directory changes cannot silently hide the pack. Startup and the map UI report
the distinction between missing and unreadable packs. `maintenance_batch_rows` (250) is the
maximum rows deleted in one writer transaction; `maintenance_max_rows` (10,000) bounds a complete
daily run. The engine rotates fairly across eligible domains when the run limit is reached.

`retention.member_positions_hours` controls how
long the latest exact POS share is retained (default 168 hours, range 1–720). Each new share stores
its own deletion time. Past-due positions are excluded immediately from maps, commands, welfare,
and safety processing; daily maintenance physically deletes them. Existing positions from before
the expiry migration are expired on upgrade and require a fresh member share to reappear.

The remaining retention keys govern BBS/mail, authentication, digests, terminal watch history,
provider history/cache, completed federation service/delivery history, and terminal durable
outbound work. Active incidents/events, pending federation approvals, live deliveries, identity,
configuration, and audit evidence are not age-deleted. See
[Data retention and storage](RETENTION.md) for the table-by-table contract and capacity estimates.

## Environment overrides

Nested keys use double underscores; values are JSON-decoded when possible:

```sh
OUTPOST__WEB__PORT=8081
OUTPOST__NODE__UNITS='"imperial"'
OUTPOST__MODULES__FED__ENABLED=true
OUTPOST__NODE__LOCATION='{"lat":40.4406,"lon":-79.9959}'
```

Use a protected systemd environment file or override. Never commit credentials, channel keys, or
precise private positions.

## Removed no-effect settings

Outpost rejects unknown settings so a stale value cannot appear active while doing nothing. The
following pre-release keys were removed because they never had a runtime consumer:

- `airtime.broadcast_max_per_hour`, `airtime.coalesce_window_s`
- `router.page_sizes` (response sizes remain command-specific; `page_ttl_minutes` is configurable)
- `ai.max_tool_rounds`, `ai.cold_placeholder_enabled`, `ai.cold_placeholder_threshold_s`, and
  `ai.embeddings.*`
- `security.handle_change_per_hours`, `security.handle_reserve_days`
- `mail.notify_window_hours`, `watch.self_resolve_hours`
- `fed.mqtt.discovery_enabled`, `fed.mqtt.server`, `fed.mqtt.port`, `fed.mqtt.topic_root`
- `web.auth.mode` (named-account authentication is always enabled)

Remove these keys before upgrading. Position freshness and incident duplicate detection are live
through `watch.position_max_age_minutes`, `watch.dedupe_radius_m`, and
`watch.dedupe_window_minutes`.

## High-risk settings

- MQTT carries Meshtastic traffic through infrastructure outside local control.
- AI can be incorrect and must not autonomously trigger emergency action.
- Provider data can be delayed; preserve timestamps and provenance.
- SAME is an additional warning source, not an emergency service; antenna, frequency, county code,
  silence state, and receiver restarts require routine checks.
- Federation trust permits bounded exchange and must be reviewed per peer.
