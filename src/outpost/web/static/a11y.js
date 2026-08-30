const focusableSelector = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "[tabindex]:not([tabindex='-1'])",
].join(",");

const visibleFocusable = (surface) => [...surface.querySelectorAll(focusableSelector)]
  .filter(element => !element.hidden && element.getClientRects().length > 0);

let generatedId = 0;
const ensureId = (element, prefix) => {
  if (!element.id) element.id = `${prefix}-${++generatedId}`;
  return element.id;
};

function labelDialog(surface) {
  const visible = elements => elements.find(element => element.getClientRects().length > 0) || elements[0];
  const heading = visible([...surface.querySelectorAll("h1, h2, h3")]);
  const description = visible([...surface.querySelectorAll(".login-copy, form > p:not(.eyebrow), header p:last-child")]);
  if (heading) {
    surface.setAttribute("aria-labelledby", ensureId(heading, "dialog-title"));
  }
  if (description) {
    surface.setAttribute("aria-describedby", ensureId(description, "dialog-description"));
  }
}

const overlays = new Set();

function syncOverlayBackground() {
  const open = [...overlays].some(surface => !surface.hidden && !surface.classList.contains("hidden"));
  document.querySelector(".rail")?.toggleAttribute("inert", open);
  document.querySelector(".shell")?.toggleAttribute("inert", open);
}

