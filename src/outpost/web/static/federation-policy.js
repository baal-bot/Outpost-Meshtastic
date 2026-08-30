import {escapeHtml as safe} from "/ui-primitives.js";

const PRESETS = {
  isolated: {
    label: "Discovery only",
    description: "Keep trust available without exchanging content or serving peer requests.",
  },
  bbs: {
    label: "BBS only",
    description: "Share only boards already approved for federation.",
  },
  mutual: {
    label: "Mutual aid",
    description: "Boards, bounded incidents, alerts, mail, weather, and public alerts.",
  },
  full: {
    label: "Full trusted partner",
    description: "All selected content and every peer information service.",
  },
};

const policyLabels = {
  boards: "Board streams",
  incidents: "Incident exchange",
  alerts: "Public alert relay",
  mail: "Encrypted mail relay",
  services: "Peer services",
  itemQuota: "Content item quota",
  mailQuota: "Mail quota (each direction)",
  recipientMailQuota: "Inbound quota per recipient",
  serviceLimits: "Service limits",
  review: "Policy review date",
};

function epochDate(epoch) {
  return epoch ? new Date(epoch * 1000).toISOString().slice(0, 10) : "";
}

function printable(value) {
  if (Array.isArray(value)) return value.length ? value.join(", ") : "None";
  if (value === true) return "Enabled";
  if (value === false) return "Disabled";
  return String(value ?? "None");
}

function comparablePolicy(peer) {
  const boundary = peer.incident_lat == null || peer.incident_lon == null
    ? "Enabled · unlocated records only"
    : `Enabled · ${peer.incident_lat}, ${peer.incident_lon} · ${peer.incident_radius_km} km`;
  return {
    boards: peer.boards || [],
    incidents: peer.sync_incidents ? boundary : false,
    alerts: Boolean(peer.relay_alerts),
    mail: Boolean(peer.relay_mail),
    services: peer.service_permissions || [],
    itemQuota: `${peer.quota_items_per_hour || 200} / hour`,
    mailQuota: `${peer.quota_mail_per_hour || 20} / hour in each direction`,
    recipientMailQuota: `${peer.quota_mail_per_recipient_per_hour || 5} / hour`,
    serviceLimits: `${peer.quota_services_per_hour || 6} requests · ` +
      `${peer.service_concurrency || 1} concurrent · ` +
      `${peer.service_max_response_bytes || 1200} bytes · ` +
      `${peer.service_airtime_seconds_per_hour || 15} sec airtime / hour`,
    review: epochDate(peer.policy_review_at) || "Not scheduled",
  };
}

