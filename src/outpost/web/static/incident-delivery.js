import {escapeHtml as safe} from "/ui-primitives.js";

const labels = {
  pending: "Pending", queued: "Queued, not confirmed remotely", waiting: "Waiting",
  awaiting_receipt: "Radio completed; storage receipt missing",
  stored: "Exact remote storage observed", expired: "Expired without confirmation",
  cancelled: "Cancelled", blocked: "Blocked", retry_exhausted: "Automatic retries exhausted",
};
const reasons = {
  peer_offline: "Peer has not been heard recently. Trust is retained.",
  source_not_staged: "Waiting for the source or parent to be staged.",
  parent_storage_pending: "Waiting for the current parent incident's storage receipt.",
  source_changed: "A newer local version awaits staging; old receipts do not cover it.",
  peer_queue_full: "This peer's bounded delivery lane is occupied.",
  queue_full: "The shared radio queue is full.",
  quiet_hours: "Existing federation quiet hours are active; there is no emergency exemption.",
  governed_queue: "Waiting under the radio's airtime, link, priority and pacing rules.",
  payload_too_large: "Content exceeds the negotiated frame limit. Correct the source; nothing was truncated.",
  storage_receipt_missing: "No matching remote-storage receipt. Radio success is not delivery proof.",
  delivery_deadline: "The finite delivery window ended. Explicit retry starts a new window.",
  transport_expired: "Transport work expired. It will not be revived automatically.",
  transport_cancelled: "Transport work was cancelled; automatic retry is stopped.",
  operator_cancelled: "An operator cancelled this version. In-flight bytes cannot be recalled.",
  policy_or_content_changed: "Source, identity or sharing policy needs review before retry.",
  peer_policy_changed: "Current peer sharing policy does not authorize this work.",
  lineage_review_required: "Producer history needs operator review; do not reset counters blindly.",
  binding_changed: "Stored transport evidence belongs to an older identity/key binding. Review before retry.",
  dispatch_policy_denied: "A recovered or queued attempt failed its current authorization check.",
  transport_history_missing: "Retained transport evidence is missing. Review before explicit retry.",
  application_attempt_limit: "The application-attempt limit was reached.",
  not_exportable: "The current source is not exportable under this peer's policy.",
  invalid_payload: "The source payload is invalid. Correct it before retrying.",
};

export async function loadIncidentDelivery(api) {
  const anchor = document.querySelector(".path-grid")?.closest(".panel");
  if (!anchor) return async () => {};
  const panel = document.createElement("section");
  panel.className = "ui-card panel content-panel";
  panel.id = "incident-delivery";
  panel.innerHTML = `<div class="heading"><div><p class="eyebrow">INCIDENT DELIVERY</p>
    <h2>Automatic peer updates</h2></div><button type="button" data-refresh>Refresh</button></div>
    <p>Local changes are queued separately from bulk sync. A storage receipt means the peer
    stored that exact version, not that a person reviewed it or a responder acknowledged it.</p>
    <p data-policy></p><p role="status" aria-live="polite" data-result></p>
    <div data-items><p>Loading delivery status…</p></div>
    <div class="heading"><button type="button" data-first>First page</button>
    <button type="button" data-next disabled>Next page</button></div>`;
  anchor.before(panel);
  let cursor = {}, next = null, busy = false, items = [];
  const result = panel.querySelector("[data-result]");
  const refresh = async () => {
    if (busy) return;
    busy = true;
    try {
      const response = await api(`/api/v1/federation/incident-delivery?${new URLSearchParams(cursor)}`);
      if (!response.ok) throw new Error("Delivery status is unavailable; no delivery claim can be made.");
      const body = await response.json();
      items = body.items;
      next = body.next;
      panel.querySelector("[data-next]").disabled = !next;
      panel.querySelector("[data-policy]").textContent =
        `${body.policy_enabled ? "Automatic policy enabled" : "Automatic policy paused"} · ` +
        `${body.maximum_application_attempts} application attempts per window · ` +
        `${body.delivery_window_seconds / 60}-minute window` +
        (body.quiet_hours_active ? " · Federation quiet hours active" : "");
      panel.querySelector("[data-items]").innerHTML = items.map((item, index) => `
        <article class="transfer-card"><div class="transfer-head"><div>
        <strong>${safe(item.peer_name || item.peer_mesh_id)}</strong>
        <code>${safe(item.stream)} · ${safe(item.uid)} · revision ${safe(item.revision)}</code>
        </div><span>${safe(labels[item.state] || item.state)}</span></div>
        <p>${safe(reasons[item.reason] || item.reason || (item.remote_storage === "observed"
          ? "Exact storage evidence retained; later remote deletion or restore is not observable here."
          : "Pending local work; no remote storage confirmation."))}</p>
        <p>Local change recorded · ${safe(item.queued_frames)} frames queued ·
        ${safe(item.radio_completed_frames)} frames completed by radio ·
        Remote storage ${item.remote_storage === "observed" ? "observed" : "not confirmed"}</p>
        <p>Remote human review: not reported · Responder notification: not reported ·
        Responder acknowledgement: not reported</p>
        <small>${safe(item.application_attempts)} application attempts · ${safe(item.lane)} lane</small>
        <div>${item.can_retry
          ? `<button type="button" data-action="retry" data-index="${index}">Explicit retry</button>` : ""}
        ${item.can_cancel
          ? `<button type="button" data-action="cancel" data-index="${index}">Cancel this version</button>` : ""}</div>
        </article>`).join("") || "<p>No incident delivery intents on this page. This does not prove peer connectivity or outage readiness.</p>";
    } catch (error) {
      result.textContent = error.message;
    } finally {
      busy = false;
    }
  };
  panel.querySelector("[data-refresh]").addEventListener("click", refresh);
  panel.querySelector("[data-first]").addEventListener("click", () => { cursor = {}; refresh(); });
  panel.querySelector("[data-next]").addEventListener("click", () => { if (next) { cursor = next; refresh(); } });
  panel.addEventListener("click", async event => {
    const button = event.target.closest("button[data-action]");
    if (!button || busy) return;
    const item = items[Number(button.dataset.index)], action = button.dataset.action;
    if (!item || !window.confirm(action === "retry"
      ? "Start a new finite delivery window for this exact version? Current sharing and airtime rules still apply."
      : "Cancel this version's unsent work? In-flight radio bytes cannot be recalled.")) return;
    busy = true;
    try {
      const response = await api("/api/v1/federation/incident-delivery", {
        method: "POST", body: JSON.stringify({peer_id: item.peer_id, stream: item.stream,
          uid: item.uid, action, action_token: item.action_token}),
      });
      const body = await response.json();
      result.textContent = response.ok ? "Action recorded. No remote or human acknowledgement is implied."
        : body.error?.message || "Action failed. Refresh the current version before retrying.";
    } catch {
      result.textContent = "Action outcome uncertain. Refresh before repeating it.";
    } finally {
      busy = false;
      await refresh();
    }
  });
  await refresh();
  return refresh;
}
