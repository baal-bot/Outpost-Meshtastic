import("/nav.js");

const $ = (id) => document.getElementById(id);
const safe = (value) => String(value ?? "").replace(
  /[&<>'"]/g,
  (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", "\"": "&quot;" })[char],
);
const RESTORE_JOB_KEY = "outpost.restore.job";
const TERMINAL_STATES = new Set(["completed", "failed_recovered", "failed", "interrupted"]);
let csrfToken = "";

const formatSize = (bytes) => {
  const value = Number(bytes || 0);
  const absolute = Math.abs(value);
  if (absolute < 1024) return `${value} B`;
  if (absolute < 1048576) return `${(value / 1024).toFixed(1)} KB`;
  if (absolute < 1073741824) return `${(value / 1048576).toFixed(1)} MB`;
  return `${(value / 1073741824).toFixed(2)} GB`;
};
const signed = (value, formatter = String) => value === null || value === undefined
  ? "Baseline pending"
  : `${Number(value) > 0 ? "+" : ""}${formatter(Number(value))}`;
const delay = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

function renderRestore(job) {
  const panel = $("restore-progress");
  const state = String(job.state || "queued");
  panel.hidden = false;
  panel.dataset.state = state;
  $("restore-state").textContent = state.replaceAll("_", " ").toUpperCase();
  $("restore-title").textContent = state === "completed"
    ? "Restore verified"
    : state === "failed_recovered"
      ? "Original state recovered"
      : state === "failed" || state === "interrupted"
        ? "Restore needs attention"
        : "Controlled restore in progress";
  $("restore-message").textContent = job.message || "Recovery status is being updated.";
  $("restore-detail").textContent = [
    job.backup ? `Snapshot: ${job.backup}` : "",
    job.safety_backup ? `Safety copy: ${job.safety_backup}` : "",
  ].filter(Boolean).join(" · ");
  $("restore-finish").hidden = !TERMINAL_STATES.has(state);
  document.querySelectorAll("button").forEach((button) => {
    if (button.id !== "restore-finish") button.disabled = !TERMINAL_STATES.has(state);
  });
}

async function monitorRestore(jobId) {
  localStorage.setItem(RESTORE_JOB_KEY, jobId);
  for (;;) {
    try {
      const response = await fetch(
        `/api/v1/recovery/restores/${encodeURIComponent(jobId)}`,
        { cache: "no-store" },
      );
      if (response.ok) {
        const job = await response.json();
        renderRestore(job);
        if (TERMINAL_STATES.has(job.state)) return true;
      }
    } catch (_error) {
      // A brief disconnect is expected while systemd restarts the restored Outpost.
    }
    await delay(1000);
  }
}

async function resumeRestore() {
  const jobId = localStorage.getItem(RESTORE_JOB_KEY);
  if (!jobId) return false;
  try {
    const response = await fetch(
      `/api/v1/recovery/restores/${encodeURIComponent(jobId)}`,
      { cache: "no-store" },
    );
    if (!response.ok) return false;
    const job = await response.json();
    renderRestore(job);
    if (!TERMINAL_STATES.has(job.state)) void monitorRestore(jobId);
    return true;
  } catch (_error) {
    renderRestore({
      state: "queued",
      message: "Outpost is restarting; waiting for durable restore status…",
    });
    void monitorRestore(jobId);
    return true;
  }
}

async function initialize() {
  const trackingRestore = await resumeRestore();
  if (trackingRestore) return;
  const response = await fetch("/api/v1/auth/session");
  if (!response.ok) {
    location.href = "/";
    return;
  }
  csrfToken = (await response.json()).csrf_token;
  await Promise.allSettled([loadBackups(), loadStorage()]);
}

