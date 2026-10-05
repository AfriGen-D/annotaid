// The signed-in user's own account page: who am I, change password, sign out.
// Also where a user with a temporary password is sent (?force=1) — the server
// refuses everything else until they've changed it.
import { logout } from "./session.js";

const $ = (id) => document.getElementById(id);
const force = new URLSearchParams(location.search).get("force") === "1";

function msg(text, kind = "err") {
  const el = $("pwMsg");
  el.textContent = text || "";
  el.className = `auth-msg ${kind}`;
  el.hidden = !text;
}

async function init() {
  const r = await fetch("/api/me", { credentials: "same-origin" });
  if (r.status === 401) { location.href = "/login?next=/account"; return; }
  const me = await r.json();
  const role = { superadmin: "Superadmin", manager: "Manager", user: "Member" }[me.role] || me.role;
  $("who").textContent = `${me.name} · ${me.email} · ${role}`;
  if (force || me.mustChangePassword) {
    $("forceMsg").hidden = false;
    $("homeLink").hidden = true;
  }
}

$("pwForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  if (f.new.value !== f.new2.value) { msg("The two new passwords don't match."); return; }
  if (f.new.value.length < 10) { msg("The new password must be at least 10 characters."); return; }
  const btn = f.querySelector("button");
  btn.disabled = true;
  const r = await fetch("/api/me/password", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ current: f.current.value, new: f.new.value }),
    credentials: "same-origin",
  });
  btn.disabled = false;
  const data = await r.json().catch(() => ({}));
  if (!r.ok) { msg(data.error || "Couldn't change the password."); return; }
  f.reset();
  if (force || !$("forceMsg").hidden) {
    location.href = "/";
    return;
  }
  msg("Password changed.", "ok");
});

$("logoutBtn").addEventListener("click", logout);
init();
