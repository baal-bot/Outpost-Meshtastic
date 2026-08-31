import "/nav.js";
import("/member-map.js?v=8");
import("/member-groups.js?v=1");
import {byId as $, escapeHtml as safe, relativeAge} from "/ui-primitives.js";
const relative = stamp => relativeAge(stamp, {suffix:" ago"});
const exactTime = stamp => stamp ? new Date(stamp).toLocaleString() : "Not recorded";
const trustLevels = ["blocked", "guest", "member", "trusted", "responder", "operator"];

let csrfToken = "";
let memberItems = [];
let memberCursor = null;
let memberSavedFilter = null;
let selectedMembers = new Set();
let selectedDetail = null;
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
      '<p class="ui-empty empty">Loading…</p></div></section>',
  );
}

function memberQuery(cursor = 0) {
  const query = new URLSearchParams({
    view: $("member-view").value,
    cursor: String(cursor),
    limit: "50",
  });
  if (memberSavedFilter) query.set("saved", memberSavedFilter);
  const search = $("member-query").value.trim();
  if (search) query.set("query", search);
  return query;
}

function categoryLabel(member) {
  return {
    approved: "Approved member",
    discovered: "Discovered",
    blocked: "Blocked radio",
    archived: "Archived",
    ignored: "Ignored",
  }[member.category] || member.category;
}

function positionLabel(member) {
  if (!member.active_position) return `Not shared · ${member.position_consent}`;
  return `Active · ${member.position_consent}`;
}

function canSuppress(member) {
  return member.directory_state === "active" && !member.handle &&
    ["guest", "blocked"].includes(member.trust);
}

function renderSavedFilters(filters) {
  $("saved-filters").innerHTML = filters.map(filter =>
    `<button type="button" class="filter-chip${memberSavedFilter === filter.key ? " active" : ""}" ` +
    `data-saved-filter="${safe(filter.key)}" title="${safe(filter.description)}">` +
    `${safe(filter.label)} <span>${safe(filter.count)}</span></button>`,
  ).join("");
}

function renderMemberRows() {
  $("member-rows").innerHTML = memberItems.map(member => {
    const identity = member.handle ? "@" + member.handle :
      (member.long_name || member.short_name || "Unnamed radio");
    const signal = member.last_heard_snr == null ? "Signal unknown" :
      `${member.last_heard_snr} dB · ${member.hops_away ?? "—"} hops`;
    return `<tr data-member-row="${safe(member.id)}">` +
      `<td class="check-cell"><input type="checkbox" data-select-member="${safe(member.id)}" ` +
      `${selectedMembers.has(member.id) ? "checked" : ""} aria-label="Select ${safe(identity)}"></td>` +
      `<td><strong>${safe(identity)}</strong><code>${safe(member.mesh_id)}</code>` +
      `<small>${safe(member.notes || member.hw_model || "No operator notes")}</small></td>` +
      `<td><span class="category-pill ${safe(member.category)}">${safe(categoryLabel(member))}</span>` +
      `<small class="category-reason">${safe(member.category_reason)}</small></td>` +
      `<td><span title="${safe(exactTime(member.last_seen))}">${safe(relative(member.last_seen))}</span>` +
      `<small>${safe(signal)}</small></td>` +
      `<td><span class="position-state ${member.active_position ? "active" : ""}">${safe(positionLabel(member))}</span>` +
      `<small>${member.position_expires_at ? `Until ${safe(exactTime(member.position_expires_at))}` : "No retained coordinate"}</small></td>` +
      `<td><span class="trust-pill ${safe(member.trust)}">${safe(member.trust)}</span>` +
      `<small>PKI ${safe(member.pki_state)}</small></td>` +
      `<td><button type="button" class="small-button secondary" data-review-member="${safe(member.id)}">${member.needs_review ? "Review" : "Details"}</button></td></tr>`;
  }).join("") || '<tr><td colspan="7" class="ui-empty empty">No identities match this view.</td></tr>';
  $("member-more").hidden = memberCursor === null;
  updateSelectionBar();
}

