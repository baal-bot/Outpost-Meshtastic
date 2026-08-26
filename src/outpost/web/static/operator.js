import("/nav.js");
import("/member-map.js?v=4");

const $ = id => document.getElementById(id);
const safe = value => String(value ?? "").replace(
  /[&<>'"]/g,
  char => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;"})[char],
);
const relative = stamp => {
  const seconds = Math.max(0, (Date.now() - new Date(stamp)) / 1000);
  if (seconds < 60) return "now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
};
const trustLevels = ["blocked", "guest", "member", "trusted", "responder", "operator"];

let csrfToken = "";
let auditItems = [];
let auditCursor = null;

function installSafetyFloorPanel() {
  $("audit").insertAdjacentHTML(
    "beforebegin",
    '<section class="panel content-panel operator-panel">' +
      '<div class="heading"><div><p class="eyebrow">SAFETY FLOOR</p>' +
      '<h2>Repeat coalescing</h2></div>' +
      '<p id="safety-floor-summary" class="safety-floor-summary">Loading…</p></div>' +
      '<div id="safety-floor-list" class="audit-list safety-floor-list">' +
      '<p class="empty">Loading…</p></div></section>',
  );
}

function auditQuery(cursor = 0) {
  const query = new URLSearchParams({cursor: String(cursor), limit: "50"});
  const hours = $("audit-time").value;
  if (hours) query.set("from_time", new Date(Date.now() - Number(hours) * 3600000).toISOString());
  for (const [parameter, id] of [
    ["actor", "audit-actor"],
    ["action", "audit-action"],
    ["target", "audit-target"],
    ["outcome", "audit-outcome"],
  ]) {
    const value = $(id).value.trim();
    if (value) query.set(parameter, value);
  }
  return query;
}

function auditDetail(event, index) {
  if (!event.detail) return "";
  const format = event.detail_format === "json" ? "Structured JSON" : "Recorded detail";
  return `<details class="audit-detail"><summary>${format}</summary>` +
    `<pre>${safe(event.detail)}</pre><div class="audit-detail-actions">` +
    `<button type="button" class="small-button" data-copy-audit="${index}">Copy details</button>` +
    '<span role="status"></span></div></details>';
}

function renderAudit(total) {
  $("audit-list").innerHTML = auditItems.map((event, index) => {
    const actor = `${event.actor_kind}:${event.actor_ref}`;
    const exactTime = new Date(event.created_at).toLocaleString();
    return `<article class="audit-event">` +
      `<div class="audit-action"><code>${safe(event.action)}</code>` +
      `<span class="audit-outcome ${safe(event.outcome)}">${safe(event.outcome)}</span></div>` +
      `<div class="audit-value audit-actor"><small>Actor</small><span>${safe(actor)}</span></div>` +
      `<div class="audit-value audit-target"><small>Target</small>` +
      `<span>${safe(event.target || "system")}</span></div>` +
      `<time datetime="${safe(event.created_at)}" title="${safe(exactTime)}">` +
      `${safe(relative(event.created_at))}</time>${auditDetail(event, index)}</article>`;
  }).join("") || '<p class="empty">No audit events match these filters.</p>';
  $("audit-count").textContent = total;
  $("audit-summary").textContent = `Showing ${auditItems.length} of ${total} matching events`;
  $("audit-more").hidden = auditCursor === null;
}

async function copyAuditDetail(index, button) {
  const text = auditItems[index]?.detail;
  if (!text) return;
  let copied = false;
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      copied = true;
    } else {
      const field = document.createElement("textarea");
      field.value = text;
      field.setAttribute("readonly", "");
      field.style.position = "fixed";
      field.style.opacity = "0";
      document.body.append(field);
      field.select();
      copied = document.execCommand("copy");
      field.remove();
    }
  } catch (_) {
    copied = false;
  }
  const status = button.nextElementSibling;
  status.textContent = copied ? "Copied" : "Copy failed";
  if (copied) button.textContent = "Copied";
}

