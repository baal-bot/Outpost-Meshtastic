import("/nav.js");
import("/member-map.js?v=2");
const $ = (id) => document.getElementById(id);
const safe = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;","\"":"&quot;"})[char]);
const relative = (stamp) => { const seconds = Math.max(0, (Date.now() - new Date(stamp)) / 1000); if (seconds < 60) return "now"; if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`; if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`; return `${Math.floor(seconds / 86400)}d ago`; };
let csrfToken = "";
const trustLevels = ["blocked", "guest", "member", "trusted", "responder", "operator"];

async function load() {
  const sessionResponse = await fetch("/api/v1/auth/session");
  if (!sessionResponse.ok) { location.href = "/"; return; }
  csrfToken = (await sessionResponse.json()).csrf_token;
  const view = $("member-view").value;
  const [members, audit] = await Promise.all([fetch(`/api/v1/members?view=${view}`).then((r) => r.json()), fetch("/api/v1/audit").then((r) => r.json())]);
  $("member-count").textContent = members.approved_count;
  $("discovered-count").textContent = members.discovered_count;
  $("trusted-count").textContent = members.trusted_count;
  $("audit-count").textContent = audit.items.length;
  $("member-view-title").textContent = view === "approved" ? "Community members" : view === "discovered" ? "Discovered radios" : "All identities";
  $("discovered-note").hidden = view !== "discovered";
  $("member-rows").innerHTML = members.items.map((member) => `<tr><td><strong>${safe(member.handle ? `@${member.handle}` : "Unnamed")}</strong><small>${safe(member.notes || "No operator notes")}</small></td><td><code>${safe(member.mesh_id)}</code></td><td>${safe(relative(member.last_seen))}</td><td>${safe(member.last_heard_snr ?? "—")} dB</td><td><select data-member="${safe(member.id)}" aria-label="Trust for ${safe(member.handle || member.mesh_id)}">${trustLevels.map((level) => `<option ${level === member.trust ? "selected" : ""}>${safe(level)}</option>`).join("")}</select></td></tr>`).join("") || `<tr><td colspan="5">No members yet.</td></tr>`;
  $("audit-list").innerHTML = audit.items.map((event) => `<div class="audit-event"><code>${safe(event.action)}</code><p>${safe(event.target || "system")}${event.detail ? ` · ${safe(event.detail)}` : ""}</p><time>${safe(relative(event.created_at))}</time></div>`).join("") || `<p class="empty">No audit events yet.</p>`;
  document.querySelectorAll("select[data-member]").forEach((select) => select.addEventListener("change", async () => {
    select.disabled = true;
    const response = await fetch(`/api/v1/members/${select.dataset.member}`, {method:"PATCH", headers:{"content-type":"application/json","x-csrf-token":csrfToken}, body:JSON.stringify({trust:select.value})});
    select.disabled = false;
    if (!response.ok) window.alert("Trust update failed.");
    await load();
  }));
}
$("member-view").addEventListener("change", load);
load();
