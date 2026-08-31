import {byId as $, escapeHtml as safe} from "/ui-primitives.js";

const typeLabels = {
  general: "General",
  medical: "Medical",
  fire: "Fire",
  search: "Search & rescue",
  logistics: "Logistics",
  communications: "Communications",
  public_safety: "Public safety",
};

let csrfToken = "";
let groups = [];
let eligibleMembers = [];
let selectedGroupId = null;
let draftMemberIds = new Set();
let draftDirty = false;

function memberLabel(member) {
  if (member.handle) return `@${member.handle}`;
  return member.long_name || member.short_name || member.mesh_id;
}

function selectedGroup() {
  return groups.find(group => group.id === selectedGroupId) || null;
}

function membershipsFor(memberId) {
  return groups.filter(group => group.members.some(member => member.id === memberId));
}

function setResult(message, error = false) {
  const result = $("group-admin-result");
  result.textContent = message;
  result.classList.toggle("error", error);
}

async function groupApi(path, options = {}) {
  const method = options.method || "GET";
  const headers = {...(options.headers || {})};
  if (method !== "GET") {
    headers["content-type"] = "application/json";
    headers["x-csrf-token"] = csrfToken;
  }
  return fetch(path, {...options, method, headers});
}

async function responseError(response, fallback) {
  try {
    const body = await response.json();
    return body.error?.message || fallback;
  } catch (_) {
    return fallback;
  }
}

function resetDraft() {
  const group = selectedGroup();
  const eligibleIds = new Set(eligibleMembers.map(member => member.id));
  draftMemberIds = new Set(
    (group?.members || []).map(member => member.id).filter(id => eligibleIds.has(id)),
  );
  draftDirty = Boolean(
    group?.members.some(member => !eligibleIds.has(member.id)),
  );
}

function renderSummary() {
  const assigned = new Set(groups.flatMap(group => group.members.map(member => member.id)));
  const eligibleIds = new Set(eligibleMembers.map(member => member.id));
  $("group-admin-count").textContent = groups.length;
  $("group-admin-assigned").textContent = [...assigned].filter(id => eligibleIds.has(id)).length;
  $("group-admin-unassigned").textContent = eligibleMembers.filter(member => !assigned.has(member.id)).length;
  $("group-admin-memberships").textContent = groups.reduce(
    (count, group) => count + Number(group.member_count || group.members.length),
    0,
  );
  $("group-directory-count").textContent = groups.length;
}

function renderDirectory() {
  $("group-directory-list").innerHTML = groups.map(group => {
    const active = group.id === selectedGroupId;
    const label = typeLabels[group.response_type] || group.response_type;
    return `<button type="button" class="group-directory-item${active ? " active" : ""}" data-select-group="${safe(group.id)}" aria-pressed="${active}"><span class="group-directory-icon">✦</span><span><strong>${safe(group.name)}</strong><small>${safe(label)}</small></span><b>${safe(group.member_count)}<small> people</small></b></button>`;
  }).join("") || '<p class="ui-empty empty">No responder groups configured. Create the first response team above.</p>';
}

function assignmentChoice(member, group) {
  const checked = draftMemberIds.has(member.id);
  const memberGroups = membershipsFor(member.id);
  const query = $("group-member-search").value.trim().toLowerCase();
  const filter = $("group-member-filter").value;
  const searchable = [memberLabel(member), member.mesh_id, member.handle, member.long_name, member.short_name]
    .filter(Boolean).join(" ").toLowerCase();
  if (query && !searchable.includes(query)) return "";
  if (filter === "assigned" && !checked) return "";
  if (filter === "unassigned" && checked) return "";
  const otherGroups = memberGroups.filter(value => value.id !== group.id);
  const secondary = member.handle && member.long_name ? member.long_name : member.mesh_id;
  return `<label class="group-member-choice${checked ? " assigned" : ""}"><input type="checkbox" value="${safe(member.id)}" ${checked ? "checked" : ""}><span><strong>${safe(memberLabel(member))}</strong><small>${safe(secondary)} · ${safe(member.trust)}</small></span><em>${otherGroups.length ? safe(`${otherGroups.length} other group${otherGroups.length === 1 ? "" : "s"}`) : "No other groups"}</em></label>`;
}

function renderAssignments() {
  const group = selectedGroup();
  if (!group) return;
  const choices = eligibleMembers.map(member => assignmentChoice(member, group)).filter(Boolean);
  const eligibleIds = new Set(eligibleMembers.map(member => member.id));
  const ineligible = group.members.filter(member => !eligibleIds.has(member.id));
  const stale = ineligible.map(member => `<div class="group-member-choice ineligible"><span><strong>${safe(memberLabel(member))}</strong><small>${safe(member.mesh_id)} · ${safe(member.trust)}</small></span><em>No longer eligible · saving removes assignment</em></div>`);
  $("group-member-list").innerHTML = [...choices, ...stale].join("") || '<p class="ui-empty empty">No eligible responders match this filter.</p>';
  $("group-draft-count").textContent = `${draftMemberIds.size} selected`;
  $("group-save-members").disabled = !draftDirty;
}

