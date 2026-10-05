// Client persistence: debounced autosave to the backend + a transient, refresh-safe
// localStorage buffer. Backend is the source of truth; localStorage only guards work
// done between an edit and the debounced flush.
import { api, projectId } from "./api.js";
import { toast } from "./dom.js";

// Buffer key: "annotaid:dirty:<projectId> <uid> <modelId>".
//
// The project id is part of the key because two projects can curate the SAME
// paper with the same model — without it they share one buffer, and flushPending
// would post one project's unsaved edits into the other on boot.
//
// The separator is a SPACE, not a colon: model slugs contain colons
// ("openai/gpt-oss-20b:free"). Project ids and uids are hex and no slug contains
// a space, so a three-part space-split is unambiguous.
const BUF = "annotaid:dirty:";
const DEBOUNCE_MS = 600;

const timers = new Map();             // key -> timeout id

// Only the paper's assignee, while it is in progress, may save curated values
// (the server answers anyone else with a 403). main.js installs the rule here
// so NO save path — a stray evidence re-match on load included — can fire for
// a read-only paper, and onForbidden so a 403 that slips through anyway (the
// paper was reassigned under us) is shown and the state reloaded.
let canSave = () => true;
let onForbidden = () => {};
export function setEditGuard(fn) { canSave = fn; }
export function setForbiddenHandler(fn) { onForbidden = fn; }

function key(uid, modelId) { return uid + " " + modelId; }
function bufKey(uid, modelId) { return BUF + projectId() + " " + uid + " " + modelId; }

// Editable subset of a run that we persist. Everything else on a cell —
// aiValue, type, the timestamps — is the server's to own.
function editableCells(cells) {
  const out = {};
  for (const [name, f] of Object.entries(cells || {})) {
    out[name] = {
      value: f.value,
      present: f.present,
      evidence: f.evidence,
      evidenceMatch: f.evidenceMatch,
      confirmed: f.confirmed,
    };
  }
  return out;
}

function editableRun(run) {
  const groups = {};
  for (const [name, g] of Object.entries(run.groups || {})) {
    groups[name] = {
      present: g.present,
      confirmed: g.confirmed,
      // rowId null means "new" — the SERVER mints the id and stamps the origin.
      // Row order in this array is the order that gets stored.
      // `_purge` is a client-side marker; `deleted` is what the server reads.
      rows: (g.rows || []).map(r => ({
        rowId: r.rowId,
        deleted: !!r.deletedAt,
        features: editableCells(r.features),
      })),
    };
  }
  return { features: editableCells(run.features), groups };
}

export function writeBuffer(uid, modelId, run) {
  try {
    localStorage.setItem(bufKey(uid, modelId),
      JSON.stringify({ ...editableRun(run), t: Date.now() }));
  } catch (_) { /* quota — non-fatal */ }
}

function clearBuffer(uid, modelId) {
  try { localStorage.removeItem(bufKey(uid, modelId)); } catch (_) {}
}

// Debounced autosave: called on every edit/confirm.
export function scheduleSave(uid, modelId, run) {
  if (!canSave(uid)) return;                  // read-only: nothing to keep either
  writeBuffer(uid, modelId, run);             // synchronous, refresh-safe
  const k = key(uid, modelId);
  clearTimeout(timers.get(k));
  timers.set(k, setTimeout(() => flush(uid, modelId, run), DEBOUNCE_MS));
}

async function flush(uid, modelId, run) {
  try {
    await api.saveRun(uid, modelId, editableRun(run));
    clearBuffer(uid, modelId);
  } catch (e) {
    // A 403 will never succeed on retry — the paper isn't ours to edit (any
    // more). Drop the buffer, say why, and let main.js resync.
    if (e && e.status === 403) {
      clearBuffer(uid, modelId);
      toast(e.message || "You can't edit this paper");
      onForbidden(e);
      return;
    }
    toast("Autosave failed — will retry (kept locally)");
  }
}

// Structural changes to a repeating group — adding, rejecting or restoring an
// entry — save straight away and RETURN the server's merged run.
//
// They cannot be debounced-and-forgotten like a cell edit, because the server
// mints the row id and stamps the origin: a newly added row is sent with
// rowId:null, so without adopting the reply the next save would send it as new
// all over again and duplicate it.
export async function flushNow(uid, modelId, run) {
  const k = key(uid, modelId);
  clearTimeout(timers.get(k));
  if (!canSave(uid)) {
    const e = new Error("This paper is read only for you");
    e.status = 403;
    throw e;
  }
  writeBuffer(uid, modelId, run);
  try {
    const res = await api.saveRun(uid, modelId, editableRun(run));
    clearBuffer(uid, modelId);
    return res.run;
  } catch (e) {
    if (e && e.status === 403) clearBuffer(uid, modelId);   // see flush()
    throw e;
  }
}

// On boot: flush anything left in localStorage from a previous session — for
// EVERY project, not just the open one, since a buffer may belong to a project
// the curator hasn't reopened.
export async function flushPending() {
  const keys = [];
  for (let i = 0; i < localStorage.length; i++) {
    const k = localStorage.key(i);
    if (k && k.startsWith(BUF)) keys.push(k);
  }
  for (const k of keys) {
    try {
      const [pid, uid, modelId] = k.slice(BUF.length).split(" ");
      if (!pid || !uid || !modelId) { localStorage.removeItem(k); continue; }
      const buffered = JSON.parse(localStorage.getItem(k));
      await api.saveRunIn(pid, uid, modelId,
                          { features: buffered.features, groups: buffered.groups });
      localStorage.removeItem(k);
    } catch (err) {
      // A 404 means the project or paper is gone for good, a 403 that the
      // paper is no longer ours to edit (reassigned, submitted, or we left the
      // project) — retrying either every boot would be a buffer that never drains.
      if (err && (err.status === 404 || err.status === 403)) {
        try { localStorage.removeItem(k); } catch (_) {}
        if (err.status === 403) toast(`Unsaved edits from an earlier session were discarded: ${err.message}`);
      }
      /* otherwise leave it; will retry next boot */
    }
  }
}

// UI niceties (zoom, last doc) — debounced, best-effort.
let uiTimer;
export function saveUi(ui) {
  clearTimeout(uiTimer);
  uiTimer = setTimeout(() => api.saveState(ui).catch(() => {}), 800);
}
