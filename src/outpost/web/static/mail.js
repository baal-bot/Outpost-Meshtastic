import "/nav.js";
import {byId as $, createApiClient, escapeHtml as safe} from "/ui-primitives.js";
let csrf = "";
let selectedKey = null;
let conversations = [];
let peers = [];
let searchTimer = null;

const api = createApiClient(() => csrf);

function date(value) {
  return value ? new Date(value).toLocaleString() : "Not recorded";
}

function routeName(item) {
  return item.route_kind === "federated"
    ? item.peer_name || item.peer_mesh_id || "Remote Outpost"
    : "Local mesh";
}

function transportName(item) {
  const labels = (item.transports || []).map(value => value === "radio" ? "LoRa" : "MQTT");
  return labels.length ? labels.join(" + ") : item.route_kind === "federated"
    ? "best available federation path" : "local radio";
}

function filters() {
  return new URLSearchParams({
    q: $("mail-search").value.trim(),
    status: $("status-filter").value,
    route: $("route-filter").value,
    kind: $("kind-filter").value,
    archive: $("archive-filter").value,
  });
}

async function loadConversations() {
  const response = await api(`/api/v1/mail/conversations?${filters()}`);
  if (!response.ok) return;
  const body = await response.json();
  conversations = body.items;
  $("unread-count").textContent = body.counts.unread;
  $("action-count").textContent = body.counts.actionable;
  $("mail-count").textContent = body.total;
  $("filter-summary").textContent = `${body.total} shown`;
  $("mail-list").innerHTML = conversations.map(item => {
    const identity = item.message_kind === "system"
      ? "Outpost system"
      : `Member @${safe(item.participant_handle)}`;
    const badges = [
      item.unread_count ? `<span class="mail-badge unread">${item.unread_count} unread</span>` : "",
      item.failed_count ? `<span class="mail-badge failed">Delivery failed</span>` : "",
      `<span class="mail-badge route">${item.route_kind === "federated" ? "⤨ Federated" : "⌁ Local"}</span>`,
    ].join("");
    return `<button class="conversation-row ${selectedKey === item.conversation_key ? "active" : ""} ` +
      `${item.unread_count ? "has-unread" : ""}" data-conversation="${safe(item.conversation_key)}">` +
      `<div class="conversation-top"><strong>${safe(item.subject)}</strong><time>${safe(date(item.updated_at))}</time></div>` +
      `<p>${identity} · ${safe(routeName(item))}</p><div class="conversation-foot"><span>${item.message_count} ` +
      `message${item.message_count === 1 ? "" : "s"} · ${safe(item.latest_state)}</span><span>${badges}</span></div></button>`;
  }).join("") || `<div class="mail-zero"><span>✓</span><h3>No conversations match</h3>` +
    `<p>Try another filter or start a new federated message.</p></div>`;
  document.querySelectorAll("[data-conversation]").forEach(button => {
    button.addEventListener("click", () => openConversation(button.dataset.conversation));
  });
  if (selectedKey && !conversations.some(item => item.conversation_key === selectedKey)) {
    selectedKey = null;
    renderEmpty();
  }
}

function renderEmpty() {
  $("mail-detail").className = "ui-empty mail-empty-state";
  $("mail-detail").innerHTML = `<span aria-hidden="true">✉</span><h2>Select a conversation</h2>` +
    `<p>Message bodies are loaded only when an operator opens the conversation.</p>`;
}

function messageCard(message) {
  const outbound = message.mail_direction === "out";
  const heading = outbound ? `You → @${message.to_label}` : `@${message.from_label} → ${message.to_label === "operator" ? "Operations inbox" : `@${message.to_label}`}`;
  const actor = message.operator_actor
    ? `<span>Actor ${safe(message.operator_actor)}</span>` : "";
  return `<article class="mail-message ${outbound ? "outbound" : "inbound"}">` +
    `<header><div><strong>${safe(heading)}</strong><span>${outbound ? "OUTBOUND" : message.mail_direction === "in" ? "INBOUND" : "LOCAL"}</span></div>` +
    `<time>${safe(date(message.created_at))}</time></header>` +
    `<div class="mail-body">${safe(message.body)}</div><footer><span class="delivery-state ${safe(message.state)}">` +
    `${safe(message.state)}</span>${message.delivered_at ? `<span>Receipt ${safe(date(message.delivered_at))}</span>` : ""}` +
    `${actor}</footer></article>`;
}

