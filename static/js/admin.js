// Superadmin console (/admin): pending sign-ups, accounts, projects, system.
//
// The server already restricts this page and every /api/admin route to
// superadmins; nothing here is a security boundary. Names and emails are
// user-supplied, so everything from the server goes in via textContent.
import { renderUserMenu, toLogin } from "./session.js";

const $ = (s) => document.querySelector(s);

const S = {
  me: null,
  users: [],
  projects: [],
  system: null,
  tab: "pending",
};

const ROLES = [["user", "User"], ["manager", "Manager"], ["superadmin", "Superadmin"]];

/* ---------------- fetch helpers ---------------- */
class ApiError extends Error {
  constructor(msg, status) { super(msg); this.status = status; }
}

async function handle(r) {
  if (r.status === 401) { toLogin(); return new Promise(() => {}); }
  let data = null;
  try { data = await r.json(); } catch (_) { data = null; }
  if (!r.ok) {
    if (r.status === 403 && data && data.code === "must_change_password") {
      location.href = "/account?force=1";
      return new Promise(() => {});
    }
    throw new ApiError((data && data.error) || `${r.status} ${r.statusText}`, r.status);
  }
  return data;
}

async function jget(path) {
  return handle(await fetch(path, { credentials: "same-origin" }));
}

async function jpost(path, body) {
  return handle(await fetch(path, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  }));
}

const enc = encodeURIComponent;

/* ---------------- small DOM helpers ---------------- */
function el(tag, cls, txt) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (txt != null) e.textContent = txt;
  return e;
}

function button(label, cls, onClick) {
  const b = el("button", cls || "btn", label);
  b.type = "button";
  if (onClick) b.addEventListener("click", onClick);
  return b;
}

function roleSelect(value) {
  const sel = el("select");
  for (const [v, label] of ROLES) {
    const o = el("option", null, label);
    o.value = v;
    sel.append(o);
  }
  sel.value = value || "user";
  return sel;
}

function showMsg(node, text, kind = "err") {
  if (!node) return;
  if (!text) { node.hidden = true; node.textContent = ""; return; }
  node.className = "auth-msg " + kind;
  node.textContent = text;
  node.hidden = false;
}

function fmtTime(iso) {
  if (!iso) return "never";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

function fmtBytes(n) {
  if (n == null || Number.isNaN(Number(n))) return "–";
  n = Number(n);
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${i === 0 ? n : n.toFixed(n < 10 ? 1 : 0)} ${units[i]}`;
}

/* ---------------- busy wrapper ---------------- */
// Disables the clicked control while a request runs, so a double click can't
// approve twice or create two accounts.
async function busy(btn, fn) {
  if (btn) btn.disabled = true;
  try { return await fn(); }
  finally { if (btn && btn.isConnected) btn.disabled = false; }
}

/* ---------------- tabs ---------------- */
function wireTabs() {
  for (const t of document.querySelectorAll(".admin-tab")) {
    t.addEventListener("click", () => selectTab(t.dataset.tab));
  }
}

function selectTab(name) {
  S.tab = name;
  for (const t of document.querySelectorAll(".admin-tab")) {
    const on = t.dataset.tab === name;
    t.classList.toggle("on", on);
    t.setAttribute("aria-selected", on ? "true" : "false");
  }
  for (const p of document.querySelectorAll(".admin-pane")) {
    p.hidden = p.id !== `pane-${name}`;
  }
  try { history.replaceState(null, "", name === "pending" ? "/admin" : `/admin#${name}`); } catch (_) {}
  if (name === "projects") loadProjects();
  if (name === "system") loadSystem();
}

/* ---------------- temporary-password dialog ---------------- */
function showTempPassword(user, pw) {
  $("#pwFor").textContent = `For ${user.name} (${user.email})`;
  $("#pwValue").textContent = pw;
  $("#pwCopy").textContent = "Copy";
  $("#pwModal").hidden = false;
  $("#pwCopy").focus();
}

function closeTempPassword() {
  $("#pwModal").hidden = true;
  $("#pwValue").textContent = "";
  $("#pwFor").textContent = "";
}

async function copyTempPassword() {
  const text = $("#pwValue").textContent;
  let ok = false;
  try {
    await navigator.clipboard.writeText(text);
    ok = true;
  } catch (_) {
    // Fallback for non-secure contexts: select the text and use execCommand.
    try {
      const range = document.createRange();
      range.selectNodeContents($("#pwValue"));
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
      ok = document.execCommand("copy");
    } catch (_) { ok = false; }
  }
  $("#pwCopy").textContent = ok ? "Copied" : "Select and copy it";
}

function wireDialog() {
  $("#pwCopy").addEventListener("click", copyTempPassword);
  $("#pwDone").addEventListener("click", closeTempPassword);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("#pwModal").hidden) closeTempPassword();
  });
}

