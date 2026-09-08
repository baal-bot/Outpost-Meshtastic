# Local-first map behavior

The shared Watch, Environment, Members, and Federation maps consult the Outpost's
`/tiles/manifest.json` before requesting basemap images. A usable local tile is requested
directly from the Outpost; Internet failure is not a prerequisite. This works with a fresh
browser cache. Local tiles are optional: coordinates, markers, details, and the corresponding
incident/member/waypoint/peer lists remain usable when the basemap is unavailable.

The default is **local-first**, with OpenStreetMap Internet fallback for local tiles that
cannot be loaded, or when the local pack cannot be inspected. Checking **Offline only**
prevents new external basemap requests, including late fallback callbacks. Requests already
transmitted cannot be recalled. The mode applies to maps at the same Outpost address
(browser origin), persists in browser local storage, and synchronizes across open maps/tabs.
Different browser profiles or Outpost addresses have separate preferences. If saving is
blocked, the map explicitly says that the mode applies only to the current page. Clearing
browser storage restores the default. This is not a whole-dashboard network isolation switch;
other configured data feeds are unaffected.

The status strip distinguishes an absent pack, an unreadable manifest/pack, unavailable
manifest status, loading images, incomplete local coverage, failed basemap images, and Internet
fallback. Counts describe only tiles needed by the current view, not the whole advertised
region. A successful Internet tile does not hide a local coverage hole, and one successful
local tile does not clear failures elsewhere. Attribution uses the local manifest's text and
the Internet source when requested; local-only views do not imply an OpenStreetMap source.
Pack operators remain responsible for supplying accurate source/license attribution.

Failed visible tiles are retained as failed entries, so ordinary rendering or a small pan
within unchanged coverage does not continually retry them. The **↻** button explicitly rechecks
the manifest and retries visible tiles. Changing mode or leaving/re-entering a tile's coverage
also permits a new attempt. Explicit retry uses fresh local tile URLs to bypass cached corrupt
images; Internet-provider caching is unchanged. The manifest is shared within a page and has
a five-second deadline;
there is no periodic tile/manifest refresh. Pending image requests remain visibly loading until
the browser completes or fails them, or the operator changes mode/retries. Removed tiles,
superseded manifest inspections, and destroyed maps cannot start late fallback requests.

## Remaining qualification — #139

The [worldwide setup design](WORLDWIDE-MAP-SETUP.md) proposes a small global overview,
GPS-assisted regional downloads with manual fallback, and an optional full-planet
pack. That provisioning workflow and vector rendering remain implementation work;
the behavior described above is the currently implemented raster-map behavior.

This browser change (#164) does not provision or certify a map pack. The existing service
inspector only finds a tile with a recognizable raster header. Its `ready` result and legacy
manifest bounds/zoom/count fields do **not** certify complete coverage, decode validity, checksums,
source consistency, or service-path installation. The client reports actual visible image
success/failure and accepts existing manifests; it does not upgrade their guarantees.

Before claiming outage-ready mapping, select the intended region/zooms and source, provision
the configured `store.tiles_path` with service-readable permissions, independently verify full
coverage/integrity and the install/update/copy workflow, and run a fresh-browser physical WAN-down
test across that region and every supported zoom. These are still tracked in
[#139](https://github.com/baal-bot/Outpost-Meshtastic/issues/139). No such field qualification,
appliance deployment, tile download, or live service change is implied by browser tests.

Automated browser evidence lives in `test_mobile_navigation.py` under `test_shared_map_*`:
fresh-cache/held-WAN tests in all three themes at 320, 390, and 1280 px; corrupt/partial packs;
explicit retry; persistence and blocked storage; late image/manifest callbacks; safe legacy
attribution; keyboard/selection behavior; and the existing shared DOM/animation-frame budget.
