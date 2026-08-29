import {initTheme} from "/theme.js";
import "/a11y.js";
import {scheduler} from "/refresh-scheduler.js";

initTheme();
const nativeFetch = window.fetch.bind(window);
let stepUpPrompt = null;

async function confirmOperatorCredentials(mfaRequired) {
  const password = await window.OutpostUI?.prompt({
    eyebrow: "SECURITY CHECK",
    title: "Confirm operator credentials",
    message: "This action changes protected Outpost state. Confirmation remains valid for 10 minutes.",
    label: "Account password",
    type: "password",
    autocomplete: "current-password",
    confirmLabel: "Confirm identity",
  });
  if (!password) return false;
  let code = null;
  if (mfaRequired) {
    code = await window.OutpostUI?.prompt({
      eyebrow: "SECOND FACTOR",
      title: "Enter a verification code",
      message: "Use your authenticator app or one unused recovery code.",
      label: "6-digit or recovery code",
      autocomplete: "one-time-code",
      confirmLabel: "Verify",
    });
    if (!code) return false;
  }
  const sessionResponse = await nativeFetch("/api/v1/auth/session", {cache: "no-store"});
  if (!sessionResponse.ok) return false;
  const session = await sessionResponse.json();
  const response = await nativeFetch("/api/v1/auth/step-up", {
    method: "POST",
    headers: {"content-type": "application/json", "x-csrf-token": session.csrf_token},
    body: JSON.stringify({password, code}),
  });
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    await window.OutpostUI?.alert({
      eyebrow: "VERIFICATION FAILED",
      title: "Credentials were not confirmed",
      message: detail.error?.message || "Check the password and verification code, then try again.",
    });
    return false;
  }
  return true;
}

window.fetch = async (input, init) => {
  const request = new Request(input, init);
  const retry = request.clone();
  const response = await nativeFetch(request);
  const url = new URL(request.url, window.location.href);
  if (response.status === 403 && url.origin === window.location.origin) {
    const detail = await response.clone().json().catch(() => ({}));
    if (detail.error?.code === "read_only") {
      void window.OutpostUI?.alert({
        eyebrow: "READ-ONLY ACCOUNT",
        title: "This wallboard cannot make changes",
        message: detail.error.message,
      });
    }
  }
  if (
    response.status !== 428
    || url.origin !== window.location.origin
    || !url.pathname.startsWith("/api/v1/")
    || url.pathname === "/api/v1/auth/step-up"
  ) return response;
  const detail = await response.clone().json().catch(() => ({}));
  stepUpPrompt ||= confirmOperatorCredentials(Boolean(detail.mfa_required))
    .finally(() => { stepUpPrompt = null; });
  const verified = await stepUpPrompt;
  return verified ? nativeFetch(retry) : response;
};