/* ---------------- users (shared by Pending and Users tabs) ---------------- */
async function loadUsers() {
  try {
    const data = await jget("/api/admin/users");
    S.users = data.users || [];
    showMsg($("#usersMsg"), "");
  } catch (err) {
    showMsg($("#usersMsg"), `Could not load accounts: ${err.message}`);
    showMsg($("#pendingMsg"), `Could not load sign-ups: ${err.message}`);
    return;
  }
  renderPending();
  renderUsers();
}

function renderPending() {
  const pending = S.users.filter((u) => u.state === "pending");
  const badge = $("#pendingCount");
  badge.textContent = String(pending.length);
  badge.hidden = pending.length === 0;

  const list = $("#pendingList");
  list.replaceChildren();
  if (!pending.length) {
    list.append(el("div", "empty-note", "No sign-ups waiting for approval."));
    return;
  }
  for (const u of pending) list.append(pendingCard(u));
}

function pendingCard(u) {
  const card = el("div", "pending-card");
  const who = el("div", "who");
  who.append(el("div", "nm", u.name), el("div", "em", u.email),
    el("div", "at", `requested ${fmtTime(u.createdAt)}`));

  const acts = el("div", "acts");
  const sel = roleSelect("user");
  sel.className = "small";
  sel.setAttribute("aria-label", `Account role for ${u.name}`);
  const msg = el("div", "row-msg");

  const approve = button("Approve", "btn primary", () => busy(approve, async () => {
    msg.textContent = "";
    try {
      const out = await jpost(`/api/admin/users/${enc(u.id)}/approve`, { role: sel.value });
      const parts = [`Approved ${u.name}.`];
      if (out.joinedProjectId) {
        const p = S.projects.find((x) => x.id === out.joinedProjectId);
        parts.push(`They were added to the project ${p ? `"${p.name}"` : out.joinedProjectId} through their invite link.`);
      }
      if (out.joinError) parts.push(`They could not be added to the project from their invite link: ${out.joinError}`);
      showMsg($("#pendingMsg"), parts.join(" "), out.joinError ? "info" : "ok");
      await loadUsers();
    } catch (err) {
      msg.textContent = err.message;
    }
  }));

  const reject = button("Reject", "btn", () => {
    if (!confirm(`Reject the sign-up from ${u.name} (${u.email})? They won't be able to sign in.`)) return;
    busy(reject, async () => {
      msg.textContent = "";
      try {
        await jpost(`/api/admin/users/${enc(u.id)}/reject`, {});
        showMsg($("#pendingMsg"), `Rejected the sign-up from ${u.name}.`, "ok");
        await loadUsers();
      } catch (err) {
        msg.textContent = err.message;
      }
    });
  });

  acts.append(sel, approve, reject);
  card.append(who, acts, msg);
  return card;
}

function renderUsers() {
  const body = $("#usersBody");
  body.replaceChildren();
  if (!S.users.length) {
    const tr = el("tr");
    const td = el("td", "empty", "No accounts.");
    td.colSpan = 6;
    tr.append(td);
    body.append(tr);
    return;
  }
  for (const u of S.users) body.append(userRow(u));
}

