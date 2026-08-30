import("/nav.js");

const $ = (id) => document.getElementById(id);
let csrfToken = "";
let documents = [];

function element(name, className, text) {
  const node = document.createElement(name);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = String(text);
  return node;
}

async function body(response) {
  const value = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(value.error?.message || `Request failed (${response.status})`);
  return value;
}

function mutation(path, options = {}) {
  return fetch(path, {
    ...options,
    headers: {
      "content-type": "application/json",
      "x-csrf-token": csrfToken,
      ...(options.headers || {}),
    },
  });
}

function renderStatus(value) {
  const health = value.health?.state || "unavailable";
  const badge = $("ai-health");
  badge.classList.toggle("up", health === "healthy");
  badge.classList.toggle("down", health === "unavailable");
  badge.textContent = health === "healthy" ? "● Provider healthy" : `● ${health}`;
  $("provider-name").textContent = value.provider || "—";
  $("provider-scope").textContent = value.external ? "External endpoint" : "On this Outpost";
  $("model-name").textContent = value.model || "—";
  $("model-context").textContent = `${Number(value.capabilities?.context_tokens || 0).toLocaleString()} token context`;
  $("queue-depth").textContent = String(value.queue?.active_and_waiting ?? "—");
  $("queue-capacity").textContent = `${value.queue?.capacity ?? "—"} total capacity`;
  $("circuit-state").textContent = value.circuit?.open ? "OPEN" : "Ready";
  $("circuit-detail").textContent = value.circuit?.recent_failures
    ? `${value.circuit.recent_failures} recent provider failures`
    : "No recent provider failures";
}

function resetKnowledgeForm() {
  $("knowledge-form").reset();
  $("knowledge-id").value = "";
  $("knowledge-cancel").hidden = true;
  $("knowledge-message").textContent = "";
}

function editKnowledge(document) {
  $("knowledge-id").value = document.id;
  $("knowledge-title").value = document.title;
  $("knowledge-slug").value = document.slug;
  $("knowledge-body").value = document.body;
  $("knowledge-cancel").hidden = false;
  $("knowledge-title").focus();
}

async function removeKnowledge(document) {
  const confirmed = await window.OutpostUI?.confirm({
    eyebrow: "VERIFIED EVIDENCE",
    title: `Delete ${document.title}?`,
    message: "ASK will no longer be allowed to cite this local source.",
    confirmLabel: "Delete document",
    danger: true,
  });
  if (!confirmed) return;
  await body(await mutation(`/api/v1/ai/kb/${document.id}`, {method: "DELETE"}));
  await loadKnowledge();
}

function renderKnowledge() {
  $("knowledge-count").textContent = `${documents.length} ${documents.length === 1 ? "document" : "documents"}`;
  const list = $("knowledge-list");
  list.replaceChildren();
  if (!documents.length) {
    list.append(element("p", "ui-empty empty", "No verified local knowledge yet."));
    return;
  }
  for (const document of documents) {
    const row = element("article", "knowledge-row");
    const header = element("header");
    const title = element("div");
    const status = document.retrievable
      ? `${document.chunk_count} ${document.chunk_count === 1 ? "chunk" : "chunks"} · retrievable`
      : `${document.chunk_count} ${document.chunk_count === 1 ? "chunk" : "chunks"} · unavailable to ASK`;
    const statusNode = element("span", "knowledge-status", status);
    statusNode.dataset.retrievable = String(Boolean(document.retrievable));
    title.append(
      element("h3", "", document.title),
      element("code", "", `src: kb:${document.slug}`),
      statusNode,
    );
    const actions = element("div", "knowledge-actions");
    const edit = element("button", "", "Edit");
    edit.type = "button";
    edit.addEventListener("click", () => editKnowledge(document));
    const remove = element("button", "danger", "Delete");
    remove.type = "button";
    remove.addEventListener("click", () => void removeKnowledge(document));
    actions.append(edit, remove);
    header.append(title, actions);
    row.append(header, element("p", "", document.body));
    if (document.warning) row.append(element("p", "knowledge-warning", document.warning));
    list.append(row);
  }
}

async function loadKnowledge() {
  documents = (await body(await fetch("/api/v1/ai/kb", {cache: "no-store"}))).items || [];
  renderKnowledge();
}

function renderRules(items) {
  const list = $("refusal-list");
  list.replaceChildren();
  if (!items.length) {
    list.append(element("p", "ui-empty empty", "No operator rules. Built-in safety rules remain active."));
    return;
  }
  for (const item of items) {
    const row = element("div", "refusal-row");
    row.append(element("strong", "", item.phrase), element("span", "", item.reason));
    list.append(row);
  }
}

async function loadRules() {
  const value = await body(await fetch("/api/v1/ai/refusal-rules", {cache: "no-store"}));
  renderRules(value.items || []);
}

