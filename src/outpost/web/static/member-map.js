const memberMapCss = document.createElement("link");
memberMapCss.rel = "stylesheet";
memberMapCss.href = "/member-map.css?v=3";
document.head.appendChild(memberMapCss);

const memberMapPanel = document.createElement("section");
memberMapPanel.className = "panel member-map-panel";
memberMapPanel.innerHTML = `
  <div class="member-map-heading">
    <div><p class="eyebrow">APPROVED MEMBER POSITIONS</p><h2>Members map</h2></div>
    <div class="member-map-filters">
      <select id="member-map-trust" aria-label="Filter member map by trust level">
        <option value="all">All trust levels</option><option value="member">Members</option>
        <option value="trusted">Trusted</option><option value="responder">Responders</option>
        <option value="operator">Operators</option>
      </select>
      <select id="member-map-age" aria-label="Filter member map by position age">
        <option value="168">Past 7 days</option><option value="24">Past 24 hours</option>
        <option value="1">Past hour</option>
      </select>
      <button id="member-map-purge-expired" class="member-map-danger" type="button">Purge expired</button>
    </div>
  </div>
  <p class="member-privacy-notice">Operator view shows full received coordinates for approved members only. Every share has a configured deletion time; expired positions are hidden immediately and physically removed by maintenance. Member-facing POS responses still honor each member’s visibility preference.</p>
  <p id="member-position-result" class="member-position-result" aria-live="polite"></p>
  <div id="member-map" class="outpost-map member-position-map" tabindex="0" aria-label="Interactive members map. Use arrow keys to pan, plus and minus to zoom, and zero to fit visible members.">
    <div id="member-map-tiles" class="outpost-map-tiles"></div>
    <div id="member-map-markers" class="outpost-map-markers"></div>
    <div class="outpost-map-controls">
      <button id="member-map-in" data-map-action="zoom-in" title="Zoom in" aria-label="Zoom in">+</button>
      <button id="member-map-out" data-map-action="zoom-out" title="Zoom out" aria-label="Zoom out">−</button>
      <button id="member-map-fit" data-map-action="fit" title="Fit visible members" aria-label="Fit visible members">⌖</button>
    </div>
    <span id="member-map-coordinates" class="outpost-map-coordinates">—</span>
    <aside id="member-map-detail" class="outpost-map-detail" hidden></aside>
    <p id="member-map-empty" class="outpost-map-empty">No approved member positions match these filters.</p>
    <div id="member-map-attribution" class="outpost-map-attribution"></div>
  </div>`;
document.querySelector("#member-directory").after(memberMapPanel);

