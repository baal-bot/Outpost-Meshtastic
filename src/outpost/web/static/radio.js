import("/nav.js");

const $ = (id) => document.getElementById(id);
const safe = (value) =>
  String(value ?? "").replace(
    /[&<>'"]/g,
    (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char],
  );
let csrfToken = "";
const messagePageSize = 25;
let messageItems = [];
let messageNextCursor = null;
let messageFilterKey = "";
let messageHistoryExpanded = false;

$("send-channel").disabled = true;
$("send-form").querySelector("button").disabled = true;

const api = async (url, options = {}) =>
  fetch(url, {
    ...options,
    headers: {
      ...(options.body ? { "content-type": "application/json" } : {}),
      ...(options.method && options.method !== "GET" ? { "x-csrf-token": csrfToken } : {}),
      ...options.headers,
    },
  });

const activeQueueStates = new Set(["pending", "held", "sending", "awaiting_ack"]);

function queueItemMatches(item, filter) {
  const state = item.state || "pending";
  if (filter === "active") return activeQueueStates.has(state);
  if (filter === "failed" || filter === "expired") return state === filter;
  if (filter === "all") return true;
  return state !== "expired";
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

function installInboundHealthCard() {
  const nodeCard = $("radio-node").closest("article");
  nodeCard.insertAdjacentHTML(
    "beforebegin",
    '<article id="inbound-health"><small>INBOUND WORK QUEUE</small>' +
      '<strong id="inbound-backlog">—</strong>' +
      '<p id="inbound-detail">loading worker health</p></article>',
  );
}

async function initialize() {
  const response = await fetch("/api/v1/auth/session");
  if (!response.ok) {
    location.href = "/";
    return;
  }
  csrfToken = (await response.json()).csrf_token;
  const {initRadioConfigurator} = await import("/radio-config.js");
  await initRadioConfigurator({api});
  await refresh();
  const {scheduler} = await import("/refresh-scheduler.js");
  scheduler.schedule("radio-main", refresh, {interval:10000});
}

async function refresh() {
  const [status, airtime, queue, channelMap] = await Promise.all([
    api("/api/v1/status").then((response) => response.json()),
    api("/api/v1/mesh/airtime").then((response) => response.json()),
    api("/api/v1/mesh/queue").then((response) => response.json()),
    api("/api/v1/radio/channels").then((response) => response.json()),
  ]);
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
  const messages = await api(`/api/v1/mesh/messages?${messageQuery(0)}`).then((response) =>
    response.json(),
  );
  const state = $("link-state");
  state.className = `ui-pill status ${status.radio}`;
  state.innerHTML = `<i></i>${safe(status.radio)}`;
  $("radio-node").textContent = status.radio_config.node_id || "—";
  $("radio-preset").textContent =
    `${status.radio_config.region} · ${status.radio_config.preset}`;
  $("airtime-total").textContent = airtime.used_seconds.toFixed(2);
  $("queue-count").textContent = queue.items.filter((item) =>
    ["pending", "held", "sending"].includes(item.state || "pending"),
  ).length;
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
  const stateLabel = {
    pending: "Queued",
    held: "Committing",
    sending: "Transmitting",
    awaiting_ack: "Awaiting acknowledgement",
    failed: "Failed",
    expired: "Expired",
  };
  const queueFilter = $("filter-queue-state").value;
  const visibleQueue = queue.items.filter((item) => queueItemMatches(item, queueFilter));
  const emptyQueueCopy = {
    current: "No current or failed outbound work.",
    active: "No active outbound work.",
    failed: "No failed outbound work.",
    expired: "No expired outbound history.",
    all: "No outbound work records.",
  };
  $("queue-list").innerHTML =
    visibleQueue
      .map((item) => {
        const stateName = item.state || "pending";
        const created = item.created_at
          ? new Date(item.created_at * 1000).toLocaleString()
          : "Current session";
        const payload = item.text || `${item.byte_len} byte application frame`;
        const detail = item.last_error
          ? `<p class="${stateName === "failed" ? "queue-error" : "queue-meta"}">${safe(
              item.last_error,
            )}</p>`
          : `<p class="queue-meta">Queued ${safe(created)} · ${item.attempts || 0} attempt${
              Number(item.attempts || 0) === 1 ? "" : "s"
            }</p>`;
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
      .join("") || `<p class="ui-empty empty">${emptyQueueCopy[queueFilter]}</p>`;
  document.querySelectorAll("[data-cancel]").forEach((button) =>
    button.addEventListener("click", async () => {
      await api(`/api/v1/mesh/queue/${button.dataset.cancel}`, { method: "DELETE" });
      await refresh();
    }),
  );
}

$("send-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("send-result").textContent = "";
  const response = await api("/api/v1/mesh/send", {
    method: "POST",
    body: JSON.stringify({
      text: $("send-text").value,
      destination: $("send-destination").value,
      channel: Number($("send-channel").value),
      traffic_class: $("send-class").value,
    }),
  });
  const body = await response.json();
  if (!response.ok) {
    $("send-result").textContent = body.error.message;
    return;
  }
  $("send-result").textContent = `Queued as item #${body.queue_id}`;
  $("send-text").value = "";
  await refresh();
});

installInboundHealthCard();
$("refresh-radio").addEventListener("click", refresh);
$("filter-queue-state").addEventListener("change", refresh);
$("filter-direction").addEventListener("change", refresh);
$("filter-channel").addEventListener("change", refresh);
$("load-more-messages").addEventListener("click", loadMoreMessages);
initialize();
