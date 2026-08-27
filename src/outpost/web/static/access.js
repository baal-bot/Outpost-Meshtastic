import("/nav.js");

const $ = (id) => document.getElementById(id);
const safe = (value) => String(value ?? "").replace(
  /[&<>'"]/g,
  (character) => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", "'":"&#39;", '"':"&quot;"})[character],
);
let csrf = "";
let session = null;

const roleLabel = (role) => ({
  administrator: "Administrator",
  operator: "Operator",
  viewer: "Read-only / wallboard",
})[role] || role;
const timeLabel = (stamp) => stamp
  ? new Date(Number(stamp) * 1000).toLocaleString([], {dateStyle: "medium", timeStyle: "short"})
  : "Never";

async function api(path, options = {}) {
  const headers = {...(options.headers || {})};
  if (options.method && !["GET", "HEAD"].includes(options.method)) headers["x-csrf-token"] = csrf;
  if (options.body) headers["content-type"] = "application/json";
  const response = await fetch(path, {...options, headers});
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error?.message || `Request failed (${response.status})`);
  return body;
}

function renderIdentity() {
  $("signed-account").innerHTML = `<i></i><span><b>${safe(session.display_name)}</b><small>@${safe(session.username)} · ${safe(roleLabel(session.role))}</small></span>`;
  $("welcome-name").textContent = `${session.display_name}'s Outpost identity`;
  $("mfa-status").textContent = session.mfa_enabled ? "ENABLED" : "RECOMMENDED";
  $("mfa-status").classList.toggle("enabled", session.mfa_enabled);
  $("security-state").textContent = session.mfa_enabled ? "HARDENED" : "PASSWORD ONLY";
  $("security-title").textContent = session.mfa_enabled
    ? "Second factor enabled"
    : "Add a second factor";
  $("security-copy").textContent = session.mfa_enabled
    ? "Sensitive actions require recent password and authenticator confirmation."
    : "Sensitive actions still require password confirmation; TOTP adds stronger protection.";
  $("mfa-summary").innerHTML = session.mfa_enabled
    ? `<div class="mfa-mark enabled">✓</div><div><strong>Authenticator protection is active</strong><p>Sign-in and protected step-up checks require a current code or unused recovery code.</p><button id="disable-mfa" type="button" class="danger-button">Disable authenticator</button></div>`
    : `<div class="mfa-mark">✣</div><div><strong>Protect this account with TOTP</strong><p>Works offline with common authenticator apps. Eight field-safe recovery codes are issued once.</p><button id="enable-mfa" type="button" class="primary-button">Set up authenticator</button></div>`;
  $("accounts-panel").hidden = session.role !== "administrator";
}

async function loadSessions() {
  const body = await api("/api/v1/auth/sessions");
  $("session-list").innerHTML = body.items.map((item) => `
    <article class="session-row ${item.current ? "current" : ""}" data-session-id="${safe(item.id)}">
      <div class="session-icon">${item.current ? "●" : "○"}</div>
      <div><strong>${item.current ? "This session" : safe(item.source)}</strong><p>${safe(item.user_agent)}</p><small>Last active ${safe(timeLabel(item.last_activity_at))} · expires ${safe(timeLabel(item.expires_at))}</small></div>
      <button type="button" data-revoke-session>${item.current ? "Sign out" : "Revoke"}</button>
    </article>`).join("") || `<p class="empty">No active sessions.</p>`;
}

function accountCard(account) {
  const current = account.id === session.account_id;
  const actions = current
    ? '<span class="current-account-note">Managed above</span>'
    : `<div class="account-actions">
      <select data-account-role aria-label="Role for ${safe(account.username)}">
        <option value="administrator" ${account.role === "administrator" ? "selected" : ""}>Administrator</option>
        <option value="operator" ${account.role === "operator" ? "selected" : ""}>Operator</option>
        <option value="viewer" ${account.role === "viewer" ? "selected" : ""}>Read-only</option>
      </select>
      <button type="button" data-reset-password>Reset password</button>
      <button type="button" data-toggle-account class="${account.enabled ? "danger-button" : "primary-button"}">${account.enabled ? "Disable" : "Enable"}</button>
    </div>`;
  return `<article class="account-row ${account.enabled ? "" : "disabled"}" data-account-id="${account.id}">
    <div class="account-avatar">${safe(account.display_name.slice(0, 1).toUpperCase())}</div>
    <div class="account-copy"><div><strong>${safe(account.display_name)}</strong>${current ? "<span>CURRENT</span>" : ""}${account.mfa_enabled ? "<span>MFA</span>" : ""}</div><p>@${safe(account.username)} · ${safe(roleLabel(account.role))}</p><small>${account.enabled ? `Last sign-in ${safe(timeLabel(account.last_login_at))}` : "Account disabled"}${account.must_change ? " · password change required" : ""}</small></div>
    ${actions}
  </article>`;
}

