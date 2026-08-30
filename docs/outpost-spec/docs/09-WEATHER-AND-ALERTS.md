# 09 — Weather, Public Alerts & Geo

**Status:** Baseline · **Phase:** 4 · **Prerequisite:** [08-COMMUNITY-WATCH.md](08-COMMUNITY-WATCH.md)
**Implements:** `src/outpost/env/`

---

## 1. Design stance

Weather is the highest-demand feature on every community mesh, and the one most likely to be
needed exactly when the internet is gone. So the architecture inverts the usual assumption:

**REQ-WX-001** — The **offline** path is primary. Over-the-air SAME/EAS decoding (§5) and
locally-computed astronomy (§7) work with the WAN down and have no rate limits, no terms of
service, and no staleness. Internet APIs are the *enrichment* layer.

**REQ-WX-002** — Every weather or alert value transmitted **MUST** carry its age when older
than `env.fresh_threshold_minutes` (default 60). `WX (4h old): …` is honest; an unlabelled
stale forecast is dangerous.

**REQ-WX-003** — A cached alert **MUST NOT** be transmitted after its CAP `expires` time,
under any circumstance, regardless of cache policy. Rebroadcasting an expired tornado
warning is actively harmful.

---

## 2. Providers

**REQ-WX-004** — Weather providers **MUST** be behind an interface with at least two
implementations plus a cache-only mode:

```python
class WeatherProvider(Protocol):
    name: str
    async def current(self, loc: LatLon) -> CurrentConditions | None: ...
    async def forecast(self, loc: LatLon, days: int) -> Forecast | None: ...
    async def hourly(self, loc: LatLon, hours: int) -> HourlyForecast | None: ...
    async def alerts(self, loc: LatLon) -> list[CapAlert]: ...
    def attribution(self) -> str | None: ...
```

### 2.1 NWS / `api.weather.gov` (default in the US)

| Aspect | Requirement |
|---|---|
| Point resolution | `GET /points/{lat},{lon}` → cache long-lived (default 90 days) and revalidate; it yields the forecast/hourly/grid/zone/station URLs and changes only when a WFO re-grids. **Not** cached forever — the same reasoning as REQ-WX-016 applies |
| Forecast | `GET /gridpoints/{wfo}/{x},{y}/forecast` |
| Hourly | `GET /gridpoints/{wfo}/{x},{y}/forecast/hourly` |
| Observations | `GET /stations/{id}/observations/latest` |
| Alerts | `GET /alerts/active?point={lat},{lon}` |
| Auth | None, but a `User-Agent` header is **mandatory** — omitting it returns 403 |
| Rate limit | Not published; "generous for typical use". Retry after ~5 s on 429 |
| Caching | Honour `Cache-Control` and `Last-Modified`; use `If-Modified-Since` → 304. Do **not** cache-bust by appending arbitrary query parameters — the API validates parameters strictly and unrecognised ones can be rejected outright |
| Licensing | Public domain. May not imply NWS endorsement or present modified content as official |

**REQ-WX-005** — `env.user_agent` **MUST** be config-driven in the NWS-recommended form
`(node-domain-or-name, operator-contact)` and startup **MUST** fail if the AI/weather module
is enabled with a placeholder value still in place.

