// Thin fetch wrapper for every backend endpoint. The ONLY coupling seam to the server.
//
// Everything a curator does happens inside a project, so almost every path is
// prefixed with /api/projects/<id>. The id lives in the URL rather than as a
// "current project" on the server: a curator with two projects open in two tabs
// is an obvious thing to do, and server-side current-project state would quietly
// cross their work over.
//
// Every call rides the session cookie. A 401 anywhere means the session ended
// (signed out elsewhere, password reset, deactivated): go and sign in again
// rather than surface an error nobody can act on.
import { toLogin } from "./session.js";

let PID = null;

export function setProject(id) { PID = id; }
export function projectId() { return PID; }

function P(path) {
  if (!PID) throw new Error("no project selected");
  return `/api/projects/${encodeURIComponent(PID)}${path}`;
}

// Never settles: the page is already on its way to the sign-in screen.
function signedOut() { toLogin(); return new Promise(() => {}); }

async function fail(r) {
  const e = new Error((await safeErr(r)) || r.statusText);
  e.status = r.status;
  throw e;
}

async function jget(path) {
  const r = await fetch(path, { credentials: "same-origin" });
  if (r.status === 401) return signedOut();
  if (!r.ok) return fail(r);
  return r.json();
}
async function jpost(path, body, opts) {
  const r = await fetch(path, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
    signal: opts && opts.signal,
  });
  if (r.status === 401) return signedOut();
  if (!r.ok) return fail(r);
  return r.json();
}
async function safeErr(r) {
  try { return (await r.json()).error; } catch { return null; }
}

const U = encodeURIComponent;