function candidatePolicy(dialog, peer, boards) {
  const bbs = dialog.querySelector("#wizard-bbs").checked;
  const selectedBoards = bbs
    ? [...dialog.querySelectorAll("#wizard-board-list input:checked")].map(input => input.value)
    : [];
  const selected = new Set(selectedBoards);
  const lat = dialog.querySelector("#wizard-lat").value;
  const lon = dialog.querySelector("#wizard-lon").value;
  const incidents = dialog.querySelector("#wizard-incidents").checked;
  const review = dialog.querySelector("#wizard-review-date").value;
  const policy = {
    boards: selectedBoards.sort(),
    sync_incidents: incidents,
    incident_lat: lat === "" ? null : Number(lat),
    incident_lon: lon === "" ? null : Number(lon),
    incident_radius_km: Number(dialog.querySelector("#wizard-radius").value) || 25,
    relay_alerts: dialog.querySelector("#wizard-alerts").checked,
    relay_mail: dialog.querySelector("#wizard-mail").checked,
    quota_items_per_hour: peer.quota_items_per_hour || 200,
    quota_mail_per_hour: Number(dialog.querySelector("#wizard-mail-quota").value) || 20,
    quota_mail_per_recipient_per_hour:
      Number(dialog.querySelector("#wizard-mail-recipient-quota").value) || 5,
    service_permissions: [...dialog.querySelectorAll(".wizard-service-choices input:checked")]
      .map(input => input.value).sort(),
    quota_services_per_hour: Number(dialog.querySelector("#wizard-service-quota").value) || 6,
    service_concurrency: Number(dialog.querySelector("#wizard-service-concurrency").value) || 1,
    service_max_response_bytes: Number(dialog.querySelector("#wizard-service-bytes").value) || 1200,
    service_airtime_seconds_per_hour:
      Number(dialog.querySelector("#wizard-service-airtime").value) || 15,
    policy_review_at: review ? `${review}T23:59:59Z` : null,
    enable_boards: boards.filter(board => selected.has(board.slug) && !board.federated)
      .map(board => board.slug),
  };
  const boundary = policy.incident_lat == null || policy.incident_lon == null
    ? "Enabled · unlocated records only"
    : `Enabled · ${policy.incident_lat}, ${policy.incident_lon} · ` +
      `${policy.incident_radius_km} km`;
  return {
    policy,
    comparable: {
      boards: policy.boards,
      incidents: incidents ? boundary : false,
      alerts: policy.relay_alerts,
      mail: policy.relay_mail,
      services: policy.service_permissions,
      itemQuota: `${policy.quota_items_per_hour} / hour`,
      mailQuota: `${policy.quota_mail_per_hour} / hour in each direction`,
      recipientMailQuota: `${policy.quota_mail_per_recipient_per_hour} / hour`,
      serviceLimits: `${policy.quota_services_per_hour} requests · ` +
        `${policy.service_concurrency} concurrent · ` +
        `${policy.service_max_response_bytes} bytes · ` +
        `${policy.service_airtime_seconds_per_hour} sec airtime / hour`,
      review: review || "Not scheduled",
    },
  };
}

function applyPreset(dialog, name, boards) {
  const globallyApproved = boards.filter(board => board.federated).map(board => board.slug);
  const selected = name === "full"
    ? boards.map(board => board.slug)
    : name === "isolated" ? [] : globallyApproved;
  dialog.querySelector("#wizard-bbs").checked = selected.length > 0;
  dialog.querySelectorAll("#wizard-board-list input").forEach(input => {
    input.checked = selected.includes(input.value);
  });
  dialog.querySelector("#wizard-incidents").checked = ["mutual", "full"].includes(name);
  dialog.querySelector("#wizard-alerts").checked = ["mutual", "full"].includes(name);
  dialog.querySelector("#wizard-mail").checked = ["mutual", "full"].includes(name);
  const services = name === "full" ? ["weather", "alerts", "knowledge"]
    : name === "mutual" ? ["weather", "alerts"] : [];
  dialog.querySelectorAll(".wizard-service-choices input").forEach(input => {
    input.checked = services.includes(input.value);
  });
  dialog.querySelectorAll("[data-preset]").forEach(button => {
    button.classList.toggle("selected", button.dataset.preset === name);
  });
  updateOptionState(dialog);
}

function updateOptionState(dialog) {
  const boardsEnabled = dialog.querySelector("#wizard-bbs").checked;
  dialog.querySelector("#wizard-board-list").classList.toggle("disabled", !boardsEnabled);
  dialog.querySelectorAll("#wizard-board-list input").forEach(input => {
    input.disabled = !boardsEnabled;
  });
  const incidents = dialog.querySelector("#wizard-incidents").checked;
  dialog.querySelector(".wizard-boundary").classList.toggle("disabled", !incidents);
  dialog.querySelectorAll(".wizard-boundary input").forEach(input => {
    input.disabled = !incidents;
  });
  const mail = dialog.querySelector("#wizard-mail").checked;
  dialog.querySelector(".wizard-mail-limits").classList.toggle("disabled", !mail);
  dialog.querySelectorAll(".wizard-mail-limits input").forEach(input => {
    input.disabled = !mail;
  });
}

