// Thin fetch wrapper for every backend endpoint. The ONLY coupling seam to the server.

async function jget(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error((await safeErr(r)) || r.statusText);
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
  config: () => jget("/api/config"),
  state: () => jget("/api/state"),

  // pmid: optional — the curator named it on the fetch tab before falling back
  // to a manual upload. Prefills the confirm box; never auto-commits.
  uploadPdf: async (file, pmid) => {
    const r = await fetch("/api/papers", {
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
    jpost(`/api/papers/${encodeURIComponent(uid)}/pmid`, { pmid, source }),

  fetchByPmid: (pmid, opts) => jpost("/api/papers/fetch-by-pmid", { pmid }, opts),

  pdfUrl: (ident) => `/api/pdf/${encodeURIComponent(ident)}`,

  extract: (pmid, models, promptId, parseEngine, force) =>
    jpost("/api/extract", { pmid, models, promptId, parseEngine, force }),

  importItems: (items) => jpost("/api/import", { items }),

  runs: (pmid) => jget(`/api/runs/${encodeURIComponent(pmid)}`),

  saveRun: (pmid, modelId, features) =>
    jpost(`/api/runs/${encodeURIComponent(pmid)}`, { modelId, features }),

  saveState: (ui) => jpost("/api/state", ui),
};
