import "/nav.js";
import {byId as $, createApiClient, escapeHtml as safe} from "/ui-primitives.js";
let csrfToken = "";
const messagePageSize = 25;
let messageItems = [];
let messageNextCursor = null;
let messageFilterKey = "";
let messageHistoryExpanded = false;
let queueItems = [];
let queueNextCursor = null;
let queueFilterKey = "";
let queueHistoryExpanded = false;
let queueMeta = { counts: {}, total: 0, retention_days: 30 };
let sendInFlight = false;
let sendEstimate = null;
let estimateSequence = 0;
let estimateTimer = null;

$("send-channel").disabled = true;
$("send-form").querySelector("button").disabled = true;

const api = createApiClient(() => csrfToken);

const sendAirtime = document.createElement("small");
sendAirtime.id = "send-airtime";
sendAirtime.textContent = "0 / 200 UTF-8 bytes · enter a message to estimate airtime.";
$("send-form").insertBefore(sendAirtime, $("send-channel-state"));

function seconds(value) {
  return `${Number(value || 0).toFixed(2)}s`;
}

function sendEstimatePayload(airtimeConfirmation = false) {
  return {
    text: $("send-text").value,
    destination: $("send-destination").value,
    channel: Number($("send-channel").value),
    traffic_class: $("send-class").value,
    airtime_confirmation: airtimeConfirmation,
  };
}

function renderSendEstimate(estimate) {
  const byteCount = new TextEncoder().encode($("send-text").value).length;
  if (!estimate) {
    sendAirtime.textContent = `${byteCount} / 200 UTF-8 bytes · ${
      byteCount ? "airtime estimate unavailable." : "enter a message to estimate airtime."
    }`;
    sendAirtime.classList.toggle("history-warning", byteCount > 200);
    return;
  }
  const remaining = estimate.budget?.remaining_after_seconds;
  const cost = `${seconds(estimate.total_seconds)} · ${estimate.part_count} part${
    estimate.part_count === 1 ? "" : "s"
  } · ${estimate.costing_preset}`;
  const budget = remaining == null ? "" : ` · ${seconds(remaining)} normal budget remains`;
  const warning = estimate.requires_confirmation
    ? ` · CONFIRMATION REQUIRED: ${estimate.displacement}`
    : "";
  sendAirtime.textContent = `${byteCount} / 200 UTF-8 bytes · ${cost}${budget}${warning}`;
  sendAirtime.classList.toggle("history-warning", estimate.requires_confirmation);
}

async function refreshSendEstimate() {
  const sequence = ++estimateSequence;
  const byteCount = new TextEncoder().encode($("send-text").value).length;
  if (!byteCount || byteCount > 200 || $("send-channel").disabled) {
    sendEstimate = null;
    renderSendEstimate(null);
    return null;
  }
  try {
    const response = await api("/api/v1/mesh/estimate", {
      method: "POST",
      body: JSON.stringify(sendEstimatePayload()),
    });
    const body = await response.json();
    if (sequence !== estimateSequence) return sendEstimate;
    sendEstimate = response.ok ? body : null;
    renderSendEstimate(sendEstimate);
    return sendEstimate;
  } catch {
    if (sequence === estimateSequence) {
      sendEstimate = null;
      renderSendEstimate(null);
    }
    return null;
  }
}

function scheduleSendEstimate() {
  clearTimeout(estimateTimer);
  renderSendEstimate(null);
  estimateTimer = setTimeout(refreshSendEstimate, 180);
}

const activeQueueStates = new Set(["pending", "held", "sending", "awaiting_ack"]);
const terminalQueueStates = new Set([
  "sent",
  "acked",
  "failed",
  "expired",
  "cancelled",
  "superseded",
  "retracted",
]);

function queueQuery(cursor = null) {
  const query = new URLSearchParams({
    state: $("filter-queue-state").value,
    limit: "25",
  });
  if (cursor !== null) query.set("cursor", String(cursor));
  return query;
}

