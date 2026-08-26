import("/nav.js");
const spacingStyles = document.createElement("link");
spacingStyles.rel = "stylesheet";
spacingStyles.href = "/federation-spacing.css";
document.head.appendChild(spacingStyles);
const $ = id => document.getElementById(id);
const safe = value => String(value ?? "").replace(/[&<>'"]/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"})[char]);
let csrf = "";
let policyWizardOpen = false;
const api = (url, options = {}) => fetch(url, {...options, headers: {...(options.body ? {"content-type":"application/json"} : {}), ...(options.method && options.method !== "GET" ? {"x-csrf-token":csrf} : {}), ...options.headers}});
function age(epoch) { if (!epoch) return "Never"; const seconds = Math.max(0, Date.now()/1000-epoch); if (seconds < 90) return "Just now"; if (seconds < 3600) return `${Math.floor(seconds/60)}m ago`; if (seconds < 86400) return `${Math.floor(seconds/3600)}h ago`; return `${Math.floor(seconds/86400)}d ago`; }
function serviceProvenance(item) {
  const value = item.provenance || {};
  if (!value.provider) return new Date(item.created_at * 1000).toLocaleString();
  const source = ({observation:"station observation",forecast:"near-term forecast",estimate:"model estimate"})[value.source_kind] || value.source_kind || "provider data";
  const delivery = value.delivery_kind === "peer" ? "Peer-provided" : "Source";
  const valid = value.valid_age_seconds == null ? "valid time unavailable" : value.valid_age_seconds < 60 ? "valid now" : `valid ${Math.floor(value.valid_age_seconds / 60)}m ago`;
  const cache = value.cached === true ? "cached" : value.cached === false ? "live fetch" : value.cache_age_seconds == null ? "cache age unavailable" : `cache ${value.cache_age_seconds}s`;
  return `${delivery} · ${source} · ${value.provider} · ${valid} · ${cache}`;
}
function transportBadges(peer) { const paths=peer.discovery_transports||[]; const badges=[]; if(paths.includes("radio"))badges.push(`<span class="transport-chip radio">⌁ LoRa observed</span>`); if(paths.includes("mqtt"))badges.push(`<span class="transport-chip mqtt">◫ MQTT observed</span>`); return badges.join("")||`<span class="transport-chip unknown">Path not yet observed</span>`; }
async function showPolicyWizard(peer) {
  if (!peer || policyWizardOpen) return;
  policyWizardOpen = true;
  const returnFocus = document.activeElement;
  const boardResponse = await api("/api/v1/boards?limit=200");
  const boards = boardResponse.ok ? (await boardResponse.json()).items.filter(board => board.federated) : [];
  const existing = new Set(peer.boards || []);
  const dialog = document.createElement("dialog");
  dialog.className = "sharing-wizard";
  dialog.innerHTML = `<form method="dialog" class="sharing-wizard-shell"><header><div><p class="eyebrow">PAIRING COMPLETE</p><h2>Share with ${safe(peer.node_name || peer.mesh_id)}</h2><p>Choose what this trusted Outpost may exchange. Nothing is shared until you enable it here.</p></div><button class="wizard-close" value="cancel" aria-label="Close">×</button></header><div class="sharing-options"><label class="sharing-option"><input id="wizard-bbs" type="checkbox" ${existing.size ? "checked" : ""}><span><b>Community boards</b><small>Synchronize approved boards and their replies.</small></span></label><div id="wizard-board-list" class="wizard-board-list">${boards.length ? boards.map(board => `<label><input type="checkbox" value="${safe(board.slug)}" ${existing.has(board.slug) ? "checked" : ""}><span>${safe(board.title)} <code>${safe(board.slug)}</code></span></label>`).join("") : `<p>No boards are currently marked for federation. Enable federation on a board first.</p>`}</div><label class="sharing-option"><input id="wizard-incidents" type="checkbox" ${peer.sync_incidents ? "checked" : ""}><span><b>Incidents</b><small>Exchange incidents inside the peer's geographic boundary; operator review still applies.</small></span></label><div class="wizard-boundary"><label><span>Peer latitude</span><input id="wizard-lat" type="number" min="-90" max="90" step="any" value="${safe(peer.incident_lat ?? "")}" placeholder="Optional"></label><label><span>Peer longitude</span><input id="wizard-lon" type="number" min="-180" max="180" step="any" value="${safe(peer.incident_lon ?? "")}" placeholder="Optional"></label><label><span>Radius</span><div><input id="wizard-radius" type="number" min="1" max="500" value="${safe(peer.incident_radius_km || 25)}"><em>km</em></div></label></div><label class="sharing-option"><input id="wizard-alerts" type="checkbox" ${peer.relay_alerts ? "checked" : ""}><span><b>Public alerts</b><small>Relay eligible alerts; imported alerts are never auto-broadcast.</small></span></label><label class="sharing-option"><input id="wizard-mail" type="checkbox" ${peer.relay_mail ? "checked" : ""}><span><b>Encrypted mail</b><small>Allow operator and member mail through this peer.</small></span></label><div class="wizard-service-policy"><div><b>Peer information services</b><small>Explicitly allow this peer to use this Outpost's internet connection. All services are denied by default.</small></div><div class="wizard-service-choices"><label><input type="checkbox" value="weather" ${peer.service_permissions?.includes("weather") ? "checked" : ""}>Weather</label><label><input type="checkbox" value="alerts" ${peer.service_permissions?.includes("alerts") ? "checked" : ""}>Public alerts</label><label><input type="checkbox" value="knowledge" ${peer.service_permissions?.includes("knowledge") ? "checked" : ""}>Knowledge</label></div><div class="wizard-service-limits"><label><span>Requests / hour</span><input id="wizard-service-quota" type="number" min="1" max="60" value="${safe(peer.quota_services_per_hour || 6)}"></label><label><span>Concurrent</span><input id="wizard-service-concurrency" type="number" min="1" max="4" value="${safe(peer.service_concurrency || 1)}"></label><label><span>Max response</span><div><input id="wizard-service-bytes" type="number" min="256" max="1600" value="${safe(peer.service_max_response_bytes || 1200)}"><em>bytes</em></div></label><label><span>Airtime / hour</span><div><input id="wizard-service-airtime" type="number" min="1" max="120" value="${safe(peer.service_airtime_seconds_per_hour || 15)}"><em>sec</em></div></label></div></div></div><p id="wizard-result" class="wizard-result">Review: only the checked content and services will be shared. Member positions, waypoints, private boards, and credentials are never included.</p><footer><button id="wizard-isolate" type="button" class="secondary">Keep isolated</button><button id="wizard-save" type="button">Apply sharing setup</button></footer></form>`;
  document.body.appendChild(dialog);
  const finish = () => { policyWizardOpen = false; dialog.remove(); window.requestAnimationFrame(() => returnFocus?.focus()); };
  dialog.addEventListener("close", finish, {once:true});
  dialog.querySelector(".wizard-close").addEventListener("click", () => dialog.close());
  const save = async isolated => {
    const lat = isolated ? "" : dialog.querySelector("#wizard-lat").value;
    const lon = isolated ? "" : dialog.querySelector("#wizard-lon").value;
    const bbs = !isolated && dialog.querySelector("#wizard-bbs").checked;
    const selectedBoards = bbs ? [...dialog.querySelectorAll("#wizard-board-list input:checked")].map(input => input.value) : [];
    dialog.querySelector("#wizard-save").disabled = true;
    const servicePermissions = isolated ? [] : [...dialog.querySelectorAll(".wizard-service-choices input:checked")].map(input => input.value);
    const response = await api(`/api/v1/federation/peers/${encodeURIComponent(peer.mesh_id)}/sync-policy`, {method:"PUT",body:JSON.stringify({boards:selectedBoards,sync_incidents:!isolated&&dialog.querySelector("#wizard-incidents").checked,incident_lat:lat===""?null:Number(lat),incident_lon:lon===""?null:Number(lon),incident_radius_km:Number(dialog.querySelector("#wizard-radius").value)||25,relay_alerts:!isolated&&dialog.querySelector("#wizard-alerts").checked,relay_mail:!isolated&&dialog.querySelector("#wizard-mail").checked,quota_items_per_hour:peer.quota_items_per_hour||200,quota_mail_per_hour:peer.quota_mail_per_hour||20,service_permissions:servicePermissions,quota_services_per_hour:Number(dialog.querySelector("#wizard-service-quota").value)||6,service_concurrency:Number(dialog.querySelector("#wizard-service-concurrency").value)||1,service_max_response_bytes:Number(dialog.querySelector("#wizard-service-bytes").value)||1200,service_airtime_seconds_per_hour:Number(dialog.querySelector("#wizard-service-airtime").value)||15})});
    if (!response.ok) { dialog.querySelector("#wizard-result").textContent=(await response.json()).error.message; dialog.querySelector("#wizard-save").disabled=false; return; }
    dialog.close(); await refresh();
  };
  dialog.querySelector("#wizard-save").addEventListener("click", () => save(false));
  dialog.querySelector("#wizard-isolate").addEventListener("click", () => save(true));
  dialog.showModal();
}
async function loadMqtt() {
  const response = await api("/api/v1/federation/mqtt");
  if (!response.ok) return;
  const mqtt = await response.json();
  const radioUp = document.querySelector(".path-grid article:first-child .pill")?.classList.contains("live");
  const mqttUp = mqtt.available && mqtt.enabled;
  const transportKpi = document.querySelector(".fed-kpis article:last-child");
  transportKpi.querySelector("strong").textContent = radioUp && mqttUp ? "Radio + MQTT" : mqttUp ? "MQTT" : "Radio";
  transportKpi.querySelector("p").textContent = mqttUp ? "Redundant mesh paths enabled" : mqtt.available ? "MQTT available but disabled" : "Radio transport available";
  const transportCards = document.querySelectorAll(".path-grid article");
  const mqttPolicy = transportCards[1]?.querySelector(".pill");
  if (mqttPolicy) {
    mqttPolicy.textContent = mqtt.available ? (mqtt.enabled ? "Enabled" : "Disabled") : "Unavailable";
    mqttPolicy.classList.toggle("live", mqtt.available && mqtt.enabled);
  }
  const panel = document.createElement("section");
  panel.className = "panel content-panel mqtt-panel";
  panel.innerHTML = `<div class="heading"><div><p class="eyebrow">MQTT TRANSPORT</p><h2>Meshtastic gateway</h2></div><span class="chip ${mqtt.enabled ? "active" : ""}">${mqtt.enabled ? "Enabled" : "Disabled"}</span></div><p class="mqtt-note">The radio firmware handles broker access and Meshtastic channel encryption. Discovery still creates pending peers and never establishes trust.</p><form id="mqtt-form" class="mqtt-form"><label><span>Enable MQTT</span><input id="mqtt-enabled" type="checkbox" ${mqtt.enabled ? "checked" : ""}></label><label><span>Broker address</span><input id="mqtt-address" value="${safe(mqtt.address)}" placeholder="mqtt.meshtastic.org (firmware default)"></label><label><span>Root topic</span><input id="mqtt-root" value="${safe(mqtt.root || "msh")}"></label><label><span>Federation channel</span><select id="mqtt-channel">${mqtt.channels.map(channel => `<option value="${channel.index}">${safe(channel.name)} · ${channel.index}</option>`).join("")}</select></label><label><span>Use TLS</span><input id="mqtt-tls" type="checkbox" ${mqtt.tls_enabled ? "checked" : ""}></label><label><span>Uplink announcements</span><input id="mqtt-uplink" type="checkbox" ${mqtt.channels[0]?.uplink_enabled ? "checked" : ""}></label><label><span>Receive federation traffic</span><input id="mqtt-downlink" type="checkbox" ${mqtt.channels[0]?.downlink_enabled ? "checked" : ""}></label><button>Apply to radio</button><p id="mqtt-result"></p></form>`;
  const policy = document.querySelector(".path-grid").closest(".panel");
  policy.parentElement.insertBefore(panel, policy);
  $("mqtt-form").addEventListener("submit", async event => {
    event.preventDefault();
    $("mqtt-result").textContent = "Writing radio configuration…";
    const result = await api("/api/v1/federation/mqtt", {method:"PUT", body:JSON.stringify({enabled:$("mqtt-enabled").checked,address:$("mqtt-address").value,tls_enabled:$("mqtt-tls").checked,root:$("mqtt-root").value,channel:Number($("mqtt-channel").value),uplink_enabled:$("mqtt-uplink").checked,downlink_enabled:$("mqtt-downlink").checked})});
    const body = await result.json();
    $("mqtt-result").textContent = result.ok ? "Radio MQTT settings updated." : body.error.message;
  });
}
async function loadServices() {
  const policy = document.querySelector(".path-grid").closest(".panel");
  const panel = document.createElement("section");
  panel.className = "panel content-panel service-panel";
  panel.innerHTML = `<div class="heading"><div><p class="eyebrow">PEER SERVICES</p><h2>Request resilient information</h2></div></div><p class="mqtt-note">Requests go to one capable trusted Outpost at a time. Results include their provider, fetch time, and cache age.</p><form id="service-form" class="service-form"><select id="service-type"><option value="weather">Current weather</option><option value="alerts">Public alerts</option><option value="knowledge">Public knowledge</option></select><input id="service-query" maxlength="200" placeholder="Question (knowledge requests only)"><button>Request from peer</button><span id="service-result"></span></form><div id="service-history" class="service-history"><p class="empty">No peer requests yet.</p></div>`;
  policy.parentElement.insertBefore(panel, policy);
  $("service-form").addEventListener("submit", async event => {
    event.preventDefault();
    $("service-result").textContent = "Selecting a capable peer…";
    const service = $("service-type").value;
    const query = $("service-query").value.trim();
    const response = await api("/api/v1/federation/services", {method:"POST", body:JSON.stringify({service, ...(query ? {query} : {})})});
    const body = await response.json();
    $("service-result").textContent = response.ok ? "Request sent." : body.error.message;
    await refreshServices();
  });
  await refreshServices();
}
async function refreshServices() {
  const history = $("service-history");
  if (!history) return;
  const response = await api("/api/v1/federation/services");
  if (!response.ok) return;
  const items = (await response.json()).items;
  history.innerHTML = items.map(item => `<article><div><strong>${safe(item.service)} · ${safe(item.status)}</strong><code>${safe(item.peer_mesh_id)}</code></div><p>${item.status === "complete" ? safe(JSON.stringify(item.result)) : safe(item.error || "Awaiting peer response")}</p><small>${safe(serviceProvenance(item))}</small></article>`).join("") || `<p class="empty">No peer requests yet.</p>`;
}
async function loadInbox() {
  const directory = $("peer-list").closest(".panel");
  const panel = document.createElement("section");
  panel.id = "federation-inbox";
  panel.className = "panel content-panel inbox-panel";
  panel.innerHTML = `<div class="heading"><div><p class="eyebrow">FEDERATION INBOX</p><h2>Quarantined records</h2></div><button id="refresh-inbox" class="small-button">Refresh</button></div><p class="mqtt-note">Review remote records before they enter local boards, incidents, or alerts. Imported alerts are not automatically broadcast.</p><div id="fed-inbox"><p class="empty">No records awaiting review.</p></div>`;
  directory.parentElement.insertBefore(panel, directory);
  $("refresh-inbox").addEventListener("click", refreshInbox);
  await refreshInbox();
}
async function refreshInbox() {
  const target = $("fed-inbox"); if (!target) return;
  const response = await api("/api/v1/federation/inbox"); if (!response.ok) return;
  const items = (await response.json()).items;
  target.innerHTML = items.map(item => `<article class="inbox-card"><div><strong>${safe(item.stream)}</strong><code>${safe(item.node_name || item.mesh_id)} · ${safe(item.uid)}</code></div><p>${safe(item.payload.headline || item.payload.title || item.payload.subject || item.payload.body || "Federated record")}</p><div><button data-import="${item.id}">Approve import</button><button class="danger" data-reject="${item.id}">Reject</button></div></article>`).join("") || `<p class="empty">No records awaiting review.</p>`;
  document.querySelectorAll("[data-import]").forEach(button => button.addEventListener("click", async () => { await reviewInbox(button.dataset.import, "imported"); }));
  document.querySelectorAll("[data-reject]").forEach(button => button.addEventListener("click", async () => { await reviewInbox(button.dataset.reject, "rejected"); }));
}
async function reviewInbox(id, state) { await api(`/api/v1/federation/inbox/${id}`, {method:"PATCH", body:JSON.stringify({state, reason:"Rejected by operator"})}); window.dispatchEvent(new Event("outpost:federation-reviewed")); await refreshInbox(); }
async function loadSyncStatus() {
  const policy = document.querySelector(".path-grid").closest(".panel");
  const panel = document.createElement("section");
  panel.className = "panel content-panel sync-panel";
  panel.innerHTML = `<div class="heading"><div><p class="eyebrow">SYNC HEALTH</p><h2>Federation transfers</h2></div><button id="refresh-sync" class="small-button">Refresh</button></div><div id="sync-status"><p class="empty">No paired sync activity yet.</p></div>`;
  policy.parentElement.insertBefore(panel, policy);
  $("refresh-sync").addEventListener("click", refreshSyncStatus);
  await refreshSyncStatus();
}
async function refreshSyncStatus() {
  const target = $("sync-status"); if (!target) return;
  const response = await api("/api/v1/federation/sync-status"); if (!response.ok) return;
  const result = await response.json();
  const items = result.items;
  target.innerHTML = `<div class="transfer-summary"><span><b>${result.outbound.frames_24h}</b> mesh frames · 24h</span><span>${result.outbound.last_at ? `Last outbound ${age(result.outbound.last_at)}` : "No outbound federation traffic"}</span></div>` + (items.map(peer => { const transfer=peer.transfers,delivery=transfer.deliveries,security=transfer.security,catchup=transfer.catch_up,radio=transfer.paths.radio,mqtt=transfer.paths.mqtt; const success=Math.max(peer.last_sync_at||0,delivery.last_delivered_at||0,...peer.cursors.map(cursor=>cursor.updated_at||0)); const health=security.rejected_24h||delivery.errors?"attention":delivery.pending||catchup.active?"delayed":"healthy"; const state=catchup.active?"catching up":health; const reasons=security.recent.map(item=>`${safe(item.reason)} · ${age(item.created_at)}`).join("<br>"); return `<article class="transfer-card ${health}"><div class="transfer-head"><div><strong>${safe(peer.node_name || peer.mesh_id)}</strong><code>${safe(peer.mesh_id)}</code></div><span class="transfer-state">${state}</span></div><div class="path-metrics"><div><span class="path-icon radio">⌁</span><b>${radio.count_24h}</b><small>LoRa received · 24h</small><em>${radio.last_at?age(radio.last_at):"No observed traffic"}</em></div><div><span class="path-icon mqtt">◫</span><b>${mqtt.count_24h}</b><small>MQTT received · 24h</small><em>${mqtt.last_at?age(mqtt.last_at):"No observed traffic"}</em></div></div><div class="delivery-metrics"><span><b>${delivery.delivered}</b><small>Delivered</small></span><span><b>${delivery.pending}</b><small>Queued</small></span><span><b>${delivery.retries}</b><small>Retries</small></span><span><b>${delivery.recovered}</b><small>Recovered</small></span></div>${catchup.active?`<div class="catchup-strip"><strong>↻ Bounded catch-up in progress</strong><span>${catchup.waiting?"Awaiting peer response":"Next batch scheduled"} · snapshot ${new Date(catchup.snapshot*1000).toLocaleString()}</span></div>`:""}<div class="security-strip ${security.rejected_24h?"warn":"clear"}"><strong>${security.rejected_24h?`⚠ ${security.rejected_24h} rejected frame${security.rejected_24h===1?"":"s"} · 24h`:"✓ No rejected frames · 24h"}</strong>${reasons?`<span>${reasons}</span>`:""}</div><div class="transfer-foot"><span>${success?`Last success ${age(success)}`:"No successful sync yet"}</span><span>Authenticated frames ${peer.tx_counter} sent · ${peer.rx_counter} received</span><span>${peer.cursors.length} active cursor${peer.cursors.length===1?"":"s"}</span></div></article>`; }).join("") || `<p class="empty">No paired sync activity yet.</p>`);
  document.querySelectorAll(".transfer-card").forEach((card,index) => { const services=items[index]?.transfers?.services;if(!services)return;const usage=services.usage||{},open=(services.circuits||[]).filter(value=>(value.open_until||0)>Date.now()/1000).map(value=>value.service);const strip=document.createElement("div");strip.className=`service-budget-strip ${open.length?"warn":""}`;strip.innerHTML=`<strong>Peer services · ${services.permissions.length?services.permissions.map(safe).join(", "):"denied"}${open.length?` · circuit open: ${open.map(safe).join(", ")}`:""}</strong><span>${safe(usage.requests||0)} / ${safe(services.request_limit)} requests · ${Number(usage.response_airtime_seconds||0).toFixed(1)} / ${safe(services.airtime_limit_seconds)} sec airtime · ${safe(usage.denied||0)} denied</span>`;card.querySelector(".security-strip")?.before(strip); });
}
async function loadPeerPolicy() {
  const peers = await api("/api/v1/federation/peers?state=active").then(r => r.json()).then(v => v.items);
  const policy = document.querySelector(".path-grid").closest(".panel");
  const panel = document.createElement("section"); panel.className = "panel content-panel policy-panel";
  panel.innerHTML = `<div class="heading"><div><p class="eyebrow">PEER POLICY</p><h2>Sharing and relay permissions</h2></div></div><form id="peer-policy-form" class="policy-form"><label><span>Paired Outpost</span><select id="policy-peer">${peers.map(p => `<option value="${safe(p.mesh_id)}">${safe(p.node_name || p.mesh_id)}</option>`).join("")}</select></label><label><span>Board slugs</span><input id="policy-boards" placeholder="gen, roads"></label><label><span>Sync incidents</span><input id="policy-incidents" type="checkbox"></label><label><span>Peer latitude</span><input id="policy-incident-lat" type="number" min="-90" max="90" step="any" placeholder="Required for located incidents"></label><label><span>Peer longitude</span><input id="policy-incident-lon" type="number" min="-180" max="180" step="any" placeholder="Required for located incidents"></label><label><span>Incident radius · km</span><input id="policy-incident-radius" type="number" min="1" max="500" value="25"></label><p class="policy-privacy">Located incidents are shared only inside this boundary. Without peer coordinates, only incidents without a location can leave this Outpost. Member positions and waypoints are never federated.</p><label><span>Relay alerts</span><input id="policy-alerts" type="checkbox"></label><label><span>Encrypted mail relay</span><input id="policy-mail" type="checkbox"></label><label><span>Items/hour</span><input id="policy-item-quota" type="number" min="1" max="500" value="20"></label><label><span>Mail/hour</span><input id="policy-mail-quota" type="number" min="1" max="100" value="20"></label><div class="policy-service-permissions"><b>Serve peer requests</b><label><input type="checkbox" value="weather">Weather</label><label><input type="checkbox" value="alerts">Alerts</label><label><input type="checkbox" value="knowledge">Knowledge</label></div><label><span>Service requests/hour</span><input id="policy-service-quota" type="number" min="1" max="60" value="6"></label><label><span>Service concurrency</span><input id="policy-service-concurrency" type="number" min="1" max="4" value="1"></label><label><span>Max response bytes</span><input id="policy-service-bytes" type="number" min="256" max="1600" value="1200"></label><label><span>Service airtime sec/hour</span><input id="policy-service-airtime" type="number" min="1" max="120" value="15"></label><button ${peers.length ? "" : "disabled"}>Save peer policy</button><p id="policy-result">${peers.length ? "Changes affect only the selected paired Outpost." : "Pair an Outpost before configuring sharing."}</p></form>`;
  policy.parentElement.insertBefore(panel, policy);
  function fill() { const peer = peers.find(p => p.mesh_id === $("policy-peer").value); if (!peer) return; $("policy-boards").value = peer.boards.join(", "); $("policy-incidents").checked = peer.sync_incidents; $("policy-incident-lat").value = peer.incident_lat ?? ""; $("policy-incident-lon").value = peer.incident_lon ?? ""; $("policy-incident-radius").value = peer.incident_radius_km ?? 25; $("policy-alerts").checked = peer.relay_alerts; $("policy-mail").checked = peer.relay_mail; $("policy-item-quota").value = peer.quota_items_per_hour; $("policy-mail-quota").value = peer.quota_mail_per_hour; document.querySelectorAll(".policy-service-permissions input").forEach(input => { input.checked = (peer.service_permissions || []).includes(input.value); }); $("policy-service-quota").value = peer.quota_services_per_hour || 6; $("policy-service-concurrency").value = peer.service_concurrency || 1; $("policy-service-bytes").value = peer.service_max_response_bytes || 1200; $("policy-service-airtime").value = peer.service_airtime_seconds_per_hour || 15; }
  $("policy-peer").addEventListener("change", fill); fill();
  $("peer-policy-form").addEventListener("submit", async event => { event.preventDefault(); const lat=$("policy-incident-lat").value,lon=$("policy-incident-lon").value; const response = await api(`/api/v1/federation/peers/${encodeURIComponent($("policy-peer").value)}/sync-policy`, {method:"PUT", body:JSON.stringify({boards:$("policy-boards").value.split(",").map(v => v.trim()).filter(Boolean),sync_incidents:$("policy-incidents").checked,incident_lat:lat===""?null:Number(lat),incident_lon:lon===""?null:Number(lon),incident_radius_km:Number($("policy-incident-radius").value),relay_alerts:$("policy-alerts").checked,relay_mail:$("policy-mail").checked,quota_items_per_hour:Number($("policy-item-quota").value),quota_mail_per_hour:Number($("policy-mail-quota").value),service_permissions:[...document.querySelectorAll(".policy-service-permissions input:checked")].map(input=>input.value),quota_services_per_hour:Number($("policy-service-quota").value),service_concurrency:Number($("policy-service-concurrency").value),service_max_response_bytes:Number($("policy-service-bytes").value),service_airtime_seconds_per_hour:Number($("policy-service-airtime").value)})}); const body = await response.json(); $("policy-result").textContent = response.ok ? "Peer policy saved with explicit service limits." : body.error.message; });
}
async function loadRelayMail() {
  const peers = await api("/api/v1/federation/peers?state=active").then(r => r.json()).then(v => v.items.filter(p => p.relay_mail));
  const directory = $("peer-list").closest(".panel");
  const panel = document.createElement("section"); panel.className = "panel content-panel relay-mail-panel";
  panel.innerHTML = `<div class="heading"><div><p class="eyebrow">ENCRYPTED RELAY MAIL</p><h2>Outpost-to-Outpost delivery</h2></div></div><form id="relay-mail-form" class="relay-mail-form"><select id="relay-mail-peer">${peers.map(p => `<option value="${safe(p.mesh_id)}">${safe(p.node_name || p.mesh_id)}</option>`).join("")}</select><input id="relay-mail-recipient" maxlength="40" placeholder="Recipient handle or @handle" required><input id="relay-mail-subject" maxlength="120" placeholder="Subject"><textarea id="relay-mail-body" maxlength="800" placeholder="Encrypted message" required></textarea><button ${peers.length ? "" : "disabled"}>Send encrypted relay</button><p id="relay-mail-result">${peers.length ? "" : "Enable mail relay for a paired peer above."}</p></form><div id="relay-mail-history" class="relay-mail-history"></div>`;
  directory.insertAdjacentElement("afterend", panel);
  $("relay-mail-form").addEventListener("submit", async event => { event.preventDefault(); const response = await api("/api/v1/federation/mail", {method:"POST",body:JSON.stringify({peer_mesh_id:$("relay-mail-peer").value,recipient_handle:$("relay-mail-recipient").value,subject:$("relay-mail-subject").value,body:$("relay-mail-body").value})}); const body = await response.json(); $("relay-mail-result").textContent = response.ok ? `Relay ${body.relay_id} queued.` : body.error.message; if (response.ok) $("relay-mail-body").value = ""; await refreshRelayMail(); });
  await refreshRelayMail();
}
async function refreshRelayMail() { const target = $("relay-mail-history"); if (!target) return; const response = await api("/api/v1/federation/mail"); if (!response.ok) return; const items = (await response.json()).items; target.innerHTML = items.map(item => `<article><strong>${safe(item.direction)} · ${safe(item.state)}</strong><span>${safe(item.node_name || item.mesh_id)} → ${safe(item.recipient_handle)}</span><code>${safe(item.relay_id)}</code><time>${new Date(item.created_at*1000).toLocaleString()}</time></article>`).join("") || `<p class="empty">No federated mail deliveries yet.</p>`; }
async function loadOriginHistory() {
  const policy = document.querySelector(".path-grid").closest(".panel");
  const panel = document.createElement("section"); panel.className = "panel content-panel origin-panel";
  panel.innerHTML = `<div class="heading"><div><p class="eyebrow">CONTENT IDENTITY</p><h2>Former Outpost history</h2></div><button id="refresh-origins" class="small-button">Refresh</button></div><p class="mqtt-note">Retained content is never deleted when trust ends. Assign a paired successor only after verifying that its operator controls the former Outpost.</p><div id="origin-history"><p class="empty">Loading retained origins…</p></div>`;
  policy.parentElement.insertBefore(panel, policy);
  $("refresh-origins").addEventListener("click", refreshOriginHistory);
  await refreshOriginHistory();
}
async function refreshOriginHistory() {
  const target = $("origin-history"); if (!target) return;
  const [originResponse, peerResponse] = await Promise.all([api("/api/v1/federation/origins"), api("/api/v1/federation/peers?state=active")]);
  if (!originResponse.ok || !peerResponse.ok) return;
  const origins = (await originResponse.json()).items;
  const peers = (await peerResponse.json()).items;
  target.innerHTML = origins.map(origin => `<article class="sync-row origin-row"><div><strong>${safe(origin.node_name || origin.mesh_id)}</strong><code>${safe(origin.mesh_id)} · ${safe(origin.status)}</code></div><div><b>${origin.thread_count}</b><span>Threads</span></div><div><b>${origin.post_count}</b><span>Posts</span></div>${origin.successor_mesh_id?`<p>Successor: ${safe(origin.successor_name || origin.successor_mesh_id)}<br><code>${safe(origin.successor_mesh_id)}</code></p>`:origin.status==="former"&&peers.length?`<div class="origin-adopt"><select data-origin-peer="${safe(origin.mesh_id)}">${peers.map(peer=>`<option value="${safe(peer.mesh_id)}">${safe(peer.node_name||peer.mesh_id)}</option>`).join("")}</select><button data-adopt-origin="${safe(origin.mesh_id)}">Adopt history</button></div>`:`<p>${origin.status==="former"?"Former peer · retained read-only":"Current peer identity"}</p>`}</article>`).join("") || `<p class="empty">No retained remote board history.</p>`;
  document.querySelectorAll("[data-adopt-origin]").forEach(button=>button.addEventListener("click",async()=>{const oldId=button.dataset.adoptOrigin;const select=document.querySelector(`[data-origin-peer="${oldId}"]`);if(!await window.OutpostUI.confirm({title:"Adopt retained Outpost history?",message:`Assign retained content from ${oldId} to ${select.options[select.selectedIndex].text}. Trust and pairing keys are not transferred.`,confirmLabel:"Adopt history"}))return;const response=await api(`/api/v1/federation/peers/${encodeURIComponent(select.value)}/adopt-origin`,{method:"POST",body:JSON.stringify({old_mesh_id:oldId})});if(!response.ok){await window.OutpostUI.alert({title:"History not adopted",message:(await response.json()).error.message});return;}await refreshOriginHistory();}));
}
async function refresh() {
  const filter = $("peer-filter").value;
  const response = await api(`/api/v1/federation/peers${filter ? `?state=${filter}` : ""}`);
  if (!response.ok) return;
  const items = (await response.json()).items;
  const runtime = await api("/api/v1/status").then(result => result.json());
  const all = filter ? await api("/api/v1/federation/peers").then(r => r.json()).then(v => v.items) : items;
  $("peer-total").textContent = all.length;
  $("peer-pending").textContent = all.filter(p => p.state === "pending").length;
  $("peer-active").textContent = all.filter(p => p.state === "active").length;
  const radioUp = runtime.radio === "up";
  $("fed-state").className = `status ${radioUp ? "up" : "down"}`;
  $("fed-state").innerHTML = `<i></i>${radioUp ? "Discovery enabled" : "Radio unavailable"}`;
  const radioPolicy = document.querySelector(".path-grid article:first-child .pill");
  if (radioPolicy) {
    radioPolicy.textContent = radioUp ? "Connected" : "Disconnected";
    radioPolicy.classList.toggle("live", radioUp);
  }
  $("peer-list").innerHTML = items.map(peer => `<article class="peer-card"><div class="peer-head"><div><strong>${safe(peer.node_name || "Unnamed Outpost")}</strong><br><code>${safe(peer.mesh_id)}</code></div><span class="chip ${safe(peer.state)}">${safe(peer.state)}</span></div><div class="peer-transports">${transportBadges(peer)}</div><div class="peer-meta">${Object.entries(peer.capabilities).filter(([, enabled]) => enabled).map(([name]) => `<span class="chip">${safe(name)}</span>`).join("")}</div><p>Last heard ${age(peer.last_seen_at)} <span class="protocol-label">Federation protocol v${safe(peer.protocol_version)}</span></p>${peer.state === "pairing" ? `<div class="pair-box" data-code-for="${safe(peer.mesh_id)}">Waiting for key exchange…</div>` : ""}<div class="peer-actions">${peer.state === "pending" ? `<button data-pair="${safe(peer.mesh_id)}">Pair securely</button>` : ""}${peer.state === "paused" ? `<button data-state="pending" data-id="${safe(peer.mesh_id)}">Resume review</button>` : peer.state !== "active" && peer.state !== "rejected" ? `<button data-state="paused" data-id="${safe(peer.mesh_id)}">Pause</button>` : ""}${peer.state === "active" ? `<button data-sharing="${safe(peer.mesh_id)}">Sharing setup</button><button class="danger" data-state="pending" data-id="${safe(peer.mesh_id)}">Unpair</button>` : peer.state === "rejected" ? `<button class="danger" data-forget="${safe(peer.mesh_id)}">Forget</button>` : `<button class="danger" data-state="rejected" data-id="${safe(peer.mesh_id)}">Reject</button>`}</div></article>`).join("") || `<p class="empty">No Outposts match this view. Discovery announcements are intentionally infrequent to conserve airtime.</p>`;
  document.querySelectorAll("[data-sharing]").forEach(button => button.addEventListener("click", () => showPolicyWizard(all.find(peer => peer.mesh_id === button.dataset.sharing))));
  document.querySelectorAll("[data-state]").forEach(button => button.addEventListener("click", async () => { await api(`/api/v1/federation/peers/${encodeURIComponent(button.dataset.id)}`, {method:"PATCH", body:JSON.stringify({state:button.dataset.state})}); await refresh(); }));
  document.querySelectorAll("[data-forget]").forEach(button => button.addEventListener("click", async () => { if (!await window.OutpostUI.confirm({title:"Forget rejected Outpost?",message:"The rejected peer record and its federation history will be permanently removed. Retained imported community content is not deleted.",confirmLabel:"Forget Outpost",danger:true})) return; await api(`/api/v1/federation/peers/${encodeURIComponent(button.dataset.forget)}`, {method:"DELETE"}); await refresh(); }));
  document.querySelectorAll("[data-pair]").forEach(button => button.addEventListener("click", async () => { button.disabled = true; await api(`/api/v1/federation/peers/${encodeURIComponent(button.dataset.pair)}/pair`, {method:"POST"}); await refresh(); }));
  for (const box of document.querySelectorAll("[data-code-for]")) {
    const id = box.dataset.codeFor;
    const codeResponse = await api(`/api/v1/federation/peers/${encodeURIComponent(id)}/pairing-code`);
    if (codeResponse.ok) {
      const code = (await codeResponse.json()).confirmation_code;
      const peer = items.find(item => item.mesh_id === id);
      const localApproved = Boolean(peer?.local_approved);
      const remoteApproved = Boolean(peer?.remote_approved);
      box.innerHTML = `<div class="pair-code"><span>Verify on both Outposts</span><strong>${safe(code)}</strong></div><p>Only approve if both operators see this exact code.</p><div class="approval-progress"><span class="${localApproved ? "done" : "waiting"}"><i>${localApproved ? "✓" : "○"}</i>This Outpost: ${localApproved ? "approved" : "awaiting approval"}</span><span class="${remoteApproved ? "done" : "waiting"}"><i>${remoteApproved ? "✓" : "○"}</i>Peer Outpost: ${remoteApproved ? "approved" : "awaiting approval"}</span></div>${localApproved ? `<div class="pair-wait">Approval recorded here. Waiting for the peer Outpost to approve.</div>` : `<div class="pair-controls"><input inputmode="numeric" maxlength="6" value="${safe(code)}" aria-label="Confirmation code"><button data-approve="${safe(id)}">Approve pairing</button></div>`}`;
    }
  }
  document.querySelectorAll("[data-approve]").forEach(button => button.addEventListener("click", async () => { const input = button.parentElement.querySelector("input"); button.disabled = true; const result = await api(`/api/v1/federation/peers/${encodeURIComponent(button.dataset.approve)}/approve`, {method:"POST", body:JSON.stringify({confirmation_code:input.value})}); if (!result.ok) { button.disabled = false; return; } await refresh(); }));
  const unconfigured = all.find(peer => peer.state === "active" && !peer.policy_configured);
  if (unconfigured && !policyWizardOpen) await showPolicyWizard(unconfigured);
}
async function initialize() { const response = await fetch("/api/v1/auth/session"); if (!response.ok) { location.href = "/"; return; } csrf = (await response.json()).csrf_token; await refresh(); await loadMqtt(); await loadServices(); await loadInbox(); await loadSyncStatus(); await loadOriginHistory(); await loadPeerPolicy(); await loadRelayMail(); }
$("refresh-fed").addEventListener("click", refresh); $("peer-filter").addEventListener("change", refresh); initialize(); setInterval(() => { refresh(); refreshServices(); refreshSyncStatus(); }, 15000);
