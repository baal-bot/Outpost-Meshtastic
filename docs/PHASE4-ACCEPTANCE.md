# Phase 4 — Environment acceptance evidence

Status: **automated reliability gate passed; field/hardware items pending**

Validated on 2026-08-24 with `ruff check src tests tools` and 90 passing pytest tests.

| # | Criterion | Status | Evidence |
|---|---|---|---|
| 1 | `WX`, `FC`, `SUN`, `WARN`, `QUAKE` use one radio part | Pass | `test_environment_gate_commands_are_single_part`; UTF-8 output is capped at 200 bytes |
| 2 | WAN-down weather serves cached data with age | Pass | `test_weather_cache_age_labels_and_safe_wan_failure` |
| 3 | Weather refuses beyond `max_age` | Pass | `test_weather_cache_age_labels_and_safe_wan_failure` |
| 4 | Expired CAP alerts never transmit | Pass | `test_cap_gate_rejects_expired_test_and_unlikely`; approval requires accepted/pending state |
| 5 | Point query and polygon relevance | Pass | CAP poll constructs `point=`; `test_cap_polygon_must_contain_outpost` |
| 6 | CAP Update supersedes; Cancel issues all-clear | Pass | `test_cap_update_supersedes_and_cancel_issues_all_clear` |
| 7 | User-Agent, conditional requests, and 304 reuse | Pass | `_request_json`; `test_provider_conditional_request_reuses_body_on_304` |
| 8 | Open-Meteo attribution in dashboard and `ABOUT`, not weather replies | Pass | Environment footer and environment-enabled command context attribution |
| 9 | Injected SAME test decodes/logs without broadcast | Software pass; tone pending | Header injection is parsed, logged once, and test-suppressed; audio pipeline awaits hardware/tooling |
| 10 | SAME warning deduplicates against NWS CAP | Partial | SAME header dedupe and county relevance pass; cross-source live warning validation awaits receiver |
| 11 | SDR restart supervision and silence warning | Partial | Durable config and silence health logic pass; subprocess/hardware restart validation awaits receiver |
| 12 | Solar reference accuracy and explicit polar behavior | Pass | Five-location independent reference matrix and explicit polar tests |
| 13 | Distance/bearing including antimeridian | Pass | `test_distance_bearing_handles_antimeridian_and_polar_routes` |
| 14 | State-clipped zone/county GeoJSON under 5 MB | Pass | Official NWS PA zone/county pack is 683,575 bytes; builder and artifact tests |

Additional exit gates:

| # | Criterion | Status |
|---|---|---|
| 4.15 | 30-day run capturing a real NWS alert end-to-end | Field observation pending |
| 4.16 | Full WAN-down day with cached `WX`, offline `SUN`, and SAME | 24-hour WAN simulation passes; SAME portion is hardware pending |

## Implemented reliability behavior

- NWS is primary and Open-Meteo is fallback, with provider health surfaced in the dashboard.
- NWS current conditions use the latest available observation station. If no station is available,
  the first hourly period is explicitly labeled as a near-term forecast rather than an observation.
- Weather values carry provider, source kind, valid time/age, and cached/availability state; missing
  measurements are never synthesized as zero.
- Current conditions and forecasts persist in SQLite and survive process restarts.
- Stale values carry age labels and are refused after the configured safety limit.
- Provider HTTP requests are host-allowlisted, identify the operator, send conditional headers,
  and reuse the held representation on HTTP 304.
- CAP alerts are review-first, geofenced, expiry-gated, deduplicated, and lifecycle-aware.
- Astronomy is computed locally and explicitly reports unavailable sunrise/sunset at polar
  locations.
- Environment command output is constrained to one 200-byte radio part.
- SAME headers are parsed, county-filtered, deduplicated, test-suppressed, persisted, and expose
  silence health before receiver activation.
- The Pennsylvania pack was built from the official NWS public-zone and county shapefiles
  valid 2026-04-16 using `tools/build_region_data.py`.

## Remaining work before the complete Phase 4 exit gate

1. Implement and validate SAME ingest when the ordered RTL-SDR hardware arrives.
2. Accumulate the 30-day NWS field observation and one full WAN-down field day.
