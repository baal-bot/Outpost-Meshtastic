import("/nav.js");
const $ = (id) => document.getElementById(id);
const authHintKey = "outpost.operator.authenticated";
let refreshSchedulersStarted = false;
let viewerMode = false;
if (sessionStorage.getItem(authHintKey) === "true") $("login-screen").classList.add("hidden");
const safe = (value) => String(value).replace(/[&<>'"]/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;","\"":"&quot;"})[char]);
if (location.protocol === "http:" && !["localhost", "127.0.0.1", "::1"].includes(location.hostname)) {
  $("login-copy").insertAdjacentHTML("afterend", '<p class="login-transport-warning" role="status"><b>Trusted local HTTP</b><span>This sign-in is not encrypted. Use it only on the operator’s isolated LAN, Outpost hotspot, or encrypted VPN.</span></p>');
}
const ago = (stamp) => { const seconds = Math.max(0, (Date.now() - new Date(stamp)) / 1000); if (seconds < 60) return "now"; if (seconds < 3600) return `${Math.floor(seconds / 60)}m`; if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`; return `${Math.floor(seconds / 86400)}d`; };
const item = (title, description, badge) => `<div class="item"><div><strong>${safe(title)}</strong><p>${safe(description || "")}</p></div><span class="badge">${safe(badge || "")}</span></div>`;
document.querySelector("#system").insertAdjacentHTML("beforebegin", '<section id="subsystems" class="ui-card panel content-panel subsystem-panel"><div class="heading"><div><p class="eyebrow">FAILURE DOMAINS</p><h2>Subsystem health</h2></div><span id="subsystem-state" class="chip">Checking</span></div><p class="subsystem-intro">Core mesh routing fails safe. Local services and optional providers recover independently without taking the radio offline.</p><div id="subsystem-list" class="subsystem-list"><p class="ui-empty empty">Loading task health…</p></div></section>');
document.querySelector(".kpis").insertAdjacentHTML("afterend", '<section class="panel weather-panel"><div><p id="weather-kind" class="eyebrow">LOCAL CONDITIONS</p><h2 id="weather-title">Weather</h2><p id="weather-summary">Set the Outpost location to enable weather.</p></div><div id="weather-reading" class="weather-reading"><strong>—</strong><span>Not configured</span></div><div id="weather-details" class="weather-details"></div></section>');
document.querySelector(".weather-panel>div").insertAdjacentHTML("beforeend", '<div id="provider-health" class="provider-health"></div>');
document.querySelector(".weather-panel").insertAdjacentHTML("afterend", '<section class="panel forecast-panel"><div class="forecast-heading"><div><p class="eyebrow">LOCAL FORECAST</p><h2>What’s ahead</h2></div><span id="forecast-meta">Loading forecast…</span></div><div id="forecast-days" class="forecast-days"></div><div id="forecast-hours" class="forecast-hours"></div></section>');
document.querySelector(".capability-grid article:last-child").insertAdjacentHTML("beforebegin", '<article><b>Operator access</b><span class="phase now">AVAILABLE</span><p>Named accounts, roles, strong authentication, and active sessions.</p><a class="action" href="/access.html">Manage access</a></article>');
document.querySelector(".forecast-panel").insertAdjacentHTML("afterend", '<section class="panel astronomy-panel"><div class="astronomy-heading"><p class="eyebrow">OFFLINE ASTRONOMY</p><h2>Daylight & moon</h2><p id="astronomy-date">Calculated locally from the Outpost position.</p></div><div class="astro-metrics"><div><span>Sunrise</span><strong id="astro-rise">—</strong><small id="astro-dawn">Civil dawn —</small></div><div><span>Sunset</span><strong id="astro-set">—</strong><small id="astro-dusk">Civil dusk —</small></div><div><span>Daylight</span><strong id="astro-daylight">—</strong><small>Above the horizon</small></div><div class="moon-metric"><span>Moon</span><strong id="astro-moon">—</strong><small id="astro-phase">Phase unavailable</small></div></div></section>');
document.querySelector(".astronomy-panel").insertAdjacentHTML("afterend", '<section class="panel seismic-panel"><div class="seismic-heading"><div><p class="eyebrow">USGS SEISMIC MONITOR</p><h2>Nearby earthquakes</h2><p id="seismic-health">Waiting for the first feed update.</p></div><div class="seismic-count"><strong id="seismic-total">0</strong><span>past 24 hours</span></div></div><div id="seismic-list" class="seismic-list"><p class="ui-empty empty">No nearby earthquakes recorded.</p></div><p class="seismic-credit">Earthquake data courtesy of the U.S. Geological Survey.</p></section>');
const disclaimerLabel = document.querySelector('label[for="setting-disclaimer"]');
disclaimerLabel.insertAdjacentHTML("beforebegin", '<section class="appearance-settings"><p class="eyebrow">APPEARANCE</p><h3>Display theme</h3><p>Choose a mode for this display. The preference stays in this browser.</p><div class="theme-options" role="radiogroup" aria-label="Display theme"><button type="button" data-theme-choice="system"><i>◐</i><span><strong>Follow system</strong><small>Match this device</small></span></button><button type="button" data-theme-choice="dark"><i>●</i><span><strong>Outpost Dark</strong><small>Default operations console</small></span></button><button type="button" data-theme-choice="daylight"><i>○</i><span><strong>Daylight</strong><small>High contrast for bright displays</small></span></button><button type="button" data-theme-choice="night"><i>◒</i><span><strong>Night Ops</strong><small>Low-light red colorway</small></span></button></div></section>');
disclaimerLabel.insertAdjacentHTML("beforebegin", '<div class="location-settings"><p class="eyebrow">OUTPOST LOCATION</p><p>Used for weather, alerts, map defaults, and regional downloads.</p><label for="setting-lat">Latitude</label><input id="setting-lat" type="number" min="-90" max="90" step="0.00001" placeholder="Waiting for radio GPS"><label for="setting-lon">Longitude</label><input id="setting-lon" type="number" min="-180" max="180" step="0.00001" placeholder="Waiting for radio GPS"><small id="location-source"></small><fieldset class="temperature-units"><legend>Temperature</legend><label><input type="radio" name="setting-units" value="imperial"> °F</label><label><input type="radio" name="setting-units" value="metric"> °C</label></fieldset></div>');
const syncThemeOptions = () => {
  const selected = window.OutpostTheme?.get() || "system";
  document.querySelectorAll("[data-theme-choice]").forEach(button => {
    const active = button.dataset.themeChoice === selected;
    button.classList.toggle("selected", active);
    button.setAttribute("aria-checked", String(active));
  });
};
document.querySelector(".theme-options").addEventListener("click", event => {
  const button = event.target.closest("[data-theme-choice]");
  if (!button) return;
  window.OutpostTheme?.set(button.dataset.themeChoice);
  syncThemeOptions();
});
window.addEventListener("outpost:theme", syncThemeOptions);
setTimeout(syncThemeOptions);
const weatherSource = (kind) => ({observation:"Station observation",forecast:"Near-term forecast",estimate:"Current model estimate",peer:"Peer-provided conditions"})[kind] || "Weather data";
const weatherAge = (seconds) => seconds == null ? "valid time unavailable" : seconds < 60 ? "valid now" : seconds < 3600 ? `valid ${Math.floor(seconds/60)}m ago` : `valid ${Math.floor(seconds/3600)}h ago`;
async function refreshWeather() { const response = await fetch("/api/v1/environment/weather"); if (response.status === 401) return; const body = await response.json(); if (!response.ok) { $("weather-summary").textContent = body.error?.message || "Weather unavailable."; $("weather-reading").innerHTML = '<strong>—</strong><span>Setup required</span>'; $("weather-details").innerHTML = ""; return; } const imperial=body.units==="imperial",temperature=body.temperature_c==null?null:imperial?body.temperature_c*9/5+32:body.temperature_c,apparent=body.apparent_c==null?null:imperial?body.apparent_c*9/5+32:body.apparent_c,wind=body.wind_kph==null?null:imperial?body.wind_kph/1.609344:body.wind_kph,tempUnit=imperial?"F":"C",windUnit=imperial?"MPH":"KM/H",source=weatherSource(body.source_kind),cacheAge=body.stale?` · cached ${body.age_seconds<60?"now":`${Math.floor(body.age_seconds/60)}m ago`}`:""; $("weather-kind").textContent=body.source_kind==="forecast"?"LOCAL FORECAST":body.source_kind==="observation"?"LOCAL OBSERVATION":"LOCAL CONDITIONS"; $("weather-title").textContent=source; $("weather-summary").textContent=`${body.stale?"Cached ":""}${source} · ${body.provider}${body.source_detail?` · ${body.source_detail}`:""} · ${weatherAge(body.valid_age_seconds)}${cacheAge}`; $("weather-summary").title=body.valid_at?`Valid ${new Date(body.valid_at).toLocaleString()}`:"Valid time unavailable"; $("weather-reading").innerHTML=`<strong>${temperature==null?"—":`${Number(temperature).toFixed(1)}°${tempUnit}`}</strong><span>${apparent==null?"Feels-like unavailable":`Feels ${Number(apparent).toFixed(1)}°${tempUnit}`}</span>`; $("weather-details").innerHTML=`<span><b>${wind==null?"—":`${Number(wind).toFixed(0)} ${windUnit}`}</b><em>Wind speed${wind==null?" unavailable":""}</em></span><span><b>${body.precipitation_mm==null?"—":`${Number(body.precipitation_mm).toFixed(1)} MM`}</b><em>Precipitation${body.precipitation_mm==null?" unavailable":""}</em></span><span><b>${body.wind_direction==null?"—":`${safe(body.wind_direction)}°`}</b><em>Wind direction${body.wind_direction==null?" unavailable":""}</em></span>`; $("weather-reading").classList.toggle("stale", body.stale); }
async function refreshForecast() { const response=await fetch("/api/v1/environment/forecast"); if(response.status===401)return; const body=await response.json(); if(!response.ok){$("forecast-meta").textContent=body.error?.message||"Forecast unavailable.";$("forecast-days").innerHTML="";$("forecast-hours").innerHTML="";return;} const imperial=body.units==="imperial",temp=(c)=>c==null?null:Math.round(imperial?c*9/5+32:c),wind=(k)=>k==null?null:Math.round(imperial?k/1.609344:k),unit=imperial?"F":"C",windUnit=imperial?"mph":"km/h",tempText=(c)=>c==null?"—":`${temp(c)}°`,rainText=(value)=>value==null?"—":`${safe(value)}%`; $("forecast-meta").textContent=`Forecast · ${body.provider}${body.stale?" · cached":""} · ${body.age_seconds<60?"updated now":`${Math.floor(body.age_seconds/60)}m old`}`; $("forecast-days").innerHTML=body.daily.slice(0,5).map((day,index)=>`<article class="forecast-day ${index===0?"current":""}"><span>${safe(index===0?"Today":index===1?"Tomorrow":day.name)}</span><strong>${tempText(day.high_c)}<small> / ${tempText(day.low_c)}${day.low_c==null?"":unit}</small></strong><p>${safe(day.summary)}</p><div><b>${rainText(day.precipitation_probability)}</b> rain <b>${wind(day.wind_kph)==null?"—":wind(day.wind_kph)}</b> ${wind(day.wind_kph)==null?"":windUnit}</div></article>`).join(""); const now=Date.now()-3600000, hours=body.hourly.filter((hour)=>new Date(hour.start_time).getTime()>now).slice(0,8); $("forecast-hours").innerHTML=hours.map((hour)=>`<div><time>${new Date(hour.start_time).toLocaleTimeString([],{hour:"numeric"})}</time><strong>${tempText(hour.temperature_c)}</strong><span>${rainText(hour.precipitation_probability)} rain</span></div>`).join(""); }
async function refreshAstronomy(){const response=await fetch("/api/v1/environment/astronomy");if(response.status===401)return;const body=await response.json();if(!response.ok){$("astronomy-date").textContent=body.error?.message||"Astronomy unavailable.";return;}const time=(stamp)=>stamp?new Date(stamp).toLocaleTimeString([],{hour:"numeric",minute:"2-digit"}):"—",minutes=body.daylight_minutes,hours=minutes==null?"—":`${Math.floor(minutes/60)}h ${minutes%60}m`;$("astronomy-date").textContent=`${new Date(`${body.date}T12:00:00`).toLocaleDateString([],{weekday:"long",month:"long",day:"numeric"})} · ${body.timezone} · no internet required`;$("astro-rise").textContent=time(body.sunrise);$("astro-set").textContent=time(body.sunset);$("astro-dawn").textContent=`Civil dawn ${time(body.civil_dawn)}`;$("astro-dusk").textContent=`Civil dusk ${time(body.civil_dusk)}`;$("astro-daylight").textContent=hours;$("astro-moon").textContent=`${body.moon_illumination}%`;$("astro-phase").textContent=`${body.moon_phase} · ${body.moon_age_days} days`;}
async function refreshSeismic(){const response=await fetch("/api/v1/environment/earthquakes");if(response.status===401)return;const body=await response.json();if(!response.ok)return;const values=body.items||[];$("seismic-total").textContent=values.length;$("seismic-health").textContent=body.health.last_error?`Feed unavailable · showing stored events · ${body.health.last_error}`:body.health.last_poll_at?`${body.radius_km} km radius · updated ${new Date(body.health.last_poll_at*1000).toLocaleTimeString([],{hour:"numeric",minute:"2-digit"})}`:`${body.radius_km} km monitoring radius`;$("seismic-list").innerHTML=values.slice(0,5).map(value=>`<article class="seismic-event ${value.significance?"significant":""}"><div class="magnitude">M<strong>${Number(value.magnitude).toFixed(1)}</strong></div><div><h3>${safe(value.place)}</h3><p>${Number(value.distance_km).toFixed(0)} km away · ${safe(value.bearing_deg)}° · ${Number(value.depth_km).toFixed(1)} km deep</p></div><time>${new Date(value.occurred_at*1000).toLocaleString()}</time><span class="review-state">${safe(value.review_state)}</span></article>`).join("")||'<p class="ui-empty empty">No nearby earthquakes recorded in the past 24 hours.</p>';}
async function refreshProviderHealth() { const response = await fetch("/api/v1/environment/providers"); if (!response.ok) return; const values = (await response.json()).items; $("provider-health").innerHTML = Object.entries(values).map(([name, value]) => `<span class="${safe(value.status)}"><i></i>${safe(name)} ${safe(value.status)}</span>`).join(""); }

function activityRow(entry) {
  const outbound = entry.direction === "outbound";
  const identity = entry.handle ? `@${entry.handle}` : entry.peer_mesh_id || "mesh";
  const detail = [entry.command, entry.outcome, `channel ${entry.channel}`].filter(Boolean).join(" · ");
  return `<div class="activity-row"><span class="direction ${outbound ? "outbound" : ""}">${outbound ? "↗" : "↙"}</span><div><strong>${safe(identity)} · ${outbound ? "sent" : "received"}</strong><p>${safe(detail)}</p></div><time>${safe(ago(entry.created_at))}</time></div>`;
}

function renderSubsystems(status) {
  const target = $("subsystem-list");
  if (!target || viewerMode) return;
  const tasks = Object.entries(status.tasks || {});
  const degraded = tasks.filter(([, task]) => task.state !== "running");
  const state = $("subsystem-state");
  state.className = status.tasks_healthy === false ? "chip bad" : degraded.length ? "chip warn" : "chip";
  state.textContent = status.tasks_healthy === false ? "Core fault" : degraded.length ? `${degraded.length} degraded` : "Healthy";
  const friendly = (name) => name.split("-").map((part) => part === "ai" ? "AI" : part.charAt(0).toUpperCase() + part.slice(1)).join(" ");
  const domain = (value) => ({core:"Core · fail fast",restartable_local:"Local · auto restart",optional_provider:"Optional · isolated"})[value] || value || "Task";
  const when = (stamp) => stamp ? ago(new Date(stamp * 1000)) : "never";
  const retry = (stamp) => { if (!stamp) return "retry pending"; const seconds = Math.max(0, stamp - Date.now() / 1000); return seconds < 2 ? "retrying now" : seconds < 60 ? `retry in ${Math.ceil(seconds)}s` : `retry in ${Math.ceil(seconds / 60)}m`; };
  target.innerHTML = tasks.map(([name, task]) => {
    const healthy = task.state === "running";
    const level = healthy ? "" : task.required ? "critical" : "degraded";
    const reason = healthy
      ? task.failure_count ? `Recovered · last error ${when(task.last_error_at)} · ${task.last_error || "details unavailable"}` : `Last progress ${when(task.last_ok_at)}`
      : task.degraded_reason || task.last_error || "Task stopped unexpectedly";
    const detail = healthy
      ? `${task.failure_count || 0} failures · ${task.restart_count || 0} restarts`
      : `${task.failure_count || 0} failures · ${task.circuit_open ? "circuit open · " : ""}${retry(task.next_retry_at)}`;
    return `<article class="subsystem-task ${level}"><header><strong>${safe(friendly(name))}</strong><span>${safe(task.state)}</span></header><p>${safe(reason)}</p><small>${safe(domain(task.failure_domain))} · ${safe(detail)}</small></article>`;
  }).join("") || '<p class="ui-empty empty">Task health is not available.</p>';
}

async function refresh() {
  try {
    let status, overview, boards, channels;
    if (viewerMode) {
      const response = await fetch("/api/v1/wallboard/summary");
      if (!response.ok) throw new Error(`wallboard summary ${response.status}`);
      ({status, overview, boards, channels} = await response.json());
    } else {
      [status, overview, boards, channels] = await Promise.all([
        fetch("/api/v1/status").then((r) => r.json()),
        fetch("/api/v1/dashboard/overview").then((r) => r.json()),
        fetch("/api/v1/boards").then((r) => r.json()),
        fetch("/api/v1/channels").then((r) => r.json()),
      ]);
    }
    $("node-name").textContent = status.node;
    const radio = $("radio-state");
    radio.className = `ui-pill status ${status.radio}`;
    radio.innerHTML = `<i></i>${safe(status.radio)}`;
    $("radio-label").textContent = status.radio === "up" ? "Connected" : "Unavailable";
    const ratio = Math.max(0, Math.min(1, status.airtime_used_ratio || 0));
    $("airtime-bar").style.width = `${ratio * 100}%`;
    $("airtime-value").textContent = `${(ratio * 100).toFixed(1)}%`;
    const budget = $("budget-state");
    budget.className = ratio > .9 ? "chip bad" : ratio > .7 ? "chip warn" : "chip";
    budget.textContent = ratio > .9 ? "Budget critical" : ratio > .7 ? "Budget elevated" : "Within budget";
    $("node-id").textContent = viewerMode ? "Restricted" : status.radio_config.node_id || "—";
    $("region").textContent = viewerMode ? "Restricted" : status.radio_config.region || "—";
    $("preset").textContent = viewerMode ? "Restricted" : status.radio_config.preset || "—";
    $("channel-count").textContent = viewerMode
      ? channels.items.length
      : status.radio_config.channels.length;
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
    $("activity-list").innerHTML = viewerMode
      ? `<p class="ui-empty empty">Individual mesh activity is hidden on this aggregate wallboard.</p>`
      : overview.activity.map(activityRow).join("") || `<p class="ui-empty empty">No mesh activity recorded yet.</p>`;
    $("boards").innerHTML = boards.items.map((board) => item(board.title, board.description, `${board.thread_count} threads`)).join("") || `<p class="ui-empty empty">No boards.</p>`;
    $("channels").innerHTML = channels.items.map((channel) => item(channel.name, channel.description, `slot ${channel.slot}`)).join("") || `<p class="ui-empty empty">No channels.</p>`;
    renderSubsystems(status);
    $("updated").textContent = `Updated ${new Date().toLocaleTimeString([], {hour:"2-digit",minute:"2-digit"})}`;
  } catch (_) {
    const radio = $("radio-state"); radio.className = "ui-pill status down"; radio.innerHTML = "<i></i>offline";
  }
}
let csrfToken = "";
let loginNeedsMfa = false;
async function configureLogin(message = "") {
  const response = await fetch("/api/v1/auth/setup");
  const setup = response.ok ? await response.json() : {required: false, available: false};
  const setupMode = Boolean(setup.required);
  $("login-eyebrow").textContent = setupMode ? "ONE-TIME LOCAL SETUP" : "OUTPOST OPERATOR";
  $("login-title").textContent = setupMode ? "Finish Outpost setup" : "Sign in to the console";
  if (setupMode && setup.available) {
    const expiry = new Date(setup.expires_at * 1000).toLocaleTimeString([], {hour:"numeric", minute:"2-digit"});
    $("login-copy").innerHTML = `On the Outpost host, run <code>sudo outpost-setup-token show</code> Enter that one-time token here before ${expiry}.`;
  } else if (setupMode) {
    $("login-copy").innerHTML = "The setup token expired or was already used. On the Outpost host, run <code>sudo outpost-setup-token reset</code> to issue a new one.";
  } else {
    $("login-copy").textContent = "This dashboard controls community infrastructure on your mesh.";
  }
  $("password-label").textContent = setupMode ? "One-time setup token" : "Operator password";
  $("username-label").hidden = setupMode;
  $("username").hidden = setupMode;
  $("username").required = !setupMode;
  if (setupMode) $("username").value = "operator";
  $("password").autocomplete = setupMode ? "one-time-code" : "current-password";
  $("login-submit").textContent = setupMode ? "Continue setup" : "Sign in";
  if (message) {
    $("login-error").dataset.success = "true";
    $("login-error").textContent = message;
  } else {
    delete $("login-error").dataset.success;
  }
}
async function initialize() {
  const sessionResponse = await fetch("/api/v1/auth/session");
  if (sessionResponse.ok) {
    const session = await sessionResponse.json();
    viewerMode = session.role === "viewer";
    if (viewerMode) {
      document.body.dataset.operatorRole = "viewer";
      for (const selector of [
        "#system",
        "#subsystems",
        ".weather-panel",
        ".forecast-panel",
        ".astronomy-panel",
        ".seismic-panel",
      ]) {
        document.querySelector(selector)?.setAttribute("hidden", "");
      }
      document.querySelectorAll("#community a").forEach((link) => link.setAttribute("hidden", ""));
    }
    sessionStorage.setItem(authHintKey, "true");
    csrfToken = session.csrf_token;
    if (session.must_change) {
      $("login-form").classList.add("hidden");
      $("change-form").classList.remove("hidden");
    } else {
      $("login-screen").classList.add("hidden");
      await refresh();
      startRefreshSchedulers();
    }
  } else {
    sessionStorage.removeItem(authHintKey);
    $("login-screen").classList.remove("hidden");
    await configureLogin();
  }
}

$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  delete $("login-error").dataset.success;
  $("login-error").textContent = "";
  const response = await fetch("/api/v1/auth/login", {
    method: "POST",
    headers: {"content-type": "application/json"},
    body: JSON.stringify({
      username: $("username").value || "operator",
      password: $("password").value,
      code: $("login-code").value || null,
    }),
  });
  if (response.status === 202) {
    loginNeedsMfa = true;
    $("mfa-field").hidden = false;
    $("login-code").required = true;
    $("login-submit").textContent = "Verify and sign in";
    $("login-error").dataset.success = "true";
    $("login-error").textContent = "Password accepted. Enter an authenticator or recovery code.";
    $("login-code").focus();
    return;
  }
  if (!response.ok) {
    delete $("login-error").dataset.success;
    $("login-error").textContent = loginNeedsMfa
      ? "Verification failed. Check the code and try again."
      : "Sign-in failed. Check the account name and password.";
    return;
  }
  const session = await response.json();
  sessionStorage.setItem(authHintKey, "true");
  csrfToken = session.csrf_token;
  if (session.must_change) {
    $("login-form").classList.add("hidden");
    $("change-form").classList.remove("hidden");
  } else {
    if (session.role === "viewer") {
      window.location.reload();
      return;
    }
    $("login-screen").classList.add("hidden");
    window.dispatchEvent(new Event("outpost:authenticated"));
    await refresh();
    startRefreshSchedulers();
  }
  $("password").value = "";
  $("login-code").value = "";
  $("mfa-field").hidden = true;
  $("login-code").required = false;
  loginNeedsMfa = false;
});

$("change-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("change-error").textContent = "";
  if ($("new-password").value !== $("confirm-password").value) {
    $("change-error").textContent = "The permanent passwords do not match.";
    return;
  }
  const response = await fetch("/api/v1/auth/password", {
    method: "POST",
    headers: {"content-type": "application/json", "x-csrf-token": csrfToken},
    body: JSON.stringify({
      current_password: "",
      new_password: $("new-password").value,
    }),
  });
  if (!response.ok) {
    $("change-error").textContent = "Could not change password. Use 12 or more characters.";
    return;
  }
  csrfToken = "";
  sessionStorage.removeItem(authHintKey);
  $("change-form").classList.add("hidden");
  $("login-form").classList.remove("hidden");
  $("new-password").value = "";
  $("confirm-password").value = "";
  await configureLogin("Permanent password saved. Sign in to continue.");
  $("password").focus();
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
  if (enabled && !await window.OutpostUI.confirm({
    title: "Enable emergency detection?",
    message: "False positives create urgent incidents and notify responders. Public broadcasts still require operator review.",
    confirmLabel: "Enable detection",
    danger: true,
  })) return;
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
  if (!await window.OutpostUI.confirm({
    title: "Reconnect the radio?",
    message: "The radio link will briefly disconnect. Queued traffic remains scheduled through the airtime governor.",
    confirmLabel: "Reconnect radio",
  })) return;
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

async function startRefreshSchedulers() {
  if (refreshSchedulersStarted) return;
  refreshSchedulersStarted = true;
  const {scheduler} = await import("/refresh-scheduler.js");
  scheduler.schedule("overview-main", refresh, {interval:15000});
  if (viewerMode) return;
  scheduler.schedule("overview-weather", refreshWeather, {initial:1500, interval:30000});
  scheduler.schedule("overview-forecast", refreshForecast, {initial:1700, interval:30000});
  scheduler.schedule("overview-providers", refreshProviderHealth, {initial:1800, interval:30000});
  scheduler.schedule("overview-astronomy", refreshAstronomy, {initial:1900, interval:300000});
  scheduler.schedule("overview-seismic", refreshSeismic, {initial:2100, interval:60000});
}
initialize();
