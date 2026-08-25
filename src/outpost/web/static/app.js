import("/nav.js");
document.head.insertAdjacentHTML("beforeend", '<link rel="stylesheet" href="/weather.css?v=1">');
document.head.insertAdjacentHTML("beforeend", '<link rel="stylesheet" href="/weather-controls.css?v=2">');
const $ = (id) => document.getElementById(id);
const authHintKey = "outpost.operator.authenticated";
if (sessionStorage.getItem(authHintKey) === "true") $("login-screen").classList.add("hidden");
const safe = (value) => String(value).replace(/[&<>'"]/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;","\"":"&quot;"})[char]);
const ago = (stamp) => { const seconds = Math.max(0, (Date.now() - new Date(stamp)) / 1000); if (seconds < 60) return "now"; if (seconds < 3600) return `${Math.floor(seconds / 60)}m`; if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`; return `${Math.floor(seconds / 86400)}d`; };
const item = (title, description, badge) => `<div class="item"><div><strong>${safe(title)}</strong><p>${safe(description || "")}</p></div><span class="badge">${safe(badge || "")}</span></div>`;
document.querySelector(".kpis").insertAdjacentHTML("afterend", '<section class="panel weather-panel"><div><p class="eyebrow">LOCAL CONDITIONS</p><h2>Weather</h2><p id="weather-summary">Set the Outpost location to enable weather.</p></div><div id="weather-reading" class="weather-reading"><strong>—</strong><span>Not configured</span></div><div id="weather-details" class="weather-details"></div></section>');
document.querySelector(".weather-panel>div").insertAdjacentHTML("beforeend", '<div id="provider-health" class="provider-health"></div>');
document.querySelector(".weather-panel").insertAdjacentHTML("afterend", '<section class="panel forecast-panel"><div class="forecast-heading"><div><p class="eyebrow">LOCAL FORECAST</p><h2>What’s ahead</h2></div><span id="forecast-meta">Loading forecast…</span></div><div id="forecast-days" class="forecast-days"></div><div id="forecast-hours" class="forecast-hours"></div></section>');
document.querySelector(".forecast-panel").insertAdjacentHTML("afterend", '<section class="panel astronomy-panel"><div class="astronomy-heading"><p class="eyebrow">OFFLINE ASTRONOMY</p><h2>Daylight & moon</h2><p id="astronomy-date">Calculated locally from the Outpost position.</p></div><div class="astro-metrics"><div><span>Sunrise</span><strong id="astro-rise">—</strong><small id="astro-dawn">Civil dawn —</small></div><div><span>Sunset</span><strong id="astro-set">—</strong><small id="astro-dusk">Civil dusk —</small></div><div><span>Daylight</span><strong id="astro-daylight">—</strong><small>Above the horizon</small></div><div class="moon-metric"><span>Moon</span><strong id="astro-moon">—</strong><small id="astro-phase">Phase unavailable</small></div></div></section>');
document.querySelector(".astronomy-panel").insertAdjacentHTML("afterend", '<section class="panel seismic-panel"><div class="seismic-heading"><div><p class="eyebrow">USGS SEISMIC MONITOR</p><h2>Nearby earthquakes</h2><p id="seismic-health">Waiting for the first feed update.</p></div><div class="seismic-count"><strong id="seismic-total">0</strong><span>past 24 hours</span></div></div><div id="seismic-list" class="seismic-list"><p class="empty">No nearby earthquakes recorded.</p></div><p class="seismic-credit">Earthquake data courtesy of the U.S. Geological Survey.</p></section>');
const disclaimerLabel = document.querySelector('label[for="setting-disclaimer"]');
disclaimerLabel.insertAdjacentHTML("beforebegin", '<div class="location-settings"><p class="eyebrow">OUTPOST LOCATION</p><p>Used for weather, alerts, map defaults, and regional downloads.</p><label for="setting-lat">Latitude</label><input id="setting-lat" type="number" min="-90" max="90" step="0.00001" placeholder="Waiting for radio GPS"><label for="setting-lon">Longitude</label><input id="setting-lon" type="number" min="-180" max="180" step="0.00001" placeholder="Waiting for radio GPS"><small id="location-source"></small><fieldset class="temperature-units"><legend>Temperature</legend><label><input type="radio" name="setting-units" value="imperial"> °F</label><label><input type="radio" name="setting-units" value="metric"> °C</label></fieldset></div>');
async function refreshWeather() { const response = await fetch("/api/v1/environment/weather"); if (response.status === 401) return; const body = await response.json(); if (!response.ok) { $("weather-summary").textContent = body.error?.message || "Weather unavailable."; $("weather-reading").innerHTML = '<strong>—</strong><span>Setup required</span>'; $("weather-details").innerHTML = ""; return; } const age = body.age_seconds < 60 ? "just now" : `${Math.floor(body.age_seconds / 60)}m ago`,imperial=body.units==="imperial",temperature=imperial?body.temperature_c*9/5+32:body.temperature_c,apparent=imperial?body.apparent_c*9/5+32:body.apparent_c,wind=imperial?body.wind_kph/1.609344:body.wind_kph; $("weather-summary").textContent = `${body.stale ? "Cached conditions" : "Current conditions"} · ${body.provider} · ${age}`; $("weather-reading").innerHTML = `<strong>${Number(temperature).toFixed(1)}°${imperial?"F":"C"}</strong><span>Feels ${Number(apparent).toFixed(1)}°${imperial?"F":"C"}</span>`; $("weather-details").innerHTML = `<span><b>${Number(wind).toFixed(0)} ${imperial?"MPH":"KM/H"}</b><em>Wind speed</em></span><span><b>${Number(body.precipitation_mm).toFixed(1)} MM</b><em>Precipitation</em></span><span><b>${safe(body.wind_direction)}°</b><em>Wind direction</em></span>`; $("weather-reading").classList.toggle("stale", body.stale); }
setTimeout(refreshWeather, 1500); setInterval(refreshWeather, 30000);
async function refreshForecast() { const response=await fetch("/api/v1/environment/forecast"); if(response.status===401)return; const body=await response.json(); if(!response.ok){$("forecast-meta").textContent=body.error?.message||"Forecast unavailable.";$("forecast-days").innerHTML="";$("forecast-hours").innerHTML="";return;} const imperial=body.units==="imperial",temp=(c)=>Math.round(imperial?c*9/5+32:c),wind=(k)=>Math.round(imperial?k/1.609344:k),unit=imperial?"F":"C",windUnit=imperial?"mph":"km/h"; $("forecast-meta").textContent=`${body.provider}${body.stale?" · cached":""} · ${body.age_seconds<60?"updated now":`${Math.floor(body.age_seconds/60)}m old`}`; $("forecast-days").innerHTML=body.daily.slice(0,5).map((day,index)=>`<article class="forecast-day ${index===0?"current":""}"><span>${safe(index===0?"Today":index===1?"Tomorrow":day.name)}</span><strong>${temp(day.high_c)}°<small> / ${temp(day.low_c)}°${unit}</small></strong><p>${safe(day.summary)}</p><div><b>${safe(day.precipitation_probability)}%</b> rain <b>${wind(day.wind_kph)}</b> ${windUnit}</div></article>`).join(""); const now=Date.now()-3600000, hours=body.hourly.filter((hour)=>new Date(hour.start_time).getTime()>now).slice(0,8); $("forecast-hours").innerHTML=hours.map((hour)=>`<div><time>${new Date(hour.start_time).toLocaleTimeString([],{hour:"numeric"})}</time><strong>${temp(hour.temperature_c)}°</strong><span>${safe(hour.precipitation_probability)}% rain</span></div>`).join(""); }
setTimeout(refreshForecast,1700); setInterval(refreshForecast,30000);
async function refreshAstronomy(){const response=await fetch("/api/v1/environment/astronomy");if(response.status===401)return;const body=await response.json();if(!response.ok){$("astronomy-date").textContent=body.error?.message||"Astronomy unavailable.";return;}const time=(stamp)=>stamp?new Date(stamp).toLocaleTimeString([],{hour:"numeric",minute:"2-digit"}):"—",minutes=body.daylight_minutes,hours=minutes==null?"—":`${Math.floor(minutes/60)}h ${minutes%60}m`;$("astronomy-date").textContent=`${new Date(`${body.date}T12:00:00`).toLocaleDateString([],{weekday:"long",month:"long",day:"numeric"})} · ${body.timezone} · no internet required`;$("astro-rise").textContent=time(body.sunrise);$("astro-set").textContent=time(body.sunset);$("astro-dawn").textContent=`Civil dawn ${time(body.civil_dawn)}`;$("astro-dusk").textContent=`Civil dusk ${time(body.civil_dusk)}`;$("astro-daylight").textContent=hours;$("astro-moon").textContent=`${body.moon_illumination}%`;$("astro-phase").textContent=`${body.moon_phase} · ${body.moon_age_days} days`;}
setTimeout(refreshAstronomy,1900);setInterval(refreshAstronomy,300000);
async function refreshSeismic(){const response=await fetch("/api/v1/environment/earthquakes");if(response.status===401)return;const body=await response.json();if(!response.ok)return;const values=body.items||[];$("seismic-total").textContent=values.length;$("seismic-health").textContent=body.health.last_error?`Feed unavailable · showing stored events · ${body.health.last_error}`:body.health.last_poll_at?`${body.radius_km} km radius · updated ${new Date(body.health.last_poll_at*1000).toLocaleTimeString([],{hour:"numeric",minute:"2-digit"})}`:`${body.radius_km} km monitoring radius`;$("seismic-list").innerHTML=values.slice(0,5).map(value=>`<article class="seismic-event ${value.significance?"significant":""}"><div class="magnitude">M<strong>${Number(value.magnitude).toFixed(1)}</strong></div><div><h3>${safe(value.place)}</h3><p>${Number(value.distance_km).toFixed(0)} km away · ${safe(value.bearing_deg)}° · ${Number(value.depth_km).toFixed(1)} km deep</p></div><time>${new Date(value.occurred_at*1000).toLocaleString()}</time><span class="review-state">${safe(value.review_state)}</span></article>`).join("")||'<p class="empty">No nearby earthquakes recorded in the past 24 hours.</p>';}
setTimeout(refreshSeismic,2100);setInterval(refreshSeismic,60000);
async function refreshProviderHealth() { const response = await fetch("/api/v1/environment/providers"); if (!response.ok) return; const values = (await response.json()).items; $("provider-health").innerHTML = Object.entries(values).map(([name, value]) => `<span class="${safe(value.status)}"><i></i>${safe(name)} ${safe(value.status)}</span>`).join(""); }
setTimeout(refreshProviderHealth, 1800); setInterval(refreshProviderHealth, 30000);

function activityRow(entry) {
  const outbound = entry.direction === "outbound";
  const identity = entry.handle ? `@${entry.handle}` : entry.peer_mesh_id || "mesh";
  const detail = [entry.command, entry.outcome, `channel ${entry.channel}`].filter(Boolean).join(" · ");
  return `<div class="activity-row"><span class="direction ${outbound ? "outbound" : ""}">${outbound ? "↗" : "↙"}</span><div><strong>${safe(identity)} · ${outbound ? "sent" : "received"}</strong><p>${safe(detail)}</p></div><time>${safe(ago(entry.created_at))}</time></div>`;
}

async function refresh() {
  try {
    const [status, overview, boards, channels] = await Promise.all([
      fetch("/api/v1/status").then((r) => r.json()),
      fetch("/api/v1/dashboard/overview").then((r) => r.json()),
      fetch("/api/v1/boards").then((r) => r.json()),
      fetch("/api/v1/channels").then((r) => r.json()),
    ]);
    $("node-name").textContent = status.node;
    const radio = $("radio-state");
    radio.className = `status ${status.radio}`;
    radio.innerHTML = `<i></i>${safe(status.radio)}`;
    $("radio-label").textContent = status.radio === "up" ? "Connected" : "Unavailable";
    const ratio = Math.max(0, Math.min(1, status.airtime_used_ratio || 0));
    $("airtime-bar").style.width = `${ratio * 100}%`;
    $("airtime-value").textContent = `${(ratio * 100).toFixed(1)}%`;
    const budget = $("budget-state");
    budget.className = ratio > .9 ? "chip bad" : ratio > .7 ? "chip warn" : "chip";
    budget.textContent = ratio > .9 ? "Budget critical" : ratio > .7 ? "Budget elevated" : "Within budget";
    $("node-id").textContent = status.radio_config.node_id || "—";
    $("region").textContent = status.radio_config.region || "—";
    $("preset").textContent = status.radio_config.preset || "—";
    $("channel-count").textContent = status.radio_config.channels.length;
    const inbound = overview.traffic_24h.inbound?.count || 0;
    const outbound = overview.traffic_24h.outbound?.count || 0;
    $("messages-24h").textContent = inbound + outbound;
    $("message-split").textContent = `${inbound} received · ${outbound} sent`;
    $("heard-24h").textContent = overview.members.heard_24h;
    $("heard-7d").textContent = overview.members.heard_7d;
    $("members-total").textContent = overview.members.members_total;
    const queued = Object.values(status.queues).reduce((sum, count) => sum + count, 0);
    $("queued-total").textContent = queued;
    const maxQueue = Math.max(1, ...Object.values(status.queues));
    $("queues").innerHTML = Object.entries(status.queues).map(([name, count]) => `<div class="queue-row"><span>${safe(name)}</span><div class="queue-track"><i style="width:${safe(count / maxQueue * 100)}%"></i></div><strong>${safe(count)}</strong></div>`).join("");
    $("activity-list").innerHTML = overview.activity.map(activityRow).join("") || `<p class="empty">No mesh activity recorded yet.</p>`;
    $("boards").innerHTML = boards.items.map((board) => item(board.title, board.description, `${board.thread_count} threads`)).join("") || `<p class="empty">No boards.</p>`;
    $("channels").innerHTML = channels.items.map((channel) => item(channel.name, channel.description, `slot ${channel.slot}`)).join("") || `<p class="empty">No channels.</p>`;
    $("updated").textContent = `Updated ${new Date().toLocaleTimeString([], {hour:"2-digit",minute:"2-digit"})}`;
  } catch (_) {
    const radio = $("radio-state"); radio.className = "status down"; radio.innerHTML = "<i></i>offline";
  }
}
let csrfToken = "";
async function initialize() {
  const sessionResponse = await fetch("/api/v1/auth/session");
  if (sessionResponse.ok) {
    const session = await sessionResponse.json();
    sessionStorage.setItem(authHintKey, "true");
    csrfToken = session.csrf_token;
    if (session.must_change) {
      $("login-form").classList.add("hidden");
      $("change-form").classList.remove("hidden");
    } else {
      $("login-screen").classList.add("hidden");
      await refresh();
    }
  } else {
    sessionStorage.removeItem(authHintKey);
    $("login-screen").classList.remove("hidden");
  }
}

$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("login-error").textContent = "";
  const response = await fetch("/api/v1/auth/login", {
    method: "POST",
    headers: {"content-type": "application/json"},
    body: JSON.stringify({password: $("password").value}),
  });
  if (!response.ok) {
    $("login-error").textContent = "Sign-in failed. Check the password and try again.";
    return;
  }
  const session = await response.json();
  sessionStorage.setItem(authHintKey, "true");
  csrfToken = session.csrf_token;
  if (session.must_change) {
    $("current-password").value = $("password").value;
    $("login-form").classList.add("hidden");
    $("change-form").classList.remove("hidden");
  } else {
    $("login-screen").classList.add("hidden");
    await refresh();
  }
  $("password").value = "";
});

$("change-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("change-error").textContent = "";
  const response = await fetch("/api/v1/auth/password", {
    method: "POST",
    headers: {"content-type": "application/json", "x-csrf-token": csrfToken},
    body: JSON.stringify({
      current_password: $("current-password").value,
      new_password: $("new-password").value,
    }),
  });
  if (!response.ok) {
    $("change-error").textContent = "Could not change password. Use 12 or more characters.";
    return;
  }
  $("login-screen").classList.add("hidden");
  await refresh();
});

const emergencySection = document.querySelector(".emergency-settings");
const emergencyScreen = document.createElement("div");
emergencyScreen.id = "emergency-screen";
emergencyScreen.className = "login-screen hidden";
const emergencyCard = document.createElement("div");
emergencyCard.className = "login-card settings-card emergency-card";
emergencyCard.innerHTML = '<button id="close-emergency" class="close" type="button" aria-label="Close">×</button>';
emergencyCard.appendChild(emergencySection);
emergencySection.querySelector("#save-watch-settings").insertAdjacentHTML("beforebegin", '<div class="escalation-editor"><div class="escalation-heading"><p class="eyebrow">ALERT ESCALATION</p><h3>Durable response policy</h3><p>Stages run from the alert start time and stop when enough members acknowledge.</p></div><div id="escalation-policies" class="escalation-policies"></div><label class="advanced-toggle"><input id="escalation-advanced-toggle" type="checkbox"><span><strong>Advanced</strong><small>View or edit the raw policy JSON</small></span></label><div id="escalation-advanced" class="advanced-policy" hidden><textarea id="setting-escalation" rows="14" spellcheck="false" aria-label="Alert escalation policy JSON"></textarea></div></div>');
emergencyCard.insertAdjacentHTML("beforeend", '<p id="emergency-error" class="login-error" role="alert"></p>');
emergencyScreen.appendChild(emergencyCard);
document.body.appendChild(emergencyScreen);
document.querySelector(".capability-grid").insertAdjacentHTML("beforeend", '<article><b>Emergency settings</b><span class="phase now">AVAILABLE</span><p>Keyword detection, responder notification, cooldown, and safety policy.</p><button id="open-emergency" type="button">Open emergency settings</button></article>');

let escalationPolicy = {};
const stageRow = (stage = {after_minutes: 0, notify: "responders", channels: [3], repeat: false}) => `<div class="stage-row"><label><span>After</span><input data-stage-after type="number" min="0" value="${safe(stage.after_minutes)}"><small>minutes</small></label><label><span>Notify</span><select data-stage-notify>${["responders","trusted","all"].map(value => `<option value="${value}" ${stage.notify === value ? "selected" : ""}>${value}</option>`).join("")}</select></label><label><span>Channels</span><input data-stage-channels value="${safe((stage.channels || []).join(", "))}" placeholder="0, 3"></label><label class="repeat-control"><input data-stage-repeat type="checkbox" ${stage.repeat ? "checked" : ""}><span>Repeat</span></label><button data-remove-stage type="button" aria-label="Remove stage">×</button></div>`;
function renderEscalationPolicy(policy) {
  escalationPolicy = policy;
  $("escalation-policies").innerHTML = ["caution", "urgent", "critical"].map(severity => { const value = policy[severity] || {ack_threshold: 0, stages: []}; return `<section class="policy-card ${severity}" data-policy="${severity}"><header><div><small>${severity.toUpperCase()}</small><strong>${severity === "caution" ? "Advisory" : severity === "urgent" ? "Immediate response" : "Widespread danger"}</strong></div><label>Acknowledgements to stop <input data-ack-threshold type="number" min="0" value="${safe(value.ack_threshold)}"></label></header><div class="policy-stages">${value.stages.map(stageRow).join("")}</div><button data-add-stage type="button">+ Add escalation stage</button></section>`; }).join("");
}
function readEscalationPolicy() {
  const policy = {};
  document.querySelectorAll("[data-policy]").forEach(card => { policy[card.dataset.policy] = {ack_threshold: Number(card.querySelector("[data-ack-threshold]").value), stages: [...card.querySelectorAll(".stage-row")].map(row => ({after_minutes: Number(row.querySelector("[data-stage-after]").value), notify: row.querySelector("[data-stage-notify]").value, channels: row.querySelector("[data-stage-channels]").value.split(",").map(value => Number(value.trim())).filter(Number.isInteger), repeat: row.querySelector("[data-stage-repeat]").checked}))}; });
  return policy;
}
$("escalation-policies").addEventListener("click", event => { const remove = event.target.closest("[data-remove-stage]"); if (remove) remove.closest(".stage-row").remove(); const add = event.target.closest("[data-add-stage]"); if (add) add.previousElementSibling.insertAdjacentHTML("beforeend", stageRow()); });
$("escalation-advanced-toggle").addEventListener("change", event => { const advanced = $("escalation-advanced"); if (event.target.checked) { $("setting-escalation").value = JSON.stringify(readEscalationPolicy(), null, 2); advanced.hidden = false; } else { try { renderEscalationPolicy(JSON.parse($("setting-escalation").value)); advanced.hidden = true; $("emergency-error").textContent = ""; } catch { event.target.checked = true; $("emergency-error").textContent = "Fix the JSON before leaving Advanced mode."; } } });

$("open-settings").addEventListener("click", async () => {
  const [response, statusResponse] = await Promise.all([fetch("/api/v1/config"), fetch("/api/v1/status")]);
  if (!response.ok || !statusResponse.ok) return;
  const config = await response.json(), status = await statusResponse.json(), radioGps = status.radio_config?.gps;
  $("setting-name").value = config.node.name;
  $("setting-short").value = config.node.short_name;
  $("setting-contact").value = config.node.operator_contact;
  $("setting-timezone").value = config.node.timezone;
  $("setting-disclaimer").value = config.node.disclaimer;
  const location = config.node.location || (radioGps?.lat != null && radioGps?.lon != null ? radioGps : null);
  $("setting-lat").value = location?.lat ?? "";
  $("setting-lon").value = location?.lon ?? "";
  $("location-source").textContent = config.node.location ? "Saved Outpost location" : location ? "Auto-filled from radio GPS · save to confirm" : "Radio GPS position unavailable";
  document.querySelector(`input[name="setting-units"][value="${config.node.units}"]`).checked = true;
  $("settings-screen").classList.remove("hidden");
});

$("open-emergency").addEventListener("click", async () => {
  const response = await fetch("/api/v1/config");
  if (!response.ok) return;
  const config = await response.json();
  $("setting-emergency-enabled").checked = config.watch.emergency_keywords_enabled;
  $("setting-emergency-keywords").value = config.watch.emergency_keywords.join(", ");
  $("setting-emergency-cooldown").value = config.watch.emergency_cooldown_minutes;
  $("setting-escalation").value = JSON.stringify(config.watch.escalation, null, 2);
  renderEscalationPolicy(config.watch.escalation);
  $("escalation-advanced-toggle").checked = false;
  $("escalation-advanced").hidden = true;
  $("emergency-error").textContent = "";
  emergencyScreen.classList.remove("hidden");
});

$("close-emergency").addEventListener("click", () => emergencyScreen.classList.add("hidden"));

$("close-settings").addEventListener("click", () => $("settings-screen").classList.add("hidden"));
$("settings-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("settings-error").textContent = "";
  const response = await fetch("/api/v1/config/node", {
    method: "PATCH",
    headers: {"content-type": "application/json", "x-csrf-token": csrfToken},
    body: JSON.stringify({
      name: $("setting-name").value,
      short_name: $("setting-short").value,
      operator_contact: $("setting-contact").value,
      timezone: $("setting-timezone").value,
      disclaimer: $("setting-disclaimer").value,
      location: $("setting-lat").value && $("setting-lon").value ? {lat: Number($("setting-lat").value), lon: Number($("setting-lon").value)} : null,
      units: document.querySelector('input[name="setting-units"]:checked').value,
    }),
  });
  if (!response.ok) {
    const body = await response.json();
    $("settings-error").textContent = body.error?.message || "Could not save settings.";
    return;
  }
  $("settings-screen").classList.add("hidden");
  await refresh();
  await refreshWeather();
});

$("save-watch-settings").addEventListener("click", async () => {
  $("emergency-error").textContent = "";
  const enabled = $("setting-emergency-enabled").checked;
  if (enabled && !window.confirm("Enable emergency keyword detection? False positives create urgent incidents and notify responders.")) return;
  const keywords = $("setting-emergency-keywords").value.split(",").map(value => value.trim()).filter(Boolean);
  let escalation;
  try { escalation = $("escalation-advanced-toggle").checked ? JSON.parse($("setting-escalation").value) : readEscalationPolicy(); }
  catch { $("emergency-error").textContent = "Escalation policy must be valid JSON."; return; }
  const response = await fetch("/api/v1/config/watch", {
    method: "PATCH",
    headers: {"content-type": "application/json", "x-csrf-token": csrfToken},
    body: JSON.stringify({
      emergency_keywords_enabled: enabled,
      emergency_keywords: keywords,
      emergency_cooldown_minutes: Number($("setting-emergency-cooldown").value),
      escalation,
    }),
  });
  const body = await response.json();
  if (!response.ok) { $("emergency-error").textContent = body.error?.message || "Could not save emergency policy."; return; }
  $("emergency-error").textContent = `Emergency policy saved · keyword detection ${body.watch.emergency_keywords_enabled ? "enabled" : "disabled"} immediately.`;
});

$("reconnect-radio").addEventListener("click", async () => {
  if (!window.confirm("Reconnect the radio now? Queued traffic will remain scheduled.")) return;
  const button = $("reconnect-radio");
  button.disabled = true; button.textContent = "Reconnecting…";
  await fetch("/api/v1/radio/reconnect", {method: "POST", headers: {"x-csrf-token": csrfToken}});
  setTimeout(() => { button.disabled = false; button.textContent = "Reconnect radio"; refresh(); }, 3500);
});

$("create-backup").addEventListener("click", async () => {
  const button = $("create-backup");
  button.disabled = true; button.textContent = "Verifying…";
  const response = await fetch("/api/v1/backups", {method: "POST", headers: {"x-csrf-token": csrfToken}});
  if (response.ok) {
    const body = await response.json();
    $("backup-result").innerHTML = `Verified · <a href="/api/v1/backups/${encodeURIComponent(body.backup.name)}">download</a>`;
  } else {
    $("backup-result").textContent = "Backup failed";
  }
  button.disabled = false; button.textContent = "Create backup";
});

initialize(); setInterval(() => { if (csrfToken) refresh(); }, 15000);
