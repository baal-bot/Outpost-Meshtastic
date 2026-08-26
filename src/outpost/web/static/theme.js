const storageKey = "outpost.appearance.theme";
let corrections = document.querySelector('link[href^="/theme-corrections.css"]');
if (!corrections) {
  corrections = document.createElement("link");
  corrections.rel = "stylesheet";
  corrections.href = "/theme-corrections.css?v=29";
  document.head.appendChild(corrections);
}
if (!corrections.sheet) {
  await new Promise(resolve => {
    corrections.addEventListener("load", resolve, {once: true});
    corrections.addEventListener("error", resolve, {once: true});
  });
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
