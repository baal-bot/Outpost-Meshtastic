# Worldwide offline map setup

Design for [#139](https://github.com/baal-bot/Outpost-Meshtastic/issues/139), researched
September 8, 2026. The owner wants installations worldwide to obtain useful maps
without choosing a region in source code. This document describes proposed behavior;
the GPS-assisted download and vector-map support are not implemented yet.

## Recommended coverage

Use a small worldwide overview with detailed regional packs selected during setup.
Offer a full-planet pack as an optional large-storage installation.

| Coverage | Purpose | Size reference |
| --- | --- | --- |
| Worldwide overview | Countries, major places and orientation before regional setup | Protomaps documents about 60 MB for zooms 0–6 |
| Regional detail | Streets and local context around the installation; additional regions can be retained | Estimate from the selected region, detail and source archive before downloading |
| Full planet | Detailed coverage for operators who choose the storage and download cost | Protomaps documents roughly 120 GB for zooms 0–15 |

These are upstream examples, not measurements of an Outpost release or promises of
equal map detail in every country. Fonts, styles, application files, temporary download
space and an older retained pack require additional storage.
[Overview example](https://docs.protomaps.com/guide/getting-started),
[planet downloads and regional extraction](https://docs.protomaps.com/basemaps/downloads).

## Setup flow

1. After the radio connects, suggest an area using the connected local radio's valid,
   recent position. Show whether the source is onboard GPS, external GPS, or a manual
   fixed location, with age when known. Do not select another mesh member's position.
2. Let the operator confirm or move the center and choose the area and detail. Start
   with the existing downloader's 20 km radius as a proposal, and show download size,
   installed size and available space. Region size remains adjustable.
3. If GPS is absent, stale, invalid or of unknown age, offer manual coordinates or a
   map selection. Preserve an explicitly configured installation location as the
   default. Do not silently replace it, change radio position settings, or broadcast
   a newly selected map center.
4. Download the accepted region, with progress, bounded retries and cancellation.
   Validate it before atomically selecting it. An interrupted operation leaves the
   previous verified pack available; resumable work must retain the exact source
   identity and validate retained chunks.
5. Offer importing a verified pack from USB or preparing it on another computer when
   the installation has no Internet. Keep the world overview and ordinary lists usable
   if regional setup is deferred.
6. Later, allow additional regions, replacement or explicit refresh. Movement or GPS
   jitter must not silently start repeated downloads or remove existing regions.

Downloading follows the setup selection; normal offline operation uses the Pi's
stored maps. The regional download provider can infer the requested area. Keep exact
setup coordinates out of public logs and telemetry; no external geocoder is required
for manual coordinates or selection on the local overview.

## Source and rendering approach

The preferred candidate is an OpenStreetMap-derived vector basemap in PMTiles format.
The [PMTiles extractor](https://docs.protomaps.com/pmtiles/cli) supports selecting a
region from a remote archive without first downloading the entire planet. Its
structural verifier is only one validation layer; application coverage and decoding
checks are still required.

Use a versioned download catalogue with source date, format/style versions, published
integrity information, attribution and size information. Determine a permitted,
maintainable distribution endpoint during the implementation prototype; do not ship
an unversioned or expiring daily URL as the permanent default. Protomaps documents
regional downloads but discourages hotlinking to its build files. Preserve a local
archive/mirror option and retain the source/licensing notices with exported packs.

The standard OpenStreetMap tile server explicitly prohibits offline prefetching and
bulk downloads. It cannot supply this setup feature.
[OSMF tile policy](https://operations.osmfoundation.org/policies/tiles/).

Outpost currently renders raster images through its shared map controller. PMTiles
can contain several tile types; the proposed Protomaps basemap contains vectors, so
changing the download URL alone would not make it render. Prototype a locally bundled
MapLibre/PMTiles basemap behind the shared controller, preserving markers, selections,
keyboard controls, themes and explicit offline behavior. Retain existing raster packs
as a compatibility path. Serve every style, font, sprite and script from the appliance,
including labels for supported international scripts; no runtime CDN dependency.
[Offline rendering assets](https://docs.protomaps.com/basemaps/maplibre).

## Existing integration points and gaps

- `deploy/configure.py` currently asks for coordinates. `deploy/install.sh` downloads
  a bounded pack only when `node.location` exists and a manifest is absent. That
  download occurs before the new application service starts.
- `tools/build_tile_pack.py` defaults to USGS Topo raster tiles. It is not the worldwide
  source-selection or vector-extraction workflow described here.
- `MeshtasticRadioLink` reads local coordinates, but `RadioSnapshot` does not preserve
  position age or source. Setup should use the service's existing connection and a
  bounded local-position observation instead of opening a competing serial connection
  or treating connection time as GPS-fix time. The upstream position message separates
  position-solution timestamp from the clock-setting time field.
  [Meshtastic position schema](https://github.com/meshtastic/protobufs/blob/master/meshtastic/mesh.proto).
- The setup download therefore belongs after radio connection, as a resumable maps
  step. An explicitly supplied location or imported pack can support unattended setup.
  Noninteractive setup must have explicit region and download-budget inputs.
- `store.tiles_path` must remain the single configured installed root. The service,
  installer, update, copy and recovery procedures must agree on it. Pack selection
  needs an additive versioned manifest; legacy raster manifests remain distinguishable.
- Current map inspection finds a recognizable image and does not certify the whole
  region. Publish a bounded verification result after the complete install scan;
  dashboard readiness must not walk a planet archive on each refresh.

## Acceptance and implementation order

First implement and test location selection and bounded worldwide region planning,
then prototype one small international vector pack with all rendering assets offline.
Use that result to pin the provider, versions, size estimates and Pi/browser budgets.
Integrate durable download/import, atomic installation and setup UI only after that
path is verified. Then package the overview and optional full-planet workflow.

Tests must cover no GPS, stale/future fixes, unknown source/age, manual overrides,
Southern Hemisphere and International Date Line regions, equator/prime-meridian
coordinates, sparse and dense areas, missing glyphs, interrupted downloads, disk
limits, corrupt/partial packs and upgrades preserving older packs. The current Web
Mercator map projection excludes the polar caps beyond approximately 85.05 degrees;
report that limitation explicitly instead of silently claiming coverage there.

Readiness must distinguish overview-only, verified regional detail, partial/corrupt
data, and current location outside installed coverage. An overview cannot pass a
regional-detail requirement. Keep missing maps optional to incident/list operation.
Before closing #139, verify fresh-browser rendering with the physical WAN unavailable,
all required local assets present, and the install/update/copy workflow exercised on
the configured service path. No such qualification is established by this design.
