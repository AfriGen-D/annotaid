// Following a background job (adding papers by PMID, AI extraction) while it runs.
//
// The work happens on the server, so nothing here can stop it by going away:
// closing the "Add Paper(s)" modal only drops that modal's listener. main.js
// keeps its own listener on every job it starts, which is what folds the
// results (new papers, new runs) into the app whatever the modal is doing.
//
// One poll loop per job however many listeners it has, so the modal, the jobs
// panel and main.js watching the same job cost one request every 2 s, not three.
import { api } from "./api.js";

const POLL_MS = 2000;
const followed = new Map();   // jid -> {listeners:Set, timer, last}

export function isActive(job) {
  return !!job && (job.state === "queued" || job.state === "running");
}

/**
 * Calls listener(job) with the full job (items included) now-ish and on every
 * poll until it finishes; the last call is the finished job. `seed` (optional)
 * is a job object already in hand — e.g. the POST reply — delivered at once so
 * the caller can render before the first poll returns.
 * Returns an unsubscribe function.
 */
export function followJob(jid, listener, seed) {
  let f = followed.get(jid);
  if (!f) {
    f = { listeners: new Set(), timer: null, last: seed || null };
    followed.set(jid, f);
    schedule(jid, f, seed && !isActive(seed) ? 0 : (seed ? POLL_MS : 0));
  }
  f.listeners.add(listener);
  if (f.last) queueMicrotask(() => { if (f.listeners.has(listener)) safeCall(listener, f.last); });
  return () => {
    f.listeners.delete(listener);
    if (!f.listeners.size && followed.get(jid) === f) { clearTimeout(f.timer); followed.delete(jid); }
  };
}

function schedule(jid, f, delay) {
  clearTimeout(f.timer);
  f.timer = setTimeout(() => poll(jid, f), delay);
}

async function poll(jid, f) {
  let job;
  try {
    job = await api.getJob(jid);
  } catch (err) {
    // A blip (server restarting, wifi): keep trying at a slower pace. A 404
    // means the job is gone for good, so stop.
    if (err && err.status === 404) { followed.delete(jid); return; }
    if (followed.get(jid) === f) schedule(jid, f, POLL_MS * 3);
    return;
  }
  if (followed.get(jid) !== f) return;     // everyone unsubscribed meanwhile
  f.last = job;
  for (const l of [...f.listeners]) safeCall(l, job);
  if (isActive(job)) schedule(jid, f, POLL_MS);
  else followed.delete(jid);
}

function safeCall(fn, job) {
  try { fn(job); } catch (err) { console.error("job listener failed", err); }
}

// Human wording for an import item. `tone` picks the badge colour (bp-state.*).
export function importItemView(it) {
  if (it.state === "waiting") return { label: "waiting", tone: "pending", msg: "" };
  if (it.state === "running") return { label: "", tone: "running", msg: "" };
  if (it.state === "cancelled") return { label: "cancelled", tone: "failed", msg: "" };
  switch (it.outcome) {
    case "added": return { label: "added", tone: "ok", msg: "" };
    case "added_abstract":
      return { label: "added", tone: "ok", msg: "abstract only — no open-access PDF" };
    case "duplicate": return { label: "in project", tone: "dup", msg: "already in this project" };
    case "no_pdf":
      return { label: "no PDF", tone: "failed", msg: it.error || "no open-access PDF found" };
    case "not_found": return { label: "not found", tone: "failed", msg: it.error || "not found on PubMed" };
    case "invalid": return { label: "invalid", tone: "failed", msg: it.error || "not a PMID" };
    case "interrupted":
      return { label: "failed", tone: "failed", msg: "interrupted (server restarted) — retry it" };
    default: return { label: "failed", tone: "failed", msg: it.error || "could not fetch this paper" };
  }
}

// Same for an extraction item.
export function extractItemView(it) {
  if (it.state === "waiting") return { label: "waiting", tone: "pending", msg: "" };
  if (it.state === "running") return { label: "", tone: "running", msg: "" };
  if (it.state === "cancelled") return { label: "cancelled", tone: "failed", msg: "" };
  if (it.state === "skipped") {
    return { label: "skipped", tone: "dup", msg: "already extracted with these models" };
  }
  if (it.state === "done") {
    const n = ((it.result && it.result.models) || []).length;
    return { label: "done", tone: "ok", msg: n ? `${n} model(s) ok` : "" };
  }
  if (it.outcome === "interrupted") {
    return { label: "failed", tone: "failed", msg: "interrupted (server restarted) — retry it" };
  }
  return { label: "failed", tone: "failed", msg: it.error || "extraction failed" };
}

// "3 of 10 complete"-style numbers from a job's counts.
export function jobProgress(job) {
  const c = job.counts || {};
  const finished = (c.done || 0) + (c.failed || 0) + (c.skipped || 0) + (c.cancelled || 0);
  return { finished, total: job.total || 0, failed: (c.failed || 0) + (c.cancelled || 0) };
}
