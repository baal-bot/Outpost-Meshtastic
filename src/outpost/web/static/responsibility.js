// Offers and explicit acceptance are independent of delivery acknowledgements.
const node = (tag, text = "") => {const element = document.createElement(tag); element.textContent = text; return element;};
const stamp = value => value == null ? "not verified" : new Date(value * 1000).toISOString();

export async function mountResponsibility(root, incidentId) {
  const status = node("div"), message = node("p"), refresh = node("button", "Refresh responsibility");
  message.setAttribute("role", "status"); refresh.type = "button";
  const form = node("form"), action = node("select"), kind = node("select"), target = node("select"), query = node("input"), next = node("input"), consent = node("input");
  action.id = "responsibility-action"; kind.id = "responsibility-kind"; target.id = "responsibility-target"; query.id = "responsibility-query"; next.id = "responsibility-next"; consent.id = "responsibility-consent";
  const label = (text, field) => {const value = node("label", text); value.append(field); return value;};
  for (const value of ["group", "member", "account"]) {const option = node("option", value === "group" ? "Responder team" : value === "member" ? "Responder person" : "Operator account"); option.value = value; kind.append(option);}
  query.maxLength = 50; next.maxLength = 160; consent.type = "checkbox"; consent.required = true;
  const find = node("button", "Find targets"), more = node("button", "Next target page"); find.type = more.type = "button";
  const offerFields = node("fieldset"); offerFields.append(node("legend", "Offer or handoff target"), label("Target type", kind), label("Target name filter", query), find, more, label("Choose target explicitly", target));
  const nextField = label("Next action (one line; maximum 160 UTF-8 bytes)", next), submit = node("button", "Record decision"); submit.type = "submit";
  form.append(label("Decision", action), offerFields, nextField, label("I reviewed the current owner and pending offer. Acceptance is explicit; release/completion does not resolve the incident.", consent), submit);
  const history = node("ol"), historyMore = node("button", "More history"); historyMore.type = "button";
  root.replaceChildren(node("h2", "Responsibility and handoff"), node("p", "This Outpost only. A partition cannot establish globally exclusive ownership. Retrieve offers here or with verified TASK direct messages; this form does not page responders."), status, refresh, message, form, node("h3", "Responsibility decision history"), history, historyMore);
  let view = null, csrf = "", blocked = true, busy = false, targetAfter = 0, historyAfter = 0, targetGeneration = 0;
  const endpoint = `/api/v1/incidents/${incidentId}/responsibility`;
  async function get(path) {const response = await fetch(path, {cache: "no-store"}); const value = await response.json(); if (!response.ok) throw new Error(value.error?.message || "Request unavailable."); return value;}
  function fields() {offerFields.hidden = action.value !== "offer"; nextField.hidden = !["offer", "update"].includes(action.value); target.required = !offerFields.hidden; next.required = !nextField.hidden; form.inert = busy; submit.disabled = blocked || busy || !action.value || (!offerFields.hidden && target.disabled); submit.textContent = `Record ${action.value || "decision"}`;}
  function fail(error) {blocked = true; fields(); message.textContent = error.message || "Responsibility unavailable. Refresh before making a decision.";}
  async function targets(reset = true) {
    const generation = ++targetGeneration; if (reset) targetAfter = 0;
    target.replaceChildren(node("option", "Loading eligible targets…")); target.disabled = true; fields();
    try {
      const result = await get(`/api/v1/incidents/responsibility/targets?${new URLSearchParams({kind: kind.value, query: query.value, after: String(targetAfter)})}`);
      if (generation !== targetGeneration) return;
      const empty = node("option", "Choose a target explicitly"); empty.value = ""; target.replaceChildren(empty);
      for (const value of result.items) {const option = node("option", `${value.label} (${value.kind}:${value.reference})`); option.value = String(value.reference); target.append(option);}
      targetAfter = result.items.at(-1)?.reference || targetAfter; more.disabled = result.items.length < 25; target.disabled = false; fields();
    } catch (error) {if (generation === targetGeneration) {target.replaceChildren(node("option", "Targets unavailable; retry Find targets")); more.disabled = true; message.textContent = error.message;}}
  }
  function render(current) {
    view = current;
    const name = value => value ? `${value.label}${value.available ? "" : " — UNAVAILABLE; operator review required"}` : "none";
    status.replaceChildren(node("p", `Accepted owner: ${name(view.owner)} · ${view.state}`), node("p", `Pending acceptance: ${name(view.offer)}`), node("p", `Accepted next action: ${view.next_action || "none"}`), node("p", `Offered next action: ${view.offer_action || "none"}`), node("p", `Last verified: ${stamp(view.verified_at)} · ${view.verification} at this refresh. Stale after 30 minutes; refresh before deciding.`), node("p", `Decision version: ${view.version}. An ACK does not mean accepted or complete.`));
    action.replaceChildren(); for (const value of view.allowed_actions || []) {const option = node("option", value); option.value = value; action.append(option);}
    consent.checked = false; next.value = ""; fields();
  }
  async function loadHistory(reset = true) {
    if (reset) {historyAfter = 0; history.replaceChildren();}
    const result = await get(`${endpoint}/history?after=${historyAfter}`);
    for (const entry of result.items) history.append(node("li", `Decision ${entry.version}: ${entry.action} by ${entry.actor} at ${stamp(entry.created_at)}. Target ${entry.target?.label || "none"}; owner afterward ${entry.owner?.label || "none"}. ${entry.next_action}`));
    historyAfter = result.items.at(-1)?.version || historyAfter; historyMore.disabled = result.items.length < 100;
  }
  async function reload() {
    blocked = true; refresh.disabled = true; fields();
    try {csrf = (await get("/api/v1/auth/session")).csrf_token; const current = await get(endpoint); blocked = false; render(current); await Promise.all([targets(), loadHistory()]);}
    catch (error) {fail(error);}
    finally {refresh.disabled = busy;}
  }
  action.addEventListener("change", () => {consent.checked = false; next.value = action.value === "update" ? view.next_action : ""; fields();});
  kind.addEventListener("change", () => targets()); find.addEventListener("click", () => targets()); more.addEventListener("click", () => targets(false));
  refresh.addEventListener("click", () => {message.textContent = "Refreshing; review again before submitting."; reload();});
  historyMore.addEventListener("click", () => loadHistory(false).catch(fail));
  form.addEventListener("submit", async event => {
    event.preventDefault(); if (blocked || busy || !view || !form.reportValidity()) return;
    if (!nextField.hidden && new TextEncoder().encode(next.value.trim()).length > 160) {message.textContent = "Next action exceeds 160 UTF-8 bytes."; return;}
    const body = {action: action.value, review_token: view.review_token};
    if (action.value === "offer") {body.target_kind = kind.value; body.target_ref = Number(target.value);}
    if (["offer", "update"].includes(action.value)) body.next_action = next.value;
    busy = true; blocked = true; refresh.disabled = true; fields();
    try {
      const response = await fetch(endpoint, {method: "POST", headers: {"content-type": "application/json", "x-csrf-token": csrf}, body: JSON.stringify(body)});
      const result = await response.json();
      if (response.status === 409) {message.textContent = "Responsibility changed. Refresh and review; nothing is resubmitted automatically."; return;}
      if ([400, 401, 403, 422].includes(response.status)) {message.textContent = `Not accepted: ${result.error?.message || "Check your decision and session."} Refresh before trying again.`; return;}
      if (!response.ok) throw new Error("Unconfirmed result");
      message.textContent = "Decision recorded locally. This is not a radio-delivery receipt or incident resolution."; await reload();
    } catch (_) {message.textContent = "Outcome unconfirmed; the decision may already be recorded. Refresh before trying again. No automatic retry.";}
    finally {busy = false; refresh.disabled = false; fields();}
  });
  await reload();
}
