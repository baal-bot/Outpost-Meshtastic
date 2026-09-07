const form = document.getElementById("recovery-login");
const status = document.getElementById("recovery-status");
const evidence = document.getElementById("recovery-evidence");
const logout = document.getElementById("recovery-logout");
let csrf = "", busy = false;
async function request(path, body) {
  const response = await fetch(`/api/v1/${path}`, {
    method: body ? "POST" : "GET", credentials: "same-origin", cache: "no-store",
    headers: body ? {"Content-Type": "application/json", "X-CSRF-Token": csrf} : {},
    body: body ? JSON.stringify(body) : undefined, signal: AbortSignal.timeout(15000),
  });
  if (!response.ok) throw new Error("Access was denied or could not be confirmed. Sign in again; no request is retried automatically.");
  return response.json();
}
form.addEventListener("submit", async event => {
  event.preventDefault();
  if (busy) return;
  busy = true;
  const fields = new FormData(form);
  form.inert = true;
  evidence.textContent = "No authenticated evidence yet.";
  try {
    let password = String(fields.get("password"));
    let session = await request("auth/login", {
      username: fields.get("username"), password, code: fields.get("code"),
    });
    csrf = session.csrf_token;
    if (session.must_change) {
      const replacement = String(fields.get("new_password") || "");
      if (!replacement) throw new Error("This account requires a password change. Enter a new password and sign in again.");
      await request("auth/password", {current_password: password, new_password: replacement});
      password = replacement;
      session = await request("auth/login", {username: fields.get("username"), password, code: fields.get("code")});
      csrf = session.csrf_token;
    }
    logout.disabled = false;
    const result = await request("recovery/review");
    if (result.state !== "review_required") throw new Error("This is not a fenced recovery workbench. Normal services have not been stopped.");
    evidence.textContent = JSON.stringify(result, null, 2);
    status.textContent = "Restored operator access verified. Identity remains offline pending reactivation review.";
    logout.disabled = false;
  } catch (error) {
    status.textContent = error.message;
  } finally {
    form.reset();
    form.inert = false;
    busy = false;
  }
});
logout.addEventListener("click", async () => {
  if (busy) return;
  busy = true;
  logout.disabled = true;
  evidence.textContent = "No authenticated evidence yet.";
  try {
    await request("auth/logout", {});
    csrf = "";
    status.textContent = "Signed out. Recovery remains fenced.";
  } catch (error) {
    status.textContent = error.message;
    logout.disabled = false;
  } finally { busy = false; }
});
