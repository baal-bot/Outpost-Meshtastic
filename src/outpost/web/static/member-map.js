import {byId as mm, escapeHtml as escapeMap, formatAgeSeconds} from "/ui-primitives.js";

const memberMapPanel = document.createElement("section");
memberMapPanel.className = "ui-card panel member-map-panel";
memberMapPanel.innerHTML = `
  <div class="member-map-heading">
    <div><p class="eyebrow">RETAINED RADIO POSITIONS</p><h2>Members & discoveries map</h2></div>
    <div class="member-map-filters">
      <select id="member-map-category" aria-label="Filter member map by identity type">
        <option value="approved">Approved members</option>
        <option value="discovered">Discovered radios</option>
        <option value="all">All positioned radios</option>
      </select>
      <select id="member-map-trust" aria-label="Filter member map by trust level">
        <option value="all">All trust levels</option><option value="guest">Guests</option>
        <option value="blocked">Blocked</option><option value="member">Members</option>
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
  <p class="member-privacy-notice">Operator view shows retained coordinates for approved members and radios that broadcast a position on the mesh. A discovered location is observational and does not make that radio a member. Expired positions are hidden immediately and physically removed by maintenance; member-facing POS responses still honor each member’s visibility preference.</p>
  <div class="member-map-legend" aria-label="Member map marker legend"><span><i class="regular"></i>Member</span><span><i class="grouped">✦</i>Response team member</span><span><i class="discovered"></i>Discovered radio</span></div>
  <p id="member-position-result" class="member-position-result" aria-live="polite"></p>
  <div id="member-map" class="outpost-map member-position-map" tabindex="0" aria-label="Interactive radio map. Use arrow keys to pan, plus and minus to zoom, and zero to fit visible radios.">
    <div id="member-map-tiles" class="outpost-map-tiles"></div>
    <div id="member-map-markers" class="outpost-map-markers"></div>
    <div class="ui-map-controls outpost-map-controls">
      <button id="member-map-in" data-map-action="zoom-in" title="Zoom in" aria-label="Zoom in">+</button>
      <button id="member-map-out" data-map-action="zoom-out" title="Zoom out" aria-label="Zoom out">−</button>
      <button id="member-map-fit" data-map-action="fit" title="Fit visible radios" aria-label="Fit visible radios">⌖</button>
    </div>
    <span id="member-map-coordinates" class="outpost-map-coordinates">—</span>
    <aside id="member-map-detail" class="outpost-map-detail" hidden></aside>
    <p id="member-map-empty" class="outpost-map-empty">No retained radio positions match these filters.</p>
    <div id="member-map-attribution" class="outpost-map-attribution"></div>
  </div>`;
document.querySelector("#member-directory").after(memberMapPanel);

const positionAge = seconds => formatAgeSeconds(seconds, {
  immediate: "just now",
  suffix: " ago",
});

let memberMapItems = [];

function visibleMembers() {
  const category = mm("member-map-category").value;
  const trust = mm("member-map-trust").value;
  const cutoff = Date.now() - Number(mm("member-map-age").value) * 3600000;
  return memberMapItems.filter(value =>
    (category === "all" || value.category === category) &&
    (trust === "all" || value.trust === trust) &&
    new Date(value.received_at).getTime() >= cutoff
  );
}

function radioLabel(value) {
  if (value.handle) return `@${value.handle}`;
  if (value.long_name) return `${value.long_name} (${value.mesh_id})`;
  if (value.short_name) return `${value.short_name} (${value.mesh_id})`;
  return value.mesh_id;
}

function responderGroups(value) {
  return Array.isArray(value.responder_groups) ? value.responder_groups : [];
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
  const discovered = value.category === "discovered";
  const source = value.source === "position_app" ?
    `Meshtastic position ${discovered ? "broadcast" : "share"}` : value.source;
  const groups = responderGroups(value);
  const groupMarkup = groups.length
    ? `<div class="member-map-groups"><b>Response teams</b><div>${groups.map(group => `<span>${escapeMap(group.name)}</span>`).join("")}</div></div>`
    : "";
  detail.hidden = false;
  detail.innerHTML = `
    <button class="outpost-map-detail-close" aria-label="Close">×</button>
    <p class="eyebrow">${discovered ? "DISCOVERED RADIO" : `${escapeMap(value.trust.toUpperCase())} MEMBER`}</p>
    <h3>${escapeMap(radioLabel(value))}</h3>
    <p>${Number(value.lat).toFixed(5)}, ${Number(value.lon).toFixed(5)}</p>
    <p><b>${discovered ? "Received" : "Shared"}</b> ${new Date(value.received_at).toLocaleString()} · ${positionAge(value.age_seconds)}</p>
    <p><b>Source</b> ${escapeMap(source)}</p>
    <p><b>Visibility</b> ${escapeMap(value.visibility)}</p>
    <p><b>Scheduled deletion</b> ${new Date(value.expires_at).toLocaleString()} · ${Math.max(1, Math.ceil(value.deletes_in_seconds / 3600))}h remaining</p>
    <p><b>Last heard</b> ${new Date(value.last_seen).toLocaleString()} · ${value.last_heard_snr ?? "—"} dB · ${value.hops_away ?? "—"} hops</p>
    ${groupMarkup}
    <button class="member-map-danger delete-position" type="button">Delete exact position</button>`;
  detail.querySelector(".outpost-map-detail-close").onclick = closeMemberDetail;
  detail.querySelector(".delete-position").onclick = () => deleteMemberPosition(value);
}

function renderMemberMap() {
  const values = visibleMembers();
  memberMapController.setMarkers(values.map(value => {
    const groups = responderGroups(value);
    const grouped = value.category !== "discovered" && groups.length > 0;
    return {
      id: `member-${value.id}`,
      lat: value.lat,
      lon: value.lon,
      className: value.category === "discovered" ? "shape-diamond tone-discovered" : grouped
        ? "shape-group tone-grouped"
        : `shape-circle ${["trusted", "responder", "operator"].includes(value.trust) ? "tone-trusted" : "tone-member"}`,
      title: grouped ? `${radioLabel(value)} · ${groups.map(group => group.name).join(", ")}` : radioLabel(value),
      label: grouped ? `Show response team member ${radioLabel(value)} on the radio map` : `Show ${radioLabel(value)} on the radio map`,
      data: value,
      onActivate: showMember,
    };
  }));
  memberMapController.setEmpty(values.length === 0);
}

function fitMemberMap() {
  const values = visibleMembers();
  renderMemberMap();
  if (values.length) memberMapController.fit(values, {maxZoom: 14, padding: 60});
}

async function loadMemberMap() {
  const response = await fetch("/api/v1/members/map?view=all");
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
      `Show ${radioLabel(value)} on the radio map`,
    );
    button.onclick = () => {
      mm("member-map-category").value = value.category;
      mm("member-map-trust").value = "all";
      renderMemberMap();
      memberMapController.setView({lat: value.lat, lon: value.lon, zoom: 15});
      showMember(value);
      mm("member-map").scrollIntoView({behavior: "smooth", block: "center"});
    };
    row.cells[row.cells.length - 1].prepend(button);
  });
}

mm("member-map-category").onchange = () => {
  mm("member-map-trust").value = "all";
  fitMemberMap();
};
mm("member-map-trust").onchange = fitMemberMap;
mm("member-map-age").onchange = fitMemberMap;
mm("member-map-purge-expired").onclick = purgeExpiredPositions;
new MutationObserver(bindMemberRows).observe(mm("member-rows"), {childList: true});
window.addEventListener("outpost:responder-groups-changed", loadMemberMap);
loadMemberMap();