const path = window.location.pathname;
if (!document.querySelector('link[rel="icon"]')) {
  document.head.insertAdjacentHTML("beforeend", '<link rel="icon" href="/favicon.svg">');
}
const accessibleNames = {
  "member-map-category": "Filter member map by identity type",
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
  ["/sitrep.html", "◈", "Sitrep"],
  ["/environment.html", "☼", "Environment"],
  ["/radio.html", "⌁", "Radio"],
  ["/federation.html", "⤨", "Federation"],
  ["/access.html", "♜", "Access"],
  ["/backups.html", "▣", "Backups"],
  ["/#activity", "◫", "Activity"],
  ["/#system", "⚙", "System"],
  ["/ai.html", "✦", "AI"],
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

async function showTransportBoundary(role) {
  if (role === "viewer" || document.querySelector(".web-transport-banner")) return;
  const response = await nativeFetch("/api/v1/web/transport", {cache: "no-store"});
  if (!response.ok) return;
  const transport = await response.json();
  if (!transport.warning) return;
  const main = document.querySelector("main");
  if (!main) return;
  const banner = document.createElement("aside");
  banner.className = "web-transport-banner";
  banner.setAttribute("role", "status");
  const title = document.createElement("b");
  title.textContent = transport.warning.title;
  const message = document.createElement("span");
  message.textContent = transport.warning.message;
  const link = document.createElement("a");
  link.href = "/api/v1/web/transport";
  link.textContent = "Transport status →";
  banner.append(title, message, link);
  main.prepend(banner);
}

async function showOperatorIdentity() {
  try {
    const response = await nativeFetch("/api/v1/auth/session", {cache: "no-store"});
    if (!response.ok) return;
    const session = await response.json();
    document.body.dataset.operatorRole = session.role || "operator";
    const footer = document.querySelector(".rail-foot");
    if (footer && !footer.querySelector(".operator-role-chip")) {
      const chip = document.createElement("a");
      chip.className = "operator-role-chip";
      chip.href = "/access.html";
      const name = document.createElement("b");
      name.textContent = session.display_name || session.username || "Operator";
      const role = document.createElement("small");
      role.textContent = session.role === "viewer"
        ? "Read-only / wallboard"
        : session.role || "Operator";
      chip.append(name, role);
      footer.prepend(chip);
    }
    if (session.role === "viewer" && !document.querySelector(".read-only-banner")) {
      for (const link of navigation?.querySelectorAll("a") || []) {
        if (new URL(link.href).pathname !== "/") link.remove();
      }
      const main = document.querySelector("main");
      main?.insertAdjacentHTML("afterbegin", '<div class="read-only-banner" role="status"><b>Aggregate wallboard</b><span>Identities, message content, locations, welfare, mail, configuration, and operator data are not available to this display.</span></div>');
    } else {
      await showTransportBoundary(session.role);
    }
  } catch (_) {
    // Identity presentation is optional while the authenticated page remains usable.
  }
}

const operatorIdentity = showOperatorIdentity();
window.addEventListener("outpost:authenticated", showOperatorIdentity);

const moduleLinks = {
  BBS: "bbs",
  Watch: "watch",
  Environment: "env",
  Federation: "fed",
  AI: "ai",
};
const modulePages = {
  "/ai.html": "ai",
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
let navigationStatus = null;
let navigationStatusEtag = "";
let navigationStatusRequest = null;

async function loadNavigationStatus() {
  if (navigationStatusRequest) return navigationStatusRequest;
  navigationStatusRequest = (async () => {
    const viewer = document.body.dataset.operatorRole === "viewer";
    const response = await fetch(
      viewer ? "/api/v1/wallboard/summary" : "/api/v1/dashboard/poll",
      {
      headers: navigationStatusEtag ? {"if-none-match": navigationStatusEtag} : {},
      },
    );
    if (response.status === 304) return navigationStatus;
    if (!response.ok) throw new Error(`navigation status ${response.status}`);
    navigationStatusEtag = response.headers.get("etag") || "";
    const value = await response.json();
    navigationStatus = viewer ? value.navigation : value;
    return navigationStatus;
  })();
  try {
    return await navigationStatusRequest;
  } finally {
    navigationStatusRequest = null;
  }
}

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
    effectiveModules = (await loadNavigationStatus()).modules.items;
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
operatorIdentity.then(refreshModuleState);

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

const reviewLabel = (count) => count > 99 ? "99+" : String(count);

function setReviewBadge(href, count, label = "pending reviews") {
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
  badge.setAttribute("aria-label", `${count} ${label}`);
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
    const navigationState = await loadNavigationStatus();
    const reviews = navigationState.reviews;
    const counts = {board: reviews.board, incidents: reviews.incidents, alerts: reviews.alerts};
    const localIncidentReviews = Number(
      navigationState.watch?.incidents_pending_review || 0,
    );
    setReviewBadge("/federation.html", reviews.total);
    setReviewBadge("/bbs.html", counts.board);
    setReviewBadge(
      "/watch.html",
      localIncidentReviews + counts.incidents + counts.alerts,
      "Watch items pending operator review",
    );
    setReviewBadge(
      "/operator.html",
      Number(reviews.members || 0),
      "authenticated radio keys pending review",
    );
    setReviewBadge(
      "/environment.html",
      Number(navigationState.environment?.same_pending || 0),
      "SAME alerts pending review",
    );
    const target = reviewTargets[path];
    if (target) {
      const kinds = [...target].filter(kind => counts[kind]).map(kind => kind === "incidents" ? "incident" : kind === "alerts" ? "alert" : "BBS item");
      renderReviewCallout([...target].reduce((total, kind) => total + counts[kind], 0), kinds);
    }
  } catch (_) {
    // Navigation remains usable while the backend reconnects.
  }
}

operatorIdentity.then(refreshFederationReviews);
window.addEventListener("outpost:federation-reviewed", refreshFederationReviews);
window.addEventListener("outpost:reviews-updated", refreshFederationReviews);

async function refreshOperationsInboxBadge() {
  try {
    const count = Number((await loadNavigationStatus()).mail.actionable || 0);
    setReviewBadge("/mail.html", count, "actionable mail conversations");
  } catch (_) {
    // Navigation remains usable while the backend reconnects.
  }
}

operatorIdentity.then(refreshOperationsInboxBadge);
window.addEventListener("outpost:mail-updated", refreshOperationsInboxBadge);
scheduler.schedule(
  "navigation-status",
  () => sessionStorage.getItem("outpost.operator.authenticated") === "true"
    ? Promise.all([refreshModuleState(), refreshFederationReviews(), refreshOperationsInboxBadge()])
    : undefined,
  {interval: 30000},
);