async function rateInteraction(id, rating) {
  await body(await mutation(`/api/v1/ai/interactions/${id}/rating`, {
    method: "PATCH",
    body: JSON.stringify({rating}),
  }));
  await loadInteractions();
}

async function promoteInteraction(item) {
  const title = await window.OutpostUI?.prompt({
    eyebrow: "PROMOTE TO KNOWLEDGE",
    title: "Create a verified local source",
    message: "Review the answer first. Promotion does not make model-generated claims true.",
    label: "Knowledge document title",
    confirmLabel: "Promote for editing",
  });
  if (!title) return;
  const value = await body(await mutation(`/api/v1/ai/interactions/${item.id}/promote`, {
    method: "POST",
    body: JSON.stringify({title}),
  }));
  await loadKnowledge();
  const created = documents.find((document) => document.id === value.document_id);
  if (created) editKnowledge(created);
}

function renderInteractions(items) {
  const list = $("interaction-list");
  list.replaceChildren();
  if (!items.length) {
    list.append(element("p", "ui-empty empty", "No ASK or console tests have been logged."));
    return;
  }
  for (const item of items) {
    const row = element("article", "interaction-row");
    row.dataset.refused = String(Boolean(item.refused));
    const header = element("header");
    const heading = element("div");
    heading.append(
      element("h3", "", item.question),
      element("span", "interaction-meta", `${item.outcome} · ${item.question_class} · ${item.member || "console"}`),
    );
    const actions = element("div", "interaction-actions");
    for (const [label, rating] of [["Useful", 1], ["Wrong", -1]]) {
      const button = element("button", "interaction-rating", label);
      button.type = "button";
      button.setAttribute("aria-pressed", String(item.rated === rating));
      button.addEventListener("click", () => void rateInteraction(item.id, rating));
      actions.append(button);
    }
    if (item.answer && !item.refused) {
      const promote = element("button", "", "Promote");
      promote.type = "button";
      promote.addEventListener("click", () => void promoteInteraction(item));
      actions.append(promote);
    }
    header.append(heading, actions);
    row.append(header, element("p", "answer", item.answer || "No answer recorded."));
    const evidence = (item.evidence_refs || []).join(", ");
    if (evidence) row.append(element("p", "interaction-meta", `Evidence: ${evidence}`));
    list.append(row);
  }
}

async function loadInteractions() {
  const value = await body(await fetch("/api/v1/ai/interactions?limit=100", {cache: "no-store"}));
  renderInteractions(value.items || []);
}

async function refresh() {
  const results = await Promise.allSettled([
    fetch("/api/v1/ai/status", {cache: "no-store"}).then(body).then(renderStatus),
    loadKnowledge(),
    loadRules(),
    loadInteractions(),
  ]);
  const failed = results.find((result) => result.status === "rejected");
  if (failed) $("ai-health").textContent = "● AI data unavailable";
}

$("ai-test-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const output = $("ai-test-result");
  output.classList.remove("ready");
  output.textContent = "Running guard checks and local retrieval…";
  try {
    const value = await body(await mutation("/api/v1/ai/test", {
      method: "POST",
      body: JSON.stringify({question: $("ai-question").value}),
    }));
    output.textContent = `${value.text}\n\nOutcome: ${value.outcome} · Class: ${value.question_class}`;
    output.classList.add("ready");
    await loadInteractions();
  } catch (error) {
    output.textContent = error.message;
  }
});

$("knowledge-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const id = $("knowledge-id").value;
  const payload = {
    title: $("knowledge-title").value,
    slug: $("knowledge-slug").value || null,
    body: $("knowledge-body").value,
  };
  try {
    const result = await body(await mutation(id ? `/api/v1/ai/kb/${id}` : "/api/v1/ai/kb", {
      method: id ? "PATCH" : "POST",
      body: JSON.stringify(payload),
    }));
    resetKnowledgeForm();
    $("knowledge-message").textContent = result.warning || `Saved as ${result.chunk_count} retrievable ${result.chunk_count === 1 ? "chunk" : "chunks"}.`;
    await loadKnowledge();
  } catch (error) {
    $("knowledge-message").textContent = error.message;
  }
});

$("knowledge-cancel").addEventListener("click", resetKnowledgeForm);
$("refusal-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  await body(await mutation("/api/v1/ai/refusal-rules", {
    method: "POST",
    body: JSON.stringify({
      phrase: $("refusal-phrase").value,
      reason: $("refusal-reason").value,
    }),
  }));
  event.target.reset();
  await loadRules();
});
$("refresh-ai").addEventListener("click", refresh);

async function initialize() {
  const response = await fetch("/api/v1/auth/session", {cache: "no-store"});
  if (!response.ok) {
    location.href = "/";
    return;
  }
  csrfToken = (await response.json()).csrf_token;
  await refresh();
}

initialize();