function renderMembers(result, append) {
  memberItems = append ? [...memberItems, ...result.items] : result.items;
  memberCursor = result.next_cursor;
  $("member-count").textContent = result.approved_count;
  $("discovered-count").textContent = result.discovered_count;
  $("review-count").textContent = result.review_count;
  $("inactive-count").textContent = result.archived_count + result.ignored_count;
  $("trusted-count").textContent = result.trusted_count;
  $("responder-warning").hidden = result.responder_count > 0;
  const labels = {
    approved: "Community members",
    discovered: "Discovered radio triage",
    archived: "Archived & ignored radios",
    all: "Complete identity directory",
  };
  const activeFilter = result.saved_filters.find(item => item.key === memberSavedFilter);
  $("member-view-title").textContent = activeFilter?.label || labels[$("member-view").value];
  $("discovered-note").hidden = $("member-view").value !== "discovered" &&
    !["new", "stale"].includes(memberSavedFilter);
  $("member-summary").textContent = `${result.total} matching ${result.total === 1 ? "identity" : "identities"}`;
  renderSavedFilters(result.saved_filters);
  renderMemberRows();
}

async function loadMembers(append = false) {
  const cursor = append ? memberCursor : 0;
  if (cursor === null) return;
  if (!append) {
    $("member-summary").textContent = "Loading directory…";
    selectedMembers.clear();
  }
  const response = await fetch(`/api/v1/members?${memberQuery(cursor)}`);
  if (!response.ok) {
    $("member-summary").textContent = "The member directory could not be loaded.";
    return;
  }
  renderMembers(await response.json(), append);
}

function updateSelectionBar() {
  $("selected-count").textContent = selectedMembers.size;
  $("bulk-bar").hidden = selectedMembers.size === 0;
  $("select-visible").checked = memberItems.length > 0 &&
    memberItems.every(member => selectedMembers.has(member.id));
  $("select-visible").indeterminate = memberItems.some(member => selectedMembers.has(member.id)) &&
    !$("select-visible").checked;
}

async function apiError(response, fallback) {
  try {
    const body = await response.json();
    return body.error?.message || fallback;
  } catch (_) {
    return fallback;
  }
}

async function runBulkAction(action) {
  const ids = [...selectedMembers];
  if (!ids.length) return;
  if (action === "export") {
    location.href = `/api/v1/members/export?ids=${ids.join(",")}`;
    return;
  }
  const explanations = {
    archive: "Archive hides eligible discovered radios from active review while preserving all evidence. They can be restored later.",
    ignore: "Ignore suppresses eligible discovered radios from active review even when heard again. Traffic remains logged and the identity can be restored later.",
  };
  const reason = await window.OutpostUI.prompt({
    title: `${action === "archive" ? "Archive" : "Ignore"} ${ids.length} selected radios?`,
    message: explanations[action],
    label: "Operator reason",
    confirmLabel: action === "archive" ? "Archive eligible radios" : "Ignore eligible radios",
    danger: action === "ignore",
  });
  if (!reason) return;
  const response = await fetch("/api/v1/members/bulk", {
    method: "POST",
    headers: {"content-type": "application/json", "x-csrf-token": csrfToken},
    body: JSON.stringify({member_ids: ids, action, reason}),
  });
  if (!response.ok) {
    await window.OutpostUI.alert({title: "Directory not changed", message: await apiError(response, "Bulk action failed.")});
    return;
  }
  const result = await response.json();
  await window.OutpostUI.alert({
    title: "Directory updated",
    message: `${result.changed} changed; ${result.skipped} safely skipped because they were not eligible.`,
  });
  selectedMembers.clear();
  await Promise.all([loadMembers(), loadAudit(false)]);
}

function detailMetric(label, value, hint = "") {
  return `<article><small>${safe(label)}</small><strong>${safe(value)}</strong>${hint ? `<span>${safe(hint)}</span>` : ""}</article>`;
}

function renderActivity(items) {
  if (!items.length) return '<p class="ui-empty empty">No retained activity for this identity.</p>';
  return items.map(item => `<li><div><span class="activity-direction ${safe(item.direction)}">${safe(item.direction)}</span>` +
    `<strong>${safe(item.command || `Port ${item.portnum ?? "—"}`)}</strong></div>` +
    `<span>${safe(item.outcome || item.drop_reason || "recorded")} · ${safe(item.transport || "radio")}</span>` +
    `<time title="${safe(exactTime(item.created_at))}">${safe(relative(item.created_at))}</time></li>`).join("");
}