function queueItemMatches(item, filter) {
  const state = item.state || "pending";
  if (filter === "active") return activeQueueStates.has(state);
  if (filter === "failed" || filter === "expired") return state === filter;
  if (filter === "terminal") return terminalQueueStates.has(state);
  if (filter === "all") return true;
  return activeQueueStates.has(state) || state === "failed";
}

function updateNewestQueue(result) {
  const fallbackCounts = {};
  for (const item of result.items) {
    const state = item.state || "pending";
    fallbackCounts[state] = Number(fallbackCounts[state] || 0) + 1;
  }
  queueMeta = {
    counts: result.counts || fallbackCounts,
    total: Number(result.total ?? result.items.length),
    retention_days: Number(result.retention_days ?? 30),
  };
  if (!queueHistoryExpanded) {
    queueItems = result.items;
    queueNextCursor = result.next_cursor ?? null;
    return;
  }
  const newestIds = new Set(result.items.map((item) => item.id));
  queueItems = [
    ...result.items,
    ...queueItems.filter((item) => !newestIds.has(item.id)),
  ];
}

function queueOutcome(item) {
  if (item.outcome_explanation) return item.outcome_explanation;
  const state = item.state || "pending";
  if (state === "acked") return "Acknowledged by the destination.";
  if (state === "sent") return "Sent; no ACK was requested.";
  if (state === "failed") return "Delivery failed after bounded retries.";
  if (state === "expired") return "Expired before delivery completed.";
  if (state === "cancelled" || state === "retracted") return "Cancelled before transmission.";
  if (state === "superseded") return "Superseded by newer queued work.";
  if (state === "awaiting_ack") return "Sent; waiting for an ACK.";
  return "Waiting for airtime policy.";
}

function queueTimestamp(item) {
  const timestamp = item.outcome_at || item.created_at;
  return timestamp ? new Date(timestamp * 1000).toLocaleString() : "Current session";
}

function renderQueue() {
  const stateLabel = {
    pending: "Queued",
    held: "Committing",
    sending: "Transmitting",
    awaiting_ack: "Awaiting acknowledgement",
    sent: "Sent",
    acked: "Acknowledged",
    failed: "Failed",
    expired: "Expired",
    cancelled: "Cancelled",
    superseded: "Superseded",
    retracted: "Retracted",
  };
  const queueFilter = $("filter-queue-state").value;
  const visibleQueue = queueItems.filter((item) => queueItemMatches(item, queueFilter));
  const emptyQueueCopy = {
    current: "No active or failed outbound work.",
    active: "No active outbound work.",
    failed: "No failed outbound work.",
    expired: "No expired outbound history in the retention window.",
    terminal: "No completed outbound history in the retention window.",
    all: "No outbound work or retained history.",
  };
  $("queue-list").innerHTML =
    visibleQueue
      .map((item) => {
        const stateName = item.state || "pending";
        const payload = item.text || `${item.binary_len || item.byte_len} byte application frame`;
        const detail = terminalQueueStates.has(stateName)
          ? `<p class="${stateName === "failed" ? "queue-error" : "queue-meta"}">${safe(
              queueOutcome(item),
            )} <small>${safe(item.reason_code || stateName)} · ${safe(queueTimestamp(item))}</small></p>`
          : `<p class="queue-meta">${safe(queueOutcome(item))} <small>${
              item.attempts || 0
            } attempt${Number(item.attempts || 0) === 1 ? "" : "s"} · ${safe(
              queueTimestamp(item),
            )}</small></p>`;
        return (
          `<article class="queue-card queue-${safe(stateName)}">` +
          `<div class="queue-card-head"><strong>#${item.id} · ${safe(item.traffic_class)} → ` +
          `${safe(item.destination)}</strong><span>${safe(stateLabel[stateName] || stateName)}${
            item.stale ? " · stale" : ""
          }</span></div><p title="${safe(payload)}">${safe(payload)}</p>${detail}` +
          (item.cancellable
            ? `<button data-cancel="${item.id}">Cancel item</button>`
            : "") +
          `</article>`
        );
      })
      .join("") ||
    `<p class="ui-empty empty">${
      queueMeta.total > 0
        ? "No matching records on this page. Choose another filter or refresh."
        : emptyQueueCopy[queueFilter]
    }</p>`;
  const counts = queueMeta.counts || {};
  const activeCount = [...activeQueueStates].reduce(
    (total, state) => total + Number(counts[state] || 0),
    0,
  );
  $("queue-history-summary").textContent =
    `Showing ${visibleQueue.length} of ${queueMeta.total || 0} · ${activeCount} active · ` +
    `${Number(counts.failed || 0)} failed · ${queueMeta.retention_days}-day history, not current backlog`;
  $("load-more-queue").hidden = queueNextCursor === null;
  document.querySelectorAll("[data-cancel]").forEach((button) =>
    button.addEventListener("click", async () => {
      const response = await api(`/api/v1/mesh/queue/${button.dataset.cancel}`, {
        method: "DELETE",
      });
      if (response.ok) queueHistoryExpanded = false;
      await refresh();
    }),
  );
}