async function openConversation(key) {
  selectedKey = key;
  $("mail-detail").className = "mail-loading";
  $("mail-detail").innerHTML = `<p class="ui-empty empty">Opening conversation and recording audit event…</p>`;
  await loadConversations();
  const response = await api(`/api/v1/mail/conversations/${encodeURIComponent(key)}`);
  if (!response.ok) {
    renderEmpty();
    return;
  }
  const body = await response.json();
  const item = body.conversation;
  const identity = item.message_kind === "system"
    ? `<span class="identity-pill system">OUTPOST SYSTEM TRAFFIC</span>`
    : `<span class="identity-pill member">MEMBER · @${safe(item.participant_handle)}</span>`;
  const reply = item.reply_available ? `<form id="conversation-reply" class="conversation-reply">` +
    `<label><span>Operator reply</span><textarea id="reply-body" maxlength="800" required ` +
    `placeholder="Reply in this conversation…"></textarea></label>` +
    `<div class="route-preview"><b>Reply route</b><span>@${safe(item.reply_address)} at ` +
    `${safe(routeName(item))}</span><small>${safe(transportName(item))} · encrypted federation mail · ` +
    `address preserved from this conversation</small></div><div class="reply-actions"><p id="reply-result" ` +
    `role="status"></p><button>Send reply</button></div></form>`
    : `<div class="route-preview unavailable"><b>Reply unavailable</b><span>This record has no safe ` +
      `federated return route. Use the radio workspace for local mesh mail.</span></div>`;
  $("mail-detail").className = "conversation-detail";
  $("mail-detail").innerHTML = `<div class="conversation-heading"><div><p class="eyebrow">` +
    `${safe(routeName(item))} · ${safe(item.peer_mesh_id || "LOCAL")}</p><h2>${safe(item.subject)}</h2>` +
    `<div class="identity-row">${identity}<span>${body.messages.length} messages</span><span>` +
    `${safe(transportName(item))}</span></div></div><div class="conversation-actions">` +
    `<button data-mail-state="unread" class="secondary-button">Mark unread</button>` +
    `<button data-mail-state="${item.archived_at ? "active" : "archive"}" class="secondary-button">` +
    `${item.archived_at ? "Restore" : "Archive"}</button></div></div>` +
    `<div class="conversation-route"><div><small>PARTICIPANT</small><strong>${item.message_kind === "system" ? "Remote Outpost operator" : `@${safe(item.participant_handle)}`}</strong></div>` +
    `<div><small>ROUTE</small><strong>${safe(routeName(item))}</strong></div><div><small>LAST ACTIVITY</small>` +
    `<strong>${safe(date(item.updated_at))}</strong></div><div><small>DELIVERY</small><strong>` +
    `${safe(item.latest_state)}</strong></div></div><div class="message-thread">` +
    `${body.messages.map(messageCard).join("")}</div>${reply}` +
    `<p class="audit-confirm">Conversation access was recorded in the audit trail.</p>`;
  document.querySelectorAll("[data-mail-state]").forEach(button => {
    button.addEventListener("click", () => setConversationState(button.dataset.mailState));
  });
  $("conversation-reply")?.addEventListener("submit", sendReply);
  await loadConversations();
  window.dispatchEvent(new Event("outpost:mail-updated"));
}

async function setConversationState(state) {
  const response = await api(`/api/v1/mail/conversations/${encodeURIComponent(selectedKey)}`, {
    method: "PATCH",
    body: JSON.stringify({state}),
  });
  if (!response.ok) return;
  if (state === "archive" || state === "active") {
    selectedKey = null;
    renderEmpty();
  }
  await loadConversations();
  window.dispatchEvent(new Event("outpost:mail-updated"));
}

