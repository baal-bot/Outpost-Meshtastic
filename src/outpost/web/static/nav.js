import {initTheme} from "/theme.js";
import "/a11y.js";

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
const applyHeadingActions = () => {
  for (const heading of document.querySelectorAll(".heading")) {
    [...heading.children].slice(1).forEach(actions => {
      actions.classList.add("heading-actions");
    });
  }
};
const applySharedEnhancements = () => {
  applyAccessibleNames();
  applyHeadingActions();
};
applySharedEnhancements();
new MutationObserver(applySharedEnhancements).observe(document.body, {
  childList: true,
  subtree: true,
});
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
  navigation.id = "primary-navigation";
  navigation.setAttribute("aria-label", "Primary navigation");
  navigation.innerHTML = links.map(([href, icon, label]) => {
    return `<a href="${href}" aria-label="${label}" title="${label}"><i aria-hidden="true">${icon}</i><span>${label}</span></a>`;
  }).join("");
}

const moduleLinks = {
  BBS: "bbs",
  Watch: "watch",
  Environment: "env",
  Federation: "fed",
  AI: "ai",
};
const modulePages = {
  "/bbs.html": "bbs",
  "/watch.html": "watch",
  "/environment.html": "env",
  "/federation.html": "fed",
};
const moduleNames = {
  bbs: "Community boards",
  watch: "Community Watch",
  env: "Environment",
  fed: "Federation",
  ai: "Local AI",
};
const capabilityModules = {
  Moderation: "bbs",
  "Emergency settings": "watch",
  "AI settings": "ai",
};
let effectiveModules = null;

function applyModuleState() {
  if (!effectiveModules) return;
  for (const [label, module] of Object.entries(moduleLinks)) {
    const link = navigation?.querySelector(`a[aria-label="${label}"]`);
    if (!link) continue;
    const disabled = effectiveModules[module]?.enabled === false;
    link.classList.toggle("module-disabled", disabled);
    if (disabled) link.setAttribute("aria-disabled", "true");
    else link.removeAttribute("aria-disabled");
    link.title = disabled
      ? `${moduleNames[module]} is disabled · restart required to enable`
      : label;
  }

  for (const card of document.querySelectorAll(".capability-grid article")) {
    const module = capabilityModules[card.querySelector("b")?.textContent?.trim()];
    if (!module) continue;
    const disabled = effectiveModules[module]?.enabled === false;
    card.classList.toggle("module-disabled", disabled);
    const phase = card.querySelector(".phase");
    if (phase && !phase.dataset.enabledLabel) phase.dataset.enabledLabel = phase.textContent;
    const phaseLabel = disabled ? "DISABLED" : phase?.dataset.enabledLabel;
    if (phase && phase.textContent !== phaseLabel) phase.textContent = phaseLabel;
    for (const control of card.querySelectorAll("button, input, select, textarea, a")) {
      if (control.matches("a")) {
        if (disabled) control.setAttribute("aria-disabled", "true");
        else control.removeAttribute("aria-disabled");
        control.tabIndex = disabled ? -1 : 0;
      } else {
        control.disabled = disabled;
      }
    }
  }

  const pageModule = modulePages[path];
  const disabled = pageModule && effectiveModules[pageModule]?.enabled === false;
  document.body.classList.toggle("module-disabled-page", Boolean(disabled));
  const main = document.querySelector("main");
  if (!main) return;
  let banner = document.querySelector(".module-disabled-banner");
  if (disabled && !banner) {
    banner = document.createElement("section");
    banner.className = "module-disabled-banner";
    banner.setAttribute("role", "status");
    banner.innerHTML = `<div><p class="eyebrow">MODULE DISABLED</p><h2>${moduleNames[pageModule]} is offline</h2></div><p>Its commands, background work, federation exchange, and API are inactive. Enable <code>modules.${pageModule}.enabled</code> in the Outpost configuration and restart the service.</p>`;
    main.prepend(banner);
  } else if (!disabled) {
    banner?.remove();
  }
  for (const section of main.children) {
    if (!section.classList.contains("module-disabled-banner")) {
      section.toggleAttribute("inert", Boolean(disabled));
    }
  }
}

