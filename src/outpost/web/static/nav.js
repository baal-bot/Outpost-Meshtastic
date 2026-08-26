import {initTheme} from "/theme.js";

initTheme();
const path = window.location.pathname;
if (!document.querySelector('link[rel="icon"]')) {
  document.head.insertAdjacentHTML("beforeend", '<link rel="icon" href="/favicon.svg">');
}
const accessibleNames = {
  "member-map-trust": "Filter member map by trust level",
  "member-map-age": "Filter member map by position age",
  "member-view": "Filter member directory",
  "state-filter": "Filter mail by state",
  "event-name": "Watch event name",
  "event-policy": "Watch event recipient policy",
  "map-time": "Incident map time range",
  "report-text": "Incident report details",
  "alert-severity": "Alert severity",
  "alert-incident": "Linked incident",
  "alert-channel": "Alert channel",
  "alert-headline": "Alert headline",
  "type-filter": "Filter incidents by type",
  "send-text": "Radio message",
  "filter-direction": "Filter messages by direction",
  "filter-channel": "Filter messages by channel",
  "peer-filter": "Filter federation peers",
  "service-type": "Peer service type",
  "service-query": "Peer service question",
  "relay-mail-peer": "Mail relay peer",
  "relay-mail-recipient": "Mail relay recipient",
  "relay-mail-subject": "Mail relay subject",
  "relay-mail-body": "Encrypted relay message",
};
const applyAccessibleNames = () => {
  for (const [id, label] of Object.entries(accessibleNames)) {
    document.getElementById(id)?.setAttribute("aria-label", label);
  }
};
applyAccessibleNames();
new MutationObserver(applyAccessibleNames).observe(document.body, {childList: true, subtree: true});
const links = [
  ["/", "⌂", "Overview"],
  ["/operator.html", "♙", "Members"],
  ["/bbs.html", "◎", "BBS"],
  ["/mail.html", "✉", "Mail"],
  ["/watch.html", "△", "Watch"],
  ["/environment.html", "☼", "Environment"],
  ["/radio.html", "⌁", "Radio"],
  ["/federation.html", "⤨", "Federation"],
  ["/backups.html", "▣", "Backups"],
  ["/#activity", "◫", "Activity"],
  ["/#system", "⚙", "System"],
  ["/#system", "✦", "AI"],
  ["/api/docs", "◇", "API"],
];
const navigation = document.querySelector(".rail nav");
if (navigation) {
  navigation.innerHTML = links.map(([href, icon, label]) => {
    const active = (path === "/" && href === "/") || path === href;
    return `<a ${active ? 'class="active"' : ""} href="${href}" aria-label="${label}" title="${label}"><i aria-hidden="true">${icon}</i><span>${label}</span></a>`;
  }).join("");
}

const reviewTargets = {
  "/bbs.html": new Set(["board"]),
  "/watch.html": new Set(["incidents", "alerts"]),
  "/federation.html": null,
};

const reviewArea = (stream) => stream.startsWith("board:") ? "board" : stream;
const reviewLabel = (count) => count > 99 ? "99+" : String(count);

function setReviewBadge(href, count) {
  const link = navigation?.querySelector(`a[href="${href}"]`);
  if (!link) return;
  let badge = link.querySelector(".nav-review-badge");
  if (!count) {
    badge?.remove();
    link.classList.remove("needs-review");
    return;
  }
  if (!badge) {
    badge = document.createElement("b");
    badge.className = "nav-review-badge";
    link.appendChild(badge);
  }
  badge.textContent = reviewLabel(count);
  badge.setAttribute("aria-label", `${count} pending review${count === 1 ? "" : "s"}`);
  link.classList.add("needs-review");
}

function renderReviewCallout(count, kinds) {
  document.querySelector(".federation-review-callout")?.remove();
  if (!count || !["/bbs.html", "/watch.html"].includes(path)) return;
  const main = document.querySelector("main");
  if (!main) return;
  const noun = kinds.length === 1 ? kinds[0] : "federated records";
  main.insertAdjacentHTML("afterbegin", `<a class="federation-review-callout" href="/federation.html#federation-inbox"><span><b>${reviewLabel(count)}</b><i>Operator review required</i></span><strong>${count} ${noun} ${count === 1 ? "is" : "are"} waiting in the federation approval queue.</strong><em>Review now →</em></a>`);
}

async function refreshFederationReviews() {
  try {
    const response = await fetch("/api/v1/federation/inbox?state=pending");
    if (!response.ok) return;
    const items = (await response.json()).items || [];
    const counts = {board: 0, incidents: 0, alerts: 0};
    for (const item of items) {
      const area = reviewArea(String(item.stream || ""));
      if (area in counts) counts[area] += 1;
    }
    setReviewBadge("/federation.html", items.length);
    setReviewBadge("/bbs.html", counts.board);
    setReviewBadge("/watch.html", counts.incidents + counts.alerts);
    const target = reviewTargets[path];
    if (target) {
      const kinds = [...target].filter(kind => counts[kind]).map(kind => kind === "incidents" ? "incident" : kind === "alerts" ? "alert" : "BBS item");
      renderReviewCallout([...target].reduce((total, kind) => total + counts[kind], 0), kinds);
    }
  } catch (_) {
    // Navigation remains usable while the backend reconnects.
  }
}

refreshFederationReviews();
setInterval(refreshFederationReviews, 30000);
window.addEventListener("outpost:federation-reviewed", refreshFederationReviews);