function messageOutcomeLabel(outcome) {
  if (outcome === "not_requested") return "no ACK requested";
  return outcome || "—";
}

function appendChannelOption(select, channel, suffix = "") {
  const entry = document.createElement("option");
  entry.value = String(channel.index);
  entry.textContent = `${channel.name} · ch ${channel.index}${suffix}`;
  select.append(entry);
}

function renderChannelMap(channelMap) {
  const sendSelect = $("send-channel");
  const priorSend = sendSelect.value;
  const active = channelMap.items.filter((channel) => channel.active);
  const lastVerified = channelMap.items.filter((channel) => channel.last_verified_active);
  const sendChoices = channelMap.available ? active : channelMap.stale ? lastVerified : [];
  sendSelect.replaceChildren();
  if (sendChoices.length) {
    for (const channel of sendChoices) {
      appendChannelOption(sendSelect, channel, channelMap.available ? "" : " · last verified");
    }
    sendSelect.value = sendChoices.some((channel) => String(channel.index) === priorSend)
      ? priorSend
      : String(sendChoices[0].index);
  } else {
    const empty = document.createElement("option");
    empty.value = "";
    empty.textContent = channelMap.available
      ? "No active radio channels"
      : "No verified channel map";
    sendSelect.append(empty);
  }
  const canSend = channelMap.available && active.length > 0;
  sendSelect.disabled = !canSend;
  $("send-form").querySelector("button").disabled = !canSend;
  const mapState = $("send-channel-state");
  if (channelMap.available) {
    mapState.textContent = `${active.length} active radio channel${active.length === 1 ? "" : "s"}.`;
  } else if (channelMap.stale) {
    const verified = channelMap.verified_at
      ? new Date(channelMap.verified_at * 1000).toLocaleString()
      : "an earlier session";
    mapState.textContent = `Last verified ${verified}; sending is disabled while the radio is disconnected.`;
  } else {
    mapState.textContent = "No verified radio channel map; sending is disabled.";
  }
  mapState.classList.toggle("history-warning", !channelMap.available);

  const historySelect = $("filter-channel");
  const priorHistory = historySelect.value;
  historySelect.replaceChildren();
  const all = document.createElement("option");
  all.value = "";
  all.textContent = "All channels";
  historySelect.append(all);
  const historyChoices = channelMap.items.filter(
    (channel) => channel.active || channel.historical || (channelMap.stale && channel.last_verified_active),
  );
  for (const channel of historyChoices) {
    const suffix = channel.active
      ? ""
      : channel.historical
        ? " · retained"
        : " · last verified";
    appendChannelOption(historySelect, channel, suffix);
  }
  historySelect.value = historyChoices.some(
    (channel) => String(channel.index) === priorHistory,
  )
    ? priorHistory
    : "";
  if (!sendEstimate && $("send-text").value) scheduleSendEstimate();
}