function renderTrustHistory(items) {
  if (!items.length) return '<p class="ui-empty empty">No reviewed trust changes recorded.</p>';
  return items.map(item => `<li><div><strong>${safe(item.from_trust)} → ${safe(item.to_trust)}</strong>` +
    `<span>${safe(item.reason)}</span></div><small>${safe(item.changed_by)} · ${safe(relative(item.created_at))}</small></li>`).join("");
}

function renderPkiEvents(items) {
  if (!items.length) return '<p class="ui-empty empty">No PKI identity events recorded.</p>';
  return items.map(item => `<li><div><strong>${safe(item.event.replaceAll("_", " "))}</strong>` +
    `<span>${safe(item.fingerprint ? item.fingerprint.slice(0, 16) : "No key fingerprint")}</span></div>` +
    `<small>${safe(item.actor)} · ${safe(relative(item.created_at))}</small></li>`).join("");
}

function renderDetail(result) {
  selectedDetail = result;
  const member = result.member;
  const passiveDiscovery = ["discovered", "blocked"].includes(member.category);
  const label = member.handle ? "@" + member.handle :
    (member.long_name || member.short_name || "Unnamed radio");
  $("detail-eyebrow").textContent = member.needs_review ? "IDENTITY REVIEW" : "IDENTITY DETAILS";
  $("detail-title").textContent = label;
  $("detail-subtitle").textContent = `${member.mesh_id} · ${categoryLabel(member)}`;
  const position = member.position_state === "active"
    ? `${Number(member.position_lat).toFixed(5)}, ${Number(member.position_lon).toFixed(5)}`
    : member.position_state === "expired" ? "Expired and hidden" : "Not shared";
  const stateActions = member.directory_state === "active" && canSuppress(member)
    ? '<button type="button" class="small-button secondary" data-state-action="archive">Archive</button>' +
      '<button type="button" class="small-button danger" data-state-action="ignore">Ignore</button>'
    : member.directory_state !== "active"
      ? '<button type="button" class="small-button" data-state-action="restore">Restore to active triage</button>'
      : "";
  $("detail-body").innerHTML = `
    <section class="detail-callout ${safe(member.category)}"><span class="category-pill ${safe(member.category)}">${safe(categoryLabel(member))}</span><div><strong>Why it is here</strong><p>${safe(member.category_reason)}</p></div></section>
    <section class="detail-metrics">
      ${detailMetric("Last heard", relative(member.last_seen), exactTime(member.last_seen))}
      ${detailMetric("Signal", member.last_heard_snr == null ? "Unknown" : `${member.last_heard_snr} dB`, `${member.hops_away ?? "—"} hops`)}
      ${detailMetric("Messages", result.stats.messages, "retained activity")}
      ${detailMetric("Position", member.position_state.replace("_", " "), member.position_consent)}
    </section>
    <section class="detail-grid">
      <article class="detail-card">
        <div class="detail-card-heading"><div><p class="eyebrow">${member.needs_review ? "OPERATOR REVIEW" : "DIRECTORY RECORD"}</p><h3>Trust & notes</h3></div><span>${member.needs_review ? "Needs review" : member.reviewed_at ? `Updated ${safe(relative(member.reviewed_at))}` : passiveDiscovery ? "Discovery only" : "No pending review"}</span></div>
        <form id="member-review-form" class="review-form">
          <label><span>Trust level</span><select id="detail-trust">${trustLevels.map(level => `<option value="${level}" ${level === member.trust ? "selected" : ""}>${level}</option>`).join("")}</select></label>
          <p id="trust-impact" class="trust-impact">${safe(member.promotion_effects[member.trust])}</p>
          <label><span>Operator notes</span><textarea id="detail-notes" maxlength="2000" rows="4" placeholder="Context that will help the next operator">${safe(member.notes || "")}</textarea></label>
          <label><span>Reason for trust change</span><input id="detail-reason" maxlength="240" placeholder="Required when trust changes"></label>
          <p id="detail-result" class="form-result" aria-live="polite"></p>
          <div class="form-actions"><button type="submit">${member.needs_review ? "Save reviewed changes" : "Save directory changes"}</button>${stateActions}</div>
        </form>
      </article>
      <article class="detail-card">
        <p class="eyebrow">${passiveDiscovery ? "POSITION EVIDENCE" : "POSITION CONSENT"}</p><h3>${safe(position)}</h3>
        <dl class="detail-list"><div><dt>${passiveDiscovery ? "Directory use" : "Member visibility"}</dt><dd>${passiveDiscovery ? "Operator only" : safe(member.position_consent)}</dd></div><div><dt>State</dt><dd>${safe(member.position_state.replace("_", " "))}</dd></div><div><dt>Source</dt><dd>${safe(member.position_source || "—")}</dd></div><div><dt>Scheduled expiry</dt><dd>${safe(exactTime(member.position_expires_at))}</dd></div></dl>
        <p class="privacy-copy">Exact coordinates stay in this operator-only detail and are never included in directory CSV exports.</p>
      </article>
      <article class="detail-card">
        <p class="eyebrow">IDENTITY EVIDENCE</p><h3>Radio profile</h3>
        <dl class="detail-list"><div><dt>First heard</dt><dd>${safe(exactTime(member.first_seen))}</dd></div><div><dt>Long name</dt><dd>${safe(member.long_name || "—")}</dd></div><div><dt>Short name</dt><dd>${safe(member.short_name || "—")}</dd></div><div><dt>Hardware</dt><dd>${safe(member.hw_model || "Unknown")}</dd></div><div><dt>Directory state</dt><dd>${safe(member.directory_state)}</dd></div><div><dt>PKI state</dt><dd>${safe(member.pki_state)}</dd></div><div><dt>Reviewed key</dt><dd><code>${safe(member.pki_fingerprint || "None")}</code></dd></div><div><dt>Pending key</dt><dd><code>${safe(member.pki_pending_fingerprint || "None")}</code></dd></div><div><dt>Last authenticated</dt><dd>${safe(exactTime(member.pki_last_seen_at))}</dd></div></dl>
        ${member.pki_pending_fingerprint ? '<div class="form-actions"><button type="button" data-pki-action="approve">Approve authenticated key</button><button type="button" class="small-button danger" data-pki-action="reject">Reject pending key</button></div>' : ""}
      </article>
      <article class="detail-card">
        <p class="eyebrow">TRUST HISTORY</p><h3>Reviewed changes</h3>
        <ul class="trust-history">${renderTrustHistory(result.trust_history)}</ul>
      </article>
      <article class="detail-card">
        <p class="eyebrow">PKI HISTORY</p><h3>Authentication evidence</h3>
        <ul class="trust-history">${renderPkiEvents(result.pki_events)}</ul>
      </article>
    </section>
    <section class="detail-card activity-card"><div class="detail-card-heading"><div><p class="eyebrow">RECENT ACTIVITY</p><h3>Retained radio events</h3></div><span>${result.stats.incidents} incidents · ${result.stats.checkins} check-ins · ${result.stats.mail} mail</span></div><ul class="member-activity">${renderActivity(result.recent_activity)}</ul></section>`;
  $("detail-trust").addEventListener("change", event => {
    $("trust-impact").textContent = member.promotion_effects[event.target.value];
    $("detail-reason").required = event.target.value !== member.trust;
  });
  $("member-review-form").addEventListener("submit", saveMemberReview);
  document.querySelectorAll("[data-pki-action]").forEach(button =>
    button.addEventListener("click", () => reviewPki(button.dataset.pkiAction)),
  );
}