async function loadAudit(append = false) {
  const cursor = append ? auditCursor : 0;
  if (cursor === null) return;
  $("audit-summary").textContent = append ? "Loading more…" : "Filtering…";
  const response = await fetch(`/api/v1/audit?${auditQuery(cursor)}`);
  if (!response.ok) {
    $("audit-summary").textContent = "Audit events could not be loaded.";
    return;
  }
  const result = await response.json();
  auditItems = append ? [...auditItems, ...result.items] : result.items;
  auditCursor = result.next_cursor;
  renderAudit(result.total);
}

function renderMembers(members, view) {
  $("member-count").textContent = members.approved_count;
  $("discovered-count").textContent = members.discovered_count;
  $("trusted-count").textContent = members.trusted_count;
  $("member-view-title").textContent = view === "approved"
    ? "Community members"
    : view === "discovered" ? "Discovered radios" : "All identities";
  $("discovered-note").hidden = view !== "discovered";
  $("member-rows").innerHTML = members.items.map(member =>
    `<tr><td><strong>${safe(member.handle ? `@${member.handle}` : "Unnamed")}</strong>` +
    `<small>${safe(member.notes || "No operator notes")}</small></td>` +
    `<td><code>${safe(member.mesh_id)}</code></td><td>${safe(relative(member.last_seen))}</td>` +
    `<td>${safe(member.last_heard_snr ?? "—")} dB</td>` +
    `<td><select data-member="${safe(member.id)}" ` +
    `aria-label="Trust for ${safe(member.handle || member.mesh_id)}">` +
    trustLevels.map(level =>
      `<option ${level === member.trust ? "selected" : ""}>${safe(level)}</option>`,
    ).join("") + "</select></td></tr>",
  ).join("") || '<tr><td colspan="5">No members yet.</td></tr>';
  document.querySelectorAll("select[data-member]").forEach(select => {
    select.addEventListener("change", async () => {
      select.disabled = true;
      const response = await fetch(`/api/v1/members/${select.dataset.member}`, {
        method: "PATCH",
        headers: {"content-type": "application/json", "x-csrf-token": csrfToken},
        body: JSON.stringify({trust: select.value}),
      });
      select.disabled = false;
      if (!response.ok) {
        await window.OutpostUI.alert({
          title: "Trust not updated",
          message: "The member trust change could not be saved.",
        });
      }
      await load();
    });
  });
}

function renderSafety(safety) {
  $("safety-floor-summary").textContent =
    `${safety.summary.attempts} retained attempts · ${safety.summary.coalesced} coalesced`;
  $("safety-floor-list").innerHTML = safety.items.map(event =>
    `<div class="audit-event safety-floor-event"><code>${safe(event.command)}</code>` +
    `<p>${safe(event.member_mesh_id)} · ${safe(event.coalesced_count)} repeats coalesced ` +
    `from ${safe(event.attempt_count)} attempts</p>` +
    `<time>${safe(relative(event.last_seen_at))}</time></div>`,
  ).join("") || '<p class="empty">No repeated safety commands in the retained activity window.</p>';
}

async function load() {
  const sessionResponse = await fetch("/api/v1/auth/session");
  if (!sessionResponse.ok) {
    location.href = "/";
    return;
  }
  csrfToken = (await sessionResponse.json()).csrf_token;
  const view = $("member-view").value;
  const [members, safety] = await Promise.all([
    fetch(`/api/v1/members?view=${view}`).then(response => response.json()),
    fetch("/api/v1/security/safety-floor").then(response => response.json()),
  ]);
  renderMembers(members, view);
  renderSafety(safety);
  await loadAudit(false);
}

$("member-view").addEventListener("change", load);
$("audit-filters").addEventListener("submit", event => {
  event.preventDefault();
  loadAudit(false);
});
$("clear-audit-filters").addEventListener("click", () => {
  $("audit-filters").reset();
  $("audit-time").value = "24";
  loadAudit(false);
});
$("audit-more").addEventListener("click", () => loadAudit(true));
$("audit-list").addEventListener("click", event => {
  const button = event.target.closest("[data-copy-audit]");
  if (button) copyAuditDetail(Number(button.dataset.copyAudit), button);
});
window.addEventListener("outpost:member-position-changed", load);

installSafetyFloorPanel();
load();