function userRow(u) {
  const tr = el("tr");
  if (u.state !== "active") tr.className = "dim";
  const isMe = S.me && u.id === S.me.id;

  const nameTd = el("td", null, u.name);
  if (isMe) nameTd.append(el("span", "faint", " (you)"));
  const emailTd = el("td", "mono", u.email);

  const roleTd = el("td");
  const sel = roleSelect(u.role);
  sel.setAttribute("aria-label", `Account role for ${u.name}`);
  roleTd.append(sel);

  const stateTd = el("td");
  stateTd.append(el("span", `state ${u.state}`, u.state));
  if (u.mustChangePassword && u.state === "active") {
    stateTd.append(el("div", "faint", "must change password"));
  }

  const loginTd = el("td", "nowrap", fmtTime(u.lastLoginAt));

  const actTd = el("td");
  const acts = el("div", "acts");
  const msg = el("div", "row-msg");

  sel.addEventListener("change", async () => {
    const prev = u.role;
    const next = sel.value;
    msg.className = "row-msg";
    msg.textContent = "";
    sel.disabled = true;
    try {
      await jpost(`/api/admin/users/${enc(u.id)}/role`, { role: next });
      await loadUsers();
    } catch (err) {
      sel.value = prev;
      msg.textContent = err.message;
    } finally {
      if (sel.isConnected) sel.disabled = false;
    }
  });

  if (u.state === "active" || u.state === "deactivated") {
    const reset = button("Reset password", "btn", () => {
      if (!confirm(`Reset the password for ${u.name}? Their current password stops working and they are signed out everywhere.`)) return;
      busy(reset, async () => {
        msg.textContent = "";
        try {
          const out = await jpost(`/api/admin/users/${enc(u.id)}/reset-password`, {});
          showTempPassword(out.user || u, out.tempPassword);
          await loadUsers();
        } catch (err) {
          msg.textContent = err.message;
        }
      });
    });
    acts.append(reset);
  }

  if (u.state === "active") {
    const deact = button("Deactivate", "btn", () => {
      if (!confirm(`Deactivate ${u.name}? They are signed out at once and can't sign in until reactivated.`)) return;
      busy(deact, async () => {
        msg.textContent = "";
        try {
          await jpost(`/api/admin/users/${enc(u.id)}/deactivate`, {});
          await loadUsers();
        } catch (err) {
          msg.textContent = err.message;
        }
      });
    });
    acts.append(deact);
  } else if (u.state === "deactivated") {
    const react = button("Reactivate", "btn", () => busy(react, async () => {
      msg.textContent = "";
      try {
        await jpost(`/api/admin/users/${enc(u.id)}/reactivate`, {});
        await loadUsers();
      } catch (err) {
        msg.textContent = err.message;
      }
    }));
    acts.append(react);
  } else if (u.state === "pending") {
    const go = button("Review in Pending", "btn", () => selectTab("pending"));
    acts.append(go);
    sel.disabled = true;
  } else if (u.state === "rejected") {
    acts.append(el("span", "faint", "sign-up rejected"));
  }

  actTd.append(acts, msg);
  tr.append(nameTd, emailTd, roleTd, stateTd, loginTd, actTd);
  return tr;
}

function wireCreateUser() {
  const form = $("#createUserForm");
  const toggle = $("#createUserToggle");
  toggle.addEventListener("click", () => {
    form.hidden = !form.hidden;
    if (!form.hidden) form.elements.name.focus();
  });
  $("#createUserCancel").addEventListener("click", () => {
    form.reset();
    showMsg($("#createUserMsg"), "");
    form.hidden = true;
  });
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const name = form.elements.name.value.trim();
    const email = form.elements.email.value.trim();
    const role = form.elements.role.value;
    if (!name || !email) {
      showMsg($("#createUserMsg"), "Name and email are both required.");
      return;
    }
    const submit = form.querySelector('button[type="submit"]');
    await busy(submit, async () => {
      showMsg($("#createUserMsg"), "");
      try {
        const out = await jpost("/api/admin/users", { name, email, role });
        form.reset();
        form.hidden = true;
        showTempPassword(out.user, out.tempPassword);
        await loadUsers();
      } catch (err) {
        showMsg($("#createUserMsg"), err.message);
      }
    });
  });
}

