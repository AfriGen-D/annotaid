// Who is signed in, for pages behind the login gate. Cached per page load.
//
// A 401 from any API call means the session ended (logout elsewhere, password
// reset, deactivation, expiry): send the user to sign in, then back here.

let mePromise = null;

export function getMe() {
  if (!mePromise) {
    mePromise = fetch("/api/me", { credentials: "same-origin" }).then(async (r) => {
      if (r.status === 401) { toLogin(); return new Promise(() => {}); }
      const data = await r.json();
      if (data.mustChangePassword) { location.href = "/account?force=1"; return new Promise(() => {}); }
      return data;
    });
  }
  return mePromise;
}

export function toLogin() {
  const next = location.pathname + location.search;
  location.href = "/login" + (next && next !== "/" ? `?next=${encodeURIComponent(next)}` : "");
}

export async function logout() {
  try {
    await fetch("/api/logout", { method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" }, body: "{}" });
  } finally {
    location.href = "/login?signedout=1";
  }
}

/** Header widget: "Name · Admin · Account · Sign out". `el` is an empty span. */
export async function renderUserMenu(el) {
  const me = await getMe();
  el.classList.add("user-menu");
  el.innerHTML = "";
  const name = document.createElement("span");
  name.className = "who";
  name.textContent = me.name;
  el.append(name);
  if (me.role === "superadmin") {
    const a = document.createElement("a");
    a.className = "btn"; a.href = "/admin"; a.textContent = "Admin";
    el.append(a);
  }
  const acct = document.createElement("a");
  acct.className = "btn"; acct.href = "/account"; acct.textContent = "Account";
  const out = document.createElement("button");
  out.className = "btn"; out.type = "button"; out.textContent = "Sign out";
  out.addEventListener("click", logout);
  el.append(acct, out);
  return me;
}