async function loadAccounts() {
  if (session.role !== "administrator") return;
  const body = await api("/api/v1/auth/accounts");
  $("account-list").innerHTML = body.items.map(accountCard).join("");
}

async function startMfa() {
  $("mfa-message").textContent = "Preparing authenticator enrollment…";
  try {
    const body = await api("/api/v1/auth/mfa/begin", {method: "POST"});
    $("mfa-secret").textContent = body.secret;
    $("mfa-uri").textContent = body.otpauth_uri;
    $("mfa-enrollment").hidden = false;
    $("recovery-codes").hidden = true;
    $("mfa-message").textContent = "Authenticator secret created. Confirm it with a current code.";
    $("mfa-code").focus();
  } catch (error) {
    $("mfa-message").textContent = error.message;
  }
}

async function disableMfa() {
  const confirmed = await window.OutpostUI.confirm({
    eyebrow: "ACCOUNT SECURITY",
    title: "Disable authenticator protection?",
    message: "Future sign-ins will rely on the password alone and all remaining recovery codes will be revoked.",
    confirmLabel: "Disable authenticator",
    danger: true,
  });
  if (!confirmed) return;
  try {
    await api("/api/v1/auth/mfa", {method: "DELETE"});
    session.mfa_enabled = false;
    renderIdentity();
    $("mfa-message").textContent = "Authenticator protection disabled.";
  } catch (error) {
    $("mfa-message").textContent = error.message;
  }
}

$("mfa-summary").addEventListener("click", (event) => {
  if (event.target.closest("#enable-mfa")) void startMfa();
  if (event.target.closest("#disable-mfa")) void disableMfa();
});

$("mfa-confirm-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const body = await api("/api/v1/auth/mfa/confirm", {
      method: "POST",
      body: JSON.stringify({code: $("mfa-code").value}),
    });
    $("mfa-enrollment").hidden = true;
    $("recovery-list").textContent = body.recovery_codes.join("\n");
    $("recovery-codes").hidden = false;
    session.mfa_enabled = true;
    renderIdentity();
    $("mfa-message").textContent = "Authenticator protection enabled. Store the recovery codes offline.";
  } catch (error) {
    $("mfa-message").textContent = error.message;
  }
});

$("copy-recovery").addEventListener("click", async () => {
  await navigator.clipboard.writeText($("recovery-list").textContent);
  $("mfa-message").textContent = "Recovery codes copied. Store them somewhere offline and secure.";
});

$("session-list").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-revoke-session]");
  if (!button) return;
  const row = button.closest("[data-session-id]");
  const confirmed = await window.OutpostUI.confirm({
    title: row.classList.contains("current") ? "Sign out this session?" : "Revoke this session?",
    message: "The selected browser will need to authenticate again.",
    confirmLabel: row.classList.contains("current") ? "Sign out" : "Revoke session",
  });
  if (!confirmed) return;
  try {
    await api(`/api/v1/auth/sessions/${encodeURIComponent(row.dataset.sessionId)}`, {method: "DELETE"});
    if (row.classList.contains("current")) location.href = "/";
    else await loadSessions();
  } catch (error) {
    await window.OutpostUI.alert({title: "Session was not revoked", message: error.message});
  }
});

$("revoke-all").addEventListener("click", async () => {
  const confirmed = await window.OutpostUI.confirm({
    eyebrow: "SESSION CONTROL",
    title: "Sign out every session?",
    message: "This includes the browser you are using now.",
    confirmLabel: "Sign out everywhere",
    danger: true,
  });
  if (!confirmed) return;
  await api("/api/v1/auth/sessions", {method: "DELETE"});
  location.href = "/";
});