async function refreshModuleState() {
  try {
    const response = await fetch("/api/v1/modules");
    if (!response.ok) return;
    effectiveModules = (await response.json()).items;
    applyModuleState();
  } catch (_) {
    // The existing page remains usable while the backend reconnects.
  }
}

navigation?.addEventListener("click", event => {
  if (event.target.closest("a[aria-disabled='true']")) {
    event.preventDefault();
    event.stopImmediatePropagation();
  }
}, true);
new MutationObserver(applyModuleState).observe(document.body, {childList: true, subtree: true});
refreshModuleState();

function updateCurrentPage() {
  if (!navigation) return;
  const candidates = [...navigation.querySelectorAll("a")];
  let current = null;
  if (window.location.hash) {
    current = candidates.find(link => {
      const target = new URL(link.href);
      return target.pathname === path && target.hash === window.location.hash;
    });
  }
  current ||= candidates.find(link => {
    const target = new URL(link.href);
    return target.pathname === path && !target.hash;
  });
  for (const link of candidates) {
    const active = link === current;
    link.classList.toggle("active", active);
    if (active) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
}

updateCurrentPage();
window.addEventListener("hashchange", updateCurrentPage);

const rail = document.querySelector(".rail");
const mobileNavigation = window.matchMedia("(max-width: 820px)");
let navigationOpen = false;
let navigationToggle = null;
let navigationBackdrop = null;

function setNavigationOpen(open, restoreFocus = false) {
  if (!navigation || !navigationToggle || !navigationBackdrop) return;
  navigationOpen = Boolean(open && mobileNavigation.matches);
  navigation.classList.toggle("mobile-open", navigationOpen);
  navigationToggle.classList.toggle("active", navigationOpen);
  navigationToggle.setAttribute("aria-expanded", String(navigationOpen));
  navigationToggle.setAttribute(
    "aria-label",
    navigationOpen ? "Close navigation" : "Open navigation",
  );
  navigationToggle.querySelector("span").textContent = navigationOpen ? "Close" : "Menu";
  navigationBackdrop.hidden = !navigationOpen;
  document.body.classList.toggle("mobile-nav-open", navigationOpen);
  document.querySelector(".shell")?.toggleAttribute("inert", navigationOpen);
  if (navigationOpen) {
    window.requestAnimationFrame(() => {
      (navigation.querySelector("a[aria-current='page']") || navigation.querySelector("a"))?.focus();
    });
  } else if (restoreFocus) {
    navigationToggle.focus();
  }
}

if (navigation && rail) {
  navigationToggle = document.createElement("button");
  navigationToggle.type = "button";
  navigationToggle.className = "mobile-nav-toggle";
  navigationToggle.setAttribute("aria-controls", navigation.id);
  navigationToggle.setAttribute("aria-expanded", "false");
  navigationToggle.setAttribute("aria-label", "Open navigation");
  navigationToggle.innerHTML = '<span>Menu</span><i aria-hidden="true"><b></b><b></b><b></b></i>';
  rail.insertBefore(navigationToggle, navigation);

  navigationBackdrop = document.createElement("div");
  navigationBackdrop.className = "mobile-nav-backdrop";
  navigationBackdrop.hidden = true;
  navigationBackdrop.setAttribute("aria-hidden", "true");
  document.body.appendChild(navigationBackdrop);

  navigationToggle.addEventListener("click", () => setNavigationOpen(!navigationOpen));
  navigationBackdrop.addEventListener("click", () => setNavigationOpen(false, true));
  navigation.addEventListener("click", event => {
    if (event.target.closest("a")) setNavigationOpen(false);
  });
  mobileNavigation.addEventListener("change", () => setNavigationOpen(false));
  document.addEventListener("keydown", event => {
    if (!navigationOpen) return;
    if (event.key === "Escape") {
      event.preventDefault();
      setNavigationOpen(false, true);
      return;
    }
    if (event.key !== "Tab") return;
    const items = [...navigation.querySelectorAll("a:not([aria-disabled='true'])")];
    if (!items.length) return;
    const first = items[0];
    const last = items.at(-1);
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  });
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