/* ---------------- projects ---------------- */
async function loadProjects() {
  try {
    const data = await jget("/api/admin/projects");
    S.projects = data.projects || [];
    showMsg($("#projectsMsg"), "");
  } catch (err) {
    showMsg($("#projectsMsg"), `Could not load projects: ${err.message}`);
    return;
  }
  renderProjects();
}

function renderProjects() {
  const body = $("#projectsBody");
  body.replaceChildren();
  if (!S.projects.length) {
    const tr = el("tr");
    const td = el("td", "empty", "No projects yet.");
    td.colSpan = 7;
    tr.append(td);
    body.append(tr);
    return;
  }
  for (const p of S.projects) body.append(projectRow(p));
}

function projectRow(p) {
  const archived = !!p.archivedAt;
  const stats = p.stats || {};
  const tr = el("tr");
  if (archived) tr.className = "dim";
  const msg = el("div", "row-msg");

  const nameTd = el("td");
  nameTd.append(el("div", null, p.name));
  if (p.createdByName) nameTd.append(el("div", "faint", `created by ${p.createdByName}`));

  // Managers: list with remove buttons, plus an add control. The project
  // member routes 404 for archived projects, so only a list is shown for those.
  const mgrTd = el("td");
  const managers = p.managers || [];
  const list = el("div", "mgr-list");
  if (!managers.length) list.append(el("span", "faint", "none"));
  for (const m of managers) {
    const row = el("div", "mgr");
    row.append(el("span", null, m.name));
    if (!archived) {
      const x = button("×", "x");
      x.title = `Remove ${m.name} from this project`;
      x.setAttribute("aria-label", `Remove ${m.name} from this project`);
      x.addEventListener("click", () => {
        if (!confirm(`Remove ${m.name} from "${p.name}"? They leave the project, and any papers they have in progress go back to the pool.`)) return;
        busy(x, async () => {
          msg.className = "row-msg";
          msg.textContent = "";
          try {
            await jpost(`/api/projects/${enc(p.id)}/members/${enc(m.userId)}/remove`, {});
            await loadProjects();
          } catch (err) {
            msg.textContent = err.message;
          }
        });
      });
      row.append(x);
    }
    list.append(row);
  }
  mgrTd.append(list);

  if (!archived) {
    const managerIds = new Set(managers.map((m) => m.userId));
    const candidates = S.users
      .filter((u) => u.state === "active" && !managerIds.has(u.id))
      .sort((a, b) => a.name.localeCompare(b.name));
    if (candidates.length) {
      const add = el("div", "mgr-add");
      const sel = el("select");
      sel.setAttribute("aria-label", `Add a manager to ${p.name}`);
      const ph = el("option", null, "Add manager…");
      ph.value = "";
      sel.append(ph);
      for (const u of candidates) {
        const o = el("option", null, `${u.name} (${u.email})`);
        o.value = u.id;
        sel.append(o);
      }
      const addBtn = button("Add", "btn", () => {
        if (!sel.value) { msg.className = "row-msg"; msg.textContent = "Pick someone to add."; return; }
        busy(addBtn, async () => {
          msg.className = "row-msg";
          msg.textContent = "";
          try {
            await jpost(`/api/projects/${enc(p.id)}/members`, { userId: sel.value, role: "manager" });
            await loadProjects();
          } catch (err) {
            msg.textContent = err.message;
          }
        });
      });
      add.append(sel, addBtn);
      mgrTd.append(add);
    }
  }

  const papersTd = el("td", "num", String(stats.papers ?? 0));
  const finishedTd = el("td", "num", String(stats.finished ?? 0));
  const membersTd = el("td", "num", String(stats.members ?? 0));

  const statusTd = el("td");
  statusTd.append(el("span", archived ? "state archived" : "state active", archived ? "archived" : "active"));
  if (archived) statusTd.append(el("div", "faint nowrap", fmtTime(p.archivedAt)));

  const actTd = el("td");
  const acts = el("div", "acts");
  const open = el("a", "btn" + (archived ? " disabled" : ""), "Open");
  if (archived) {
    open.setAttribute("aria-disabled", "true");
    open.tabIndex = -1;
    open.title = "Unarchive the project to open it";
  } else {
    open.href = `/p/${enc(p.id)}`;
  }
  acts.append(open);

  const action = archived ? "unarchive" : "archive";
  const toggle = button(archived ? "Unarchive" : "Archive", "btn", () => {
    const q = archived
      ? `Unarchive "${p.name}"? Its members will see it again.`
      : `Archive "${p.name}"? It disappears for everyone until you unarchive it.`;
    if (!confirm(q)) return;
    busy(toggle, async () => {
      msg.className = "row-msg";
      msg.textContent = "";
      try {
        await jpost(`/api/admin/projects/${enc(p.id)}/${action}`, {});
        await loadProjects();
      } catch (err) {
        msg.textContent = err.message;
      }
    });
  });
  acts.append(toggle);
  actTd.append(acts, msg);

  tr.append(nameTd, mgrTd, papersTd, finishedTd, membersTd, statusTd, actTd);
  return tr;
}