function renderEditor() {
  const group = selectedGroup();
  $("group-editor-empty").hidden = Boolean(group);
  $("group-editor").hidden = !group;
  if (!group) return;
  const label = typeLabels[group.response_type] || group.response_type;
  $("group-editor-title").textContent = group.name;
  $("group-editor-meta").textContent = `${group.member_count} assigned · created by ${group.created_by || "operator"}`;
  $("group-editor-type").textContent = label;
  $("group-edit-name").value = group.name;
  $("group-edit-type").value = group.response_type;
  renderAssignments();
}

function render() {
  renderSummary();
  renderDirectory();
  renderEditor();
}

function selectGroup(groupId) {
  if (groupId === selectedGroupId) return;
  selectedGroupId = groupId;
  resetDraft();
  render();
}

function publishGroupChange() {
  window.dispatchEvent(new CustomEvent("outpost:responder-groups-changed", {detail: {groups}}));
}

async function loadGroups(preferredId = selectedGroupId) {
  const response = await fetch("/api/v1/responder-groups");
  if (!response.ok) {
    setResult(await responseError(response, "Responder groups could not be loaded."), true);
    return;
  }
  const body = await response.json();
  groups = body.items || [];
  eligibleMembers = body.eligible_members || [];
  selectedGroupId = groups.some(group => group.id === preferredId) ? preferredId : groups[0]?.id ?? null;
  resetDraft();
  render();
  publishGroupChange();
}

$("group-directory-list").addEventListener("click", event => {
  const button = event.target.closest("[data-select-group]");
  if (button) selectGroup(Number(button.dataset.selectGroup));
});

$("group-create-form").addEventListener("submit", async event => {
  event.preventDefault();
  const response = await groupApi("/api/v1/responder-groups", {
    method: "POST",
    body: JSON.stringify({
      name: $("group-create-name").value.trim(),
      response_type: $("group-create-type").value,
    }),
  });
  if (!response.ok) {
    setResult(await responseError(response, "Responder group could not be created."), true);
    return;
  }
  const created = await response.json();
  $("group-create-form").reset();
  setResult(`Created ${created.name}. Assign eligible responders in the group editor.`);
  await loadGroups(created.id);
});

$("group-metadata-form").addEventListener("submit", async event => {
  event.preventDefault();
  const group = selectedGroup();
  if (!group) return;
  const response = await groupApi(`/api/v1/responder-groups/${group.id}`, {
    method: "PATCH",
    body: JSON.stringify({
      name: $("group-edit-name").value.trim(),
      response_type: $("group-edit-type").value,
    }),
  });
  if (!response.ok) {
    setResult(await responseError(response, "Group details could not be saved."), true);
    return;
  }
  const updated = await response.json();
  setResult(`Saved ${updated.name}.`);
  await loadGroups(updated.id);
});

$("group-delete").addEventListener("click", async () => {
  const group = selectedGroup();
  if (!group || !await window.OutpostUI.confirm({
    title: `Delete ${group.name}?`,
    message: "Saved drill records keep their frozen rosters. Active schedules using this group are paused for review.",
    confirmLabel: "Delete responder group",
    danger: true,
  })) return;
  const response = await groupApi(`/api/v1/responder-groups/${group.id}`, {method: "DELETE"});
  if (!response.ok) {
    setResult(await responseError(response, "Responder group could not be deleted."), true);
    return;
  }
  setResult(`Deleted ${group.name}. Affected schedules were paused.`);
  await loadGroups(null);
});

$("group-member-list").addEventListener("change", event => {
  const checkbox = event.target.closest('input[type="checkbox"]');
  if (!checkbox) return;
  const memberId = Number(checkbox.value);
  checkbox.checked ? draftMemberIds.add(memberId) : draftMemberIds.delete(memberId);
  draftDirty = true;
  renderAssignments();
});

$("group-member-search").addEventListener("input", renderAssignments);
$("group-member-filter").addEventListener("change", renderAssignments);
$("group-save-members").addEventListener("click", async () => {
  const group = selectedGroup();
  if (!group) return;
  const response = await groupApi(`/api/v1/responder-groups/${group.id}/members`, {
    method: "PUT",
    body: JSON.stringify({member_ids: [...draftMemberIds].sort((left, right) => left - right)}),
  });
  if (!response.ok) {
    setResult(await responseError(response, "Group assignments could not be saved."), true);
    return;
  }
  const updated = await response.json();
  setResult(`Saved ${updated.members.length} assignment${updated.members.length === 1 ? "" : "s"} for ${updated.name}.`);
  await loadGroups(updated.id);
});

window.addEventListener("outpost:member-trust-changed", () => loadGroups());

async function initialize() {
  const session = await fetch("/api/v1/auth/session");
  if (!session.ok) return;
  csrfToken = (await session.json()).csrf_token;
  await loadGroups();
}

initialize();
