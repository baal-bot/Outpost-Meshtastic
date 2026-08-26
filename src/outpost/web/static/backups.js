import("/nav.js");

const $ = (id) => document.getElementById(id);
const safe = (value) => String(value ?? "").replace(
  /[&<>'"]/g,
  (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", "\"": "&quot;" })[char],
);
const RESTORE_JOB_KEY = "outpost.restore.job";
const TERMINAL_STATES = new Set(["completed", "failed_recovered", "failed", "interrupted"]);
let csrfToken = "";

const formatSize = (bytes) => bytes < 1048576
  ? `${(bytes / 1024).toFixed(1)} KB`
  : `${(bytes / 1048576).toFixed(1)} MB`;
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
  await loadBackups();
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
  `).join("") || `<p class="empty">No backups yet.</p>`;
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
  const phrase = window.prompt(`Restore ${name}?\n\nType exactly: RESTORE ${name}`);
  if (phrase === null) return;
  if (!window.confirm("Outpost will enter maintenance mode and restart. Continue?")) return;
  const response = await fetch(`/api/v1/backups/${encodeURIComponent(name)}/restore`, {
    method: "POST",
    headers: { "content-type": "application/json", "x-csrf-token": csrfToken },
    body: JSON.stringify({ confirmation: phrase }),
  });
  const body = await response.json();
  if (!response.ok) {
    window.alert(body.error.message);
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
});
$("refresh-backups").addEventListener("click", loadBackups);
$("restore-finish").addEventListener("click", () => {
  localStorage.removeItem(RESTORE_JOB_KEY);
});

initialize();
