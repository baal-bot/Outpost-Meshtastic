const $ = id => document.getElementById(id);
const root = $("bundles"), status = $("bundle-status"), output = $("bundle-export"), input = $("bundle-import");
const download = $("bundle-download"), apply = $("bundle-apply"), next = $("bundle-next");
const base = "/api/v1/federation/bundles";
let csrf = "", busy = false, exportView = null, exportArgs = null, importView = null, uploaded = null;
const message = text => {status.textContent = text;};
function exportReset() {exportView = null; exportArgs = null; download.disabled = true; next.disabled = true; $("bundle-export-consent").checked = false;}
function importReset() {importView = null; uploaded = null; apply.disabled = true; for (const id of ["bundle-import-consent", "bundle-import-labels", "bundle-import-locations"]) $(id).checked = false;}
async function response(path, options = {}) {
  const value = await fetch(path, {cache: "no-store", signal: AbortSignal.timeout(30000), ...options, headers: {"x-csrf-token": csrf, ...options.headers}});
  if (!value.ok) {let body = {}; try {body = await value.json();} catch (_) {} throw new Error(body.error?.message || `Request failed (${value.status}); preview again before retrying.`);}
  return value;
}
async function task(work) {
  if (busy) return; busy = true; root.inert = true;
  try {await work();} catch (error) {exportReset(); importReset(); message(`${error.message || "Outcome unconfirmed; a commit may already have succeeded."} No automatic retry. Preview again.`);}
  finally {busy = false; root.inert = false;}
}
const post = (path, value) => response(path, {method: "POST", headers: {"content-type": "application/json"}, body: JSON.stringify(value)});
async function localIdentity() {
  const view = await (await response(base)).json();
  $("bundle-identity").textContent = `Commissioned: ${view.commissioned_identity || "none"}. Observed/restored: ${view.observed_identity || "none"}. Current signing fingerprint: ${view.fingerprint || "uninitialized"}. Receipt capacity: ${view.receipt_count}/${view.receipt_limit}.`;
  $("bundle-commission").hidden = Boolean(view.commissioned_identity);
}
output.addEventListener("input", event => {if (event.target.name !== "after") output.elements.after.value = 0; exportReset();});
input.addEventListener("change", importReset);
$("bundle-commission").addEventListener("submit", event => {event.preventDefault(); const identity = event.currentTarget.elements.identity.value; task(async () => {await post(`${base}/identity`, {identity}); await localIdentity(); message("Observed local identity commissioned. No peer trust or radio setting changed.");});});
output.addEventListener("submit", event => {
  event.preventDefault(); const fields = output.elements;
  const args = {destination: fields.destination.value, stream: fields.stream.value, after: Number(fields.after.value), public_labels: fields.labels.checked, precise_locations: fields.locations.checked};
  exportReset();
  task(async () => {const view = await (await post(`${base}/export`, args)).json(); exportView = view; exportArgs = args; $("bundle-export-preview").textContent = JSON.stringify(view, null, 2); download.disabled = !view.items.length; next.disabled = view.done; message(`Export preview only: ${view.items.length} eligible records, ${view.excluded} excluded candidates. Review all text before signing.`);});
});
next.addEventListener("click", () => {if (!exportView || busy) return; output.elements.after.value = exportView.next; exportReset(); message("Next cursor selected. Preview this page before signing it.");});
download.addEventListener("click", () => {
  if (!exportView || busy) return;
  if (!$("bundle-export-consent").checked) {message("Explicit public-content and privacy review is required."); return;}
  const args = {...exportArgs, expected_token: exportView.review_token, approve_public_content: true}; download.disabled = true;
  task(async () => {const file = await (await post(`${base}/export`, args)).blob(); const url = URL.createObjectURL(file); const link = document.createElement("a"); link.href = url; link.download = "outpost-transfer.opb"; link.click(); setTimeout(() => URL.revokeObjectURL(url), 30000); message("Signed file offered for download. Confirm it was saved; this is not proof of delivery or import. Keep the readable media protected.");});
});
input.addEventListener("submit", event => {
  event.preventDefault(); const file = input.elements.file.files[0]; importReset();
  if (!file || !file.size || file.size > 196608) {message("Choose a non-empty bundle of at most 192 KiB."); return;}
  task(async () => {const bytes = await file.arrayBuffer(); const view = await (await response(`${base}/import`, {method: "POST", headers: {"content-type": "application/octet-stream"}, body: bytes})).json(); uploaded = bytes; importView = view; $("bundle-import-preview").textContent = JSON.stringify(view, null, 2); apply.disabled = false; message("Signature and current trust verified. Preview only; no records have changed. Review every record, effect and declared scope.");});
});
apply.addEventListener("click", () => {
  if (!importView || !uploaded || busy) return;
  if (!$("bundle-import-consent").checked || (importView.scope.public_labels && !$("bundle-import-labels").checked) || (importView.scope.precise_locations && !$("bundle-import-locations").checked)) {message("Approve public content and every declared privacy scope before committing."); return;}
  const bytes = uploaded, headers = {"content-type": "application/octet-stream", "x-bundle-review": importView.review_token, "x-bundle-public": "true", "x-bundle-labels": String($("bundle-import-labels").checked), "x-bundle-locations": String($("bundle-import-locations").checked)};
  importReset();
  task(async () => {const result = await (await response(`${base}/import`, {method: "POST", headers, body: bytes})).json(); message(`Import committed and audited: ${result.imported} imported, ${result.skipped} skipped. No radio transmission or responder acceptance was requested.`); await localIdentity();});
});
await task(async () => {csrf = (await (await response("/api/v1/auth/session")).json()).csrf_token; await localIdentity(); message("Ready for operator review. No automatic imports or radio transmissions.");});