function messageQuery(cursor) {
  const query = new URLSearchParams({
    limit: String(messagePageSize),
    cursor: String(cursor),
  });
  const direction = $("filter-direction").value;
  const channel = $("filter-channel").value;
  if (direction) query.set("direction", direction);
  if (channel) query.set("channel", channel);
  return query;
}

function renderMessages() {
  $("message-count").textContent = messageItems.length;
  $("message-rows").innerHTML =
    messageItems
      .map(
        (message) =>
          `<tr><td>${new Date(message.created_at).toLocaleTimeString()}</td>` +
          `<td>${message.direction === "in" ? "↙ RX" : "↗ TX"}</td>` +
          `<td><code>${safe(message.peer_mesh_id || "broadcast")}</code></td>` +
          `<td>${message.channel}</td>` +
          `<td>${safe(message.text || `${message.byte_len} bytes`)}</td>` +
          `<td>${safe(messageOutcomeLabel(message.outcome))}</td>` +
          `<td>${message.rx_snr == null ? "—" : `${safe(message.rx_snr)} dB`}</td></tr>`,
      )
      .join("") || '<tr><td colspan="7">No matching messages.</td></tr>';
  $("load-more-messages").hidden = messageNextCursor === null;
}

function updateNewestMessages(result) {
  if (!messageHistoryExpanded) {
    messageItems = result.items;
    messageNextCursor = result.next_cursor;
    renderMessages();
    return;
  }
  const existingIds = new Set(messageItems.map((item) => item.id));
  const added = result.items.filter((item) => !existingIds.has(item.id));
  const newestIds = new Set(result.items.map((item) => item.id));
  messageItems = [
    ...result.items,
    ...messageItems.filter((item) => !newestIds.has(item.id)),
  ];
  if (messageNextCursor !== null) messageNextCursor += added.length;
  renderMessages();
}

async function loadMoreMessages() {
  if (messageNextCursor === null) return;
  const button = $("load-more-messages");
  button.disabled = true;
  button.textContent = "Loading…";
  try {
    const response = await api(`/api/v1/mesh/messages?${messageQuery(messageNextCursor)}`);
    if (!response.ok) return;
    const result = await response.json();
    const existingIds = new Set(messageItems.map((item) => item.id));
    messageItems.push(...result.items.filter((item) => !existingIds.has(item.id)));
    messageNextCursor = result.next_cursor;
    messageHistoryExpanded = true;
    renderMessages();
  } finally {
    button.disabled = false;
    button.textContent = "Load more";
  }
}

async function loadMoreQueue() {
  if (queueNextCursor === null) return;
  const button = $("load-more-queue");
  button.disabled = true;
  button.textContent = "Loading…";
  try {
    const response = await api(`/api/v1/mesh/queue?${queueQuery(queueNextCursor)}`);
    if (!response.ok) return;
    const result = await response.json();
    const existingIds = new Set(queueItems.map((item) => item.id));
    queueItems.push(...result.items.filter((item) => !existingIds.has(item.id)));
    queueNextCursor = result.next_cursor ?? null;
    queueMeta = {
      counts: result.counts || queueMeta.counts,
      total: Number(result.total ?? queueMeta.total),
      retention_days: Number(result.retention_days ?? queueMeta.retention_days),
    };
    queueHistoryExpanded = true;
    renderQueue();
  } finally {
    button.disabled = false;
    button.textContent = "Load more";
  }
}

function installQueueHistoryControls() {
  $("filter-queue-state").querySelector('option[value="current"]').textContent =
    "Current · active + failed";
  const allOption = $("filter-queue-state").querySelector('option[value="all"]');
  const terminalOption = document.createElement("option");
  terminalOption.value = "terminal";
  terminalOption.textContent = "Terminal history";
  allOption.before(terminalOption);
  $("queue-list").insertAdjacentHTML(
    "afterend",
    '<div class="packet-history-actions queue-history-actions"><span id="queue-history-summary"></span>' +
      '<button id="load-more-queue" class="ui-button small-button" type="button" hidden>Load more</button></div>',
  );
}

