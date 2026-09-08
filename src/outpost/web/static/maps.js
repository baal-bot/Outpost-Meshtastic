import "/nav.js";
import {scheduler} from "/refresh-scheduler.js";
import {byId as $, apiJson} from "/ui-primitives.js";
let csrf = "";
let current = null;
let filled = false;
let installed = null;
let reported = null;
const preview = new window.OutpostMap.Controller({root: $("map-preview"), offlineOnly: true, initialView: {lat: 0, lon: 0, zoom: 2}});
const mib = bytes => `${(Number(bytes || 0) / 1024 ** 2).toFixed(1)} MiB`;
const request = (path, options = {}) => apiJson(`/api/v1/maps${path}`, options, csrf);
function showError(error) { $("map-job").textContent = error.message || "Map setup is unavailable. Try again."; }
async function refresh() {
  const value = await request("");
  current = value.job;
  const suggestion = value.suggestion;
  reported = suggestion.candidate || null;
  $("map-use-reported").hidden = !reported;
  if (reported) $("map-use-reported").textContent = `Use reported coordinates manually (${reported.latitude.toFixed(5)}, ${reported.longitude.toFixed(5)})`;
  $("map-position").textContent = suggestion.detail || "Enter a location manually or install the overview first.";
  if (!filled && suggestion.available) {
    $("map-latitude").value = suggestion.latitude;
    $("map-longitude").value = suggestion.longitude;
    preview.setView({lat: suggestion.latitude, lon: suggestion.longitude, zoom: 10});
    filled = true;
  }
  $("map-job").textContent = current.detail || "Choose an area and check its download size.";
  const plan = current.plan;
  $("map-estimate").textContent = plan ? `Estimated download: ${mib(plan.download_bytes)} · Installed: about ${mib(plan.installed_bytes_estimate)} · Free storage: ${mib(value.free_bytes)}` : `Free storage: ${mib(value.free_bytes)}`;
  $("map-plan").disabled = value.busy;
  $("map-download").hidden = value.busy || !plan || current.state === "complete";
  $("map-download").textContent = current.state === "planned" ? "Download maps" : "Resume download";
  $("map-pause").hidden = !value.busy;
  $("map-progress").hidden = !plan || !["downloading", "verifying", "paused"].includes(current.state);
  $("map-progress").max = plan?.tile_count || 1;
  $("map-progress").value = current.completed_tiles || 0;
  const pack = value.installed;
  $("map-installed").textContent = value.installed_error || (pack ?
    `Installed: ${pack.region ? `${pack.region.radius_km} km region through z${pack.region.max_zoom} + world overview` : "World overview · add a region for local detail"}. ${mib(pack.bytes)}.` : "No verified vector map installed. Any existing raster pack remains available.");
  if (pack && installed !== pack.pack_id) {
    installed = pack.pack_id;
    preview._refreshManifest(true);
    if (pack.region) preview.setView({lat: pack.region.latitude, lon: pack.region.longitude, zoom: 11});
  }
  if (!value.busy) scheduler.cancel("map-setup");
}
function track() { scheduler.schedule("map-setup", refresh, {interval: 2500, initial: 0}); }
$("map-use-reported").addEventListener("click", () => {
  if (!reported) return;
  $("map-latitude").value = reported.latitude;
  $("map-longitude").value = reported.longitude;
  filled = true;
  $("map-position").textContent = "Reported coordinates selected manually. Review the area before downloading.";
});
$("map-region-fields").addEventListener("input", () => { filled = true; });
$("overview-only").addEventListener("change", () => { $("map-region-fields").disabled = $("overview-only").checked; });
$("map-plan-form").addEventListener("submit", async event => {
  event.preventDefault();
  $("map-plan").disabled = true;
  try {
    const region = $("overview-only").checked ? null : {
      latitude: Number($("map-latitude").value), longitude: Number($("map-longitude").value),
      radius_km: Number($("map-radius").value), max_zoom: Number($("map-zoom").value),
    };
    await request("/plan", {method: "POST", body: JSON.stringify({region, budget_bytes: Number($("map-budget").value) * 1024 ** 2})});
    track();
  } catch (error) { showError(error); $("map-plan").disabled = false; }
});
$("map-download").addEventListener("click", async () => {
  $("map-download").disabled = true;
  try { await request(`/download/${current.id}`, {method: "POST"}); track(); }
  catch (error) { showError(error); }
  finally { $("map-download").disabled = false; }
});
$("map-pause").addEventListener("click", async () => {
  try { await request("/pause", {method: "POST"}); track(); } catch (error) { showError(error); }
});
try {
  const session = await apiJson("/api/v1/auth/session");
  csrf = session.csrf_token;
  await refresh();
  if (["planning", "downloading", "verifying"].includes(current.state)) track();
} catch (error) { showError(error); }
window.addEventListener("pagehide", () => { scheduler.cancel("map-setup"); preview.destroy(); }, {once: true});
