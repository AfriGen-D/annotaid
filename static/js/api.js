// Thin fetch wrapper for every backend endpoint. The ONLY coupling seam to the server.
//
// Everything a curator does happens inside a project, so almost every path is
// prefixed with /api/projects/<id>. The id lives in the URL rather than as a
// "current project" on the server: a curator with two projects open in two tabs
// is an obvious thing to do, and server-side current-project state would quietly
// cross their work over.

let PID = null;

export function setProject(id) { PID = id; }
export function projectId() { return PID; }

function P(path) {
  if (!PID) throw new Error("no project selected");
  return `/api/projects/${encodeURIComponent(PID)}${path}`;
}

async function jget(path) {
  const r = await fetch(path);
  if (!r.ok) { const e = new Error((await safeErr(r)) || r.statusText); e.status = r.status; throw e; }
  return r.json();
}
async function jpost(path, body, opts) {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal: opts && opts.signal,
  });
  if (!r.ok) { const e = new Error((await safeErr(r)) || r.statusText); e.status = r.status; throw e; }
  return r.json();
}
async function safeErr(r) {
  try { return (await r.json()).error; } catch { return null; }
}

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
      headers: { "Content-Type": "text/csv; charset=utf-8" },
      body: csvText,
    });
    if (!r.ok) throw new Error((await safeErr(r)) || r.statusText);
    return r.json();   // {ok, features} | {ok:false, error}
  },

  // The local curator's own identity (name, email, optional personal
  // OpenRouter key) — not project-scoped, one per running instance.
  getIdentity: () => jget("/api/identity"),
  saveIdentity: (fields) => jpost("/api/identity", fields),

  // ---- project ----
  // The portable document — what a project manager hands to a curator.
  projectDocument: (id) => jget(`/api/projects/${encodeURIComponent(id)}`),
  updateProject: (id, fields) => jpost(`/api/projects/${encodeURIComponent(id)}`, fields),
  archiveProject: (id) => jpost(`/api/projects/${encodeURIComponent(id)}/archive`, {}),

  // Explicit-id variants for the home screen's project-card menu, which never
  // calls setProject() — P() would throw with no project "open".
  configFileUrl: (id) => `/api/projects/${encodeURIComponent(id)}/config-file`,
  exportUrlFor: (id, fmt) =>
    `/api/projects/${encodeURIComponent(id)}/export?format=${encodeURIComponent(fmt)}`,

  config: () => jget(P("/config")),
  state: () => jget(P("/state")),

  // pmid: optional — the curator named it on the fetch tab before falling back
  // to a manual upload. Prefills the confirm box; never auto-commits.
  uploadPdf: async (file, pmid) => {
    const r = await fetch(P("/papers"), {
      method: "POST",
      headers: {
        "Content-Type": "application/pdf",
        "X-Filename": file.name,
        ...(pmid ? { "X-Suggested-Pmid": String(pmid) } : {}),
      },
      body: file,
    });
    if (!r.ok) throw new Error((await safeErr(r)) || r.statusText);
    return r.json();
  },

  confirmPmid: (uid, pmid, source) =>
    jpost(P(`/papers/${encodeURIComponent(uid)}/identity`), { pmid, source }),

  // A paper that genuinely has no PubMed ID. This RESOLVES its identity (and so
  // unblocks curation) rather than skipping the question.
  markNoPmid: (uid, doi) =>
    jpost(P(`/papers/${encodeURIComponent(uid)}/identity`), { status: "none", doi }),

  // The curator's own declared row identity for the project's one repeating
  // group (e.g. which variants/haplotypes this paper reports on) — set BEFORE
  // extraction, never proposed by the AI. Replaces the list wholesale; the
  // server reassigns a rowId only for an item it doesn't already recognise.
  saveGroupItems: (uid, items) =>
    jpost(P(`/papers/${encodeURIComponent(uid)}/group-items`), { items }),

  fetchByPmid: (pmid, opts) => jpost(P("/papers/fetch-by-pmid"), { pmid }, opts),

  pdfUrl: (uid) => P(`/pdf/${encodeURIComponent(uid)}`),

  // Keyed on uid, not pmid — a paper may legitimately have no PubMed ID.
  extract: (uid, models, promptId, parseEngine, force) =>
    jpost(P("/extract"), { uid, models, promptId, parseEngine, force }),

  importItems: (items) => jpost(P("/import"), { items }),

  runs: (uid) => jget(P(`/runs/${encodeURIComponent(uid)}`)),

  // payload: {features, groups}
  saveRun: (uid, modelId, payload) =>
    jpost(P(`/runs/${encodeURIComponent(uid)}`), { modelId, ...payload }),

  // Explicitly project-addressed: on boot, store.js flushes edits buffered in a
  // PREVIOUS session, which may belong to a project other than the open one.
  // Posting those through the PID-relative saveRun would file them under the
  // wrong project.
  saveRunIn: (pid, uid, modelId, payload) =>
    jpost(`/api/projects/${encodeURIComponent(pid)}/runs/${encodeURIComponent(uid)}`,
          { modelId, ...payload }),

  saveState: (ui) => jpost(P("/state"), ui),

  exportUrl: (fmt) => P(`/export?format=${encodeURIComponent(fmt)}`),
};
