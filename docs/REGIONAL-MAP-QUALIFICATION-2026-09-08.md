# Regional maps and world overview — September 8, 2026

The regional map setup and world overview are installed on the local Pi. The owner
selected bounded regional detail with a small world overview and excluded full-planet
downloads. Setup uses a recent connected-radio GPS fix or manual coordinates and
shows the download estimate before proceeding. The operating guide is
[Worldwide regional map setup](WORLDWIDE-MAP-SETUP.md).

## Selected release and verification

- Release: `20260908T215537Z-df4c206dc5f6`.
- Source: `df4c206dc5f6ef6ee3bf550305a850ecaf1752e4`.
- [Exact-source CI run](https://github.com/baal-bot/Outpost-Meshtastic/actions/runs/34278060752):
  all four Python 3.12/3.13 test and pinned-runtime jobs passed, including production
  coverage, packaging and the configured dependency audits.
- The normal local updater completed at 21:56:44 UTC with the native HailoRT wheel.
- Database schema remains 186. Integrity and foreign-key checks passed; every protected
  pre-update record ID remained present, including durable outbound work and radio-power
  history. Web, radio, core tasks, required native AI and same-package boot readiness passed.
- Fresh readiness still recognizes USB external power with no battery percentage.

Local verification also covered the regional planner and transfer limits, interruption
and import behavior, account/CSRF boundaries, nine map-rendering theme/viewport cases,
four operational maps, the shared visual baselines, and installer preservation checks.
CI exposed an existing cancellation-test timing race; its timeout and cancellation
cases now run independently, including a delayed-cancellation regression. The pinned
jobs received enough execution time to complete all their checks.

## Installed overview

The service account imported a verified `outpost-vector-v1` pack at the configured
tile path. It contains the Protomaps September 8 world overview through zoom 6:

| Property | Verified value |
| --- | --- |
| Tiles | 5,461 |
| Installed size | 49,823,744 bytes (49.8 MB / 47.5 MiB) |
| Basemap source version | 4.15.2 |
| Regional detail | None selected on this station yet |
| Map readiness | `unknown`, reason `overview_only` |

The imported file's SHA-256 is
`3cc69f8eecff346c99a9c762d19a570027052690778e634dc3beb81b708e33da`.
Every tile and the archive were verified before selection. The live public manifest,
served overview tile and map assets matched the selected pack and installed package.
Precise setup coordinates and private plan metadata are excluded from the public vector
manifest; authenticated setup retains the information needed to review the selected area.

A new headless browser context loaded the live service's public map assets, fonts and
tiles with outside requests blocked. Its temporary page used the dashboard's shared map
markup and stylesheet order. The rendered map filled its viewport, contained geographic
features and labels, and produced no browser errors or external requests. The captured
image was visually inspected. This verifies browser isolation, not a physical WAN outage.

## Remaining field acceptance

Refresh existing dashboard tabs, then open **Access → Set up offline regional maps & world
overview** (`/maps.html`). Choose the actual operating area using a recent GPS suggestion
or manual coordinates, review its size, and download the regional pack. The world overview
alone does not establish local street-level coverage. A public Nairobi sample used for
development was kept separate from the station selection.

Issue #139 remains open for the actual station region and a fresh-browser exercise with
the physical WAN unavailable. No new reboot, physical WAN interruption or second-node
change was performed. Existing raster files and the previously unmounted recovery USB
were preserved.

Private evidence is retained in `.data/regional-maps-2026-09-08/STATE.json` and
`/var/lib/outpost-qualification/139-20260908/regional-update/`. Keep raw configuration,
record IDs and exact station locations private. Later documentation commits do not change
the installed release's CI identity.
