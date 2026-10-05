// Shared test fixtures.
//
//   server   (worker)  a real `run.py` on a free port, fresh data dir, its own
//                      bootstrap superadmin; killed when the worker ends
//   admin    (worker)  an API client signed in as that superadmin (password
//                      already changed)
//   world    (test)    helpers that set up people, projects and papers FAST,
//                      over the API — so each test spends browser time only on
//                      the flow it is actually checking
//
// Real network: PMID import jobs call NCBI; extraction calls OpenRouter only
// when E2E_KEYS points at an env file with OPENROUTER_API_KEY (tests that need
// it skip otherwise).
import { test as base, expect, request as pwRequest } from "@playwright/test";
import { spawn } from "node:child_process";
import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { samplePaper } from "./pdf.mjs";

export { expect };

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, "..", "..");
export const ADMIN = { email: "root@e2e.test", password: "bootstrap-pass-1", newPassword: "root-pass-e2e-2" };
export const PMIDS = { oa1: "30049270", oa2: "31043620", paywalled: "25686637", missing: "99999999" };
export const FREE_MODEL = "openai/gpt-oss-20b:free";

let seq = 0;
export const uniq = (p = "x") => `${p}${process.pid}${Date.now().toString(36)}${(seq++).toString(36)}`;

function freePort() {
  return new Promise((res, rej) => {
    const s = net.createServer();
    s.listen(0, "127.0.0.1", () => { const { port } = s.address(); s.close(() => res(port)); });
    s.on("error", rej);
  });
}