function renderDomains(items, growthSince) {
  const maximum = Math.max(1, ...items.map((item) => Number(item.size_bytes || 0)));
  $("growth-window").textContent = growthSince
    ? `Change since ${new Date(growthSince * 1000).toLocaleString()}`
    : "A growth baseline is saved after maintenance";
  $("storage-domains").innerHTML = items.map((item) => `
    <div class="domain-row">
      <div class="domain-copy">
        <strong>${safe(item.label)}</strong>
        <span>${Number(item.rows).toLocaleString()} rows · ${formatSize(item.size_bytes)}</span>
      </div>
      <div class="domain-meter" aria-hidden="true"><i style="width:${Math.max(2, Number(item.size_bytes) / maximum * 100)}%"></i></div>
      <small>${signed(item.growth_bytes, formatSize)} · ${signed(item.growth_rows, (value) => `${value.toLocaleString()} rows`)}</small>
    </div>
  `).join("");
}

function renderCleanup(cleanup) {
  const rows = Number(cleanup.total_rows || 0);
  $("cleanup-rows").textContent = `${rows.toLocaleString()} ${rows === 1 ? "record" : "records"}`;
  $("cleanup-summary").textContent = rows
    ? `Approximately ${formatSize(cleanup.estimated_bytes)} is eligible. A verified snapshot is created before incremental deletion.`
    : "Nothing is currently eligible under the active retention policy.";
  const eligible = (cleanup.rules || []).filter((item) => Number(item.rows) > 0);
  $("cleanup-rules").innerHTML = eligible.slice(0, 6).map((item) => `
    <div><span>${safe(item.label)}</span><b>${Number(item.rows).toLocaleString()}</b></div>
  `).join("") || `<p>Active records and protected evidence are untouched.</p>`;
  if (eligible.length > 6) {
    $("cleanup-rules").insertAdjacentHTML(
      "beforeend",
      `<p>+ ${eligible.length - 6} additional retention ${eligible.length - 6 === 1 ? "rule" : "rules"}</p>`,
    );
  }
  $("run-maintenance").disabled = rows === 0;
}

function renderPolicies(items) {
  $("retention-policies").innerHTML = items.map((item) => `
    <article>
      <div><code>${safe(item.table)}</code>${item.protected ? `<span>PROTECTED</span>` : ""}</div>
      <strong>${safe(item.policy)}</strong>
      <p>${safe(item.detail)}</p>
    </article>
  `).join("");
}

async function loadStorage() {
  const response = await fetch("/api/v1/maintenance/storage", {cache: "no-store"});
  if (!response.ok) {
    $("maintenance-status").textContent = "Storage health is unavailable on this build.";
    return;
  }
  const body = await response.json();
  $("storage-database").textContent = formatSize(body.database_bytes);
  $("storage-wal").textContent = formatSize(body.wal_bytes);
  $("storage-backups").textContent = `${body.backup_count} · ${formatSize(body.backup_bytes)}`;
  $("storage-free").textContent = formatSize(body.disk_free_bytes);
  renderDomains(body.domains || [], body.growth_since);
  renderCleanup(body.cleanup || {});
  renderPolicies(body.policies || []);
  $("maintenance-status").textContent = body.last_maintenance
    ? `Last scheduled maintenance: ${body.last_maintenance}. Audit evidence is preserved.`
    : "Maintenance has not completed on this installation yet. Audit evidence is preserved.";
}

async function loadBackups() {
  const response = await fetch("/api/v1/backups");
  const body = await response.json();
  const items = body.items;
  $("backup-count").textContent = items.length;
  $("backup-size").textContent = formatSize(
    items.reduce((sum, item) => sum + item.size_bytes, 0),
  );
  $("backup-newest").textContent = items.length
    ? new Date(items[0].created_at).toLocaleDateString()
    : "None";
  $("backup-list").innerHTML = items.map((item) => `
    <div class="backup-row">
      <strong>${safe(item.name)}</strong>
      <span>${formatSize(item.size_bytes)}</span>
      <span>${new Date(item.created_at).toLocaleString()}</span>
      <div class="backup-actions">
        <a href="/api/v1/backups/${encodeURIComponent(item.name)}">Download</a>
        <button data-validate="${safe(item.name)}">Validate</button>
        <button class="restore" data-restore="${safe(item.name)}">Restore</button>
      </div>
    </div>
  `).join("") || `<p class="ui-empty empty">No backups yet.</p>`;
  document.querySelectorAll("[data-validate]").forEach((button) => {
    button.addEventListener("click", () => validate(button));
  });
  document.querySelectorAll("[data-restore]").forEach((button) => {
    button.addEventListener("click", () => restore(button.dataset.restore));
  });
}

