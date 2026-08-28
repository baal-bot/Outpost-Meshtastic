const byId = (id) => document.getElementById(id);

function option(select, value, label = value) {
  const entry = document.createElement("option");
  entry.value = String(value);
  entry.textContent = String(label).replaceAll("_", " ");
  select.append(entry);
}

function fillSelect(select, values, selected, labels = {}) {
  select.replaceChildren();
  for (const item of values || []) option(select, item, labels[item] || item);
  if (selected && ![...select.options].some((entry) => entry.value === String(selected))) {
    option(select, selected, `${String(selected).replaceAll("_", " ")} · current`);
  }
  select.value = selected == null ? "" : String(selected);
}

function checked(id, current) { byId(id).checked = Boolean(current); }
function value(id, current) { byId(id).value = current == null ? "" : String(current); }
function intValue(id) { return Number.parseInt(byId(id).value, 10); }

function configMarkup() {
  return `<section id="radio-config-card" class="ui-card panel content-panel operator-panel radio-config-card">
    <div class="heading radio-config-heading"><div><p class="eyebrow">RADIO CONFIGURATION</p><h2>Meshtastic radio</h2></div><button id="radio-config-toggle" class="ui-button small-button" type="button">Configure radio</button></div>
    <p class="radio-config-intro">Purpose-built controls for an Outpost-connected node. Changes are written to the radio and may briefly restart its connection.</p>
    <div id="radio-config-summary" class="radio-config-summary" aria-live="polite"><span>Loading radio configuration…</span></div>
    <div id="radio-config-workspace" class="radio-config-workspace" hidden>
      <div id="radio-config-warnings" class="radio-config-warnings" hidden></div>
      <nav class="radio-config-tabs" aria-label="Radio configuration sections">
        <button class="active" data-radio-tab="essentials" type="button">Essentials</button><button data-radio-tab="channels" type="button">Channels</button><button data-radio-tab="location" type="button">Location</button><button data-radio-tab="mqtt" type="button">MQTT</button>
      </nav>
      <div class="radio-config-pane active" data-radio-pane="essentials">
        <div class="radio-config-grid">
          <form id="radio-identity-form" class="radio-config-form"><div><p class="eyebrow">IDENTITY</p><h3>Node name</h3><p>Shown to nearby Meshtastic users.</p></div><label>Long name<input id="radio-long-name" maxlength="40" required></label><label>Short name<input id="radio-short-name" maxlength="4" required></label><button type="submit">Save identity</button><output id="radio-identity-result"></output></form>
          <form id="radio-device-form" class="radio-config-form"><div><p class="eyebrow">DEVICE</p><h3>Connected-node behavior</h3><p>Only roles that preserve Outpost's client connection are available.</p></div><label>Role<select id="radio-role"></select></label><label>Rebroadcast mode<select id="radio-rebroadcast"></select></label><label>Node info interval · seconds<input id="radio-node-interval" type="number" min="900" max="86400" required></label><button type="submit">Save device settings</button><output id="radio-device-result"></output></form>
        </div>
        <form id="radio-lora-form" class="radio-config-form radio-config-wide"><div><p class="eyebrow">LORA</p><h3>Regional radio profile</h3><p>Region must match local law. LONG FAST, hop limit 3, and automatic transmit power are the Outpost defaults.</p></div><div class="radio-form-row"><label>Legal region<select id="radio-region" required></select></label><label>Modem preset<select id="radio-preset-config" required></select></label><label>Hop limit<input id="radio-hop-limit" type="number" min="1" max="7" required></label><label>Transmit power · dBm<input id="radio-tx-power" type="number" min="0" max="30" required><small>0 lets firmware choose.</small></label><label>Frequency slot<input id="radio-frequency-slot" type="number" min="0" max="65535" required><small>0 = automatic from the primary channel name. Explicit slots must exist for the selected region and preset. Shared by all messaging channels.</small></label></div><label class="radio-check"><input id="radio-tx-enabled" type="checkbox"> Radio transmission enabled</label><button type="submit">Save LoRa profile</button><output id="radio-lora-result"></output></form>
      </div>
      <div class="radio-config-pane" data-radio-pane="channels" hidden>
        <form id="radio-channel-form" class="radio-config-form radio-config-wide"><div><p class="eyebrow">CHANNELS</p><h3>Meshtastic channel slots</h3><p>Active slots must be consecutive. Keys are never read back or written to Outpost logs.</p></div><div class="radio-form-row"><label>Messaging channel slot<select id="radio-channel-index"></select></label><label>Role<select id="radio-channel-role"></select></label><label>Name<input id="radio-channel-name" maxlength="12"></label><label>Position precision<input id="radio-position-precision" type="number" min="0" max="32"><small>0 hides position; 32 is exact.</small></label></div><div id="radio-channel-policy" class="radio-policy-note"></div><fieldset><legend>Channel key</legend><label>Replace with base64 key<input id="radio-channel-psk" maxlength="44" autocomplete="off" spellcheck="false" placeholder="Leave blank to retain current key"></label><label class="radio-check"><input id="radio-channel-generate" type="checkbox"> Generate a new AES-256 key</label><small id="radio-channel-key-state"></small></fieldset><div class="radio-check-row"><label class="radio-check"><input id="radio-channel-uplink" type="checkbox"> MQTT uplink</label><label class="radio-check"><input id="radio-channel-downlink" type="checkbox"> MQTT downlink</label><label class="radio-check"><input id="radio-channel-muted" type="checkbox"> Mute received notifications</label></div><button type="submit">Save channel</button><output id="radio-channel-result"></output><div id="radio-generated-key" class="radio-generated-key" hidden><strong>Copy this key now</strong><p>It will not be shown again by Outpost.</p><code></code></div></form>
      </div>
      <div class="radio-config-pane" data-radio-pane="location" hidden>
        <form id="radio-position-form" class="radio-config-form radio-config-wide"><div><p class="eyebrow">LOCATION</p><h3>Radio position</h3><p>A fixed position is useful for a stationary Outpost. Position precision is still controlled per channel.</p></div><div class="radio-check-row"><label class="radio-check"><input id="radio-fixed-position" type="checkbox"> Use a fixed position</label><button id="radio-use-outpost-location" class="secondary" type="button">Use saved Outpost location</button></div><div class="radio-form-row"><label>Latitude<input id="radio-latitude" type="number" min="-90" max="90" step="0.000001"></label><label>Longitude<input id="radio-longitude" type="number" min="-180" max="180" step="0.000001"></label><label>Altitude · meters<input id="radio-altitude" type="number" min="-500" max="10000"></label><label>GPS mode<select id="radio-gps-mode"></select></label><label>Broadcast interval · seconds<input id="radio-position-interval" type="number" min="0" max="86400"></label></div><label class="radio-check"><input id="radio-position-smart" type="checkbox"> Smart position broadcasts</label><button type="submit">Save position settings</button><output id="radio-position-result"></output></form>
      </div>
      <div class="radio-config-pane" data-radio-pane="mqtt" hidden>
        <form id="radio-mqtt-form" class="radio-config-form radio-config-wide"><div><p class="eyebrow">MQTT</p><h3>Meshtastic gateway</h3><p>These are the same live radio settings shown in Federation. Channel payload encryption is always enforced by Outpost.</p></div><div class="radio-check-row"><label class="radio-check"><input id="radio-mqtt-enabled" type="checkbox"> Enable MQTT</label><label class="radio-check"><input id="radio-mqtt-tls" type="checkbox"> Use TLS</label></div><div class="radio-form-row"><label>Broker address<input id="radio-mqtt-address" maxlength="253" placeholder="mqtt.meshtastic.org"></label><label>Root topic<input id="radio-mqtt-root" maxlength="80" required></label><label>Channel<select id="radio-mqtt-channel"></select></label><label>Username<input id="radio-mqtt-username" maxlength="128" autocomplete="off" placeholder="Leave blank to retain"></label><label>Password<input id="radio-mqtt-password" type="password" maxlength="256" autocomplete="new-password" placeholder="Leave blank to retain"></label></div><div class="radio-check-row"><label class="radio-check"><input id="radio-mqtt-clear-username" type="checkbox"> Clear stored username</label><label class="radio-check"><input id="radio-mqtt-clear-password" type="checkbox"> Clear stored password</label></div><div class="radio-check-row"><label class="radio-check"><input id="radio-mqtt-uplink" type="checkbox"> Uplink selected channel</label><label class="radio-check"><input id="radio-mqtt-downlink" type="checkbox"> Downlink selected channel</label></div><details class="radio-advanced"><summary>Advanced MQTT behavior</summary><p>JSON and map reporting can expose node identity or location outside encrypted channel payloads. Enable only when your broker and policy require them.</p><div class="radio-check-row"><label class="radio-check"><input id="radio-mqtt-json" type="checkbox"> Publish JSON</label><label class="radio-check"><input id="radio-mqtt-proxy" type="checkbox"> Proxy through client</label><label class="radio-check"><input id="radio-mqtt-map" type="checkbox"> Map reporting</label></div></details><button type="submit">Save MQTT settings</button><output id="radio-mqtt-result"></output></form>
      </div>
    </div>
  </section>`;
}