function installInboundHealthCard() {
  const nodeCard = $("radio-node").closest("article");
  nodeCard.insertAdjacentHTML(
    "beforebegin",
    '<article id="inbound-health"><small>INBOUND WORK QUEUE</small>' +
      '<strong id="inbound-backlog">—</strong>' +
      '<p id="inbound-detail">loading worker health</p></article>',
  );
}

function installPowerCard() {
  const nodeCard = $("radio-node").closest("article");
  nodeCard.insertAdjacentHTML(
    "beforebegin",
    '<article id="radio-power-card"><small>RADIO POWER</small>' +
      '<strong id="radio-power-level">—</strong>' +
      '<div id="radio-power-trace" class="power-trace" role="img" aria-label="No battery trend yet"></div>' +
      '<p id="radio-power-detail">loading battery telemetry</p></article>',
  );
}

async function initialize() {
  const response = await fetch("/api/v1/auth/session");
  if (!response.ok) {
    location.href = "/";
    return;
  }
  csrfToken = (await response.json()).csrf_token;
  const {initRadioConfigurator} = await import("/radio-config.js?v=2");
  await initRadioConfigurator({api});
  await refresh();
  const {scheduler} = await import("/refresh-scheduler.js");
  scheduler.schedule("radio-main", refresh, {interval:10000});
}