function renderReview(dialog, peer, boards) {
  const before = comparablePolicy(peer);
  const candidate = candidatePolicy(dialog, peer, boards);
  const rows = Object.keys(policyLabels).map(key => {
    const oldValue = printable(before[key]);
    const newValue = printable(candidate.comparable[key]);
    const changed = oldValue !== newValue;
    return `<div class="wizard-diff-row ${changed ? "changed" : "unchanged"}">` +
      `<b>${safe(policyLabels[key])}</b><span>${safe(oldValue)}</span>` +
      `<i aria-hidden="true">→</i><strong>${safe(newValue)}</strong>` +
      `<em>${changed ? "Changed" : "Unchanged"}</em></div>`;
  }).join("");
  const enabled = candidate.policy.enable_boards;
  const summary = [
    candidate.policy.boards.length ? `${candidate.policy.boards.length} board stream(s)` : null,
    candidate.policy.sync_incidents ? "bounded incident exchange" : null,
    candidate.policy.relay_alerts ? "public alerts" : null,
    candidate.policy.relay_mail ? "encrypted mail" : null,
    candidate.policy.service_permissions.length
      ? `peer services: ${candidate.policy.service_permissions.join(", ")}` : null,
  ].filter(Boolean);
  dialog.querySelector("#wizard-sharing-summary").innerHTML = summary.length
    ? summary.map(value => `<span>${safe(value)}</span>`).join("")
    : "<span>No content or services will be exchanged. Discovery and trust remain active.</span>";
  dialog.querySelector("#wizard-policy-diff").innerHTML = rows;
  const confirmation = dialog.querySelector("#wizard-board-confirmation");
  confirmation.hidden = enabled.length === 0;
  confirmation.querySelector("strong").textContent = enabled.join(", ");
  confirmation.querySelector("input").checked = false;
  dialog.querySelector("#wizard-edit").hidden = true;
  dialog.querySelector("#wizard-review").hidden = false;
  dialog.querySelector("#wizard-edit-footer").hidden = true;
  dialog.querySelector("#wizard-review-footer").hidden = false;
  dialog.querySelector(".wizard-step").textContent = "REVIEW & APPLY";
  return candidate.policy;
}