async function reviewPki(action) {
  const member = selectedDetail.member;
  const reason = await window.OutpostUI.prompt({
    title: `${action === "approve" ? "Approve" : "Reject"} this authenticated radio key?`,
    message: action === "approve"
      ? "The displayed fingerprint will become the identity required for elevated mesh commands."
      : "The pending fingerprint will be discarded. The reviewed key, if any, remains authoritative.",
    label: "Operator reason",
    confirmLabel: action === "approve" ? "Approve key" : "Reject key",
    danger: action === "reject",
  });
  if (!reason) return;
  const response = await fetch(`/api/v1/members/${member.id}/pki`, {
    method: "POST",
    headers: {"content-type": "application/json", "x-csrf-token": csrfToken},
    body: JSON.stringify({action, reason}),
  });
  if (!response.ok) {
    await window.OutpostUI.alert({
      title: "PKI review not changed",
      message: await apiError(response, "PKI review failed."),
    });
    return;
  }
  await Promise.all([loadMembers(), loadAudit(false)]);
  if (payload.trust) window.dispatchEvent(new Event("outpost:member-trust-changed"));
  await openMemberDetail(member.id);
}

async function openMemberDetail(memberId) {
  $("detail-body").innerHTML = '<p class="ui-empty empty">Loading identity evidence…</p>';
  if (!$("member-detail").open) $("member-detail").showModal();
  const response = await fetch(`/api/v1/members/${memberId}`);
  if (!response.ok) {
    $("detail-body").innerHTML = `<p class="ui-empty empty">${safe(await apiError(response, "Member details could not be loaded."))}</p>`;
    return;
  }
  renderDetail(await response.json());
}