/* ---------------- system ---------------- */
async function loadSystem() {
  try {
    S.system = await jget("/api/admin/system");
    showMsg($("#systemMsg"), "");
  } catch (err) {
    showMsg($("#systemMsg"), `Could not load system status: ${err.message}`);
    return;
  }
  renderSystem();
}

function fact(label, value, detail, cls) {
  const f = el("div", "sys-fact");
  f.append(el("div", "l", label), el("div", "v" + (cls ? " " + cls : ""), value));
  if (detail) f.append(el("div", "d", detail));
  return f;
}

function renderSystem() {
  const s = S.system || {};
  const users = s.users || {};
  const jobs = s.jobs || {};

  const facts = $("#sysFacts");
  facts.replaceChildren(
    fact("Version", s.version || "–"),
    fact("Database size", fmtBytes(s.dbSize)),
    fact("AI extraction", s.extractionEnabled ? "enabled" : "disabled",
      s.extractionEnabled ? null : "No OpenRouter key configured on the server.",
      s.extractionEnabled ? "ok" : "off"),
    fact("Jobs", `${jobs.queued || 0} queued · ${jobs.running || 0} running`),
    fact("Accounts",
      `${users.active || 0} active`,
      `${users.pending || 0} pending · ${users.deactivated || 0} deactivated · ${users.rejected || 0} rejected`),
  );

  renderAiSettings(s.aiSettings || {}, s.parseEngines || []);

  $("#lastBackup").textContent = s.lastBackup
    ? `Last backup: ${fmtTime(s.lastBackup.at)} (${s.lastBackup.file}, ${fmtBytes(s.lastBackup.size)}).`
    : "No backups yet.";

  const bBody = $("#backupsBody");
  bBody.replaceChildren();
  const backups = s.backups || [];
  if (!backups.length) {
    const tr = el("tr");
    const td = el("td", "empty", "No backups yet.");
    td.colSpan = 3;
    tr.append(td);
    bBody.append(tr);
  }
  for (const b of backups) {
    const tr = el("tr");
    tr.append(el("td", "mono", b.file), el("td", "num", fmtBytes(b.size)), el("td", "nowrap", fmtTime(b.at)));
    bBody.append(tr);
  }

  const eBody = $("#errorsBody");
  eBody.replaceChildren();
  const errors = s.errors || [];
  if (!errors.length) {
    const tr = el("tr");
    const td = el("td", "empty", "No errors since the server started.");
    td.colSpan = 5;
    tr.append(td);
    eBody.append(tr);
  }
  for (const e of errors) {
    const tr = el("tr");
    tr.append(
      el("td", "nowrap", fmtTime(e.at)),
      el("td", "mono", e.method || ""),
      el("td", "mono", e.path || ""),
      el("td", "mono", e.type || ""),
      el("td", null, e.message || ""),
    );
    eBody.append(tr);
  }
}