export async function initRadioConfigurator({api}) {
  const outbound = document.querySelector(".operator-panel");
  if (!outbound) return;
  outbound.insertAdjacentHTML("beforebegin", configMarkup());
  let state = null;

  const channelAt = (index) =>
    (state?.channels || []).find((entry) => entry.index === Number(index));

  function renderChannel(index) {
    const channel = channelAt(index);
    if (!channel) return;
    fillSelect(byId("radio-channel-role"), index === 0 ? ["PRIMARY"] : ["SECONDARY", "DISABLED"], channel.role);
    value("radio-channel-name", channel.name);
    value("radio-position-precision", channel.position_precision);
    checked("radio-channel-uplink", channel.uplink_enabled);
    checked("radio-channel-downlink", channel.downlink_enabled);
    checked("radio-channel-muted", channel.muted);
    value("radio-channel-psk", "");
    checked("radio-channel-generate", false);
    byId("radio-channel-key-state").textContent = `Current radio key: ${channel.psk}`;
    const policy = (state.outpost_channel_policies || []).find((entry) => entry.index === Number(index));
    byId("radio-channel-policy").textContent = policy
      ? `Outpost policy · ${policy.name} · BBS ${policy.bbs.replaceAll("_", " ")} · reports ${policy.accept_reports ? "accepted" : "off"} · alerts ${policy.alerts ? "on" : "off"} · AI ${policy.ai ? "on" : "off"}. This slot cannot be disabled until policy changes.`
      : "No Outpost policy · commands received on this active slot are rejected.";
  }

  function renderMqttChannel(index) {
    const channel = channelAt(index);
    checked("radio-mqtt-uplink", channel?.uplink_enabled);
    checked("radio-mqtt-downlink", channel?.downlink_enabled);
  }

  function render(next) {
    state = next;
    const summary = byId("radio-config-summary");
    if (!state?.available) {
      summary.innerHTML = "<span class=\"radio-config-offline\">Radio configuration unavailable while the link is down.</span>";
      byId("radio-config-toggle").disabled = true;
      return;
    }
    byId("radio-config-toggle").disabled = false;
    summary.replaceChildren();
    const badges = [
      `${state.identity.long_name || state.node_id} · ${state.device.role}`,
      `${state.lora.region.replaceAll("_", " ")} · ${state.lora.modem_preset.replaceAll("_", " ")} · ${state.lora.frequency_slot ? `slot ${state.lora.frequency_slot}` : "auto slot"}`,
      `${state.channels.filter((entry) => entry.role !== "DISABLED").length} active channels`,
      state.mqtt.enabled ? `MQTT enabled · ${state.mqtt.address || "firmware broker"}` : "MQTT disabled",
    ];
    for (const text of badges) {
      const badge = document.createElement("span");
      badge.textContent = text;
      summary.append(badge);
    }
    const warnings = byId("radio-config-warnings");
    warnings.replaceChildren();
    warnings.hidden = !(state.warnings || []).length;
    for (const warning of state.warnings || []) {
      const paragraph = document.createElement("p");
      paragraph.textContent = warning;
      warnings.append(paragraph);
    }
    value("radio-long-name", state.identity.long_name);
    value("radio-short-name", state.identity.short_name);
    fillSelect(byId("radio-role"), state.options.roles, state.device.role);
    fillSelect(byId("radio-rebroadcast"), state.options.rebroadcast_modes, state.device.rebroadcast_mode);
    value("radio-node-interval", state.device.node_info_broadcast_secs || 10800);
    fillSelect(byId("radio-region"), state.options.regions, state.lora.region);
    fillSelect(byId("radio-preset-config"), state.options.modem_presets, state.lora.modem_preset);
    value("radio-frequency-slot", state.lora.frequency_slot || 0);
    value("radio-hop-limit", state.lora.hop_limit);
    value("radio-tx-power", state.lora.tx_power);
    checked("radio-tx-enabled", state.lora.tx_enabled);
    const indices = state.channels.map((entry) => entry.index);
    const selectedChannel = Number(byId("radio-channel-index").value || indices[0] || 0);
    fillSelect(byId("radio-channel-index"), indices, indices.includes(selectedChannel) ? selectedChannel : indices[0], Object.fromEntries(state.channels.map((entry) => [entry.index, `Slot ${entry.index} · ${entry.name || "unnamed"}`])));
    renderChannel(Number(byId("radio-channel-index").value));
    checked("radio-fixed-position", state.position.fixed_position);
    value("radio-latitude", state.position.latitude);
    value("radio-longitude", state.position.longitude);
    value("radio-altitude", state.position.altitude || 0);
    fillSelect(byId("radio-gps-mode"), state.options.gps_modes, state.position.gps_mode);
    value("radio-position-interval", state.position.broadcast_secs);
    checked("radio-position-smart", state.position.smart_broadcast);
    byId("radio-use-outpost-location").disabled = !state.outpost_location;
    checked("radio-mqtt-enabled", state.mqtt.enabled);
    checked("radio-mqtt-tls", state.mqtt.tls_enabled);
    value("radio-mqtt-address", state.mqtt.address);
    value("radio-mqtt-root", state.mqtt.root || "msh");
    value("radio-mqtt-username", "");
    value("radio-mqtt-password", "");
    byId("radio-mqtt-username").placeholder = state.mqtt.username_configured ? "Stored · leave blank to retain" : "Optional";
    byId("radio-mqtt-password").placeholder = state.mqtt.password_configured ? "Stored · leave blank to retain" : "Optional";
    checked("radio-mqtt-clear-username", false);
    checked("radio-mqtt-clear-password", false);
    checked("radio-mqtt-json", state.mqtt.json_enabled);
    checked("radio-mqtt-proxy", state.mqtt.proxy_to_client_enabled);
    checked("radio-mqtt-map", state.mqtt.map_reporting_enabled);
    const activeChannels = state.channels.filter((entry) => entry.role !== "DISABLED");
    const currentMqtt = activeChannels.find((entry) => entry.uplink_enabled || entry.downlink_enabled);
    fillSelect(byId("radio-mqtt-channel"), activeChannels.map((entry) => entry.index), currentMqtt?.index ?? activeChannels[0]?.index, Object.fromEntries(activeChannels.map((entry) => [entry.index, `${entry.name || "Channel"} · ${entry.index}`])));
    renderMqttChannel(Number(byId("radio-mqtt-channel").value));
  }

  async function load() {
    const response = await api("/api/v1/radio/config");
    if (!response.ok) {
      byId("radio-config-summary").textContent = "Radio configurator could not be loaded.";
      return;
    }
    render(await response.json());
  }

  async function apply(section, values, message, output) {
    const confirmed = await window.OutpostUI.confirm({eyebrow: "RADIO CHANGE", title: `Apply ${section} settings?`, message: `${message} The radio connection may restart briefly.`, confirmLabel: "Write to radio"});
    if (!confirmed) return;
    output.textContent = "Writing radio configuration…";
    const response = await api("/api/v1/radio/config", {method: "PUT", body: JSON.stringify({[section]: values})});
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      output.textContent = body.error?.message || "Radio configuration was not changed.";
      await window.OutpostUI.alert({title: "Radio not changed", message: output.textContent});
      return;
    }
    render(body);
    output.textContent = "Saved to the radio.";
    if (body.generated_psk) {
      const key = byId("radio-generated-key");
      key.hidden = false;
      key.querySelector("code").textContent = body.generated_psk;
    }
  }

  byId("radio-config-toggle").addEventListener("click", () => {
    const workspace = byId("radio-config-workspace");
    workspace.hidden = !workspace.hidden;
    byId("radio-config-toggle").textContent = workspace.hidden ? "Configure radio" : "Close configurator";
  });
  document.querySelectorAll("[data-radio-tab]").forEach((button) => button.addEventListener("click", () => {
    document.querySelectorAll("[data-radio-tab]").forEach((entry) => entry.classList.toggle("active", entry === button));
    document.querySelectorAll("[data-radio-pane]").forEach((pane) => {
      pane.hidden = pane.dataset.radioPane !== button.dataset.radioTab;
      pane.classList.toggle("active", !pane.hidden);
    });
  }));
  byId("radio-channel-index").addEventListener("change", (event) => renderChannel(Number(event.target.value)));
  byId("radio-mqtt-channel").addEventListener("change", (event) => renderMqttChannel(Number(event.target.value)));
  byId("radio-use-outpost-location").addEventListener("click", () => {
    if (!state?.outpost_location) return;
    checked("radio-fixed-position", true);
    value("radio-latitude", state.outpost_location.latitude);
    value("radio-longitude", state.outpost_location.longitude);
  });

  byId("radio-identity-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    await apply("identity", {long_name: byId("radio-long-name").value, short_name: byId("radio-short-name").value}, "This changes the name visible to nearby nodes.", byId("radio-identity-result"));
  });
  byId("radio-device-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    await apply("device", {role: byId("radio-role").value, rebroadcast_mode: byId("radio-rebroadcast").value, node_info_broadcast_secs: intValue("radio-node-interval")}, "The serial connection will remain enabled.", byId("radio-device-result"));
  });
  byId("radio-lora-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    await apply("lora", {region: byId("radio-region").value, modem_preset: byId("radio-preset-config").value, frequency_slot: intValue("radio-frequency-slot"), hop_limit: intValue("radio-hop-limit"), tx_power: intValue("radio-tx-power"), tx_enabled: byId("radio-tx-enabled").checked}, "Confirm that the selected region is legal at this installation.", byId("radio-lora-result"));
  });
  byId("radio-channel-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const index = intValue("radio-channel-index");
    await apply("channel", {index, role: byId("radio-channel-role").value, name: byId("radio-channel-name").value, psk: byId("radio-channel-psk").value || null, generate_psk: byId("radio-channel-generate").checked, uplink_enabled: byId("radio-channel-uplink").checked, downlink_enabled: byId("radio-channel-downlink").checked, position_precision: intValue("radio-position-precision"), muted: byId("radio-channel-muted").checked}, `This updates channel slot ${index}. Distribute any new key over a secure path.`, byId("radio-channel-result"));
  });
  byId("radio-position-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const fixed = byId("radio-fixed-position").checked;
    await apply("position", {fixed_position: fixed, gps_mode: byId("radio-gps-mode").value, smart_broadcast: byId("radio-position-smart").checked, broadcast_secs: intValue("radio-position-interval"), latitude: fixed ? Number(byId("radio-latitude").value) : null, longitude: fixed ? Number(byId("radio-longitude").value) : null, altitude: intValue("radio-altitude")}, fixed ? "This position may be shared according to each channel's precision." : "The saved fixed radio position will be removed.", byId("radio-position-result"));
  });
  byId("radio-mqtt-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const values = {enabled: byId("radio-mqtt-enabled").checked, address: byId("radio-mqtt-address").value, tls_enabled: byId("radio-mqtt-tls").checked, root: byId("radio-mqtt-root").value, channel: intValue("radio-mqtt-channel"), uplink_enabled: byId("radio-mqtt-uplink").checked, downlink_enabled: byId("radio-mqtt-downlink").checked, json_enabled: byId("radio-mqtt-json").checked, proxy_to_client_enabled: byId("radio-mqtt-proxy").checked, map_reporting_enabled: byId("radio-mqtt-map").checked};
    const username = byId("radio-mqtt-username").value;
    const password = byId("radio-mqtt-password").value;
    if (username || byId("radio-mqtt-clear-username").checked) values.username = username;
    if (password || byId("radio-mqtt-clear-password").checked) values.password = password;
    const warning = values.json_enabled || values.map_reporting_enabled ? "JSON or map reporting can expose identity or location outside encrypted channel payloads." : "The Federation page will reflect these same live settings.";
    await apply("mqtt", values, warning, byId("radio-mqtt-result"));
  });

  await load();
}
