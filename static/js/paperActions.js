// Who holds a paper and how far it got — the workflow bar above the PDF.
//
//   unassigned --claim / assign--> in_progress --submit--> submitted
//   finished --reopen (manager)--> in_progress (or unassigned if nobody holds it)
//
// Only the assignee, while the paper is in progress, may edit its curated
// values (server/workflow.py edit_block_reason). Everyone else sees the paper
// read only, with a banner saying why — the server refuses the save anyway,
// this is so nobody types into a field that can never be saved.
import { api } from "./api.js";
import { el } from "./dom.js";
import { openDialog, askReason, shortTime } from "./dialog.js";

export const STATUS_LABEL = {
  unassigned: "Unassigned",
  in_progress: "In progress",
  submitted: "Submitted",
};
const FINISHED = ["submitted"];

export function statusOf(p) { return (p && p.curationStatus) || "unassigned"; }
export function isFinished(p) { return FINISHED.includes(statusOf(p)); }

/** May `me` edit this paper's curated values? Mirrors the server rule. */
export function canEdit(p, me) {
  return !!p && !!me && p.assigneeId === me.id && statusOf(p) === "in_progress";
}

export function statusBadge(p) {
  const s = statusOf(p);
  return el("span", "st-badge st-" + s, STATUS_LABEL[s] || s);
}

function nameOf(users, id) {
  return (id && users && users[id]) || (id ? "someone" : "");
}

/** Why this paper is read only for `me`, or null if it isn't. Plain text. */
export function readOnlyReason(p, me, users) {
  if (canEdit(p, me)) return null;
  const s = statusOf(p);
  if (FINISHED.includes(s)) {
    return `${STATUS_LABEL[s]} — read only · a manager can reopen it`;
  }
  if (s === "unassigned" || !p.assigneeId) return "Unassigned — claim it to start curating";
  return `Assigned to ${nameOf(users, p.assigneeId)} — read only`;
}

/**
 * ctx: {me, isManager, members, users,
 *       onPaper(paper)  — an action succeeded; fold the updated paper in
 *       onError(err)    — an action failed (toast it; resync on a conflict)}
 */
export function renderPaperBar(container, p, ctx) {
  container.innerHTML = "";
  container.hidden = !p;
  if (!p) return;
  const { me, isManager, users } = ctx;
  const s = statusOf(p);
  const mine = p.assigneeId && me && p.assigneeId === me.id;

  const bar = el("div", "pb-row");
  bar.appendChild(statusBadge(p));
  if (p.assigneeId) {
    bar.appendChild(el("span", "pb-who",
      mine ? "Assigned to you" : `Assigned to ${nameOf(users, p.assigneeId)}`));
  }
  if (p.statusAt && s !== "unassigned") {
    const t = el("span", "pb-when", shortTime(p.statusAt));
    t.title = new Date(p.statusAt).toLocaleString();
    bar.appendChild(t);
  }
  bar.appendChild(el("span", "spacer"));

  const busy = btns => btns.forEach(b => { b.disabled = true; });
  const act = async (buttons, fn) => {
    busy(buttons);
    try {
      const res = await fn();
      if (res && res.paper) ctx.onPaper(res.paper);
    } catch (err) {
      buttons.forEach(b => { b.disabled = false; });
      ctx.onError(err);
    }
  };
  const actions = [];

  if (s === "unassigned") {
    const claim = el("button", "btn primary pb-btn", "Claim");
    claim.title = "Take this paper from the pool and start curating it";
    claim.onclick = () => act([claim], () => api.claim(p.uid));
    actions.push(claim);
  }

  if (mine && s === "in_progress") {
    const submit = el("button", "btn primary pb-btn", "Submit");
    submit.title = "Finished curating — hand it in (it becomes read only)";
    const all = [submit];
    submit.onclick = () => {
      if (!window.confirm("Submit this paper? It becomes read only; a manager can reopen it.")) return;
      act(all, () => api.submit(p.uid));
    };
    actions.push(...all);
  }

  if (isManager && (s === "unassigned" || s === "in_progress")) {
    actions.push(assignSelect(p, ctx, act));
  }

  if (isManager && FINISHED.includes(s)) {
    const reopen = el("button", "btn pb-btn", "Reopen");
    reopen.title = "Send it back to work (to the same curator if they're still on the project)";
    reopen.onclick = async () => {
      const reason = await askReason({
        title: "Reopen paper", confirmLabel: "Reopen", required: false,
        prompt: "Optional: why it needs more work. It goes back to the same curator if they're still on the project.",
      });
      if (reason == null) return;
      act([reopen], () => api.reopen(p.uid, reason));
    };
    actions.push(reopen);
  }

  const acts = el("div", "pb-actions");
  actions.forEach(a => acts.appendChild(a));
  const hist = el("button", "linkish pb-hist", "History");
  hist.onclick = () => showHistory(p, ctx);
  acts.appendChild(hist);
  bar.appendChild(acts);
  container.appendChild(bar);

  const ro = readOnlyReason(p, me, users);
  if (ro) container.appendChild(el("div", "ro-banner", ro));
}

