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