async function sendReply(event) {
  event.preventDefault();
  const button = event.target.querySelector("button");
  button.disabled = true;
  const response = await api(
    `/api/v1/mail/conversations/${encodeURIComponent(selectedKey)}/reply`,
    {method: "POST", body: JSON.stringify({body: $("reply-body").value})},
  );
  const body = await response.json();
  if (!response.ok) {
    $("reply-result").textContent = body.error?.message || "Reply could not be sent.";
    button.disabled = false;
    return;
  }
  $("reply-result").textContent = "Encrypted reply queued.";
  await openConversation(selectedKey);
}

async function loadPeers() {
  const response = await api("/api/v1/federation/peers?state=active");
  if (!response.ok) return;
  peers = (await response.json()).items.filter(peer => peer.relay_mail);
  $("compose-peer").innerHTML = peers.length
    ? peers.map(peer => `<option value="${safe(peer.mesh_id)}">${safe(peer.node_name || peer.mesh_id)}</option>`).join("")
    : `<option value="">No mail-enabled paired Outposts</option>`;
  updateComposePreview();
}

function updateComposePreview() {
  const peer = peers.find(item => item.mesh_id === $("compose-peer").value);
  const member = $("compose-recipient-type").value === "member";
  $("compose-member-field").hidden = !member;
  $("compose-member").required = member;
  const recipient = member ? `@${$("compose-member").value.trim() || "member"}` : "@operator";
  $("compose-preview").innerHTML = peer
    ? `<b>Delivery preview</b><span>${safe(recipient)} at ${safe(peer.node_name || peer.mesh_id)}</span>` +
      `<small>${safe(transportName({route_kind: "federated", transports: peer.discovery_transports}))} ` +
      `· encrypted · ${member ? "named member mail" : "web-operator-only system traffic"}</small>`
    : "Pair an Outpost and enable mail in its sharing policy before composing.";
}

function openCompose() {
  $("compose-result").textContent = "";
  loadPeers();
  $("compose-dialog").showModal();
}

async function sendNewMessage(event) {
  event.preventDefault();
  const member = $("compose-recipient-type").value === "member";
  const recipient = member ? $("compose-member").value.trim().replace(/^@/, "") : "operator";
  const button = event.target.querySelector('button[type="submit"]');
  button.disabled = true;
  const response = await api("/api/v1/federation/mail", {
    method: "POST",
    body: JSON.stringify({
      peer_mesh_id: $("compose-peer").value,
      recipient_handle: recipient,
      subject: $("compose-subject").value,
      body: $("compose-body").value,
    }),
  });
  const body = await response.json();
  button.disabled = false;
  if (!response.ok) {
    $("compose-result").textContent = body.error?.message || "Message could not be queued.";
    return;
  }
  $("compose-dialog").close();
  event.target.reset();
  updateComposePreview();
  await loadConversations();
  window.dispatchEvent(new Event("outpost:mail-updated"));
}

async function initialize() {
  const session = await fetch("/api/v1/auth/session");
  if (!session.ok) {
    location.href = "/";
    return;
  }
  csrf = (await session.json()).csrf_token;
  await loadConversations();
}

$("refresh-mail").addEventListener("click", loadConversations);
$("compose-mail").addEventListener("click", openCompose);
$("compose-form").addEventListener("submit", sendNewMessage);
$("compose-dialog").querySelector(".dialog-close").addEventListener("click", () => $("compose-dialog").close());
$("compose-dialog").querySelector(".dialog-cancel").addEventListener("click", () => $("compose-dialog").close());
$("compose-peer").addEventListener("change", updateComposePreview);
$("compose-recipient-type").addEventListener("change", updateComposePreview);
$("compose-member").addEventListener("input", updateComposePreview);
for (const id of ["status-filter", "route-filter", "kind-filter", "archive-filter"]) {
  $(id).addEventListener("change", loadConversations);
}
$("mail-search").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(loadConversations, 250);
});
initialize();
