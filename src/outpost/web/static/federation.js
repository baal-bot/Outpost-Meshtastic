import("/nav.js");
const $ = id => document.getElementById(id);
const safe = value => String(value ?? "").replace(/[&<>'"]/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"})[char]);
let csrf = "";
let policyWizardOpen = false;
let topologyMap = null;
let topologyItems = [];
let topologyIncidents = [];
let topologyFitted = false;
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
function policyMeta(peer) { if (peer.state !== "active") return ""; if (!peer.policy_configured) return `<div class="peer-policy-meta due"><b>Sharing policy needs review</b><span>Choose what this peer may exchange.</span></div>`; const applied=peer.policy_applied_at?new Date(peer.policy_applied_at*1000).toLocaleDateString():"date unavailable";const review=peer.policy_review_at?new Date(peer.policy_review_at*1000).toLocaleDateString():"No review scheduled";const due=peer.policy_review_at&&peer.policy_review_at<Date.now()/1000;return `<div class="peer-policy-meta ${due?"due":""}"><b>Policy by ${safe(peer.policy_applied_by||"operator")}</b><span>Applied ${safe(applied)} · ${due?"Review overdue":safe(review)}</span></div>`; }
async function showPolicyWizard(peer) {
  if (!peer || policyWizardOpen) return;
  policyWizardOpen = true;
  try {
    const {openPolicyWizard} = await import("/federation-policy.js?v=1");
    await openPolicyWizard({peer, api, safe, refresh});
  } finally {
    policyWizardOpen = false;
  }
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
  panel.className = "ui-card panel content-panel mqtt-panel";
  panel.innerHTML = `<div class="heading"><div><p class="eyebrow">MQTT TRANSPORT</p><h2>Meshtastic gateway</h2></div><span class="chip ${mqtt.enabled ? "active" : ""}">${mqtt.enabled ? "Enabled" : "Disabled"}</span></div><p class="mqtt-note">The radio firmware handles broker access and Meshtastic channel encryption. Discovery still creates pending peers and never establishes trust.</p><form id="mqtt-form" class="mqtt-form"><label><span>Enable MQTT</span><input id="mqtt-enabled" type="checkbox" ${mqtt.enabled ? "checked" : ""}></label><label><span>Broker address</span><input id="mqtt-address" value="${safe(mqtt.address)}" placeholder="mqtt.meshtastic.org (firmware default)"></label><label><span>Root topic</span><input id="mqtt-root" value="${safe(mqtt.root || "msh")}"></label><label><span>Federation channel</span><select id="mqtt-channel">${mqtt.channels.map(channel => `<option value="${channel.index}">${safe(channel.name)} · ${channel.index}</option>`).join("")}</select></label><label><span>Use TLS</span><input id="mqtt-tls" type="checkbox" ${mqtt.tls_enabled ? "checked" : ""}></label><label><span>Uplink announcements</span><input id="mqtt-uplink" type="checkbox" ${mqtt.channels[0]?.uplink_enabled ? "checked" : ""}></label><label><span>Receive federation traffic</span><input id="mqtt-downlink" type="checkbox" ${mqtt.channels[0]?.downlink_enabled ? "checked" : ""}></label><button>Apply to radio</button><p id="mqtt-result"></p></form>`;
  const policy = document.querySelector(".path-grid").closest(".panel");
  policy.parentElement.insertBefore(panel, policy);
  const activeChannel = mqtt.channels.find(channel => channel.uplink_enabled || channel.downlink_enabled);
  if (activeChannel) $("mqtt-channel").value = String(activeChannel.index);
  const syncChannelState = () => {
    const channel = mqtt.channels.find(entry => entry.index === Number($("mqtt-channel").value));
    $("mqtt-uplink").checked = Boolean(channel?.uplink_enabled);
    $("mqtt-downlink").checked = Boolean(channel?.downlink_enabled);
  };
  $("mqtt-channel").addEventListener("change", syncChannelState);
  syncChannelState();
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
  panel.className = "ui-card panel content-panel service-panel";
  panel.innerHTML = `<div class="heading"><div><p class="eyebrow">PEER SERVICES</p><h2>Request resilient information</h2></div></div><p class="mqtt-note">Requests go to one capable trusted Outpost at a time. Results include their provider, fetch time, and cache age.</p><form id="service-form" class="service-form"><select id="service-type"><option value="weather">Current weather</option><option value="alerts">Public alerts</option><option value="knowledge">Public knowledge</option></select><input id="service-query" maxlength="200" placeholder="Question (knowledge requests only)"><button>Request from peer</button><span id="service-result"></span></form><div id="service-history" class="service-history"><p class="ui-empty empty">No peer requests yet.</p></div>`;
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
  history.innerHTML = items.map(item => `<article><div><strong>${safe(item.service)} · ${safe(item.status)}</strong><code>${safe(item.peer_mesh_id)}</code></div><p>${item.status === "complete" ? safe(JSON.stringify(item.result)) : safe(item.error || "Awaiting peer response")}</p><small>${safe(serviceProvenance(item))}</small></article>`).join("") || `<p class="ui-empty empty">No peer requests yet.</p>`;
}
async function loadInbox() {
  const directory = $("peer-list").closest(".panel");
  const panel = document.createElement("section");
  panel.id = "federation-inbox";
  panel.className = "ui-card panel content-panel inbox-panel";
  panel.innerHTML = `<div class="heading"><div><p class="eyebrow">FEDERATION INBOX</p><h2>Quarantined records</h2></div><button id="refresh-inbox" class="small-button">Refresh</button></div><p class="mqtt-note">Review remote records before they enter local boards, incidents, or alerts. Imported alerts are not automatically broadcast.</p><div id="fed-inbox"><p class="ui-empty empty">No records awaiting review.</p></div>`;
  directory.parentElement.insertBefore(panel, directory);
  $("refresh-inbox").addEventListener("click", refreshInbox);
  await refreshInbox();
}
async function refreshInbox() {
  const target = $("fed-inbox"); if (!target) return;
  const response = await api("/api/v1/federation/inbox"); if (!response.ok) return;
  const items = (await response.json()).items;
  target.innerHTML = items.map(item => `<article class="inbox-card"><div><strong>${safe(item.stream)}</strong><code>${safe(item.node_name || item.mesh_id)} · ${safe(item.uid)}</code></div><p>${safe(item.payload.headline || item.payload.title || item.payload.subject || item.payload.body || "Federated record")}</p><div><button data-import="${item.id}">Approve import</button><button class="danger" data-reject="${item.id}">Reject</button></div></article>`).join("") || `<p class="ui-empty empty">No records awaiting review.</p>`;
  document.querySelectorAll("[data-import]").forEach(button => button.addEventListener("click", async () => { await reviewInbox(button.dataset.import, "imported"); }));
  document.querySelectorAll("[data-reject]").forEach(button => button.addEventListener("click", async () => { await reviewInbox(button.dataset.reject, "rejected"); }));
}
async function reviewInbox(id, state) { await api(`/api/v1/federation/inbox/${id}`, {method:"PATCH", body:JSON.stringify({state, reason:"Rejected by operator"})}); window.dispatchEvent(new Event("outpost:federation-reviewed")); await refreshInbox(); }
async function loadSyncStatus() {
  const policy = document.querySelector(".path-grid").closest(".panel");
  const panel = document.createElement("section");
  panel.className = "ui-card panel content-panel sync-panel";
  panel.innerHTML = `<div class="heading"><div><p class="eyebrow">SYNC HEALTH</p><h2>Federation transfers</h2></div><button id="refresh-sync" class="small-button">Refresh</button></div><div id="sync-status"><p class="ui-empty empty">No paired sync activity yet.</p></div>`;
  policy.parentElement.insertBefore(panel, policy);
  $("refresh-sync").addEventListener("click", refreshSyncStatus);
  await refreshSyncStatus();
}
async function refreshSyncStatus() {
  const target = $("sync-status"); if (!target) return;
  const response = await api("/api/v1/federation/sync-status"); if (!response.ok) return;
  const result = await response.json();
  const items = result.items;
  target.innerHTML = `<div class="transfer-summary"><span><b>${result.outbound.frames_24h}</b> mesh frames · 24h</span><span>${result.outbound.last_at ? `Last outbound ${age(result.outbound.last_at)}` : "No outbound federation traffic"}</span></div>` + (items.map(peer => { const transfer=peer.transfers,delivery=transfer.deliveries,security=transfer.security,catchup=transfer.catch_up,radio=transfer.paths.radio,mqtt=transfer.paths.mqtt; const success=Math.max(peer.last_sync_at||0,delivery.last_delivered_at||0,...peer.cursors.map(cursor=>cursor.updated_at||0)); const health=peer.sync_paused?"offline":security.rejected_24h||delivery.errors?"attention":delivery.pending||catchup.active?"delayed":"healthy"; const state=peer.sync_paused?"offline · sync paused":catchup.active?"catching up":health; const reasons=security.recent.map(item=>`${safe(item.reason)} · ${age(item.created_at)}`).join("<br>"); return `<article class="transfer-card ${health}"><div class="transfer-head"><div><strong>${safe(peer.node_name || peer.mesh_id)}</strong><code>${safe(peer.mesh_id)}</code></div><span class="transfer-state">${state}</span></div><div class="path-metrics"><div><span class="path-icon radio">⌁</span><b>${radio.count_24h}</b><small>LoRa received · 24h</small><em>${radio.last_at?age(radio.last_at):"No observed traffic"}</em></div><div><span class="path-icon mqtt">◫</span><b>${mqtt.count_24h}</b><small>MQTT received · 24h</small><em>${mqtt.last_at?age(mqtt.last_at):"No observed traffic"}</em></div></div><div class="delivery-metrics"><span><b>${delivery.delivered}</b><small>Delivered</small></span><span><b>${delivery.pending}</b><small>Queued</small></span><span><b>${delivery.retries}</b><small>Retries</small></span><span><b>${delivery.recovered}</b><small>Recovered</small></span></div>${peer.sync_paused?`<div class="offline-strip"><strong>Automatic federation traffic paused</strong><span>Trust is retained. A HELLO or authenticated message will resume syncing.</span></div>`:catchup.active?`<div class="catchup-strip"><strong>↻ Bounded catch-up in progress</strong><span>${catchup.waiting?"Awaiting peer response":"Next batch scheduled"} · snapshot ${new Date(catchup.snapshot*1000).toLocaleString()}</span></div>`:""}<div class="security-strip ${security.rejected_24h?"warn":"clear"}"><strong>${security.rejected_24h?`⚠ ${security.rejected_24h} rejected frame${security.rejected_24h===1?"":"s"} · 24h`:"✓ No rejected frames · 24h"}</strong>${reasons?`<span>${reasons}</span>`:""}</div><div class="transfer-foot"><span>${success?`Last success ${age(success)}`:"No successful sync yet"}</span><span>Authenticated frames ${peer.tx_counter} sent · ${peer.rx_counter} received</span><span>${peer.cursors.length} active cursor${peer.cursors.length===1?"":"s"}</span></div></article>`; }).join("") || `<p class="ui-empty empty">No paired sync activity yet.</p>`);
  document.querySelectorAll(".transfer-card").forEach((card,index) => { const services=items[index]?.transfers?.services;if(!services)return;const usage=services.usage||{},open=(services.circuits||[]).filter(value=>(value.open_until||0)>Date.now()/1000).map(value=>value.service);const strip=document.createElement("div");strip.className=`service-budget-strip ${open.length?"warn":""}`;strip.innerHTML=`<strong>Peer services · ${services.permissions.length?services.permissions.map(safe).join(", "):"denied"}${open.length?` · circuit open: ${open.map(safe).join(", ")}`:""}</strong><span>${safe(usage.requests||0)} / ${safe(services.request_limit)} requests · ${Number(usage.response_airtime_seconds||0).toFixed(1)} / ${safe(services.airtime_limit_seconds)} sec airtime · ${safe(usage.denied||0)} denied</span>`;card.querySelector(".security-strip")?.before(strip); });
}
async function loadRelayMail() {
  const peers = await api("/api/v1/federation/peers?state=active").then(r => r.json()).then(v => v.items.filter(p => p.relay_mail));
  const directory = $("peer-list").closest(".panel");
  const panel = document.createElement("section"); panel.className = "ui-card panel content-panel relay-mail-panel";
  panel.innerHTML = `<div class="heading"><div><p class="eyebrow">ENCRYPTED RELAY MAIL</p><h2>Outpost-to-Outpost delivery</h2></div></div><form id="relay-mail-form" class="relay-mail-form"><select id="relay-mail-peer">${peers.map(p => `<option value="${safe(p.mesh_id)}">${safe(p.node_name || p.mesh_id)}</option>`).join("")}</select><input id="relay-mail-recipient" maxlength="40" placeholder="Recipient handle or @handle" required><input id="relay-mail-subject" maxlength="120" placeholder="Subject"><textarea id="relay-mail-body" maxlength="800" placeholder="Encrypted message" required></textarea><button ${peers.length ? "" : "disabled"}>Send encrypted relay</button><p id="relay-mail-result">${peers.length ? "" : "Enable mail relay for a paired peer above."}</p></form><div id="relay-mail-history" class="relay-mail-history"></div>`;
  directory.insertAdjacentElement("afterend", panel);
  $("relay-mail-form").addEventListener("submit", async event => { event.preventDefault(); const response = await api("/api/v1/federation/mail", {method:"POST",body:JSON.stringify({peer_mesh_id:$("relay-mail-peer").value,recipient_handle:$("relay-mail-recipient").value,subject:$("relay-mail-subject").value,body:$("relay-mail-body").value})}); const body = await response.json(); $("relay-mail-result").textContent = response.ok ? `Relay ${body.relay_id} queued.` : body.error.message; if (response.ok) $("relay-mail-body").value = ""; await refreshRelayMail(); });
  await refreshRelayMail();
}
async function refreshRelayMail() { const target = $("relay-mail-history"); if (!target) return; const response = await api("/api/v1/federation/mail"); if (!response.ok) return; const items = (await response.json()).items; target.innerHTML = items.map(item => `<article><strong>${safe(item.direction)} · ${safe(item.state)}</strong><span>${safe(item.node_name || item.mesh_id)} → ${safe(item.recipient_handle)}</span><code>${safe(item.relay_id)}</code><time>${new Date(item.created_at*1000).toLocaleString()}</time></article>`).join("") || `<p class="ui-empty empty">No federated mail deliveries yet.</p>`; }
async function loadTopology() {
  const directory = $("peer-list").closest(".panel");
  const panel = document.createElement("section");
  panel.className = "ui-card panel content-panel topology-panel";
  panel.innerHTML = `<div class="heading"><div><p class="eyebrow">REGIONAL TOPOLOGY</p><h2>Trusted Outpost health</h2></div><div class="topology-tools"><label><input id="topology-incidents" type="checkbox"> Show incidents</label><button id="refresh-topology" class="small-button">Refresh</button></div></div><p class="mqtt-note">Only active peers that explicitly shared a coarse location appear on the map. The identity and health list remains available without map tiles.</p><div class="topology-workspace"><div id="topology-map" class="outpost-map" tabindex="0" aria-label="Interactive federation topology map. Use arrow keys to pan and plus or minus to zoom."><div class="outpost-map-tiles"></div><div class="outpost-map-markers"></div><div class="ui-map-controls outpost-map-controls"><button data-map-action="zoom-in" title="Zoom in" aria-label="Zoom in">+</button><button data-map-action="zoom-out" title="Zoom out" aria-label="Zoom out">−</button><button data-map-action="fit" title="Fit shared Outposts" aria-label="Fit shared Outposts">⌂</button></div><span class="outpost-map-coordinates">—</span><div class="outpost-map-attribution"></div><p class="outpost-map-empty">No trusted peers have shared a map location.</p><aside id="topology-map-detail" class="outpost-map-detail" hidden></aside></div><div id="topology-list" class="topology-list"><p class="ui-empty empty">Loading federation identities…</p></div></div>`;
  directory.insertAdjacentElement("afterend", panel);
  topologyMap = new window.OutpostMap.Controller({root:$("topology-map"),detail:$("topology-map-detail"),onFit:fitTopology,onBackground:closeTopologyDetail});
  $("refresh-topology").addEventListener("click", refreshTopology);
  $("topology-incidents").addEventListener("change", refreshTopologyIncidents);
  await refreshTopology();
}
function topologyDefinitions() {
  const peers = topologyItems.filter(item => item.location && item.raw_state === "active").map(item => ({id:`topology-${item.mesh_id}`,lat:item.location.lat,lon:item.location.lon,className:`shape-diamond ${item.degraded ? "tone-caution" : "tone-ok"}`,title:`${item.node_name || item.mesh_id} · ${item.state}`,label:`Show topology health for ${item.node_name || item.mesh_id}`,data:{...item,markerKind:"peer"},onActivate:showTopologyDetail}));
  const incidents = topologyIncidents.filter(item => item.lat != null && item.lon != null).map(item => ({id:`topology-incident-${item.id}`,lat:item.lat,lon:item.lon,className:`shape-circle tone-${item.severity || "info"}`,title:`INC ${item.local_ref}: ${item.title}`,label:`Show incident ${item.local_ref}: ${item.title}`,data:{...item,markerKind:"incident"},onActivate:showTopologyDetail}));
  return [...peers,...incidents];
}
function renderTopologyMap() { if (!topologyMap) return; const definitions=topologyDefinitions();topologyMap.setMarkers(definitions);topologyMap.setEmpty(definitions.length===0);if(!topologyFitted&&definitions.length){topologyFitted=true;topologyMap.fit(definitions,{maxZoom:10});} }
function fitTopology() { const definitions=topologyDefinitions();if(definitions.length)topologyMap.fit(definitions,{maxZoom:10}); }
function closeTopologyDetail() { topologyMap?.clearSelection();const detail=$("topology-map-detail");if(detail)detail.hidden=true; }
function showTopologyDetail(item) {
  const detail=$("topology-map-detail");if(!detail)return;
  detail.hidden=false;
  if(item.markerKind==="incident"){detail.innerHTML=`<button class="close-map" aria-label="Close">×</button><p class="eyebrow">OPTIONAL INCIDENT LAYER</p><h3>INC ${safe(item.local_ref)} · ${safe(item.title)}</h3><p>${safe(item.severity || "info")} · ${safe(item.type || "incident")}</p><p><a href="/watch.html">Open Community Watch →</a></p>`;detail.querySelector(".close-map").onclick=closeTopologyDetail;return;}
  const paths=["radio","mqtt"].map(name=>{const path=item.paths?.[name]||{};return `<span class="transport-chip ${name}">${name==="radio"?"⌁ LoRa":"◫ MQTT"} · ${path.last_at?age(path.last_at):"not observed"}</span>`;}).join("");
  const reasons=(item.degraded_reasons||[]).map(reason=>`<li>${safe(reason)}</li>`).join("");
  const audits=(item.audit||[]).map(event=>`<li>${safe(event.action)} · ${age(event.created_at)}</li>`).join("")||"<li>No matching audit events.</li>";
  const policy=item.location_policy;
  const shareForm=policy?`<form id="topology-location-form" class="topology-location-form"><label class="topology-share-check"><input id="topology-share" type="checkbox" ${policy.share_location?"checked":""}> Share this Outpost's coarse location with this peer</label><label>Latitude<input id="topology-share-lat" type="number" min="-90" max="90" step="0.00001" value="${safe(policy.lat ?? "")}"></label><label>Longitude<input id="topology-share-lon" type="number" min="-180" max="180" step="0.00001" value="${safe(policy.lon ?? "")}"></label><label>Precision<select id="topology-share-precision"><option value="10">10 km</option><option value="25">25 km</option><option value="50">50 km</option><option value="100">100 km</option></select></label><button>Save location policy</button><small>Coordinates are rounded to the selected precision before authenticated delivery.</small><span id="topology-policy-result"></span></form>`:"";
  detail.innerHTML=`<button class="close-map" aria-label="Close">×</button><p class="eyebrow">${safe(item.identity_kind || "current")} IDENTITY · ${safe(item.state)}</p><h3>${safe(item.node_name || item.mesh_id)}</h3><code>${safe(item.mesh_id)}</code><div class="topology-paths">${paths}</div><p>Preferred: <b>${safe(item.preferred_path || "unavailable")}</b> · Last successful: <b>${safe(item.last_successful_path || "none")}</b><br>Last seen ${age(item.last_seen_at)} · Last sync ${age(item.last_sync_at)} · Backlog ${safe(item.backlog || 0)}</p>${item.location?`<p>Approximate location · ${safe(item.location.precision_km)} km precision · received ${age(item.location.received_at)}</p>`:"<p>Remote location not shared. This identity remains list-only.</p>"}${reasons?`<ul class="topology-warnings">${reasons}</ul>`:""}<details><summary>Delivery, service, and policy context</summary><p>Delivery errors ${safe(item.delivery?.errors||0)} · rejected frames ${safe(item.delivery?.rejected_24h||0)}<br>Services: ${item.services?.length?item.services.map(safe).join(", "):"none"}<br>Boards: ${item.policy?.boards?.length?item.policy.boards.map(safe).join(", "):"none"}<br>Incident sync ${item.policy?.sync_incidents?"enabled":"disabled"} · relay ${item.policy?.relay_enabled?(item.policy.relay_paused?"paused":"enabled"):"disabled"}</p></details><details><summary>Recent audit context</summary><ul>${audits}</ul></details>${shareForm}`;
  detail.querySelector(".close-map").onclick=closeTopologyDetail;
  const form=$("topology-location-form");
  if(form){
    $("topology-share-precision").value=String(policy.precision_km);
    form.addEventListener("submit",async event=>{
      event.preventDefault();
      const sharing=$("topology-share").checked,lat=$("topology-share-lat").value,lon=$("topology-share-lon").value;
      if(sharing&&(!lat||!lon)){$("topology-policy-result").textContent="Latitude and longitude are required to share a location.";return;}
      if(sharing&&!policy.share_location&&!await window.OutpostUI.confirm({title:"Share a coarse Outpost location?",message:`${item.node_name||item.mesh_id} will receive this location rounded to the selected precision. This is separate from incident-sharing policy.`,confirmLabel:"Enable sharing"}))return;
      const body={share_location:sharing,location_lat:sharing?Number(lat):null,location_lon:sharing?Number(lon):null,precision_km:Number($("topology-share-precision").value)};
      const response=await api(`/api/v1/federation/topology/peers/${encodeURIComponent(item.mesh_id)}`,{method:"PUT",body:JSON.stringify(body)}),result=await response.json();
      $("topology-policy-result").textContent=response.ok?"Location-sharing policy saved; authenticated update queued.":result.error.message;
      if(response.ok)await refreshTopology();
    });
  }
}
async function refreshTopologyIncidents(){topologyIncidents=[];if($("topology-incidents").checked){const response=await api("/api/v1/watch/map?hours_ago=24");if(response.ok)topologyIncidents=(await response.json()).incidents||[];}renderTopologyMap();}
async function refreshTopology() {
  const target=$("topology-list");if(!target)return;
  const response=await api("/api/v1/federation/topology");if(!response.ok)return;
  const result=await response.json();topologyItems=result.items||[];
  target.innerHTML=topologyItems.map(item=>`<button class="topology-peer ${item.degraded?"degraded":""}" data-topology-peer="${safe(item.mesh_id)}"><span><strong>${safe(item.node_name||item.mesh_id)}</strong><code>${safe(item.mesh_id)}</code></span><span class="chip ${safe(item.state)}">${safe(item.state)}</span><small>${safe(item.identity_kind||"current")} identity · ${item.location?`shared · ${safe(item.location.precision_km)} km precision`:"list only"} · ${safe(item.backlog||0)} queued<br>${item.transports?.length?item.transports.map(safe).join(" + "):"No active transport"}</small></button>`).join("")||`<p class="ui-empty empty">No discovered, paired, adopted, or forgotten Outpost identities.</p>`;
  document.querySelectorAll("[data-topology-peer]").forEach(button=>button.addEventListener("click",()=>{const item=topologyItems.find(value=>value.mesh_id===button.dataset.topologyPeer);if(!item)return;if(item.location)topologyMap.setView({lat:item.location.lat,lon:item.location.lon,zoom:9});showTopologyDetail({...item,markerKind:"peer"});}));
  renderTopologyMap();
}
async function loadStoreForward() {
  const directory = $("peer-list").closest(".panel");
  const panel = document.createElement("section");
  panel.className = "ui-card panel content-panel store-forward-panel";
  panel.innerHTML = `<div class="heading"><div><p class="eyebrow">SIGNED STORE-AND-FORWARD</p><h2>Disconnected delivery</h2></div><button id="refresh-relay" class="small-button">Refresh</button></div><p class="mqtt-note">Every hop requires an explicit peer policy. Origin signatures and idempotency survive relays; routing metadata is visible to relay operators.</p><div id="relay-summary" class="relay-summary"></div><form id="relay-create" class="relay-create"><input id="relay-destination" aria-label="Relay destination node ID" pattern="^![0-9a-fA-F]{8}$" placeholder="Destination !1234abcd" required><select id="relay-scope" aria-label="Relay content scope"><option value="request">Request</option><option value="incident">Incident</option><option value="receipt">Receipt</option></select><textarea id="relay-payload" aria-label="Relay payload JSON" maxlength="700" placeholder='Payload JSON, for example {"kind":"status"}' required></textarea><label>Hop limit <input id="relay-hops" type="number" min="1" max="4" value="3"></label><button>Queue signed envelope</button><span id="relay-result"></span></form><div class="relay-columns"><section><h3>Peer relay policies</h3><div id="relay-policies"></div></section><section><h3>Observed origin keys</h3><div id="relay-origins"></div></section></div><h3>Custody queue</h3><div id="relay-queue"></div>`;
  directory.insertAdjacentElement("afterend", panel);
  $("refresh-relay").addEventListener("click", refreshStoreForward);
  $("relay-create").addEventListener("submit", async event => {
    event.preventDefault();
    let payload;
    try { payload = JSON.parse($("relay-payload").value); } catch { $("relay-result").textContent = "Payload must be valid JSON."; return; }
    const response = await api("/api/v1/federation/relay", {method:"POST", body:JSON.stringify({destination:$("relay-destination").value,scope:$("relay-scope").value,payload,hop_limit:Number($("relay-hops").value)})});
    const body = await response.json();
    $("relay-result").textContent = response.ok ? `Envelope ${body.envelope_id} queued.` : body.error.message;
    if (response.ok) $("relay-payload").value = "";
    await refreshStoreForward();
  });
  await refreshStoreForward();
}
async function refreshStoreForward() {
  const queue = $("relay-queue"); if (!queue) return;
  const response = await api("/api/v1/federation/relay"); if (!response.ok) return;
  const result = await response.json();
  const active = ["queued","quarantined","paused","forwarding","forwarded"].reduce((total,state)=>total+(result.summary.counts[state]||0),0);
  $("relay-summary").innerHTML = `<span><b>${active}</b> active custody items</span><span><b>${result.summary.stored_bytes}</b> stored bytes</span><span>Direct paired destinations are preferred automatically.</span>`;
  $("relay-policies").innerHTML = result.policies.map(policy => `<article><div><strong>${safe(policy.mesh_id)}</strong><small>${policy.enabled ? safe(policy.scopes.join(", ")) : "Relay disabled"}</small></div><button data-relay-enable="${safe(policy.mesh_id)}">${policy.enabled ? "Disable" : "Enable"}</button>${policy.enabled ? `<button data-relay-pause="${safe(policy.mesh_id)}">${policy.paused ? "Resume" : "Pause"}</button>` : ""}</article>`).join("") || `<p class="ui-empty empty">Pair an Outpost before granting relay custody.</p>`;
  $("relay-origins").innerHTML = result.origins.map(origin => `<article><div><strong>${safe(origin.origin_node)}</strong><small>${safe(origin.fingerprint.slice(0,16))}… · ${safe(origin.state)}</small></div>${origin.state === "observed" ? `<button data-origin-trust="${safe(origin.origin_node)}">Trust</button><button class="danger" data-origin-reject="${safe(origin.origin_node)}">Reject</button>` : ""}</article>`).join("") || `<p class="ui-empty empty">No relayed origin keys observed.</p>`;
  queue.innerHTML = result.queue.map(item => `<article class="relay-item"><div><strong>${safe(item.scope)} · ${safe(item.state)}</strong><code>${safe(item.envelope_id)}</code></div><p>${safe(item.origin_node)} → ${safe(item.destination_node)}<br>Route: ${item.route.map(safe).join(" → ")}${item.last_path ? `<br>Selected ${safe(item.last_path)} path → ${safe(item.next_hop_mesh_id)}` : ""}${item.received_transport ? `<br>Received by ${safe(item.received_transport.toUpperCase())}` : ""}</p><small>Expires ${new Date(item.expires_at*1000).toLocaleString()}${item.last_error ? ` · ${safe(item.last_error)}` : ""}</small><div>${["queued","forwarding"].includes(item.state)?`<button data-relay-action="pause" data-envelope="${safe(item.envelope_id)}">Pause</button>`:""}${item.state==="paused"?`<button data-relay-action="resume" data-envelope="${safe(item.envelope_id)}">Resume</button>`:""}${item.state!=="purged"?`<button class="danger" data-relay-action="purge" data-envelope="${safe(item.envelope_id)}">Purge payload</button>`:""}</div></article>`).join("") || `<p class="ui-empty empty">No relay custody history.</p>`;
  document.querySelectorAll("[data-relay-enable],[data-relay-pause]").forEach(button => button.addEventListener("click", async () => { const meshId=button.dataset.relayEnable||button.dataset.relayPause;const current=result.policies.find(value=>value.mesh_id===meshId);const body={enabled:button.dataset.relayEnable!==undefined?!current.enabled:current.enabled,paused:button.dataset.relayPause!==undefined?!current.paused:false,scopes:current.scopes.length?current.scopes:["incident","request"],max_stored_items:current.max_stored_items,max_stored_bytes:current.max_stored_bytes,rate_per_hour:current.rate_per_hour,airtime_seconds_per_hour:current.airtime_seconds_per_hour};await api(`/api/v1/federation/relay/peers/${encodeURIComponent(meshId)}`,{method:"PUT",body:JSON.stringify(body)});await refreshStoreForward(); }));
  document.querySelectorAll("[data-origin-trust],[data-origin-reject]").forEach(button => button.addEventListener("click", async () => { const origin=button.dataset.originTrust||button.dataset.originReject;const state=button.dataset.originTrust?"trusted":"rejected";await api(`/api/v1/federation/relay/origins/${encodeURIComponent(origin)}`,{method:"PATCH",body:JSON.stringify({state})});await refreshStoreForward(); }));
  document.querySelectorAll("[data-relay-action]").forEach(button => button.addEventListener("click", async () => { await api(`/api/v1/federation/relay/${encodeURIComponent(button.dataset.envelope)}`,{method:"PATCH",body:JSON.stringify({action:button.dataset.relayAction})});await refreshStoreForward(); }));
}
async function loadOriginHistory() {
  const policy = document.querySelector(".path-grid").closest(".panel");
  const panel = document.createElement("section"); panel.className = "ui-card panel content-panel origin-panel";
  panel.innerHTML = `<div class="heading"><div><p class="eyebrow">CONTENT IDENTITY</p><h2>Former Outpost history</h2></div><button id="refresh-origins" class="small-button">Refresh</button></div><p class="mqtt-note">Retained content is never deleted when trust ends. Assign a paired successor only after verifying that its operator controls the former Outpost.</p><div id="origin-history"><p class="ui-empty empty">Loading retained origins…</p></div>`;
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
  target.innerHTML = origins.map(origin => `<article class="sync-row origin-row"><div><strong>${safe(origin.node_name || origin.mesh_id)}</strong><code>${safe(origin.mesh_id)} · ${safe(origin.status)}</code></div><div><b>${origin.thread_count}</b><span>Threads</span></div><div><b>${origin.post_count}</b><span>Posts</span></div>${origin.successor_mesh_id?`<p>Successor: ${safe(origin.successor_name || origin.successor_mesh_id)}<br><code>${safe(origin.successor_mesh_id)}</code></p>`:origin.status==="former"&&peers.length?`<div class="origin-adopt"><select data-origin-peer="${safe(origin.mesh_id)}">${peers.map(peer=>`<option value="${safe(peer.mesh_id)}">${safe(peer.node_name||peer.mesh_id)}</option>`).join("")}</select><button data-adopt-origin="${safe(origin.mesh_id)}">Adopt history</button></div>`:`<p>${origin.status==="former"?"Former peer · retained read-only":"Current peer identity"}</p>`}</article>`).join("") || `<p class="ui-empty empty">No retained remote board history.</p>`;
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
  $("fed-state").className = `ui-pill status ${radioUp ? "up" : "down"}`;
  $("fed-state").innerHTML = `<i></i>${radioUp ? "Discovery enabled" : "Radio unavailable"}`;
  const radioPolicy = document.querySelector(".path-grid article:first-child .pill");
  if (radioPolicy) {
    radioPolicy.textContent = radioUp ? "Connected" : "Disconnected";
    radioPolicy.classList.toggle("live", radioUp);
  }
  $("peer-list").innerHTML = items.map(peer => { const visibleState=peer.state==="active"?(peer.connectivity||"active"):peer.state; const liveness=peer.state==="active"?(peer.sync_paused?`<p class="peer-connectivity offline"><strong>Paired · automatic sync paused</strong><span>Trust is retained. Sync resumes after a HELLO or authenticated message.</span></p>`:`<p class="peer-connectivity online"><strong>Paired · online</strong><span>Automatic federation sync is enabled.</span></p>`):""; return `<article class="peer-card ${peer.sync_paused?"offline":""}"><div class="peer-head"><div><strong>${safe(peer.node_name || "Unnamed Outpost")}</strong><br><code>${safe(peer.mesh_id)}</code></div><span class="chip ${safe(visibleState)}">${safe(visibleState)}</span></div>${liveness}<div class="peer-transports">${transportBadges(peer)}</div>${policyMeta(peer)}<div class="peer-meta">${Object.entries(peer.capabilities).filter(([, enabled]) => enabled).map(([name]) => `<span class="chip">${safe(name)}</span>`).join("")}</div><p>Last heard ${age(peer.last_seen_at)} <span class="protocol-label">Federation protocol v${safe(peer.protocol_version)}</span></p>${peer.state === "pairing" ? `<div class="pair-box" data-code-for="${safe(peer.mesh_id)}">Waiting for key exchange…</div>` : ""}<div class="peer-actions">${peer.state === "pending" ? `<button data-pair="${safe(peer.mesh_id)}">Pair securely</button>` : ""}${peer.state === "paused" ? `<button data-state="pending" data-id="${safe(peer.mesh_id)}">Resume review</button>` : peer.state !== "active" && peer.state !== "rejected" ? `<button data-state="paused" data-id="${safe(peer.mesh_id)}">Pause</button>` : ""}${peer.state === "active" ? `<button data-sharing="${safe(peer.mesh_id)}">Sharing setup</button><button class="danger" data-state="pending" data-id="${safe(peer.mesh_id)}">Unpair</button>` : peer.state === "rejected" ? `<button class="danger" data-forget="${safe(peer.mesh_id)}">Forget</button>` : `<button class="danger" data-state="rejected" data-id="${safe(peer.mesh_id)}">Reject</button>`}</div></article>`; }).join("") || `<p class="ui-empty empty">No Outposts match this view. Discovery announcements are intentionally infrequent to conserve airtime.</p>`;
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
async function initialize() { const response = await fetch("/api/v1/auth/session"); if (!response.ok) { location.href = "/"; return; } csrf = (await response.json()).csrf_token; await refresh(); await loadMqtt(); await loadServices(); await loadInbox(); await loadSyncStatus(); await loadOriginHistory(); await loadRelayMail(); await loadStoreForward(); await loadTopology(); const {scheduler}=await import("/refresh-scheduler.js"); scheduler.schedule("federation-main",()=>Promise.all([refresh(),refreshServices(),refreshSyncStatus(),refreshStoreForward(),refreshTopology()]),{interval:15000}); }
$("refresh-fed").addEventListener("click", refresh); $("peer-filter").addEventListener("change", refresh); initialize();