function enhanceOverlay(surface) {
  if (surface.dataset.accessibleDialog === "true") return;
  surface.dataset.accessibleDialog = "true";
  surface.setAttribute("role", "dialog");
  surface.setAttribute("aria-modal", "true");
  labelDialog(surface);
  overlays.add(surface);
  let wasOpen = false;
  let returnFocus = null;
  const closeButton = surface.querySelector(".close");
  const sync = () => {
    const open = !surface.hidden && !surface.classList.contains("hidden");
    const becameOpen = open && !wasOpen;
    const becameClosed = !open && wasOpen;
    if (open) labelDialog(surface);
    if (becameOpen) {
      returnFocus = document.activeElement;
    }
    wasOpen = open;
    syncOverlayBackground();
    if (becameOpen) {
      visibleFocusable(surface)[0]?.focus();
    } else if (becameClosed && returnFocus?.isConnected) {
      returnFocus.focus();
    }
  };
  surface.addEventListener("keydown", event => {
    if (event.key === "Escape" && closeButton) {
      event.preventDefault();
      closeButton.click();
      return;
    }
    if (event.key !== "Tab") return;
    const items = visibleFocusable(surface);
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
  new MutationObserver(sync).observe(surface, {
    attributes: true,
    attributeFilter: ["class", "hidden"],
    subtree: true,
  });
  sync();
}

function enhanceLiveRegion(element) {
  if (element.dataset.accessibleLive === "true") return;
  element.dataset.accessibleLive = "true";
  const assertive = element.id.endsWith("-error") || element.classList.contains("login-error");
  if (!element.hasAttribute("role")) element.setAttribute("role", assertive ? "alert" : "status");
  element.setAttribute("aria-live", assertive ? "assertive" : "polite");
  element.setAttribute("aria-atomic", "true");
}

function scan(root = document) {
  if (root.matches?.(".login-screen")) enhanceOverlay(root);
  root.querySelectorAll?.(".login-screen").forEach(enhanceOverlay);
  if (root.matches?.("dialog")) labelDialog(root);
  root.querySelectorAll?.("dialog").forEach(labelDialog);
  const liveSelector = [
    "[id$='-result']",
    "[id$='-error']",
    ".watch-notice",
    "#restore-progress",
    "#fed-state",
    "#env-state",
    "#radio-state",
    "#link-state",
    "#alert-delivery",
    "#cap-health",
    "#seismic-health",
  ].join(",");
  if (root.matches?.(liveSelector)) enhanceLiveRegion(root);
  root.querySelectorAll?.(liveSelector).forEach(enhanceLiveRegion);
}

const liveRegion = document.createElement("div");
liveRegion.className = "sr-only";
liveRegion.setAttribute("role", "status");
liveRegion.setAttribute("aria-live", "polite");
liveRegion.setAttribute("aria-atomic", "true");
document.body.appendChild(liveRegion);

const alertRegion = liveRegion.cloneNode();
alertRegion.setAttribute("role", "alert");
alertRegion.setAttribute("aria-live", "assertive");
document.body.appendChild(alertRegion);

const applicationDialog = document.createElement("dialog");
applicationDialog.className = "application-dialog";
applicationDialog.setAttribute("aria-labelledby", "application-dialog-title");
applicationDialog.setAttribute("aria-describedby", "application-dialog-message");
applicationDialog.innerHTML = `
  <form class="application-dialog-card">
    <p class="eyebrow" id="application-dialog-eyebrow">OPERATOR CONFIRMATION</p>
    <h2 id="application-dialog-title"></h2>
    <p id="application-dialog-message" class="application-dialog-message"></p>
    <div id="application-dialog-field" class="application-dialog-field" hidden>
      <label id="application-dialog-label"></label>
      <input id="application-dialog-input">
      <textarea id="application-dialog-textarea" rows="5" hidden></textarea>
      <small id="application-dialog-hint"></small>
    </div>
    <p id="application-dialog-error" class="application-dialog-error" role="alert"></p>
    <footer>
      <button id="application-dialog-cancel" type="button" class="secondary">Cancel</button>
      <button id="application-dialog-confirm" type="submit">Continue</button>
    </footer>
  </form>`;
document.body.appendChild(applicationDialog);

const dialogParts = {
  eyebrow: applicationDialog.querySelector("#application-dialog-eyebrow"),
  title: applicationDialog.querySelector("#application-dialog-title"),
  message: applicationDialog.querySelector("#application-dialog-message"),
  field: applicationDialog.querySelector("#application-dialog-field"),
  label: applicationDialog.querySelector("#application-dialog-label"),
  input: applicationDialog.querySelector("#application-dialog-input"),
  textarea: applicationDialog.querySelector("#application-dialog-textarea"),
  hint: applicationDialog.querySelector("#application-dialog-hint"),
  error: applicationDialog.querySelector("#application-dialog-error"),
  cancel: applicationDialog.querySelector("#application-dialog-cancel"),
  confirm: applicationDialog.querySelector("#application-dialog-confirm"),
};
let dialogState = null;
const dialogQueue = [];

function defaultDialogValue(state) {
  return state.kind === "confirm" ? false : null;
}

function settleDialog(value) {
  const state = dialogState;
  if (!state || state.settling) return;
  state.settling = true;
  state.value = value;
  applicationDialog.close();
}

applicationDialog.querySelector("form").addEventListener("submit", event => {
  event.preventDefault();
  if (!dialogState) return;
  if (dialogState.kind !== "prompt") {
    settleDialog(true);
    return;
  }
  const field = dialogState.multiline ? dialogParts.textarea : dialogParts.input;
  const value = field.value.trim();
  if (dialogState.required && !value) {
    dialogParts.error.textContent = "A value is required.";
    field.focus();
    return;
  }
  if (dialogState.verification && value !== dialogState.verification) {
    dialogParts.error.textContent = "The confirmation text does not match.";
    field.focus();
    return;
  }
  settleDialog(value);
});
dialogParts.cancel.addEventListener("click", () => settleDialog(dialogState?.kind === "confirm" ? false : null));
applicationDialog.addEventListener("close", () => {
  const state = dialogState;
  if (!state) return;
  dialogState = null;
  state.resolve(state.settling ? state.value : defaultDialogValue(state));
  showNextDialog();
});

function showNextDialog() {
  if (dialogState || applicationDialog.open) return;
  const next = dialogQueue.shift();
  if (!next) return;
  const {options, resolve} = next;
  dialogParts.eyebrow.textContent = options.eyebrow || "OPERATOR CONFIRMATION";
  dialogParts.title.textContent = options.title;
  dialogParts.message.textContent = options.message || "";
  dialogParts.cancel.hidden = options.kind === "alert";
  dialogParts.cancel.textContent = options.cancelLabel || "Cancel";
  dialogParts.confirm.textContent = options.confirmLabel || (options.kind === "alert" ? "Close" : "Continue");
  dialogParts.confirm.classList.toggle("danger", Boolean(options.danger));
  dialogParts.field.hidden = options.kind !== "prompt";
  dialogParts.input.hidden = Boolean(options.multiline);
  dialogParts.textarea.hidden = !options.multiline;
  dialogParts.label.textContent = options.label || "Response";
  dialogParts.hint.textContent = options.verification ? `Type exactly: ${options.verification}` : "";
  dialogParts.error.textContent = "";
  const field = options.multiline ? dialogParts.textarea : dialogParts.input;
  dialogParts.label.htmlFor = field.id;
  field.value = options.defaultValue || "";
  if (!options.multiline) field.type = options.type || "text";
  field.autocomplete = options.autocomplete || "off";
  const state = {...options, resolve, settling: false, value: null};
  dialogState = state;
  applicationDialog.showModal();
  window.requestAnimationFrame(() => {
    if (dialogState !== state || !applicationDialog.open) return;
    if (options.kind === "prompt") field.focus();
    else if (options.danger && options.kind !== "alert") dialogParts.cancel.focus();
    else dialogParts.confirm.focus();
  });
}

function openApplicationDialog(options) {
  return new Promise(resolve => {
    dialogQueue.push({options, resolve});
    showNextDialog();
  });
}

scan();
new MutationObserver(records => {
  for (const record of records) {
    for (const node of record.addedNodes) if (node.nodeType === Node.ELEMENT_NODE) scan(node);
  }
}).observe(document.body, {childList: true, subtree: true});

window.OutpostUI = {
  alert: options => openApplicationDialog({kind: "alert", ...options}),
  confirm: options => openApplicationDialog({kind: "confirm", ...options}),
  prompt: options => openApplicationDialog({kind: "prompt", required: true, ...options}),
  announce(message, assertive = false) {
    const target = assertive ? alertRegion : liveRegion;
    target.textContent = "";
    window.requestAnimationFrame(() => { target.textContent = message; });
  },
};

export const OutpostUI = window.OutpostUI;
