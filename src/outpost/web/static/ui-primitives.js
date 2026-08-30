export const byId = (id) => document.getElementById(id);

export function escapeHtml(value) {
  return String(value ?? "").replace(
    /[&<>'"]/g,
    (character) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      "'": "&#39;",
      '"': "&quot;",
    })[character],
  );
}

export function safeLocalHref(value) {
  return typeof value === "string"
    && value.startsWith("/")
    && !value.startsWith("//")
    && !/[\\\u0000-\u0020]/.test(value)
    ? value
    : "#";
}

export function formatAgeSeconds(
  value,
  {
    empty = "never",
    immediate = "now",
    immediateSeconds = 60,
    prefix = "",
    showSeconds = false,
    suffix = "",
  } = {},
) {
  if (value == null || value === "") return empty;
  const seconds = Math.max(0, Number(value));
  if (!Number.isFinite(seconds)) return empty;
  if (seconds < immediateSeconds) return immediate;
  if (showSeconds && seconds < 60) return `${prefix}${Math.floor(seconds)}s${suffix}`;
  if (seconds < 3600) return `${prefix}${Math.floor(seconds / 60)}m${suffix}`;
  if (seconds < 86400) return `${prefix}${Math.floor(seconds / 3600)}h${suffix}`;
  return `${prefix}${Math.floor(seconds / 86400)}d${suffix}`;
}

export function relativeAge(
  value,
  {
    epochSeconds = false,
    empty = "never",
    immediate = "now",
    immediateSeconds = 60,
    showSeconds = false,
    suffix = "",
  } = {},
) {
  if (!value) return empty;
  const stamp = epochSeconds ? Number(value) * 1000 : value;
  return formatAgeSeconds((Date.now() - new Date(stamp).getTime()) / 1000, {
    empty,
    immediate,
    immediateSeconds,
    showSeconds,
    suffix,
  });
}

export function apiFetch(url, options = {}, csrfToken = "") {
  const method = String(options.method || "GET").toUpperCase();
  const headers = new Headers(options.headers || {});
  if (options.body && !headers.has("content-type")) {
    headers.set("content-type", "application/json");
  }
  if (!["GET", "HEAD"].includes(method) && csrfToken && !headers.has("x-csrf-token")) {
    headers.set("x-csrf-token", csrfToken);
  }
  return fetch(url, {
    ...options,
    headers,
  });
}

export async function apiJson(url, options = {}, csrfToken = "") {
  const response = await apiFetch(url, options, csrfToken);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(body.error?.message || `Request failed (${response.status})`);
  }
  return body;
}

export function createApiClient(csrfToken, {json = false} = {}) {
  const token = () => typeof csrfToken === "function" ? csrfToken() : csrfToken;
  return (url, options = {}) => (json ? apiJson : apiFetch)(url, options, token());
}