export const api = {
  // ---- global (no project) ----
  globalConfig: () => jget("/api/config"),
  listProjects: () => jget("/api/projects"),
  // The starter feature set a new project may be seeded from (404 if none).
  template: () => jget("/api/template"),
  createProject: (name, description, config) =>
    jpost("/api/projects", { name, description, config }),

  // Validate a DRAFT config and preview the prompt it produces. The feature
  // editor posts here instead of reimplementing the rules in JS — two
  // implementations of one schema drift, and then the editor accepts a config
  // the server rejects.
  validateConfig: (name, description, config) =>
    jpost("/api/validate-config", { name, description, config }),

  // A downloadable, fill-in-the-blanks CSV — plain URL so it can back an <a
  // download> link the same way exportUrl() does.
  featureTemplateUrl: () => "/api/feature-template",

  // The other half: a filled-in copy of that template -> a draft features list
  // in the same shape the visual editor edits. Raw CSV text in, not JSON —
  // the file IS the payload.
  parseFeatureSheet: async (csvText) => {
    const r = await fetch("/api/parse-feature-sheet", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "text/csv; charset=utf-8" },
      body: csvText,
    });
    if (r.status === 401) return signedOut();
    if (!r.ok) return fail(r);
    return r.json();   // {ok, features} | {ok:false, error}
  },

  // ---- project ----
  // The portable document — what a project manager hands to another manager.
  projectDocument: (id) => jget(`/api/projects/${U(id)}`),
  updateProject: (id, fields) => jpost(`/api/projects/${U(id)}`, fields),
  archiveProject: (id) => jpost(`/api/projects/${U(id)}/archive`, {}),

  // Explicit-id variants for the home screen's project-card menu, which never
  // calls setProject() — P() would throw with no project "open".
  configFileUrl: (id) => `/api/projects/${U(id)}/config-file`,
  exportUrlFor: (id, fmt) => `/api/projects/${U(id)}/export?format=${U(fmt)}`,

  // + myRole, permissions
  config: () => jget(P("/config")),
  // {papers, runs, ui, me, permissions, users, members}
  state: () => jget(P("/state")),
  papers: () => jget(P("/papers")),

  // pmid: optional — the PMID this PDF is for, when the curator is uploading it
  // by hand after an import found no open-access copy. Prefills the confirm
  // box; never auto-commits.
  uploadPdf: async (file, pmid) => {
    const r = await fetch(P("/papers"), {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/pdf",
        "X-Filename": file.name,
        ...(pmid ? { "X-Suggested-Pmid": String(pmid) } : {}),
      },
      body: file,
    });
    if (r.status === 401) return signedOut();
    if (!r.ok) return fail(r);
    return r.json();
  },

  confirmPmid: (uid, pmid, source) =>
    jpost(P(`/papers/${U(uid)}/identity`), { pmid, source }),

  // A paper that genuinely has no PubMed ID. This RESOLVES its identity (and so
  // unblocks curation) rather than skipping the question.
  markNoPmid: (uid, doi) =>
    jpost(P(`/papers/${U(uid)}/identity`), { status: "none", doi }),

  // The curator's own declared row identity for the project's one repeating
  // group (e.g. which variants/haplotypes this paper reports on) — set BEFORE
  // extraction, never proposed by the AI. Replaces the list wholesale; the
  // server reassigns a rowId only for an item it doesn't already recognise.
  saveGroupItems: (uid, items) =>
    jpost(P(`/papers/${U(uid)}/group-items`), { items }),

  pdfUrl: (uid) => P(`/pdf/${U(uid)}`),

  importItems: (items) => jpost(P("/import"), { items }),

  runs: (uid) => jget(P(`/runs/${U(uid)}`)),

  // payload: {features, groups}. Only the paper's assignee, while it is in
  // progress, may save — anyone else gets a 403 whose message says why.
  saveRun: (uid, modelId, payload) =>
    jpost(P(`/runs/${U(uid)}`), { modelId, ...payload }),

  // Explicitly project-addressed: on boot, store.js flushes edits buffered in a
  // PREVIOUS session, which may belong to a project other than the open one.
  // Posting those through the PID-relative saveRun would file them under the
  // wrong project.
  saveRunIn: (pid, uid, modelId, payload) =>
    jpost(`/api/projects/${U(pid)}/runs/${U(uid)}`, { modelId, ...payload }),

  saveState: (ui) => jpost(P("/state"), ui),

  exportUrl: (fmt) => P(`/export?format=${U(fmt)}`),

  // ---- workflow: who holds a paper and how far it got. Each returns {paper}.
  claim: (uid) => jpost(P(`/papers/${U(uid)}/claim`), {}),
  // assigneeId null sends the paper back to the pool.
  assign: (uid, assigneeId) =>
    jpost(P(`/papers/${U(uid)}/assign`), { assigneeId: assigneeId || null }),
  submit: (uid) => jpost(P(`/papers/${U(uid)}/submit`), {}),
  // status: "excluded" | "unextractable"; the reason is required.
  exclude: (uid, status, reason) =>
    jpost(P(`/papers/${U(uid)}/exclude`), { status, reason }),
  reopen: (uid, reason) =>
    jpost(P(`/papers/${U(uid)}/reopen`), reason ? { reason } : {}),
  history: (uid) => jget(P(`/papers/${U(uid)}/history`)),

  // ---- background jobs: adding papers by PMID and AI extraction run on the
  // server, so closing the modal (or the tab) doesn't stop them. See jobs.js.
  // spec: {kind:"import", refs:[pmid]}
  //     | {kind:"extract", refs:[uid], models, promptId?, parseEngine?, force?}
  createJob: (spec) => jpost(P("/jobs"), spec),
  listJobs: () => jget(P("/jobs")),
  getJob: (jid) => jget(P(`/jobs/${U(jid)}`)),
  cancelJob: (jid) => jpost(P(`/jobs/${U(jid)}/cancel`), {}),
  // A NEW job holding only the failed / cancelled items of this one.
  retryJob: (jid) => jpost(P(`/jobs/${U(jid)}/retry`), {}),

  // ---- team and join links (managers) ----
  members: () => jget(P("/members")),
  directory: () => jget(P("/directory")),
  addMember: (userId, role) => jpost(P("/members"), { userId, role }),
  removeMember: (userId) => jpost(P(`/members/${U(userId)}/remove`), {}),
  links: () => jget(P("/links")),
  // The reply's `url` is shown once; the server keeps only a hash of the token.
  createLink: (days, maxUses) =>
    jpost(P("/links"), { days, ...(maxUses ? { maxUses } : {}) }),
  revokeLink: (lid) => jpost(P(`/links/${U(lid)}/revoke`), {}),
};