async function saveMemberReview(event) {
  event.preventDefault();
  const member = selectedDetail.member;
  const trust = $("detail-trust").value;
  const notes = $("detail-notes").value.trim();
  const reason = $("detail-reason").value.trim();
  const payload = {};
  if (trust !== member.trust) payload.trust = trust;
  if (notes !== (member.notes || "")) payload.notes = notes || null;
  if (!Object.keys(payload).length) {
    $("detail-result").textContent = "No changes to save.";
    return;
  }
  if (payload.trust && reason.length < 3) {
    $("detail-result").textContent = "Record a reason before changing trust.";
    $("detail-reason").focus();
    return;
  }
  if (reason) payload.reason = reason;
  const response = await fetch(`/api/v1/members/${member.id}`, {
    method: "PATCH",
    headers: {"content-type": "application/json", "x-csrf-token": csrfToken},
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    $("detail-result").textContent = await apiError(response, "Directory changes could not be saved.");
    return;
  }
  await Promise.all([loadMembers(), loadAudit(false)]);
  await openMemberDetail(member.id);
  $("detail-result").textContent = "Directory changes saved.";
}

async function changeDirectoryState(action) {
  const member = selectedDetail.member;
  let reason = "Restored by operator";
  if (action !== "restore") {
    reason = await window.OutpostUI.prompt({
      title: `${action === "archive" ? "Archive" : "Ignore"} ${member.mesh_id}?`,
      message: action === "archive"
        ? "Archive removes this discovered radio from active triage but keeps all evidence."
        : "Ignore keeps future traffic logged without reopening this radio in active triage.",
      label: "Operator reason",
      confirmLabel: action === "archive" ? "Archive radio" : "Ignore radio",
      danger: action === "ignore",
    });
    if (!reason) return;
  }
  const response = await fetch(`/api/v1/members/${member.id}/state`, {
    method: "POST",
    headers: {"content-type": "application/json", "x-csrf-token": csrfToken},
    body: JSON.stringify({action, reason}),
  });
  if (!response.ok) {
    await window.OutpostUI.alert({title: "Directory not changed", message: await apiError(response, "State change failed.")});
    return;
  }
  $("member-detail").close();
  await Promise.all([loadMembers(), loadAudit(false)]);
}

function auditQuery(cursor = 0) {
  const query = new URLSearchParams({cursor: String(cursor), limit: "50"});
  const hours = $("audit-time").value;
  if (hours) query.set("from_time", new Date(Date.now() - Number(hours) * 3600000).toISOString());
  for (const [parameter, id] of [["actor", "audit-actor"], ["action", "audit-action"], ["target", "audit-target"], ["outcome", "audit-outcome"]]) {
    const value = $(id).value.trim();
    if (value) query.set(parameter, value);
  }
  return query;
}

function auditDetail(event, index) {
  if (!event.detail) return "";
  const format = event.detail_format === "json" ? "Structured JSON" : "Recorded detail";
  return `<details class="audit-detail"><summary>${format}</summary><pre>${safe(event.detail)}</pre>` +
    `<div class="audit-detail-actions"><button type="button" class="small-button" data-copy-audit="${index}">Copy details</button><span role="status"></span></div></details>`;
}

function renderAudit(total) {
  $("audit-list").innerHTML = auditItems.map((event, index) => {
    const actor = event.actor_kind + ":" + event.actor_ref;
    return `<article class="audit-event"><div class="audit-action"><code>${safe(event.action)}</code>` +
      `<span class="audit-outcome ${safe(event.outcome)}">${safe(event.outcome)}</span></div>` +
      `<div class="audit-value audit-actor"><small>Actor</small><span>${safe(actor)}</span></div>` +
      `<div class="audit-value audit-target"><small>Target</small><span>${safe(event.target || "system")}</span></div>` +
      `<time datetime="${safe(event.created_at)}" title="${safe(exactTime(event.created_at))}">${safe(relative(event.created_at))}</time>${auditDetail(event, index)}</article>`;
  }).join("") || '<p class="ui-empty empty">No audit events match these filters.</p>';
  $("audit-count").textContent = total;
  $("audit-summary").textContent = `Showing ${auditItems.length} of ${total} matching events`;
  $("audit-more").hidden = auditCursor === null;
}

async function copyAuditDetail(index, button) {
  const text = auditItems[index]?.detail;
  if (!text) return;
  let copied = false;
  try {
    await navigator.clipboard.writeText(text);
    copied = true;
  } catch (_) {
    copied = false;
  }
  button.nextElementSibling.textContent = copied ? "Copied" : "Copy failed";
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

function renderSafety(safety) {
  $("safety-floor-summary").textContent = `${safety.summary.attempts} retained attempts · ${safety.summary.coalesced} coalesced`;
  $("safety-floor-list").innerHTML = safety.items.map(event =>
    `<div class="audit-event safety-floor-event"><code>${safe(event.command)}</code>` +
    `<p>${safe(event.member_mesh_id)} · ${safe(event.coalesced_count)} repeats coalesced from ${safe(event.attempt_count)} attempts</p>` +
    `<time>${safe(relative(event.last_seen_at))}</time></div>`,
  ).join("") || '<p class="ui-empty empty">No repeated safety commands in the retained activity window.</p>';
}

async function load() {
  const sessionResponse = await fetch("/api/v1/auth/session");
  if (!sessionResponse.ok) {
    location.href = "/";
    return;
  }
  csrfToken = (await sessionResponse.json()).csrf_token;
  const [safety] = await Promise.all([
    fetch("/api/v1/security/safety-floor").then(response => response.json()),
    loadMembers(),
    loadAudit(false),
  ]);
  renderSafety(safety);
}

$("member-view").addEventListener("change", () => {
  memberSavedFilter = null;
  loadMembers();
});
$("saved-filters").addEventListener("click", event => {
  const button = event.target.closest("[data-saved-filter]");
  if (!button) return;
  memberSavedFilter = memberSavedFilter === button.dataset.savedFilter ? null : button.dataset.savedFilter;
  loadMembers();
});
$("member-search").addEventListener("submit", event => { event.preventDefault(); loadMembers(); });
$("member-search-clear").addEventListener("click", () => { $("member-query").value = ""; loadMembers(); });
$("member-more").addEventListener("click", () => loadMembers(true));
$("member-rows").addEventListener("click", event => {
  const review = event.target.closest("[data-review-member]");
  if (review) openMemberDetail(Number(review.dataset.reviewMember));
});
$("member-rows").addEventListener("change", event => {
  const checkbox = event.target.closest("[data-select-member]");
  if (!checkbox) return;
  const id = Number(checkbox.dataset.selectMember);
  checkbox.checked ? selectedMembers.add(id) : selectedMembers.delete(id);
  updateSelectionBar();
});
$("select-visible").addEventListener("change", event => {
  memberItems.forEach(member => event.target.checked
    ? selectedMembers.add(member.id) : selectedMembers.delete(member.id));
  renderMemberRows();
});
$("bulk-bar").addEventListener("click", event => {
  const button = event.target.closest("[data-bulk]");
  if (button) runBulkAction(button.dataset.bulk);
});
$("clear-selection").addEventListener("click", () => { selectedMembers.clear(); renderMemberRows(); });
$("detail-close").addEventListener("click", () => $("member-detail").close());
$("member-detail").addEventListener("click", event => {
  if (event.target === $("member-detail")) $("member-detail").close();
  const action = event.target.closest("[data-state-action]");
  if (action) changeDirectoryState(action.dataset.stateAction);
});
$("audit-filters").addEventListener("submit", event => { event.preventDefault(); loadAudit(false); });
$("clear-audit-filters").addEventListener("click", () => { $("audit-filters").reset(); $("audit-time").value = "24"; loadAudit(false); });
$("audit-more").addEventListener("click", () => loadAudit(true));
$("audit-list").addEventListener("click", event => {
  const button = event.target.closest("[data-copy-audit]");
  if (button) copyAuditDetail(Number(button.dataset.copyAudit), button);
});
window.addEventListener("outpost:member-position-changed", loadMembers);

installSafetyFloorPanel();
load();