$("show-create-account").addEventListener("click", () => {
  $("create-account-form").hidden = false;
  $("account-username").focus();
});
$("cancel-create-account").addEventListener("click", () => {
  $("create-account-form").hidden = true;
  $("create-account-form").reset();
});
$("create-account-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await api("/api/v1/auth/accounts", {
      method: "POST",
      body: JSON.stringify({
        username: $("account-username").value,
        display_name: $("account-display").value,
        role: $("account-role").value,
        initial_password: $("account-password").value,
      }),
    });
    $("create-account-form").reset();
    $("create-account-form").hidden = true;
    $("account-message").textContent = "Account created. Its initial password must be changed at first sign-in.";
    await loadAccounts();
  } catch (error) {
    $("account-message").textContent = error.message;
  }
});

$("account-list").addEventListener("change", async (event) => {
  const select = event.target.closest("[data-account-role]");
  if (!select) return;
  const row = select.closest("[data-account-id]");
  try {
    await api(`/api/v1/auth/accounts/${row.dataset.accountId}`, {
      method: "PATCH",
      body: JSON.stringify({role: select.value}),
    });
    $("account-message").textContent = "Account role updated.";
    await loadAccounts();
  } catch (error) {
    $("account-message").textContent = error.message;
    await loadAccounts();
  }
});

$("account-list").addEventListener("click", async (event) => {
  const row = event.target.closest("[data-account-id]");
  if (!row) return;
  if (event.target.closest("[data-toggle-account]")) {
    const enabled = row.classList.contains("disabled");
    const confirmed = await window.OutpostUI.confirm({
      title: `${enabled ? "Enable" : "Disable"} this account?`,
      message: enabled ? "The operator will be able to sign in again." : "Every active session for this account will be revoked.",
      confirmLabel: enabled ? "Enable account" : "Disable account",
      danger: !enabled,
    });
    if (!confirmed) return;
    try {
      await api(`/api/v1/auth/accounts/${row.dataset.accountId}`, {
        method: "PATCH",
        body: JSON.stringify({enabled}),
      });
      await loadAccounts();
    } catch (error) {
      $("account-message").textContent = error.message;
    }
  }
  if (event.target.closest("[data-reset-password]")) {
    const password = await window.OutpostUI.prompt({
      eyebrow: "ACCOUNT RECOVERY",
      title: "Set a temporary password",
      message: "This revokes every session for the account and requires a password change at next sign-in.",
      label: "Temporary password · 12 characters minimum",
      type: "password",
      autocomplete: "new-password",
      confirmLabel: "Reset password",
    });
    if (!password) return;
    try {
      await api(`/api/v1/auth/accounts/${row.dataset.accountId}/password`, {
        method: "POST",
        body: JSON.stringify({temporary_password: password}),
      });
      $("account-message").textContent = "Temporary password set and active sessions revoked.";
      await loadAccounts();
    } catch (error) {
      $("account-message").textContent = error.message;
    }
  }
});

$("change-own-password").addEventListener("click", async () => {
  const currentPassword = await window.OutpostUI.prompt({
    eyebrow: "ACCOUNT SECURITY",
    title: "Confirm your current password",
    label: "Current password",
    type: "password",
    autocomplete: "current-password",
    confirmLabel: "Continue",
  });
  if (!currentPassword) return;
  const replacement = await window.OutpostUI.prompt({
    eyebrow: "ACCOUNT SECURITY",
    title: "Choose a new password",
    message: "Use at least 12 characters. Every active session for this account will be signed out.",
    label: "New password",
    type: "password",
    autocomplete: "new-password",
    confirmLabel: "Change password",
  });
  if (!replacement) return;
  try {
    await api("/api/v1/auth/password", {
      method: "POST",
      body: JSON.stringify({current_password: currentPassword, new_password: replacement}),
    });
    location.href = "/";
  } catch (error) {
    $("mfa-message").textContent = error.message;
  }
});

async function initialize() {
  const response = await fetch("/api/v1/auth/session");
  if (!response.ok) {
    location.href = "/";
    return;
  }
  session = await response.json();
  csrf = session.csrf_token;
  renderIdentity();
  await Promise.all([loadSessions(), loadAccounts()]);
}

initialize();
