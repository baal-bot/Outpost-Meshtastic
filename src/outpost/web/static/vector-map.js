// The renderer, workers, fonts and tiles are served by this Outpost, including offline.
import {Map as LibreMap} from "/vendor/maps/maplibre-gl.mjs";

function style(manifest, theme) {
  const light = theme === "daylight";
  const colors = light ? ["#e8ecdf", "#aacfd6", "#d3dfbd", "#faf7ed", "#9aa79c", "#263b36"] : theme === "night" ?
    ["#201711", "#18191b", "#2b2114", "#8c633c", "#624931", "#e0ac76"] :
    ["#1b2c28", "#163d49", "#244334", "#869581", "#586e62", "#dce8db"];
  const [land, water, green, road, boundary, text] = colors;
  const sources = {};
  const layers = [{id: "background", type: "background", paint: {"background-color": land}}];
  for (const kind of ["overview", "region"]) {
    if (kind === "region" && !manifest.region) continue;
    const regional = kind === "region";
    sources[kind] = {type: "vector", tiles: [
      `${location.origin}/tiles/vector/${manifest.pack_id}/${kind}/{z}/{x}/{y}.pbf`,
    ], minzoom: regional ? 7 : 0, maxzoom: regional ? manifest.region.max_zoom : 6};
    const add = (name, type, paint, extra = {}) => layers.push({
      id: `${kind}-${name}`, source: kind, "source-layer": name, type, paint,
      ...(regional ? {minzoom: 6} : {}), ...extra,
    });
    add("earth", "fill", {"fill-color": land});
    add("landcover", "fill", {"fill-color": green, "fill-opacity": 0.45});
    add("landuse", "fill", {"fill-color": green, "fill-opacity": 0.3});
    add("water", "fill", {"fill-color": water});
    add("buildings", "fill", {"fill-color": boundary, "fill-opacity": 0.5});
    add("boundaries", "line", {"line-color": boundary, "line-width": 1, "line-dasharray": [3, 2]});
    add("roads", "line", {"line-color": road, "line-width": ["interpolate", ["linear"], ["zoom"], 6, 0.5, 14, 2]});
    add("places", "symbol", {"text-color": text, "text-halo-color": land, "text-halo-width": 1.5}, {
      ...(regional ? {} : {maxzoom: 6}),
      layout: {"text-field": ["coalesce", ["get", "name:en"], ["get", "name"], ""],
        "text-font": ["Noto Sans Regular"], "text-size": regional ? 12 : 13,
        "text-max-width": 10},
    });
  }
  return {version: 8, sources, layers,
    glyphs: `${location.origin}/vendor/maps/fonts/{fontstack}/{range}.pbf`};
}

export function createVectorMap(controller, manifest) {
  const host = document.createElement("div");
  host.className = "outpost-vector-map";
  host.setAttribute("aria-hidden", "true");
  controller.tilesLayer.appendChild(host);
  const theme = () => document.documentElement.dataset.theme;
  let failed = false;
  const map = new LibreMap({container: host, style: style(manifest, theme()),
    center: [controller.view.lon, controller.view.lat], zoom: controller.view.zoom - 1,
    interactive: false, attributionControl: false, validateStyle: true,
    fadeDuration: 0, renderWorldCopies: true, canvasContextAttributes: {preserveDrawingBuffer: false}});
  map.on("error", () => { failed = true; controller.requestRender(); });
  map.on("load", () => controller.requestRender());
  const observer = new MutationObserver(() => map.setStyle(style(manifest, theme())));
  observer.observe(document.documentElement, {attributes: true, attributeFilter: ["data-theme"]});
  let dimensions = "";
  const copyright = document.createElement("a");
  copyright.href = "https://www.openstreetmap.org/copyright";
  copyright.textContent = "© OpenStreetMap contributors · Protomaps";
  copyright.target = "_blank";
  copyright.rel = "noreferrer";
  controller.attribution.replaceChildren(copyright);
  controller.attribution.hidden = false;
  return {
    render(viewport) {
      const size = `${viewport.width}/${viewport.height}`;
      if (size !== dimensions) { dimensions = size; map.resize(); }
      const {lon, lat, zoom} = controller.view;
      map.jumpTo({center: [lon, lat], zoom: zoom - 1});
      const region = manifest.region;
      const inside = region?.bounds.some(([west, south, east, north]) =>
        lon >= west && lon <= east && lat >= south && lat <= north);
      const message = failed ? "Offline map could not render · retry or replace the pack in Map setup" :
        !inside ? "World overview only here · download a region for local detail" :
        zoom > region.max_zoom ? `Regional detail ends at z${region.max_zoom} · showing enlarged map` :
        "Offline regional map · world overview outside downloaded coverage";
      controller.basemapState.textContent = message;
      controller.basemapState.hidden = false;
      controller.root.dataset.offlineTiles = failed ? "unreadable" : "ready";
      controller.root.dataset.basemapMode = "offline-only";
    },
    destroy() { observer.disconnect(); map.remove(); host.remove(); },
  };
}