**REQ-WX-006 (critical geo-filtering rule)** — Alerts **MUST** be queried by `point=` or by
**county** UGC, **never** by forecast-zone UGC alone. NWS documents that zone-UGC queries do
not return county-based alerts, which includes polygon warnings such as Tornado and Severe
Thunderstorm. Getting this wrong silently drops the most important alerts.
([NWS Geolocation guidance](https://www.weather.gov/media/documentation/docs/NWS_Geolocation.pdf))

**REQ-WX-007** — Forecast refresh **MUST** be driven by the response's own `updateTime` and
`Last-Modified`, not by an assumed schedule. NWS makes no hard guarantee about update cadence.

### 2.2 Open-Meteo (default outside the US; US fallback)

| Aspect | Requirement |
|---|---|
| Endpoint | `GET https://api.open-meteo.com/v1/forecast` |
| Auth | None on the free tier |
| Limits | Published free-tier limits are per-minute, per-hour and per-day (of the order of 600/min, 5 000/h, 10 000/day). **Re-read the current terms at implementation time** — Outpost's polling is far below any of them |
| Licence | Data CC-BY 4.0 — **attribution required** |
| Terms | Free tier is **non-commercial only** |

**REQ-WX-008** — When Open-Meteo data is used, the attribution "Weather data by
Open-Meteo.com" **MUST** appear on the dashboard footer and in `ABOUT`. It **MUST NOT** be
appended to every 200-byte forecast message — that would spend ~15% of the packet on a
credit. Attribution out-of-band satisfies the licence without burning airtime.

**REQ-WX-009** — The operator **MUST** be warned in config comments and on the dashboard that
the free tier is non-commercial, so a commercial deployment configures the paid endpoint.

### 2.3 USGS earthquakes

**REQ-WX-010** — Seismic data **MUST** come from the GeoJSON summary feeds
(`https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/{2.5|all}_hour.geojson`), polled
no more than once per 60 s — USGS updates these feeds at about that cadence, so polling
faster returns nothing new — with `Accept-Encoding: gzip`, honouring the `Expires` header,
and filtering by radius locally. USGS explicitly directs automated clients to the real-time
feeds rather than the FDSNWS query API.

**REQ-WX-011** — Change detection **MUST** compare each event's `updated` property, not the
whole payload.

### 2.4 IPAWS

**REQ-WX-012** — FEMA IPAWS **MUST NOT** be an assumed data source. The All-Hazards
Information Feed requires an IPAWS User Portal account and a signed Memorandum of Agreement
with the IPAWS Office. The spec supports it as an optional, operator-configured source for
organisations that hold an MOA; the default deployment gets the NWS half of IPAWS content via
`api.weather.gov`, and the non-weather EAS content via SAME (§5).

---

## 3. Caching and offline

**REQ-WX-013** — Every provider response **MUST** be cached in `wx_cache` (doc 05 §7) with
`fetched_at`, the provider's own `source_updated_at`, `etag`, and `last_modified`.

**REQ-WX-014** — Staleness policy **MUST** be per data kind, and serving beyond `max_age`
**MUST** be refused rather than answered with confidently-wrong data:

| Kind | Serve fresh | Serve with age label | Refuse beyond |
|---|---|---|---|
| Active alerts | ≤5 min | ≤30 min, always with age | **30 min** |
| Current conditions | ≤60 min | ≤6 h | 12 h |
| Hourly forecast | ≤2 h | ≤12 h | 24 h |
| Daily forecast | ≤6 h | ≤36 h | 72 h |
| Seismic | ≤15 min | ≤6 h | 24 h |

**REQ-WX-015** — Pre-cached static reference data **MUST** be fetched on first WAN
availability and refreshed on a slow schedule (quarterly), clipped to the operator's
configured region:

| Data | Source |
|---|---|
| SAME county code list | `https://www.weather.gov/source/nwr/SameCode.txt` |
| Zone↔county correlation | `https://www.weather.gov/gis/ZoneCounty` |
| Public forecast zone polygons | `https://www.weather.gov/gis/PublicZones` |
| County boundaries | `https://www.weather.gov/gis/Counties` |
| `/points/{lat},{lon}` for each served location | `api.weather.gov` |

**REQ-WX-016** — Zone and county boundary files carry scheduled effective dates. The node
**MUST** record which version it holds, **MUST** re-check twice yearly, and **MUST** warn the
operator when the held version is superseded — a stale UGC→county map silently misroutes
alerts.

**REQ-WX-017** — Region clipping **MUST** be applied at ingest. National boundary shapefiles
run to tens of megabytes; a single state clipped and simplified to GeoJSON should land well
under a megabyte, and **MUST** stay under the 5 MB budget of acceptance criterion 14. Measure
the actual figure during Phase 4 rather than assuming it.

**REQ-WX-018** — All outbound HTTP **MUST** use conditional requests (`If-Modified-Since` /
`If-None-Match`) so a flaky or metered link burns almost nothing when nothing changed.

**REQ-WX-019** — Outbound HTTP **MUST** be restricted to an allowlist of configured provider
hosts. Outpost makes no other network calls (doc 01 §5 N6).

---

## 4. CAP alert ingest

**REQ-WX-020** — Ingested CAP alerts **MUST** be filtered before they can generate a mesh
broadcast. Default gate:

```
status   == "Actual"
AND severity ∈ {Extreme, Severe}
AND urgency  ∈ {Immediate, Expected}
AND certainty != "Unlikely"
AND geographically relevant to the node's region
```

**REQ-WX-021** — The gate **MUST** be operator-configurable per field, and the dashboard
**MUST** show which alerts were ingested but withheld by the gate, so the operator can tune
it against reality.

**REQ-WX-022** — Geographic relevance **MUST** be evaluated as: point-in-polygon against the
alert's `polygon` when present, else membership of the node's county SAME/UGC codes in the
alert's `geocode` block. Polygon warnings **MUST** take precedence.

**REQ-WX-023** — `msgType` handling **MUST** be implemented, not ignored:

| `msgType` | Behaviour |
|---|---|
| `Alert` | New alert; broadcast if the gate passes |
| `Update` | Supersede the prior alert (Governor supersession); rebroadcast only if severity increased or the area changed materially |
| `Cancel` | Cancel; broadcast an all-clear if the original was broadcast |
| `Ack` / `Error` | Log only |

An operator-approved CAP alert and its Watch headline **MUST** retain the source CAP
`expires` instant. Approval **MUST** refuse an alert whose expiry is already past. When a
provider omits `expires`, the inbox **MUST** identify that omission and show the documented
six-hour fallback used for both records; an invalid supplied expiry remains ineligible.

**REQ-WX-024** — CAP `description` **MUST NOT** be broadcast by default. It expands to 4–8
packets. The broadcast carries `event` + `areaDesc` + expiry; the description is available on
request (`WARN <n>`) and on the dashboard.

**REQ-WX-025** — Ingested alerts **MUST** be attributed to their source in the broadcast text
and **MUST NOT** be presented as originating from the node:
`⚠⚠NWS Tornado Warning · Windham Co · until 17:45 · CRO relay`

**REQ-WX-026** — Deduplication **MUST** be by CAP `identifier` where present, and by
`(event, geocode, expires)` for SAME-radio ingest, which carries no CAP identifier.

**REQ-WX-027** — Alert polling cadence: 60–120 s when WAN is available and the node's region
has any active alert; 5 min otherwise. Forecast polling: 15 min. Both config-driven.

---

## 5. SAME / NOAA Weather Radio (offline alert path)

**REQ-WX-028** — The node **SHOULD** support an optional RTL-SDR receiving NOAA Weather Radio
and decoding SAME/EAS headers. This is the only alert path that survives a total internet
outage with zero latency and zero rate limits, and it is what makes the "community watch"
claim credible during a real event.

**Hardware:** RTL-SDR v3/v4, a 162 MHz-tuned antenna (a quarter-wave ~46 cm or a marine VHF
whip; the stock telescopic is marginal), optionally a 162 MHz band-pass filter, and a
sufficient power supply — an under-powered Pi browns out the dongle and drops packets
silently.

**Channels:** 162.400, 162.425, 162.450, 162.475, 162.500, 162.525, 162.550 MHz.

**REQ-WX-029** — The decoder **MUST** be a supervised subprocess pipeline, not an in-process
DSP implementation. Reference chain:

```
rtl_fm -f 162.550M -s 22050 - | samedec -r 22050
```

`samedec` ([cbs228/sameold](https://github.com/cbs228/sameold), Rust, prebuilt `aarch64`
binaries) is preferred: it demodulates and decodes in one step, emits one message per line,
and exposes `SAMEDEC_EVENT` / `SAMEDEC_SIGNIFICANCE` / `SAMEDEC_LOCATIONS` to a child
process. `multimon-ng -a EAS` + `dsame3` is the documented alternative.

**REQ-WX-030** — The supervisor **MUST** restart the pipeline on exit with backoff, monitor
for silence (no decode *and* no signal-level indication for `env.same.silence_alarm_minutes`,
default 720) and surface `same: no signal` on the dashboard — a dead SDR that fails silently
is worse than no SDR.

**REQ-WX-031** — Decoded SAME messages **MUST** be filtered against the node's configured
county SAME codes before generating an alert, and **MUST** map SAME event codes to the CAP
model so downstream handling is uniform.

**REQ-WX-032** — SAME-derived alerts **MUST** be deduplicated against `api.weather.gov`
alerts (REQ-WX-026) — the same warning will arrive on both paths, typically SAME first.

**REQ-WX-033** — SAME test messages (`RWT`, `RMT`, and `Test` significance) **MUST NOT**
generate a mesh broadcast by default, **MUST** be logged, and **MUST** appear on the
dashboard as proof the path is alive.

**REQ-WX-034** — The SAME subsystem **MUST** be entirely optional. Absence of an SDR
**MUST NOT** affect startup or any other feature.

---

## 6. Rendering weather

**REQ-WX-035** — Weather output **MUST** follow the terse register (doc 04 §7) and fit one
part by default. Multi-part forecasts **MUST** be an explicit opt-in per request
(`FC 5 -long`), never the default.

> Community precedent supports this: existing Meshtastic weather bots converge on a ~200-byte
> cap, and several ship both "single message" and "multi message" command variants precisely
> because busy meshes cannot afford the latter. They also insert a delay of 10–15 s between
> parts, for the ordering reason in REQ-TRANSPORT-034.

**Reference renderings:**

```
WX
> Now 8C ovc W14g22 · Tonight rain 80% lo 5C · Tue 11C/3C shwrs
NWS 41m

FC 3
> Tue 11/3 shwrs 60% · Wed 9/1 pcldy · Thu 12/4 rain 80% W20g30
NWS 41m

WARN
> ⚠⚠NWS Tornado Warning Windham Co until 17:45
! Flood Watch until Wed 08:00
WARN 1 for detail

SUN
> Rise 06:12 Set 19:48 · Civil twi 05:44/20:16 · Moon 68% waxing gib
```

**REQ-WX-036** — Units **MUST** be a per-member preference (`prefs.units`: `metric` |
`imperial`) defaulting to the node's configured locale, and **MUST** be omitted from output
where unambiguous in context.

**REQ-WX-037** — A fixed abbreviation table **MUST** be used (`ovc`, `pcldy`, `shwrs`, `tstm`,
`g` for gusts, `W14` for wind direction+speed) and documented in `HELP WX`. Ad-hoc
abbreviation is prohibited (REQ-CMD-024 rule 4).

---

## 7. Astronomy (fully offline)

**REQ-WX-038** — Sunrise, sunset, civil/nautical twilight, moon phase and illumination
**MUST** be computed locally from the node's or the member's coordinates using a small pure
Python implementation (NOAA solar position algorithm; Meeus for lunar phase). No network, no
heavy dependency.

**REQ-WX-038a** — At latitudes and dates where the sun does not rise or set, the
computation **MUST** return an explicit "no sunrise" / "no sunset" result rather than a
sentinel time, a `NaN`, or an exception, and `SUN` **MUST** render it as such
(`> Polar day · no sunset · Moon 68% waxing gib`). This is the one astronomy case that has
no numeric answer, and it is the case a naive implementation crashes on.

**REQ-WX-039** — `SUN` **MUST** work with the WAN down and the weather providers unreachable.
It is the demonstration that "offline is the design case".

---

## 8. Geo services

**REQ-WX-040** — `POS [handle]` **MUST** return position, distance, and bearing computed by
haversine, subject to the position-privacy rules in doc 12 §8.

**REQ-WX-041** — `WP <name>` **MUST** save a community waypoint at the member's current
position. Waypoints are visible on the map, usable as a location in `REPORT`
(REQ-WATCH-008), and listable with `WPS`.

**REQ-WX-042** — Waypoints **MAY** be published to the mesh as Meshtastic
`WAYPOINT_APP` packets so they appear natively in users' Meshtastic clients. This
**MUST** be opt-in per waypoint, rate-limited, and off by default.

**REQ-WX-043** — Coordinate input **MUST** accept decimal degrees (`44.123,-72.567`),
space-separated (`44.123 -72.567`), and degrees-minutes (`44 07.38N 72 34.02W`). Output is
always decimal degrees to 5 places (≈1 m) or fewer per the privacy rules.

**REQ-WX-044** — All geo maths **MUST** be unit-tested against known reference pairs,
including antimeridian and polar edge cases, and **MUST NOT** use a flat-earth approximation
for distances over 1 km.

---

## 9. Failure behaviour

| Failure | Behaviour |
|---|---|
| WAN down | Serve cache with age labels; refuse beyond `max_age`; SAME path continues; `SUN` unaffected |
| NWS 403 | Almost certainly a missing/malformed `User-Agent`. Log a specific, actionable error naming REQ-WX-005 |
| NWS 429 | Back off 5 s, then exponential; halve poll cadence for 1 h |
| Provider returns malformed JSON | Discard, keep prior cache, count metric, do not crash the poller |
| SDR unplugged | `same: no signal`; alert path degrades to API-only; dashboard warning |
| Node has no configured location and the member has no GPS | `WX` responds `No location. Send POS <lat,lon> or ask @ray to set the node location.` |
| Boundary data superseded | Warn on dashboard; continue with the held version; never silently drop alerts |

---

## 10. Metrics

```
outpost_wx_fetch_total          counter   {provider,kind,outcome}
outpost_wx_cache_hits_total     counter   {kind}
outpost_wx_cache_age_seconds    histogram {kind}
outpost_wx_stale_refusals_total counter   {kind}
outpost_cap_ingested_total      counter   {source,severity,gated}
outpost_cap_broadcast_total     counter   {severity}
outpost_same_decodes_total      counter   {event,significance}
outpost_same_pipeline_up        gauge
outpost_same_last_decode_seconds gauge
```

---

## 11. Acceptance criteria (Phase 4 exit)

| # | Criterion |
|---|---|
| 1 | `WX`, `FC`, `SUN`, `WARN`, `QUAKE` all return single-part responses within their byte budget |
| 2 | With the WAN interface down, `WX` serves cached data with a correct age label |
| 3 | Beyond `max_age`, `WX` refuses rather than serving stale data |
| 4 | A CAP alert past its `expires` is never transmitted, even if present in cache |
| 5 | Alerts are queried by `point=`; a county-based polygon warning is correctly received (REQ-WX-006) |
| 6 | A CAP `Update` supersedes a queued repeat; a `Cancel` produces an all-clear |
| 7 | NWS requests carry a valid `User-Agent` and use conditional requests; a 304 is handled |
| 8 | Open-Meteo attribution appears on the dashboard and in `ABOUT`, and **not** in per-message text |
| 9 | With an RTL-SDR attached, an injected SAME test tone decodes, is logged, and is **not** broadcast |
| 10 | A SAME-decoded live warning is deduplicated against the same warning from the NWS API |
| 11 | The SDR pipeline restarts automatically after being killed, and silence raises a dashboard warning |
| 12 | `SUN` is correct to within 60 s against a reference implementation for 5 mid-latitude locations, **and** returns an explicit no-sunrise/no-sunset result for a polar case rather than a number (REQ-WX-038a) |
| 13 | Distance/bearing match reference values including an antimeridian case |
| 14 | Region-clipped zone/county GeoJSON for a single state is under 5 MB on disk, and typically well under 1 MB |
