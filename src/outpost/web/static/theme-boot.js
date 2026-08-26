(() => {
  const preference = localStorage.getItem("outpost.appearance.theme") || "system";
  const allowed = new Set(["system", "dark", "daylight", "night"]);
  const selected = allowed.has(preference) ? preference : "system";
  const resolved = selected === "system"
    ? (window.matchMedia("(prefers-color-scheme: light)").matches ? "daylight" : "dark")
    : selected;
  document.documentElement.dataset.themePreference = selected;
  document.documentElement.dataset.theme = resolved;
  document.documentElement.style.colorScheme = selected === "system"
    ? "light dark"
    : selected === "daylight" ? "light" : "dark";
})();