async function startServer() {
  // run.py imports itself as the package "annotaid", so it must run from a
  // directory with that name: link one to this checkout.
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "annotaid-e2e-"));
  fs.symlinkSync(REPO, path.join(root, "annotaid"));
  const data = path.join(root, "data");
  const port = await freePort();
  const keys = process.env.E2E_KEYS ? path.resolve(process.env.E2E_KEYS) : "/dev/null";
  const proc = spawn("python3", [path.join(root, "annotaid", "run.py"),
    "--port", String(port), "--data-dir", data, "--keys", keys], {
    env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1",
      ANNOTAID_ADMIN_EMAIL: ADMIN.email, ANNOTAID_ADMIN_PASSWORD: ADMIN.password,
      ANNOTAID_ADMIN_NAME: "Root Admin" },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let log = "";
  proc.stdout.on("data", (d) => { log += d; });
  proc.stderr.on("data", (d) => { log += d; });
  const base = `http://127.0.0.1:${port}`;
  for (let i = 0; i < 100; i++) {
    try { const r = await fetch(base + "/login"); if (r.ok) break; } catch { /* not up yet */ }
    await new Promise((r) => setTimeout(r, 100));
    if (i === 99) throw new Error("server did not start:\n" + log);
  }
  return { base, port, root, data, proc, log: () => log,
    extraction: /OPENROUTER_API_KEY/.test(fs.existsSync(keys) && keys !== "/dev/null" ? fs.readFileSync(keys, "utf8") : ""),
    async restart() {
      proc.kill("SIGTERM");
      await new Promise((r) => proc.once("exit", r));
      const again = spawn("python3", [path.join(root, "annotaid", "run.py"),
        "--port", String(port), "--data-dir", data, "--keys", keys], {
        env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" }, stdio: ["ignore", "pipe", "pipe"] });
      again.stdout.on("data", (d) => { log += d; });
      again.stderr.on("data", (d) => { log += d; });
      this.proc = again;
      for (let i = 0; i < 100; i++) {
        try { const r = await fetch(base + "/login"); if (r.ok) return; } catch { /* not up */ }
        await new Promise((r) => setTimeout(r, 100));
      }
      throw new Error("server did not restart:\n" + log);
    },
    stop() { try { this.proc.kill("SIGTERM"); } catch { /* gone */ } },
  };
}

// --------------------------------------------------------------------------- //
// API client: a Playwright APIRequestContext with this site's Origin on POSTs.
export async function apiClient(base, storageState) {
  const ctx = await pwRequest.newContext({ baseURL: base, storageState,
    extraHTTPHeaders: { Origin: base } });
  const call = async (method, url, body, opts = {}) => {
    const r = await ctx.fetch(url, { method, data: body === undefined ? (method === "POST" ? {} : undefined) : body, ...opts });
    let json = null;
    try { json = await r.json(); } catch { /* not JSON */ }
    return { status: r.status(), json, headers: r.headers() };
  };
  return {
    ctx, call,
    get: (u, o) => call("GET", u, undefined, o),
    post: (u, b, o) => call("POST", u, b, o),
    async ok(method, u, b) {
      const r = await call(method, u, b);
      if (r.status >= 300) throw new Error(`${method} ${u} -> ${r.status} ${JSON.stringify(r.json)}`);
      return r.json;
    },
    dispose: () => ctx.dispose(),
  };
}

async function signedInClient(base, email, password, newPassword) {
  const c = await apiClient(base);
  let r = await c.post("/api/login", { email, password });
  if (r.status !== 200) throw new Error(`login ${email}: ${r.status} ${JSON.stringify(r.json)}`);
  if (r.json.user.mustChangePassword && newPassword) {
    await c.ok("POST", "/api/me/password", { current: password, new: newPassword });
  }
  return c;
}

// --------------------------------------------------------------------------- //
export const test = base.extend({
  server: [async ({}, use) => {
    const s = await startServer();
    await use(s);
    s.stop();
  }, { scope: "worker" }],

  admin: [async ({ server }, use) => {
    // Idempotent per worker: the first test changes the bootstrap password.
    let c;
    const tryLogin = await apiClient(server.base);
    const r = await tryLogin.post("/api/login", { email: ADMIN.email, password: ADMIN.newPassword });
    if (r.status === 200) c = tryLogin;
    else { await tryLogin.dispose(); c = await signedInClient(server.base, ADMIN.email, ADMIN.password, ADMIN.newPassword); }
    await use(c);
    await c.dispose();
  }, { scope: "worker" }],

  baseURL: async ({ server }, use) => { await use(server.base); },

  world: async ({ server, admin, browser }, use) => {
    const contexts = [];
    const w = {
      base: server.base,
      server,
      admin,
      adminCreds: { email: ADMIN.email, password: ADMIN.newPassword, name: "Root Admin" },

      /** An active account, password already changed. role: user|manager|superadmin */
      async user(role = "user", name) {
        name = name || `${role[0].toUpperCase()}${role.slice(1)} ${uniq("")}`;
        const email = `${uniq(role)}@e2e.test`;
        const r = await admin.ok("POST", "/api/admin/users", { name, email, role });
        const password = `pw-${uniq()}-long`;
        const c = await signedInClient(server.base, email, r.tempPassword, password);
        await c.dispose();
        return { id: r.user.id, email, password, name, role };
      },

      /** An account straight from the admin, still on its temporary password. */
      async userWithTempPassword(role = "user") {
        const name = `Temp ${uniq("")}`;
        const email = `${uniq("tmp")}@e2e.test`;
        const r = await admin.ok("POST", "/api/admin/users", { name, email, role });
        return { id: r.user.id, email, password: r.tempPassword, name, role };
      },

      /** API client signed in as `u`. */
      async api(u) {
        const c = await signedInClient(server.base, u.email, u.password);
        contexts.push(c);
        return c;
      },

      /** A browser page signed in as `u` (its own context = its own cookies). */
      async page(u) {
        const ctx = await browser.newContext({ baseURL: server.base });
        contexts.push(ctx);
        if (u) {
          const r = await ctx.request.post("/api/login", { data: { email: u.email, password: u.password },
            headers: { Origin: server.base } });
          if (r.status() !== 200) throw new Error(`login ${u.email}: ${r.status()} ${await r.text()}`);
        }
        const page = await ctx.newPage();
        return page;
      },

      /** A project owned by manager `m` (created over the API), with members. */
      async project(m, { name, curators = [], managers = [] } = {}) {
        const api = await w.api(m);
        const p = await api.ok("POST", "/api/projects", { name: name || `Project ${uniq("")}`, description: "e2e" });
        for (const c of curators) await api.ok("POST", `/api/projects/${p.id}/members`, { userId: c.id, role: "curator" });
        for (const x of managers) await admin.ok("POST", `/api/projects/${p.id}/members`, { userId: x.id, role: "manager" });
        return { ...p, api };
      },

      /** Upload a generated PDF, confirm a fake PMID, and import an AI run for
       *  it so there are values to curate — no network, no AI cost. */
      async paper(project, { pmid, model = "seeded-model", values } = {}) {
        pmid = pmid || String(10_000_000 + Math.floor(Math.random() * 80_000_000));
        const pdf = samplePaper(`${pmid}-${uniq()}`);
        const up = await project.api.call("POST", `/api/projects/${project.id}/papers`, undefined, {
          data: pdf, headers: { "Content-Type": "application/pdf", "X-Filename": `${pmid}.pdf` } });
        if (up.status !== 201) throw new Error(`upload: ${up.status} ${JSON.stringify(up.json)}`);
        const uid = up.json.uid;
        await project.api.ok("POST", `/api/projects/${project.id}/papers/${uid}/identity`, { pmid, source: "manual" });
        const data = values || {
          pubmed_id: pmid,
          p_value: { value: 5.2e-8, evidence: "The lead association reached p = 5.2e-8" },
          countries: { value: ["Kenya", "Nigeria"], evidence: "participants from Kenya and Nigeria" },
          population_description: { value: "adults aged 30-65 from rural clinics", evidence: "adults aged 30-65 from rural clinics" },
          sample_size: { value: 1250, evidence: "We recruited 1250 participants" },
          mixed_population: { value: true, evidence: "The cohort was a mixed population" },
        };
        const imp = await project.api.ok("POST", `/api/projects/${project.id}/import`,
          { items: [{ filename: `${pmid}_${model}.json`, data, modelId: model, pmid }] });
        return { uid, pmid, model, imported: imp };
      },

      async assign(project, paper, user) {
        return project.api.ok("POST", `/api/projects/${project.id}/papers/${paper.uid}/assign`,
          { assigneeId: user ? user.id : null });
      },

      /** Poll a job until it leaves queued/running. */
      async waitJob(api, pid, jid, timeoutMs = 180_000) {
        const end = Date.now() + timeoutMs;
        for (;;) {
          const j = await api.ok("GET", `/api/projects/${pid}/jobs/${jid}`);
          if (!["queued", "running"].includes(j.state)) return j;
          if (Date.now() > end) throw new Error(`job ${jid} still ${j.state}`);
          await new Promise((r) => setTimeout(r, 1000));
        }
      },
    };
    await use(w);
    for (const c of contexts) { try { await (c.close ? c.close() : c.dispose()); } catch { /* closed */ } }
  },
});