export async function openPolicyWizard({peer, api, refresh}) {
  const returnFocus = document.activeElement;
  const boardResponse = await api("/api/v1/boards?limit=200");
  const boards = boardResponse.ok ? (await boardResponse.json()).items : [];
  const existing = new Set(peer.boards || []);
  let reviewedPolicy = null;
  const dialog = document.createElement("dialog");
  dialog.className = "sharing-wizard";
  dialog.innerHTML = `<form class="sharing-wizard-shell"><header><div>` +
    `<p class="eyebrow wizard-step">${peer.policy_configured ? "PEER POLICY" : "PAIRING COMPLETE"}</p>` +
    `<h2>Share with ${safe(peer.node_name || peer.mesh_id)}</h2>` +
    `<p>Use a preset or tune one peer. Nothing new is shared until the review is approved.</p>` +
    `</div><button type="button" class="wizard-close" aria-label="Close">×</button></header>` +
    `<div id="wizard-edit" class="sharing-options"><section class="wizard-presets">` +
    `<div><b>Start with a policy preset</b><small>Presets fill the form; every value remains editable.</small></div>` +
    `<div>${Object.entries(PRESETS).map(([name, preset]) =>
      `<button type="button" data-preset="${name}" title="${safe(preset.description)}">` +
      `${safe(preset.label)}</button>`).join("")}</div></section>` +
    `<label class="sharing-option"><input id="wizard-bbs" type="checkbox" ` +
    `${existing.size ? "checked" : ""}><span><b>Community boards</b>` +
    `<small>Synchronize only the boards selected below and their replies.</small></span></label>` +
    `<div id="wizard-board-list" class="wizard-board-list">${boards.length
      ? boards.map(board => `<label class="${board.federated ? "approved" : "local"}">` +
        `<input type="checkbox" value="${safe(board.slug)}" ` +
        `${existing.has(board.slug) ? "checked" : ""}><span>${safe(board.title)} ` +
        `<code>${safe(board.slug)}</code><em>${board.federated
          ? "Federation approved" : "Local only · confirmation required"}</em></span></label>`).join("")
      : "<p>No community boards are available.</p>"}</div>` +
    `<label class="sharing-option"><input id="wizard-incidents" type="checkbox" ` +
    `${peer.sync_incidents ? "checked" : ""}><span><b>Incidents</b>` +
    `<small>Exchange records inside this peer boundary; imports still require review.</small></span></label>` +
    `<div class="wizard-boundary"><label><span>Peer latitude</span>` +
    `<input id="wizard-lat" type="number" min="-90" max="90" step="any" ` +
    `value="${safe(peer.incident_lat ?? "")}" placeholder="Optional"></label>` +
    `<label><span>Peer longitude</span><input id="wizard-lon" type="number" min="-180" ` +
    `max="180" step="any" value="${safe(peer.incident_lon ?? "")}" placeholder="Optional"></label>` +
    `<label><span>Radius</span><div><input id="wizard-radius" type="number" min="1" ` +
    `max="500" value="${safe(peer.incident_radius_km || 25)}"><em>km</em></div></label></div>` +
    `<label class="sharing-option"><input id="wizard-alerts" type="checkbox" ` +
    `${peer.relay_alerts ? "checked" : ""}><span><b>Public alerts</b>` +
    `<small>Relay eligible alerts; imported alerts are never auto-broadcast.</small></span></label>` +
    `<label class="sharing-option"><input id="wizard-mail" type="checkbox" ` +
    `${peer.relay_mail ? "checked" : ""}><span><b>Encrypted mail</b>` +
    `<small>Allow operator and member mail through this peer. The peer limit is enforced ` +
    `independently for inbound and outbound messages.</small></span></label>` +
    `<div class="wizard-mail-limits"><label><span>Messages / hour / direction</span>` +
    `<input id="wizard-mail-quota" type="number" min="1" max="100" ` +
    `value="${safe(peer.quota_mail_per_hour || 20)}"></label>` +
    `<label><span>Inbound / recipient / hour</span>` +
    `<input id="wizard-mail-recipient-quota" type="number" min="1" max="100" ` +
    `value="${safe(peer.quota_mail_per_recipient_per_hour || 5)}"></label></div>` +
    `<div class="wizard-service-policy"><div><b>Peer information services</b>` +
    `<small>Allow this peer to use specific internet-backed services. Denied by default.</small></div>` +
    `<div class="wizard-service-choices">${["weather", "alerts", "knowledge"].map(service =>
      `<label><input type="checkbox" value="${service}" ` +
      `${peer.service_permissions?.includes(service) ? "checked" : ""}>` +
      `${service === "alerts" ? "Public alerts" : service[0].toUpperCase() + service.slice(1)}</label>`
    ).join("")}</div><div class="wizard-service-limits">` +
    `<label><span>Requests / hour</span><input id="wizard-service-quota" type="number" ` +
    `min="1" max="60" value="${safe(peer.quota_services_per_hour || 6)}"></label>` +
    `<label><span>Concurrent</span><input id="wizard-service-concurrency" type="number" ` +
    `min="1" max="4" value="${safe(peer.service_concurrency || 1)}"></label>` +
    `<label><span>Max response</span><div><input id="wizard-service-bytes" type="number" ` +
    `min="256" max="1600" value="${safe(peer.service_max_response_bytes || 1200)}">` +
    `<em>bytes</em></div></label><label><span>Airtime / hour</span><div>` +
    `<input id="wizard-service-airtime" type="number" min="1" max="120" ` +
    `value="${safe(peer.service_airtime_seconds_per_hour || 15)}"><em>sec</em></div></label>` +
    `</div></div><label class="wizard-review-date"><span>Review this policy on</span>` +
    `<input id="wizard-review-date" type="date" value="${epochDate(peer.policy_review_at)}">` +
    `<small>Optional reminder metadata; it does not interrupt a field link automatically.</small></label>` +
    `</div><div id="wizard-review" class="wizard-review" hidden>` +
    `<section><p class="eyebrow">DATA-SHARING SUMMARY</p>` +
    `<div id="wizard-sharing-summary" class="wizard-sharing-summary"></div>` +
    `<p>Never included: member positions, waypoints, credentials, private mail bodies outside ` +
    `the selected relay, or boards not listed here.</p></section>` +
    `<section><p class="eyebrow">EXACT POLICY DIFF</p>` +
    `<div id="wizard-policy-diff" class="wizard-policy-diff"></div></section>` +
    `<label id="wizard-board-confirmation" class="wizard-global-confirm" hidden>` +
    `<input type="checkbox"><span><b>Enable local boards for federation</b>` +
    `<small><strong></strong> will become globally federation-eligible, then be assigned only ` +
    `to this peer by this change.</small></span></label>` +
    `<p id="wizard-result" class="wizard-result">Review complete. Apply only if this matches ` +
    `the relationship verified with the remote operator.</p></div>` +
    `<footer id="wizard-edit-footer"><button type="button" class="secondary wizard-cancel">` +
    `Cancel</button><button type="submit">Review sharing</button></footer>` +
    `<footer id="wizard-review-footer" hidden><button type="button" class="secondary ` +
    `wizard-back">Back to edit</button><button type="button" id="wizard-save">Apply policy</button>` +
    `</footer></form>`;
  document.body.append(dialog);

  const close = () => dialog.close();
  dialog.querySelector(".wizard-close").addEventListener("click", close);
  dialog.querySelector(".wizard-cancel").addEventListener("click", close);
  dialog.querySelectorAll("[data-preset]").forEach(button => {
    button.addEventListener("click", () => applyPreset(dialog, button.dataset.preset, boards));
  });
  dialog.querySelector("#wizard-bbs").addEventListener("change", () => updateOptionState(dialog));
  dialog.querySelector("#wizard-incidents").addEventListener(
    "change", () => updateOptionState(dialog),
  );
  dialog.querySelector("#wizard-mail").addEventListener(
    "change", () => updateOptionState(dialog),
  );
  dialog.querySelector("form").addEventListener("submit", event => {
    event.preventDefault();
    if (!event.currentTarget.reportValidity()) return;
    reviewedPolicy = renderReview(dialog, peer, boards);
  });
  dialog.querySelector(".wizard-back").addEventListener("click", () => {
    dialog.querySelector("#wizard-edit").hidden = false;
    dialog.querySelector("#wizard-review").hidden = true;
    dialog.querySelector("#wizard-edit-footer").hidden = false;
    dialog.querySelector("#wizard-review-footer").hidden = true;
    dialog.querySelector(".wizard-step").textContent = peer.policy_configured
      ? "PEER POLICY" : "PAIRING COMPLETE";
    reviewedPolicy = null;
  });
  dialog.querySelector("#wizard-save").addEventListener("click", async () => {
    if (!reviewedPolicy) return;
    const confirmation = dialog.querySelector("#wizard-board-confirmation");
    if (!confirmation.hidden && !confirmation.querySelector("input").checked) {
      dialog.querySelector("#wizard-result").textContent =
        "Confirm the global board eligibility change before applying this policy.";
      confirmation.querySelector("input").focus();
      return;
    }
    const button = dialog.querySelector("#wizard-save");
    button.disabled = true;
    button.textContent = "Applying…";
    const response = await api(
      `/api/v1/federation/peers/${encodeURIComponent(peer.mesh_id)}/sync-policy`,
      {
        method: "PUT",
        body: JSON.stringify({
          ...reviewedPolicy,
          confirm_enable_boards: reviewedPolicy.enable_boards.length > 0,
        }),
      },
    );
    if (!response.ok) {
      dialog.querySelector("#wizard-result").textContent = (await response.json()).error.message;
      button.disabled = false;
      button.textContent = "Apply policy";
      return;
    }
    dialog.close();
    await refresh();
  });
  updateOptionState(dialog);
  dialog.showModal();
  await new Promise(resolve => dialog.addEventListener("close", resolve, {once: true}));
  dialog.remove();
  window.requestAnimationFrame(() => returnFocus?.focus());
}