async function refresh() {
  const selectedQueueFilter = $("filter-queue-state").value;
  if (selectedQueueFilter !== queueFilterKey) {
    queueItems = [];
    queueNextCursor = null;
    queueHistoryExpanded = false;
    queueFilterKey = selectedQueueFilter;
  }
  const responses = await Promise.all([
    api("/api/v1/status"),api("/api/v1/mesh/airtime"),
    api("/api/v1/mesh/power"),api(`/api/v1/mesh/queue?${queueQuery()}`),
    api("/api/v1/radio/channels"),
  ]);
  if(responses.some(response => !response.ok)){
    const failed=responses.map((response,index)=>({response,index})).filter(value=>!value.response.ok),labels=["Status","Airtime","Power","Queue","Channels"];
    const state=$("link-state");state.className="ui-pill status down";state.innerHTML="<i></i>Data unavailable";
    $("inbound-detail").textContent=failed.map(value=>`${labels[value.index]} HTTP ${value.response.status}`).join(" · ");
    if(failed.some(value=>value.index===3))$("queue-list").innerHTML='<p class="ui-empty empty">Queue data unavailable.</p>';
    return;
  }
  const [status, airtime, power, queue, channelMap] = await Promise.all(responses.map(response=>response.json()));
  renderChannelMap(channelMap);
  const direction = $("filter-direction").value;
  const channel = $("filter-channel").value;
  const selectedFilterKey = `${direction}:${channel}`;
  if (selectedFilterKey !== messageFilterKey) {
    messageItems = [];
    messageNextCursor = null;
    messageHistoryExpanded = false;
    messageFilterKey = selectedFilterKey;
  }
  const messageResponse=await api(`/api/v1/mesh/messages?${messageQuery(0)}`);
  if(!messageResponse.ok){$("message-rows").innerHTML=`<tr><td colspan="7">Message log unavailable · HTTP ${messageResponse.status}.</td></tr>`;return;}
  const messages=await messageResponse.json();
  const state = $("link-state");
  state.className = `ui-pill status ${status.radio}`;
  state.innerHTML = `<i></i>${safe(status.radio)}`;
  $("radio-node").textContent = status.radio_config.node_id || "—";
  $("radio-preset").textContent =
    `${status.radio_config.region} · ${status.radio_config.preset}`;
  const powerCard = $("radio-power-card");
  powerCard.classList.remove("power-normal", "power-warning", "power-critical", "power-not_reported");
  powerCard.classList.add(`power-${power.condition || "not_reported"}`);
  $("radio-power-level").textContent = power.reported
    ? `${Number(power.battery_level).toFixed(0)}%`
    : "No battery";
  const trend = power.trend || {};
  const thresholds = power.thresholds || {};
  const shedding = power.shedding || {};
  let powerDetail = "No battery reported · external power or unsupported telemetry";
  if (power.reported) {
    const trendText = trend.delta_percent == null || trend.elapsed_hours == null
      ? "trend pending"
      : `${trend.direction} ${Math.abs(Number(trend.delta_percent)).toFixed(0)} points / ${Number(trend.elapsed_hours).toFixed(1)}h`;
    powerDetail = `${trendText} · warn ≤${thresholds.warning_percent}% · critical ≤${thresholds.critical_percent}%`;
  }
  if (shedding.active) powerDetail += " · AI, bulletins, and digests paused";
  else if (shedding.enabled) powerDetail += ` · shedding armed at ≤${shedding.below_percent}%`;
  if (status.radio !== "up" && power.observed_at) {
    powerDetail += ` · last observed ${new Date(power.observed_at * 1000).toLocaleString()}`;
  }
  $("radio-power-detail").textContent = powerDetail;
  const samples = (power.samples || []).filter(sample => sample.battery_level != null).slice(-24);
  const trace = $("radio-power-trace");
  trace.innerHTML = samples.map(sample => `<i style="height:${safe(Math.max(4, Number(sample.battery_level)))}%"></i>`).join("");
  trace.hidden = samples.length < 2;
  trace.setAttribute("aria-label", samples.length < 2
    ? "No battery trend yet"
    : `${samples.length} sampled battery readings; trend ${trend.direction || "unavailable"}`);
  let governorProfile = $("governor-profile");
  if (!governorProfile) {
    governorProfile = document.createElement("article");
    governorProfile.id = "governor-profile";
    governorProfile.innerHTML =
      '<small>GOVERNOR MODEL</small><strong id="governor-preset">—</strong>' +
      '<p id="governor-budget">—</p>';
    document.querySelector(".radio-kpis").append(governorProfile);
  }
  $("governor-preset").textContent = airtime.costing_preset || "—";
  const governorWarnings = airtime.warnings || [];
  const regionalCeiling = airtime.regional_ceiling_percent == null
    ? "no regional ceiling"
    : `${Number(airtime.regional_ceiling_percent).toFixed(1)}% region ceiling`;
  $("governor-budget").textContent = governorWarnings.length
    ? governorWarnings.join(" ")
    : `Radio ${airtime.reported_preset} · effective ${Number(airtime.budget_percent).toFixed(2)}% + ${Number(airtime.reserve_percent).toFixed(2)}% reserve · ${regionalCeiling}`;
  governorProfile.classList.toggle(
    "warning",
    governorWarnings.length > 0 || !airtime.profile_matches,
  );
  $("airtime-total").textContent = airtime.used_seconds.toFixed(2);
  updateNewestQueue(queue);
  const queueCounts = queueMeta.counts || {};
  $("queue-count").textContent = [...activeQueueStates].reduce(
    (total, queueState) => total + Number(queueCounts[queueState] || 0),
    0,
  );
  renderQueue();
  updateNewestMessages(messages);

  const inbound = status.inbound || {};
  const radioInbound = inbound.radio || {};
  const pipeline = inbound.pipeline_dropped || {};
  const queueLosses =
    Number(inbound.backlog_dropped || 0) + Number(radioInbound.dropped || 0);
  const filtered = Object.values(pipeline).reduce(
    (sum, value) => sum + Number(value || 0),
    0,
  );
  const backlog = Number(inbound.backlog || 0);
  const capacity = Number(inbound.capacity || 0);
  const busy = Number(inbound.busy || 0);
  const workers = Number(inbound.workers || 0);
  let queueState = "healthy";
  if (capacity > 0 && backlog >= capacity) queueState = "critical";
  else if (backlog > 0) queueState = "active";
  $("inbound-backlog").textContent = `${backlog} waiting`;
  $("inbound-detail").textContent =
    `capacity ${capacity || "—"} · ${busy}/${workers} workers busy · ` +
    (queueLosses === 0
      ? "no queue loss"
      : `${queueLosses} queue losses since restart`) +
    (filtered === 0 ? "" : ` · ${filtered} duplicate/self filtered`);
  $("inbound-detail").classList.toggle("history-warning", queueLosses > 0);
  $("inbound-health").classList.remove(
    "warning",
    "queue-healthy",
    "queue-active",
    "queue-critical",
  );
  $("inbound-health").classList.add(`queue-${queueState}`);

  const maximum = Math.max(1, ...Object.values(airtime.by_class_seconds));
  $("airtime-bars").innerHTML = Object.entries(airtime.by_class_seconds)
    .map(
      ([name, value]) =>
        `<div class="airtime-row"><span>${safe(name)}</span>` +
        `<div class="airtime-track"><i style="width:${safe((value / maximum) * 100)}%"></i></div>` +
        `<strong>${Number(value).toFixed(2)}s</strong></div>`,
    )
    .join("");
}