const mm = id => document.getElementById(id);
const escapeMap = value => String(value ?? "").replace(
  /[&<>'"]/g,
  char => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;"})[char],
);
const positionAge = seconds => seconds < 60 ? "just now" :
  seconds < 3600 ? `${Math.floor(seconds / 60)}m ago` :
  seconds < 86400 ? `${Math.floor(seconds / 3600)}h ago` :
  `${Math.floor(seconds / 86400)}d ago`;

let memberMapItems = [];

function visibleMembers() {
  const trust = mm("member-map-trust").value;
  const cutoff = Date.now() - Number(mm("member-map-age").value) * 3600000;
  return memberMapItems.filter(value =>
    (trust === "all" || value.trust === trust) &&
    new Date(value.received_at).getTime() >= cutoff
  );
}

function closeMemberDetail() {
  mm("member-map-detail").hidden = true;
  memberMapController.clearSelection();
}

const memberMapController = new window.OutpostMap.Controller({
  root: mm("member-map"),
  tiles: mm("member-map-tiles"),
  markers: mm("member-map-markers"),
  coordinates: mm("member-map-coordinates"),
  empty: mm("member-map-empty"),
  detail: mm("member-map-detail"),
  attribution: mm("member-map-attribution"),
  initialView: {lat: 40.4406, lon: -79.9959, zoom: 10},
  onFit: () => fitMemberMap(),
  onBackground: closeMemberDetail,
  onEscape: closeMemberDetail,
});

function showMember(value) {
  const markerId = `member-${value.id}`;
  memberMapController.select(markerId);
  const detail = mm("member-map-detail");
  const source = value.source === "position_app" ? "Meshtastic position share" : value.source;
  detail.hidden = false;
  detail.innerHTML = `
    <button class="outpost-map-detail-close" aria-label="Close">×</button>
    <p class="eyebrow">${escapeMap(value.trust.toUpperCase())} MEMBER</p>
    <h3>${escapeMap(value.handle ? `@${value.handle}` : value.mesh_id)}</h3>
    <p>${Number(value.lat).toFixed(5)}, ${Number(value.lon).toFixed(5)}</p>
    <p><b>Shared</b> ${new Date(value.received_at).toLocaleString()} · ${positionAge(value.age_seconds)}</p>
    <p><b>Source</b> ${escapeMap(source)}</p>
    <p><b>Visibility</b> ${escapeMap(value.visibility)}</p>
    <p><b>Scheduled deletion</b> ${new Date(value.expires_at).toLocaleString()} · ${Math.max(1, Math.ceil(value.deletes_in_seconds / 3600))}h remaining</p>
    <p><b>Last heard</b> ${new Date(value.last_seen).toLocaleString()} · ${value.last_heard_snr ?? "—"} dB · ${value.hops_away ?? "—"} hops</p>
    <button class="member-map-danger delete-position" type="button">Delete exact position</button>`;
  detail.querySelector(".outpost-map-detail-close").onclick = closeMemberDetail;
  detail.querySelector(".delete-position").onclick = () => deleteMemberPosition(value);
}

function renderMemberMap() {
  const values = visibleMembers();
  memberMapController.setMarkers(values.map(value => ({
    id: `member-${value.id}`,
    lat: value.lat,
    lon: value.lon,
    className: `shape-circle ${["trusted", "responder", "operator"].includes(value.trust) ? "tone-trusted" : "tone-member"}`,
    title: value.handle ? `@${value.handle}` : value.mesh_id,
    label: `Show ${value.handle ? `@${value.handle}` : value.mesh_id} on the members map`,
    data: value,
    onActivate: showMember,
  })));
  memberMapController.setEmpty(values.length === 0);
}

function fitMemberMap() {
  const values = visibleMembers();
  renderMemberMap();
  if (values.length) memberMapController.fit(values, {maxZoom: 14, padding: 60});
}

async function loadMemberMap() {
  const response = await fetch("/api/v1/members/map");
  if (!response.ok) return;
  memberMapItems = (await response.json()).items || [];
  fitMemberMap();
  bindMemberRows();
}

async function positionCsrf() {
  const response = await fetch("/api/v1/auth/session");
  if (!response.ok) throw new Error("Operator session expired.");
  return (await response.json()).csrf_token;
}

async function deleteMemberPosition(value) {
  const label = value.handle ? `@${value.handle}` : value.mesh_id;
  if (!await window.OutpostUI.confirm({
    title: `Delete the position for ${label}?`,
    message: "This clears the retained exact coordinate and any pending report-location prompt. Existing incidents and welfare records are not changed.",
    confirmLabel: "Delete exact position",
    danger: true,
  })) return;
  const response = await fetch(`/api/v1/members/${value.id}/position`, {
    method: "DELETE",
    headers: {"x-csrf-token": await positionCsrf()},
  });
  const body = await response.json();
  if (!response.ok) {
    await window.OutpostUI.alert({
      title: "Position not deleted",
      message: body.error?.message || "Position deletion failed.",
    });
    return;
  }
  closeMemberDetail();
  mm("member-position-result").textContent =
    `Deleted the exact position for ${label}; an audit event was recorded.`;
  await loadMemberMap();
  window.dispatchEvent(new Event("outpost:member-position-changed"));
}

async function purgeExpiredPositions() {
  const phrase = "PURGE EXPIRED POSITIONS";
  const confirmation = await window.OutpostUI.prompt({
    title: "Purge expired positions?",
    message: "Physically remove every past-due exact member position now. This action is audit logged.",
    label: "Purge confirmation",
    verification: phrase,
    confirmLabel: "Purge expired positions",
    danger: true,
  });
  if (confirmation !== phrase) return;
  const response = await fetch("/api/v1/members/positions/purge-expired", {
    method: "POST",
    headers: {"content-type": "application/json", "x-csrf-token": await positionCsrf()},
    body: JSON.stringify({confirmation}),
  });
  const body = await response.json();
  if (!response.ok) {
    await window.OutpostUI.alert({
      title: "Positions not purged",
      message: body.error?.message || "Expired-position purge failed.",
    });
    return;
  }
  mm("member-position-result").textContent =
    `Purged ${body.deleted} expired position${body.deleted === 1 ? "" : "s"}; an audit event was recorded.`;
  await loadMemberMap();
  window.dispatchEvent(new Event("outpost:member-position-changed"));
}

function bindMemberRows() {
  document.querySelectorAll("[data-member-row]").forEach(row => {
    const value = memberMapItems.find(item => item.id === Number(row.dataset.memberRow));
    if (!value || row.querySelector(".member-map-row-open")) return;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "member-map-row-open";
    button.textContent = "Map";
    button.setAttribute(
      "aria-label",
      `Show ${value.handle ? `@${value.handle}` : value.mesh_id} on the members map`,
    );
    button.onclick = () => {
      memberMapController.setView({lat: value.lat, lon: value.lon, zoom: 15});
      showMember(value);
      mm("member-map").scrollIntoView({behavior: "smooth", block: "center"});
    };
    row.cells[row.cells.length - 1].prepend(button);
  });
}

mm("member-map-trust").onchange = fitMemberMap;
mm("member-map-age").onchange = fitMemberMap;
mm("member-map-purge-expired").onclick = purgeExpiredPositions;
new MutationObserver(bindMemberRows).observe(mm("member-rows"), {childList: true});
loadMemberMap();
