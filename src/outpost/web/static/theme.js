const storageKey = "outpost.appearance.theme";
if (!document.querySelector('link[href^="/theme-corrections.css"]')) {
  document.head.insertAdjacentHTML("beforeend", '<link rel="stylesheet" href="/theme-corrections.css?v=3">');
}
const allowed = new Set(["system", "dark", "daylight", "night"]);
const media = window.matchMedia("(prefers-color-scheme: light)");

const resolvedTheme = (preference) => preference === "system"
  ? (media.matches ? "daylight" : "dark")
  : preference;

const apply = (preference, persist = true) => {
  const value = allowed.has(preference) ? preference : "system";
  document.documentElement.dataset.themePreference = value;
  document.documentElement.dataset.theme = resolvedTheme(value);
  document.documentElement.style.colorScheme = value === "system" ? "light dark" : value === "daylight" ? "light" : "dark";
  if (persist) localStorage.setItem(storageKey, value);
  window.dispatchEvent(new CustomEvent("outpost:theme", {detail: {preference: value, resolved: document.documentElement.dataset.theme}}));
  return value;
};

const current = () => {
  const value = localStorage.getItem(storageKey) || "system";
  return allowed.has(value) ? value : "system";
};

export const initTheme = () => apply(current(), false);
export const setTheme = (value) => apply(value, true);
export const getTheme = current;

media.addEventListener("change", () => {
  if (current() === "system") apply("system", false);
});

window.OutpostTheme = {init: initTheme, set: setTheme, get: getTheme};
initTheme();
