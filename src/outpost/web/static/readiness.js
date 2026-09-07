// Readiness evidence is not permission to execute a qualification procedure.
const node = (tag, text, className = "") => {
  const element = document.createElement(tag);
  if (text) element.textContent = text;
  if (className) element.className = className;
  return element;
};

export function readinessDetails(report, refresh) {
  const details = node("details", "", "readiness-details");
  details.append(node("summary", `All readiness checks (${report.checks.length})`));
  details.append(node("p", "Pass means the stated measurement only. Unknown, stale and operator-attested results are not an all-ready certificate. No radio, network-disconnection, restore or reboot test runs here."));
  for (const check of report.checks) {
    const article = node("article", "", "readiness-check");
    article.dataset.check = check.name;
    const state = check.state || (check.passed ? "pass" : "fail");
    article.append(node("strong", `${state.toUpperCase()}: ${check.title}`));
    article.append(node("p", check.detail));
    article.append(node("p", `Impact: ${check.impact}`));
    article.append(node("p", `Next step: ${check.remediation}`));
    const observed = check.evidence?.observed_at;
    if (Number.isFinite(observed)) {
      article.append(node("p", `Operator observation: ${new Date(observed * 1000).toISOString()}; ${check.evidence.observation_outcome}. Not independently verified.`));
    }
    if (/^[a-f0-9]{64}$/.test(check.review_token || "")) {
      const form = node("form", "", "readiness-observation");
      const label = node("label", "Observed at (UTC)");
      const timestamp = document.createElement("input");
      timestamp.type = "datetime-local";
      timestamp.step = "1";
      timestamp.required = true;
      timestamp.value = new Date(report.generated_at * 1000).toISOString().slice(0, 19);
      label.append(timestamp);
      const confirmation = node("label", "", "readiness-confirmation");
      const consent = document.createElement("input");
      consent.type = "checkbox";
      consent.required = true;
      confirmation.append(consent, document.createTextNode("I performed this check under an approved procedure. This records my observation, not measured certification, and runs no test."));
      const pass = node("button", "Record passed observation");
      const fail = node("button", "Record failed observation");
      pass.type = fail.type = "submit";
      pass.value = "pass";
      fail.value = "fail";
      const message = node("p", "", "readiness-observation-result");
      message.setAttribute("role", "status");
      form.append(label, confirmation, pass, fail, message);
      form.addEventListener("submit", async event => {
        event.preventDefault();
        if (!form.reportValidity() || !event.submitter) return;
        pass.disabled = fail.disabled = true;
        try {
          const sessionResponse = await fetch("/api/v1/auth/session", {cache: "no-store"});
          if (!sessionResponse.ok) throw new Error("session unavailable");
          const session = await sessionResponse.json();
          const response = await fetch("/api/v1/readiness/observations", {
            method: "POST",
            headers: {"content-type": "application/json", "x-csrf-token": session.csrf_token},
            body: JSON.stringify({
              check: check.name, outcome: event.submitter.value,
              observed_at: Math.floor(Date.parse(`${timestamp.value}Z`) / 1000),
              review_token: check.review_token,
            }),
          });
          if (response.status === 409) {
            message.textContent = "Evidence changed or the observation time is invalid. Run a fresh readiness check and review again; nothing is resubmitted automatically.";
            return;
          }
          if ([400, 401, 403, 422].includes(response.status)) {
            message.textContent = "Observation was not accepted. Check the time and your operator session, then run a fresh readiness check.";
            return;
          }
          if (!response.ok) throw new Error("observation unconfirmed");
          refresh(await response.json(), true);
        } catch (_) {
          message.textContent = "The observation outcome is unconfirmed. Refresh before trying again; it may already be recorded.";
        } finally {
          pass.disabled = fail.disabled = false;
        }
      });
      article.append(form);
    }
    details.append(article);
  }
  return details;
}