function renderAiSettings(settings, engines) {
  const box = $("#aiModels");
  box.replaceChildren();
  const models = (settings.models || []).map((m) => ({ ...m }));
  const draw = () => {
    box.replaceChildren();
    models.forEach((m, i) => {
      const row = el("div", "ai-model-row");
      const slugLab = el("label", "auth-field");
      slugLab.append(el("span", null, "OpenRouter model slug"));
      const slug = el("input"); slug.value = m.slug || "";
      slug.addEventListener("input", () => { m.slug = slug.value; syncDefaults(); });
      slugLab.append(slug);
      const labelLab = el("label", "auth-field");
      labelLab.append(el("span", null, "Display name"));
      const label = el("input"); label.value = m.label || "";
      label.addEventListener("input", () => { m.label = label.value; syncDefaults(); });
      labelLab.append(label);
      const structured = el("label", "ai-structured");
      const cb = el("input"); cb.type = "checkbox"; cb.checked = !!m.supportsStructuredOutput;
      cb.addEventListener("change", () => { m.supportsStructuredOutput = cb.checked; });
      structured.append(cb, document.createTextNode("Structured output"));
      const remove = button("Remove", "btn", () => { models.splice(i, 1); draw(); });
      row.append(slugLab, labelLab, structured, remove);
      box.append(row);
    });
    syncDefaults();
  };
  const syncDefaults = () => {
    const sel = $("#defaultModel");
    const before = sel.value || settings.defaultModel;
    sel.replaceChildren();
    for (const m of models) {
      const o = el("option", null, m.label || m.slug || "Unnamed model");
      o.value = m.slug || "";
      sel.append(o);
    }
    if (models.some((m) => m.slug === before)) sel.value = before;
  };
  draw();
  $("#addModelBtn").onclick = () => { models.push({ slug: "", label: "", supportsStructuredOutput: false }); draw(); };
  const engine = $("#parseEngine");
  engine.replaceChildren();
  for (const name of engines) { const o = el("option", null, name); o.value = name; engine.append(o); }
  engine.value = settings.parseEngine || "pdf-text";
  $("#aiForm").onsubmit = async (event) => {
    event.preventDefault();
    const submit = event.currentTarget.querySelector('button[type="submit"]');
    await busy(submit, async () => {
      showMsg($("#systemMsg"), "");
      try {
        const out = await jpost("/api/admin/ai-settings", {
          models,
          defaultModel: $("#defaultModel").value,
          parseEngine: engine.value,
        });
        S.system.aiSettings = out.aiSettings;
        renderAiSettings(out.aiSettings, engines);
        showMsg($("#systemMsg"), "AI settings saved for every project.", "ok");
      } catch (err) { showMsg($("#systemMsg"), err.message); }
    });
  };
}

function wireSystem() {
  const btn = $("#backupBtn");
  btn.addEventListener("click", () => busy(btn, async () => {
    showMsg($("#systemMsg"), "");
    try {
      const out = await jpost("/api/admin/backup", {});
      if (out.system) S.system = out.system;
      renderSystem();
      const b = out.backup;
      showMsg($("#systemMsg"), b ? `Backup written: ${b.file} (${fmtBytes(b.size)}).` : "Backup written.", "ok");
    } catch (err) {
      showMsg($("#systemMsg"), err.message);
    }
  }));
}

/* ---------------- boot ---------------- */
async function boot() {
  wireTabs();
  wireDialog();
  wireCreateUser();
  wireSystem();
  S.me = await renderUserMenu($("#userMenu"));
  if (S.me.role !== "superadmin") {
    // The server serves /admin only to superadmins; this is just a fallback.
    location.href = "/";
    return;
  }
  // Users first: the Projects tab picks manager candidates from them. Projects
  // are loaded up front too, so an approval that joined someone can name it.
  await loadUsers();
  await loadProjects();
  const initial = (location.hash || "").replace("#", "");
  selectTab(["pending", "users", "projects", "system"].includes(initial) ? initial : "pending");
}

boot();
