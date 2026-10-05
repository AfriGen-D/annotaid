// Jobs panel (managers): the project's recent background jobs — imports by
// PMID and AI extraction runs — with what each got through, and the levers:
// cancel one that is still going, retry the items that failed, look at items.
//
// Polls only while the panel is open AND something is still running.
import { api } from "./api.js";
import { el } from "./dom.js";
import { openDialog, shortTime } from "./dialog.js";
import { isActive, importItemView, extractItemView, jobProgress } from "./jobs.js";

const POLL_MS = 2000;
const KIND = { import: "Import by PMID", extract: "AI extraction" };
const STATE = { queued: "queued", running: "running", done: "done", failed: "failed", cancelled: "cancelled" };

/**
 * ctx: {users (id -> name), labelFor(uid) -> string,
 *       onJobStarted(job) — a retry made a new job; main.js follows it}
 */
export function openJobsPanel(ctx) {
  let timer = null;
  let closed = false;
  const expanded = new Set();          // job ids whose items are showing
  const d = openDialog({ title: "Jobs", cls: "dlg-wide", onClose: () => { closed = true; clearTimeout(timer); } });

  const err = el("div", "dlg-err");
  err.hidden = true;
  const list = el("div", "jobs-list");
  list.appendChild(el("div", "dlg-muted", "Loading…"));
  d.body.appendChild(err);
  d.body.appendChild(list);
  d.body.appendChild(el("div", "modal-hint",
    "Jobs run on the server: closing this window or the tab doesn't stop them. The newest 20 are shown."));

  const showErr = e => { err.textContent = (e && e.message) || String(e); err.hidden = false; };

  // A click (cancel, retry, view) refreshes at once; `seq` makes sure only the
  // newest refresh renders and schedules the next poll, so they never stack up.
  let seq = 0;
  async function refresh() {
    clearTimeout(timer);
    const mine = ++seq;
    const stale = () => closed || mine !== seq;
    let jobs;
    try {
      jobs = (await api.listJobs()).jobs || [];
    } catch (e) {
      if (!stale()) { showErr(e); timer = setTimeout(refresh, POLL_MS * 3); }
      return;
    }
    if (stale()) return;
    // Items for the expanded ones (the list route leaves them out).
    const items = {};
    await Promise.all([...expanded].map(async id => {
      try { items[id] = (await api.getJob(id)).items || []; } catch (_) { /* shown as missing */ }
    }));
    if (stale()) return;
    render(jobs, items);
    if (jobs.some(isActive)) timer = setTimeout(refresh, POLL_MS);
  }

  function render(jobs, items) {
    list.innerHTML = "";
    if (!jobs.length) { list.appendChild(el("div", "dlg-muted", "No jobs yet.")); return; }
    for (const job of jobs) list.appendChild(jobRow(job, items[job.id]));
  }

  function jobRow(job, items) {
    const wrap = el("div", "job");
    const row = el("div", "job-row");
    row.appendChild(el("span", "job-kind", KIND[job.kind] || job.kind));
    row.appendChild(el("span", "bp-state job-st " + toneOf(job), STATE[job.state] || job.state));

    const { finished, total } = jobProgress(job);
    const c = job.counts || {};
    const bits = [`${finished}/${total}`];
    if (c.done) bits.push(`${c.done} done`);
    if (c.skipped) bits.push(`${c.skipped} skipped`);
    if (c.failed) bits.push(`${c.failed} failed`);
    if (c.cancelled) bits.push(`${c.cancelled} cancelled`);
    row.appendChild(el("span", "job-counts", bits.join(" · ")));

    const who = (ctx.users && ctx.users[job.createdBy]) || "";
    const meta = el("span", "job-when", shortTime(job.createdAt) + (who ? ` · ${who}` : ""));
    if (job.createdAt) meta.title = new Date(job.createdAt).toLocaleString();
    row.appendChild(meta);
    row.appendChild(el("span", "spacer"));

    if (isActive(job)) {
      const cancel = el("button", "btn job-btn", "Cancel");
      cancel.onclick = async () => {
        cancel.disabled = true;
        try { await api.cancelJob(job.id); refresh(); } catch (e) { cancel.disabled = false; showErr(e); }
      };
      row.appendChild(cancel);
    } else if ((c.failed || 0) + (c.cancelled || 0) > 0) {
      const retry = el("button", "btn job-btn", "Retry failed");
      retry.onclick = async () => {
        retry.disabled = true;
        try {
          const next = await api.retryJob(job.id);
          if (ctx.onJobStarted) ctx.onJobStarted(next);
          refresh();
        } catch (e) { retry.disabled = false; showErr(e); }
      };
      row.appendChild(retry);
    }
    const view = el("button", "linkish job-view", expanded.has(job.id) ? "Hide items" : "View items");
    view.onclick = () => {
      if (expanded.has(job.id)) expanded.delete(job.id); else expanded.add(job.id);
      refresh();
    };
    row.appendChild(view);
    wrap.appendChild(row);

    if (job.error) wrap.appendChild(el("div", "job-err", job.error));
    if (job.retryOf) wrap.appendChild(el("div", "job-note", "a retry of an earlier job"));

    if (expanded.has(job.id)) {
      const box = el("div", "batch-list job-items");
      if (!items) box.appendChild(el("div", "batch-more", "Could not load the items."));
      for (const it of items || []) {
        const v = job.kind === "import" ? importItemView(it) : extractItemView(it);
        const r = el("div", "batch-row");
        r.appendChild(el("span", "bp-id", job.kind === "import" ? it.ref : ctx.labelFor(it.ref)));
        const s = el("span", "bp-state " + v.tone);
        if (v.tone === "running") s.appendChild(el("span", "spinner-ring"));
        else s.textContent = v.label;
        r.appendChild(s);
        r.appendChild(el("span", "bp-msg", v.msg));
        box.appendChild(r);
      }
      wrap.appendChild(box);
    }
    return wrap;
  }

  refresh();
  return d;
}

function toneOf(job) {
  if (isActive(job)) return "running";
  if (job.state === "done") return (job.counts && job.counts.failed) ? "dup" : "ok";
  return "failed";
}
