# Dashboard performance budget

Separate application-load evidence is recorded in
[emergency-burst qualification](EMERGENCY-BURST-QUALIFICATION.md) and
[federation paging qualification](FEDERATION-PAGING-QUALIFICATION.md). Their synthetic
admission/query timings are not physical radio delivery or sustainable RF-rate claims.

Outpost is designed to leave enough Raspberry Pi capacity for radio handling, federation, local
maps, and future on-device services. Dashboard convenience polling must not become a background
workload merely because an operator left a tab open.

## Budget

These limits apply to a warm, authenticated dashboard with no operator interaction. They are
measured over five minutes on a four-core Raspberry Pi 5. CPU is the Outpost process's share of one
core; Chromium is deliberately excluded because it runs on the operator's device.

| Resource | Visible Overview | Hidden tab |
| --- | ---: | ---: |
| Scheduled API requests | at most 130 | 0 after a 1 s settling window |
| Outpost process CPU | under 5% | under 1% |
| RSS growth | under 5 MiB | under 2 MiB |
| New SQLite read connections after initial load | at most 1, then 0 | 0 |
| Active SQLite read connections | at most 2 | at most 2 |
| External environment-provider requests after warm-up | 0 | 0 |

The operationally denser Watch and Radio pages may make at most 190 scheduled API requests in five
minutes while visible. Every page has the same hidden-tab zero-request budget. A cold Overview load
may contact the configured weather-provider chain to populate current conditions and forecast;
subsequent warm refreshes must use the environment cache until its configured expiry.

### Interactive map budget

The shared map controller has a separate operator-device budget:

| Interaction | Budget |
| --- | ---: |
| Pointer events handled in one event-loop burst | one rendered animation frame; at most two |
| Small pan with unchanged tile coverage | zero tile or marker DOM replacements |
| Pan/zoom render frame on Raspberry Pi 5 | under 16.7 ms target; 50 ms hard test ceiling |
| Marker hit-area change on hover, focus, or selection | 0 px |

`Controller.getDiagnostics()` reports frames, pointer moves, tile/marker creates and removals, live
DOM counts, total/average/max render time, and current view. Browser regression tests drive the
real Watch, Members, and Environment maps with mouse, synthetic touch, and keyboard input in every
theme.

## Implementation

`refresh-scheduler.js` owns repeating browser refresh work. It runs each task single-flight, pauses
nonessential tasks when `document.hidden` is true, adds resume jitter, and applies bounded
exponential backoff after failures. New dashboard code must register with that scheduler instead of
creating an independent interval.

Navigation module state, federation-review counts, and actionable-mail count share
`GET /api/v1/dashboard/poll`. The response supplies an ETag and returns `304 Not Modified` when the
operator's state is unchanged. SQLite reads use two long-lived reader threads/connections rather
than opening and configuring a connection for every query.

Prometheus exposes the relevant cumulative counters:

- `outpost_db_read_connections_opened_total`
- `outpost_db_read_connections_active`
- `outpost_db_read_queries_total`
- `outpost_environment_provider_requests_total{host=...}`

## Reproduce the measurement

Run the probe from a development checkout on the target host. It creates an isolated temporary
database and authenticated server, loads the real Overview in headless Chromium, measures five
visible minutes and five hidden minutes, then removes the database. It does not connect to or stop
the production radio service.

```sh
.venv/bin/python tools/dashboard_idle_probe.py --seconds 300
```

The JSON result includes request counts by API path, process CPU, resident memory growth, SQLite
queries and connection opens, provider calls, browser errors, kernel, and architecture. Treat a
budget regression as a release failure even when functional tests still pass.

## Raspberry Pi 5 baseline

Baseline captured 2026-08-26 on `aarch64`, Linux `6.18.34+rpt-rpi-2712`, four Cortex-A76 cores.
The checked-in thresholds above include headroom for a normally loaded field node.

| Measured resource | Visible 5 min | Hidden 5 min |
| --- | ---: | ---: |
| API requests | 119 | 0 |
| Outpost process CPU | 0.37% | 0.19% |
| RSS growth | 2.58 MiB | 0.00 MiB |
| DB queries | 247 | 0 |
| New DB reader connections | 1 | 0 |
| External provider requests | 0 | 0 |

The cold-load warm-up issued five NWS HTTP requests to populate conditions and forecast and opened
one reader connection. Visible concurrent refreshes lazily opened the pool's second and final
reader; no additional connection opened during the hidden phase. Record updated measurements here
when refresh behavior or target hardware changes.

The shared map baseline was captured on the same Pi on 2026-08-26 in headless Chromium. A burst of
200 touch-pointer moves produced one render frame in 1.2 ms, preserved all eight live tile elements,
and created or removed no tiles or markers. Cold controller initialization, including eight tile
elements, peaked at 29.8 ms. The automated test keeps the wider 50 ms ceiling for loaded CI hosts.
