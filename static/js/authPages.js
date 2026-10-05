// Login, sign-up and join-link pages (static/auth.html). PUBLIC: served before
// anyone is signed in, so this file must not import anything behind the gate.
//
//   /login            sign in; ?next=/path returns there afterwards
//   /signup           request an account (pending until a superadmin approves)
//   /join/<token>     invite link: join directly if signed in, otherwise sign
//                     in or request an account through the link

const $ = (id) => document.getElementById(id);

async function post(path, body) {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
    credentials: "same-origin",
  });
  let data = null;
  try { data = await r.json(); } catch { /* empty body */ }
  return { ok: r.ok, status: r.status, data: data || {} };
}

function show(id) {
  for (const c of ["loginCard", "signupCard", "joinCard"]) $(c).hidden = c !== id;
}

function msg(id, text, kind = "err") {
  const el = $(id);
  el.textContent = text || "";
  el.className = `auth-msg ${kind}`;
  el.hidden = !text;
}

// Only ever redirect within this site: never to whatever ?next= says if it
// isn't a plain local path.
function safeNext(raw) {
  if (!raw || !raw.startsWith("/") || raw.startsWith("//") || raw.includes("\\")) return "/";
  return raw;
}

const path = location.pathname;
const params = new URLSearchParams(location.search);
const joinToken = path.startsWith("/join/") ? decodeURIComponent(path.slice(6)) : null;

// ---- sign in ------------------------------------------------------------- //
$("loginForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  const btn = f.querySelector("button");
  btn.disabled = true;
  msg("loginMsg", "");
  const { ok, data } = await post("/api/login", {
    email: f.email.value, password: f.password.value,
  });
  btn.disabled = false;
  if (!ok) {
    msg("loginMsg", data.error || "Sign-in failed.");
    f.password.value = "";
    f.password.focus();
    return;
  }
  if (data.user && data.user.mustChangePassword) {
    location.href = "/account?force=1";
    return;
  }
  if (joinToken) {
    await doJoin();
    return;
  }
  location.href = safeNext(params.get("next"));
});

// ---- sign up ------------------------------------------------------------- //
$("signupForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  if (f.password.value !== f.password2.value) {
    msg("signupMsg", "The two passwords don't match.");
    return;
  }
  if (f.password.value.length < 10) {
    msg("signupMsg", "The password must be at least 10 characters.");
    return;
  }
  const btn = f.querySelector("button");
  btn.disabled = true;
  const body = { name: f.name.value, email: f.email.value, password: f.password.value };
  if (joinToken) body.joinToken = joinToken;
  const { ok, data } = await post("/api/signup", body);
  btn.disabled = false;
  if (!ok) {
    msg("signupMsg", data.error || "Couldn't send the request.");
    return;
  }
  f.hidden = true;
  msg("signupMsg",
    "Request sent. An administrator will review it — you can sign in once it's approved." +
    (joinToken ? " You'll be added to the project at the same time." : ""),
    "ok");
});

// ---- join link ----------------------------------------------------------- //
async function doJoin() {
  const { ok, data } = await post(`/api/join/${encodeURIComponent(joinToken)}`);
  if (ok && data.projectId) {
    location.href = `/p/${encodeURIComponent(data.projectId)}`;
    return;
  }
  show("joinCard");
  $("joinBtn").hidden = true;
  msg("joinMsg", data.error || "Couldn't join the project.");
}
$("joinBtn").addEventListener("click", doJoin);

async function initJoin() {
  let info = { state: "invalid" };
  try {
    const r = await fetch(`/api/join/${encodeURIComponent(joinToken)}`, { credentials: "same-origin" });
    info = await r.json();
  } catch { /* treat as invalid */ }

  if (info.state !== "valid") {
    show("loginCard");
    msg("loginMsg",
      info.state === "invalid"
        ? "This invite link isn't valid."
        : "This invite link is no longer valid — ask the project manager for a new one.");
    return;
  }
  const what = `You've been invited to join <b></b> as a curator.`;
  for (const id of ["loginInvite", "signupInvite"]) {
    const el = $(id);
    el.innerHTML = what;
    el.querySelector("b").textContent = info.projectName;
    el.hidden = false;
  }
  if (info.loggedIn) {
    show("joinCard");
    $("joinText").innerHTML = "Join <b></b> as a curator?";
    $("joinText").querySelector("b").textContent = info.projectName;
    return;
  }
  show("loginCard");
  $("toSignup").href = `/signup?join=${encodeURIComponent(joinToken)}`;
  $("toSignup").addEventListener("click", (e) => { e.preventDefault(); show("signupCard"); });
  $("toLogin").addEventListener("click", (e) => { e.preventDefault(); show("loginCard"); });
}

// ---- route --------------------------------------------------------------- //
if (joinToken) {
  initJoin();
} else if (path === "/signup") {
  show("signupCard");
} else {
  show("loginCard");
  if (params.get("signedout")) msg("loginMsg", "You've been signed out.", "info");
}
