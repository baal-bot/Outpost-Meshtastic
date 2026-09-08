# Worldwide regional map setup

Outpost installs a small world overview (zooms 0–6) plus optional regional detail
(zooms 7–15). **There is no full-planet download option.** World overview means
orientation across the Web Mercator globe, not street detail everywhere; areas above
85.0511° north or south are outside that projection.

Refresh dashboard tabs opened before upgrading, then open **Access → Set up offline regional maps & world overview**, or `/maps.html`.
The first-run `maps_providers` checklist points to the same page. The installer creates
the configured tile directory for the service and defers selection until the radio is
running; it no longer seeds USGS raster tiles automatically.

1. Use the suggested installation location, or enter latitude and longitude. A configured
   location takes precedence. Otherwise, only a connected local radio's GPS position with
   an actual fix timestamp no more than 15 minutes old is suggested. Radio clock time,
   connection time, peer positions and fixed/unknown-age positions are not fresh GPS fixes.
   Valid last-reported coordinates can be selected manually with an explicit button.
2. Choose radius (default 20 km), detail (default z14), and download limit (default 512 MiB).
   Without a usable position, select **World overview only** and add a region later.
3. Select **Check coverage & size** while connected to the internet. Review the estimate
   and available storage, then **Download maps**. The provider can infer the approximate
   region from requested ranges. Coordinates stay in the local setup and pack metadata;
   avoid publishing those files as diagnostic evidence.
4. Wait for verification. Every expected tile, checksum, compressed payload and vector
   structure is checked before an atomic manifest selects the completed pack. Failed or
   interrupted work leaves the existing selection intact. Pause/resume uses saved staging
   data; restart does not silently resume a network download. An expired source build or
   corrupt staging requires a new plan.
5. Use the offline preview, then check the operational maps. Outside downloaded coverage,
   the world overview remains visible; zooming past the pack's detail level is labelled.
   Open tabs can continue using their previous immutable pack until they recheck the manifest.

The planner caps work at 60,000 tiles, rejects an installed estimate over 1 GiB, and allows
64 MiB–1 GiB transfer limits per download attempt. Each HTTP request is at most 4 MiB and
must return an exact `206` range with a stable strong ETag. A server returning the entire
archive is rejected before its body is read. Metadata planning has its own 32 MiB limit.
Retries and resumed attempts are bounded; completed tiles are reused. Size is an estimate,
not a fixed worldwide price: density, latitude, radius, zoom and source version affect it.
The real September 8 sample (world overview plus a 2 km Nairobi region through z12) used
about 45 MB of tile transfer and 50.8 MB of installed storage.

Storage checks reserve at least 1 GiB or 5% of the filesystem, plus staging space. Old
verified packs and interrupted staging files are retained rather than automatically deleted.
Review disk usage before repeated installations; only remove obsolete packs after open
clients no longer need them. Configure an absolute `store.tiles_path`; setup, serving and
readiness all use that path, including packaged installations.

## Prepare elsewhere and import from USB

The same installed Outpost package provides `outpost-maps`. Use a directory writable by
the current user on the connected computer. Example coordinates below are a public sample,
not the station's location:

```sh
outpost-maps --tiles-path "$PWD/offline-maps" prepare \
  --latitude -1.2864 --longitude 36.8172 --radius-km 20 --max-zoom 14
# Inspect the estimate and saved plan ID, then download it:
outpost-maps --tiles-path "$PWD/offline-maps" resume PLAN_ID
outpost-maps --tiles-path "$PWD/offline-maps" export /media/USB/region.sqlite
```

Omit latitude/longitude for overview only. `prepare --download` explicitly plans and downloads
in one command. `status` shows the saved job and selected pack. An interrupted download can
be resumed with its plan ID while the source build remains available.

On the offline station, use the service account and configured tile path:

```sh
sudo -u outpost outpost-maps --tiles-path /var/lib/outpost/.data/tiles \
  import /media/USB/region.sqlite
```

Ensure that account can read the USB file and invoke the installed release's executable
(use its absolute path if `outpost-maps` is not on PATH). Import verifies and copies the pack
before selection. Export refuses to overwrite an existing destination. CLI and dashboard
operations share a process lock. The dashboard API does not accept arbitrary filesystem
paths or arbitrary download URLs.

## Source, runtime and offline contract

[Protomaps daily downloads](https://docs.protomaps.com/basemaps/downloads) permit regional
extraction from their OpenStreetMap-derived basemap. Outpost probes at most seven recent
published build dates, pins the selected ETag, and reads the [PMTiles v3 source format](https://github.com/protomaps/PMTiles/blob/main/spec/v3/spec.md)
with bounded HTTP ranges. If the host is unavailable, import a prepared pack or retry later.
Builds are ephemeral; installed packs do not need continued access to the provider.
Outpost never bulk-downloads the standard OpenStreetMap tile service, whose
[tile policy prohibits offline prefetch](https://operations.osmfoundation.org/policies/tiles/).

Installed packs use Outpost's `outpost-vector-v1` SQLite archive: metadata and checksummed
ZXY vector tiles, separated into overview and regional sources. This is an Outpost export
format, not a generic PMTiles/MBTiles import. The local API serves versioned vector tiles.
Legacy PNG/JPEG packs and their existing raster behavior remain supported.

MapLibre GL JS 6.8.0, its module worker, stylesheet and Noto glyph assets are vendored under
`src/outpost/web/static/vendor/maps`. `manifest.json` records upstream versions, source
commit and file hashes; tests verify the complete inventory. BSD and OFL license files are
included. The local style draws geography, roads, boundaries and place labels without
external styles, sprites, glyph services or telemetry. Labels prefer English names and fall
back to source names; this is not a promise of complete language/script localization.
Attribution remains visible for OpenStreetMap contributors and Protomaps. A browser with
WebGL support is required for vector basemaps; coordinates, markers and lists remain usable
if rendering is unavailable.

## Verification and remaining field work

Automated coverage includes the Date Line, international regions, stale/absent GPS,
whole-response rejection, changed sources, download interruption/resume, import failure,
corrupt tiles, account/CSRF boundaries, immutable old-pack access, and nine responsive/theme
browser combinations with outside network requests blocked and an empty browser cache.

Readiness distinguishes missing/changed packs, overview only and a verified selected region.
It reports failure when a configured installation location lies outside the selected region.
Its bounded check compares the installed file's size/mtime with the verification receipt;
it does not scan the entire archive on each refresh. Each served tile also checks its hash.
A verified selected region does not prove it covers a different deployment location, nor
prove a physical WAN-disconnected field exercise. Review the chosen region if the station
moves. Issue #139 retains the actual station/service-path and physical field acceptance.