async function validate(button) {
  const name = button.dataset.validate;
  button.disabled = true;
  button.textContent = "Checking…";
  const response = await fetch(`/api/v1/backups/${encodeURIComponent(name)}/validate`);
  button.textContent = response.ok ? "✓ Valid" : "Invalid";
  button.disabled = false;
}

async function restore(name) {
  const verification = `RESTORE ${name}`;
  const phrase = await window.OutpostUI.prompt({
    eyebrow: "CONTROLLED RECOVERY",
    title: `Restore ${name}?`,
    message: "Outpost will enter maintenance mode, drain background work, restore the verified snapshot, and restart. A safety copy is retained for automatic recovery.",
    label: "Restore confirmation",
    verification,
    confirmLabel: "Enter maintenance and restore",
    danger: true,
  });
  if (phrase === null) return;
  const response = await fetch(`/api/v1/backups/${encodeURIComponent(name)}/restore`, {
    method: "POST",
    headers: { "content-type": "application/json", "x-csrf-token": csrfToken },
    body: JSON.stringify({ confirmation: phrase }),
  });
  const body = await response.json();
  if (!response.ok) {
    await window.OutpostUI.alert({title: "Restore could not start", message: body.error.message});
    return;
  }
  renderRestore(body);
  await monitorRestore(body.job_id);
}

$("create-backup-page").addEventListener("click", async () => {
  const button = $("create-backup-page");
  button.disabled = true;
  button.textContent = "Verifying…";
  await fetch("/api/v1/backups", {
    method: "POST",
    headers: { "x-csrf-token": csrfToken },
  });
  button.disabled = false;
  button.textContent = "Create verified backup";
  await loadBackups();
  await loadStorage();
});
$("refresh-backups").addEventListener("click", loadBackups);
$("refresh-storage").addEventListener("click", loadStorage);
$("run-maintenance").addEventListener("click", async () => {
  const phrase = await window.OutpostUI.prompt({
    eyebrow: "RETENTION MAINTENANCE",
    title: "Clean up eligible records?",
    message: "Outpost first creates a verified snapshot, then removes only records shown by the retention preview in small batches. Active workflows and audit evidence are protected.",
    label: "Maintenance confirmation",
    verification: "CLEANUP",
    confirmLabel: "Create snapshot and clean up",
  });
  if (phrase === null) return;
  const button = $("run-maintenance");
  button.disabled = true;
  button.textContent = "Running safely…";
  $("maintenance-status").textContent = "Creating the safety snapshot and applying bounded batches…";
  const response = await fetch("/api/v1/maintenance/run", {
    method: "POST",
    headers: {"content-type": "application/json", "x-csrf-token": csrfToken},
    body: JSON.stringify({confirmation: phrase}),
  });
  const body = await response.json();
  if (!response.ok) {
    await window.OutpostUI.alert({
      title: "Maintenance could not run",
      message: body.error?.message || "The retention run was not applied.",
    });
  } else {
    const removed = Object.values(body.result.removed || {}).reduce((sum, value) => sum + Number(value), 0);
    $("maintenance-status").textContent = `${removed.toLocaleString()} records removed; the pre-cleanup snapshot is available below.`;
  }
  button.textContent = "Run maintenance";
  await Promise.allSettled([loadStorage(), loadBackups()]);
});
$("restore-finish").addEventListener("click", () => {
  localStorage.removeItem(RESTORE_JOB_KEY);
});

initialize();