$("send-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (sendInFlight) return;
  sendInFlight = true;
  const button = event.currentTarget.querySelector("button");
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "Queueing…";
  $("send-result").textContent = "";
  try {
    const estimate = await refreshSendEstimate();
    let airtimeConfirmation = false;
    if (estimate?.requires_confirmation) {
      airtimeConfirmation = await window.OutpostUI.confirm({
        title: "Cross airtime constraint?",
        message: `${seconds(estimate.total_seconds)} for ${estimate.transmission_count} transmission. ${estimate.displacement}`,
        confirmLabel: "Queue despite constraint",
        danger: true,
      });
      if (!airtimeConfirmation) return;
    }
    let response = await api("/api/v1/mesh/send", {
      method: "POST",
      body: JSON.stringify(sendEstimatePayload(airtimeConfirmation)),
    });
    let body = await response.json();
    if (response.status === 409 && body.airtime?.requires_confirmation) {
      const confirmed = await window.OutpostUI.confirm({
        title: "Airtime state changed",
        message: `${seconds(body.airtime.total_seconds)} now crosses a constraint. ${body.airtime.displacement}`,
        confirmLabel: "Queue despite constraint",
        danger: true,
      });
      if (!confirmed) return;
      response = await api("/api/v1/mesh/send", {
        method: "POST",
        body: JSON.stringify(sendEstimatePayload(true)),
      });
      body = await response.json();
    }
    if (!response.ok) {
      $("send-result").textContent = body.error.message;
      return;
    }
    $("send-result").textContent = `Queued as item #${body.queue_id}`;
    $("send-text").value = "";
    sendEstimate = null;
    renderSendEstimate(null);
    await refresh();
  } catch (error) {
    $("send-result").textContent = error?.message || "Message could not be queued.";
  } finally {
    sendInFlight = false;
    button.textContent = label;
    button.disabled = $("send-channel").disabled;
  }
});

for (const id of ["send-text", "send-destination", "send-channel", "send-class"]) {
  $(id).addEventListener(id === "send-text" ? "input" : "change", scheduleSendEstimate);
}

installInboundHealthCard();
installPowerCard();
installQueueHistoryControls();
$("refresh-radio").addEventListener("click", refresh);
$("filter-queue-state").addEventListener("change", refresh);
$("filter-direction").addEventListener("change", refresh);
$("filter-channel").addEventListener("change", refresh);
$("load-more-messages").addEventListener("click", loadMoreMessages);
$("load-more-queue").addEventListener("click", loadMoreQueue);
initialize();
