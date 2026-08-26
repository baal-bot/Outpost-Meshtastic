import("/nav.js");

const $ = (id) => document.getElementById(id);
const safe = (value) =>
  String(value ?? "").replace(
    /[&<>'"]/g,
    (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char],
  );
let csrfToken = "";

const api = async (url, options = {}) =>
  fetch(url, {
    ...options,
    headers: {
      ...(options.body ? { "content-type": "application/json" } : {}),
      ...(options.method && options.method !== "GET" ? { "x-csrf-token": csrfToken } : {}),
      ...options.headers,
    },
  });

function installInboundHealthCard() {
  const nodeCard = $("radio-node").closest("article");
  nodeCard.insertAdjacentHTML(
    "beforebegin",
    '<article id="inbound-health"><small>INBOUND BACKLOG</small>' +
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
  await refresh();
}

async function refresh() {
  const direction = $("filter-direction").value;
  const channel = $("filter-channel").value;
  const query = new URLSearchParams();
  if (direction) query.set("direction", direction);
  if (channel) query.set("channel", channel);
  const [status, airtime, queue, messages] = await Promise.all([
    api("/api/v1/status").then((response) => response.json()),
    api("/api/v1/mesh/airtime").then((response) => response.json()),
    api("/api/v1/mesh/queue").then((response) => response.json()),
    api(`/api/v1/mesh/messages?limit=100&${query}`).then((response) => response.json()),
  ]);
  const state = $("link-state");
  state.className = `status ${status.radio}`;
  state.innerHTML = `<i></i>${safe(status.radio)}`;
  $("radio-node").textContent = status.radio_config.node_id || "—";
  $("radio-preset").textContent =
    `${status.radio_config.region} · ${status.radio_config.preset}`;
  $("airtime-total").textContent = airtime.used_seconds.toFixed(2);
  $("queue-count").textContent = queue.items.filter((item) =>
    ["pending", "held", "sending"].includes(item.state || "pending"),
  ).length;
  $("message-count").textContent = messages.items.length;

  const inbound = status.inbound || {};
  const radioInbound = inbound.radio || {};
  const pipeline = inbound.pipeline_dropped || {};
  const drops =
    Number(inbound.backlog_dropped || 0) +
    Number(radioInbound.dropped || 0) +
    Object.values(pipeline).reduce((sum, value) => sum + Number(value || 0), 0);
  $("inbound-backlog").textContent = `${inbound.backlog || 0} / ${inbound.capacity || "—"}`;
  $("inbound-detail").textContent =
    `${inbound.busy || 0}/${inbound.workers || 0} workers busy · ${drops} dropped`;
  $("inbound-health").classList.toggle("warning", drops > 0);

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
  $("queue-list").innerHTML =
    queue.items
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
      .join("") || '<p class="empty">No queued, failed, or stale work.</p>';
  document.querySelectorAll("[data-cancel]").forEach((button) =>
    button.addEventListener("click", async () => {
      await api(`/api/v1/mesh/queue/${button.dataset.cancel}`, { method: "DELETE" });
      await refresh();
    }),
  );
  $("message-rows").innerHTML =
    messages.items
      .map(
        (message) =>
          `<tr><td>${new Date(message.created_at).toLocaleTimeString()}</td>` +
          `<td>${message.direction === "in" ? "↙ RX" : "↗ TX"}</td>` +
          `<td><code>${safe(message.peer_mesh_id || "broadcast")}</code></td>` +
          `<td>${message.channel}</td>` +
          `<td>${safe(message.text || `${message.byte_len} bytes`)}</td>` +
          `<td>${safe(message.outcome || "—")}</td>` +
          `<td>${message.rx_snr == null ? "—" : `${safe(message.rx_snr)} dB`}</td></tr>`,
      )
      .join("") || '<tr><td colspan="7">No matching messages.</td></tr>';
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
$("filter-direction").addEventListener("change", refresh);
$("filter-channel").addEventListener("change", refresh);
initialize();
setInterval(() => {
  if (csrfToken) refresh();
}, 10000);