// Managers assign, reassign or send a paper back to the pool. Not offered for
// finished papers: the server wants those reopened first.
function assignSelect(p, ctx, act) {
  const wrap = el("label", "pb-assign");
  wrap.appendChild(el("span", "pb-assign-lab", "Assign to"));
  const sel = el("select", "pb-select");
  const pool = el("option", null, "Pool (unassigned)");
  pool.value = "";
  sel.appendChild(pool);
  const active = (ctx.members || []).filter(m => m.state === "active");
  for (const m of active) {
    const o = el("option", null, m.name + (m.userId === ctx.me.id ? " (you)" : ""));
    o.value = m.userId;
    sel.appendChild(o);
  }
  // Held by someone who has since left or been deactivated: still show them.
  if (p.assigneeId && !active.some(m => m.userId === p.assigneeId)) {
    const o = el("option", null, nameOf(ctx.users, p.assigneeId) + " (inactive)");
    o.value = p.assigneeId;
    o.disabled = true;
    sel.appendChild(o);
  }
  sel.value = p.assigneeId || "";
  sel.onchange = () => {
    const to = sel.value || null;
    if (to === (p.assigneeId || null)) return;
    act([sel], () => api.assign(p.uid, to)).then(() => { sel.value = p.assigneeId || ""; });
  };
  wrap.appendChild(sel);
  return wrap;
}

/* ---------------- history ---------------- */
const ACTION_TEXT = {
  claim: () => "claimed it",
  assign: e => `assigned it to ${e.toAssigneeName || "someone"}` +
    (e.fromAssigneeName ? ` (from ${e.fromAssigneeName})` : ""),
  unassign: e => "returned it to the pool" + (e.fromAssigneeName ? ` (from ${e.fromAssigneeName})` : ""),
  submit: () => "submitted it",
  reopen: e => "reopened it" + (e.fromStatus ? ` (was ${STATUS_LABEL[e.fromStatus] || e.fromStatus})` : ""),
};

async function showHistory(p, ctx) {
  const d = openDialog({ title: "History", cls: "dlg-wide" });
  const label = p.pmid ? `PMID ${p.pmid}` : (p.doi || p.filename || p.uid);
  d.body.appendChild(el("div", "dlg-text", label));
  const list = el("div", "hist-list");
  list.appendChild(el("div", "dlg-muted", "Loading…"));
  d.body.appendChild(list);
  let events;
  try {
    events = (await api.history(p.uid)).events || [];
  } catch (err) {
    list.innerHTML = "";
    list.appendChild(el("div", "dlg-err", err.message));
    return;
  }
  list.innerHTML = "";
  if (p.addedBy) {
    events = [{ at: p.addedAt || null, action: "_added", actorName: nameOf(ctx.users, p.addedBy) }, ...events];
  }
  if (!events.length) { list.appendChild(el("div", "dlg-muted", "Nothing has happened to this paper yet.")); return; }
  for (const e of events) {
    const row = el("div", "hist-row");
    const when = el("span", "hist-when", shortTime(e.at));
    if (e.at) when.title = new Date(e.at).toLocaleString();
    row.appendChild(when);
    const what = el("span", "hist-what");
    what.appendChild(el("b", null, e.actorName || "someone"));
    const verb = e.action === "_added" ? "added it to the project"
      : (ACTION_TEXT[e.action] ? ACTION_TEXT[e.action](e) : e.action.replace(/_/g, " "));
    what.appendChild(document.createTextNode(" " + verb));
    row.appendChild(what);
    if (e.reason) row.appendChild(el("div", "hist-reason", "“" + e.reason + "”"));
    list.appendChild(row);
  }
}
