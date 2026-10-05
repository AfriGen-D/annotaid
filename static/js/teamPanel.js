// Team panel (project managers): who is on this project, adding people who
// already have an account, and invite links for people who don't yet.
//
// Managers staff a project; they don't create accounts (that's the admin
// page). Two things are superadmin-only on the server and shown accordingly:
// adding someone as a manager, and removing a manager.
import { api } from "./api.js";
import { el } from "./dom.js";
import { openDialog, shortTime } from "./dialog.js";

const LINK_STATE = { valid: "active", expired: "expired", revoked: "revoked", used_up: "used up" };

/**
 * ctx: {account   — getMe() (account role: superadmin | manager | user)
 *       me        — state.me (this project's id/name/role)
 *       onMembers(members) — the member list changed (assign menus, counts)
 *       onReleased(n)      — someone was removed and n papers went back to the pool}
 */
export function openTeamPanel(ctx) {
  const superadmin = ctx.account && ctx.account.role === "superadmin";
  const d = openDialog({ title: "Team", cls: "dlg-wide" });

  /* ---------------- members ---------------- */
  const memSec = section(d.body, "Members");
  const memErr = errLine(memSec);
  const memList = el("div", "team-list");
  memSec.appendChild(memList);

  /* ---------------- add person ---------------- */
  const addSec = section(d.body, "Add a person");
  addSec.appendChild(el("div", "dlg-muted",
    "Anyone with an active account. Someone without one can use an invite link below."));
  const addRow = el("div", "team-add");
  const who = el("select", "pb-select team-who");
  const role = el("select", "pb-select team-role");
  const rCur = el("option", null, "curator"); rCur.value = "curator";
  role.appendChild(rCur);
  if (superadmin) {
    const rMan = el("option", null, "manager"); rMan.value = "manager";
    role.appendChild(rMan);
  }
  role.disabled = !superadmin;
  role.title = superadmin ? "" : "Only a superadmin can add project managers";
  const addBtn = el("button", "btn primary", "Add");
  addRow.appendChild(who); addRow.appendChild(role); addRow.appendChild(addBtn);
  addSec.appendChild(addRow);
  const addErr = errLine(addSec);

  /* ---------------- invite links ---------------- */
  const linkSec = section(d.body, "Invite links");
  linkSec.appendChild(el("div", "dlg-muted",
    "Anyone with the link can sign up (or sign in) and join this project as a curator."));
  const mkRow = el("div", "team-add");
  const daysLab = el("label", "team-field");
  daysLab.appendChild(el("span", null, "Expires after (days)"));
  const days = el("input", "team-num");
  days.type = "number"; days.min = "1"; days.max = "90"; days.value = "7";
  daysLab.appendChild(days);
  const usesLab = el("label", "team-field");
  usesLab.appendChild(el("span", null, "Max uses (optional)"));
  const uses = el("input", "team-num");
  uses.type = "number"; uses.min = "1"; uses.placeholder = "no limit";
  usesLab.appendChild(uses);
  const mkBtn = el("button", "btn primary", "Create link");
  mkRow.appendChild(daysLab); mkRow.appendChild(usesLab); mkRow.appendChild(mkBtn);
  linkSec.appendChild(mkRow);
  const linkErr = errLine(linkSec);
  const fresh = el("div", "link-fresh");
  fresh.hidden = true;
  linkSec.appendChild(fresh);
  const linkList = el("div", "team-list");
  linkSec.appendChild(linkList);

  let members = [];

  function renderMembers() {
    memList.innerHTML = "";
    if (!members.length) { memList.appendChild(el("div", "dlg-muted", "No members.")); return; }
    for (const m of members) {
      const row = el("div", "team-row");
      const nm = el("span", "team-name", m.name);
      row.appendChild(nm);
      row.appendChild(el("span", "team-email", m.email || ""));
      row.appendChild(el("span", "team-role " + m.role, m.role));
      if (m.state && m.state !== "active") row.appendChild(el("span", "team-inactive", m.state));
      row.appendChild(el("span", "spacer"));
      row.appendChild(el("span", "team-count", `${m.inProgress || 0} in progress`));
      const isSelf = ctx.me && m.userId === ctx.me.id;
      if (!isSelf && (m.role === "curator" || superadmin)) {
        const rm = el("button", "btn job-btn", "Remove");
        rm.onclick = async () => {
          const held = m.inProgress ? ` Their ${m.inProgress} in-progress paper(s) go back to the pool.` : "";
          if (!window.confirm(`Remove ${m.name} from this project?${held}`)) return;
          rm.disabled = true;
          memErr.hidden = true;
          try {
            const res = await api.removeMember(m.userId);
            setMembers(res.members || []);
            if (ctx.onReleased && res.released) ctx.onReleased(res.released);
          } catch (e) {
            rm.disabled = false;
            showErr(memErr, e);
          }
        };
        row.appendChild(rm);
      }
      memList.appendChild(row);
    }
  }

  function setMembers(list) {
    members = list;
    renderMembers();
    renderDirectory();
    if (ctx.onMembers) ctx.onMembers(members);
  }

  let directory = [];
  function renderDirectory() {
    const taken = new Set(members.map(m => m.userId));
    const avail = directory.filter(u => !taken.has(u.id));
    who.innerHTML = "";
    const ph = el("option", null, avail.length ? "Choose a person…" : "Everyone is already a member");
    ph.value = "";
    who.appendChild(ph);
    for (const u of avail) {
      const o = el("option", null, `${u.name} — ${u.email}`);
      o.value = u.id;
      who.appendChild(o);
    }
    addBtn.disabled = !avail.length;
  }

  addBtn.onclick = async () => {
    addErr.hidden = true;
    if (!who.value) { showErr(addErr, "Choose a person first."); return; }
    addBtn.disabled = true;
    try {
      const res = await api.addMember(who.value, role.value || "curator");
      setMembers(res.members || []);
    } catch (e) {
      showErr(addErr, e);
    } finally {
      addBtn.disabled = false;
    }
  };

  /* ---- links ---- */
  function renderLinks(links) {
    linkList.innerHTML = "";
    if (!links.length) { linkList.appendChild(el("div", "dlg-muted", "No invite links yet.")); return; }
    for (const l of links) {
      const row = el("div", "team-row");
      row.appendChild(el("span", "bp-state " + (l.state === "valid" ? "ok" : "dup"), LINK_STATE[l.state] || l.state));
      row.appendChild(el("span", "team-name", `created ${shortTime(l.createdAt)}`));
      row.appendChild(el("span", "team-email",
        l.state === "revoked" ? `revoked ${shortTime(l.revokedAt)}` : `expires ${shortTime(l.expiresAt)}`));
      row.appendChild(el("span", "spacer"));
      row.appendChild(el("span", "team-count",
        l.maxUses ? `${l.uses}/${l.maxUses} used` : `${l.uses} used`));
      if (l.state === "valid") {
        const rv = el("button", "btn job-btn", "Revoke");
        rv.onclick = async () => {
          if (!window.confirm("Revoke this invite link? Anyone who has it will no longer be able to join.")) return;
          rv.disabled = true;
          linkErr.hidden = true;
          try { await api.revokeLink(l.id); loadLinks(); }
          catch (e) { rv.disabled = false; showErr(linkErr, e); }
        };
        row.appendChild(rv);
      }
      linkList.appendChild(row);
    }
  }
  async function loadLinks() {
    try { renderLinks((await api.links()).links || []); }
    catch (e) { showErr(linkErr, e); }
  }

  mkBtn.onclick = async () => {
    linkErr.hidden = true;
    const n = parseInt(days.value, 10);
    if (!(n >= 1 && n <= 90)) { showErr(linkErr, "Expiry must be between 1 and 90 days."); return; }
    const max = uses.value.trim() ? parseInt(uses.value, 10) : null;
    if (uses.value.trim() && !(max >= 1)) { showErr(linkErr, "Max uses must be a whole number, at least 1."); return; }
    mkBtn.disabled = true;
    try {
      const link = await api.createLink(n, max);
      showFresh(link.url);
      loadLinks();
    } catch (e) {
      showErr(linkErr, e);
    } finally {
      mkBtn.disabled = false;
    }
  };

  // The URL exists only in this reply — the server keeps a hash of it.
  function showFresh(url) {
    fresh.innerHTML = "";
    fresh.hidden = false;
    const row = el("div", "link-fresh-row");
    const inp = el("input", "link-url");
    inp.type = "text"; inp.readOnly = true; inp.value = url;
    inp.onfocus = () => inp.select();
    const copy = el("button", "btn primary", "Copy");
    copy.onclick = async () => {
      try { await navigator.clipboard.writeText(url); copy.textContent = "Copied"; }
      catch (_) { inp.focus(); inp.select(); copy.textContent = "Press ⌘C / Ctrl+C"; }
    };
    row.appendChild(inp); row.appendChild(copy);
    fresh.appendChild(row);
    fresh.appendChild(el("div", "link-fresh-note", "Copy it now — it won't be shown again."));
  }

  /* ---- load ---- */
  (async () => {
    try { setMembersQuiet((await api.members()).members || []); }
    catch (e) { showErr(memErr, e); }
    try { directory = (await api.directory()).users || []; renderDirectory(); }
    catch (e) { showErr(addErr, e); }
  })();
  loadLinks();

  // First load: no need to tell main.js about a list it already has.
  function setMembersQuiet(list) { members = list; renderMembers(); renderDirectory(); }

  return d;
}

function section(parent, title) {
  const s = el("div", "team-sec");
  s.appendChild(el("div", "modal-lab", title));
  parent.appendChild(s);
  return s;
}
function errLine(parent) {
  const e = el("div", "dlg-err");
  e.hidden = true;
  parent.appendChild(e);
  return e;
}
function showErr(node, e) {
  node.textContent = typeof e === "string" ? e : ((e && e.message) || "Something went wrong");
  node.hidden = false;
}
