# AnnotAid v2

AnnotAid is an AI-assisted biocuration tool: an LLM proposes structured field values from a paper's PDF, and a human curator verifies each against evidence in the source and confirms it, with the original AI value kept alongside the curated one for audit.

v2 turns the local single-user v1 into a hosted app for one institution running multiple projects, with login, superadmin / project-manager / curator roles, and background jobs.

Items marked **(v1)** already exist single-user and are extended, not rebuilt.

---

## Stack

| Layer | Choice |
|---|---|
| Backend | Python stdlib HTTP server (v1), extended |
| Database | SQLite (WAL), single file |
| Background jobs | `jobs` table + one worker thread in the same process; browser polls status |
| File storage | PDFs on a local volume, served only through authenticated endpoints |
| Auth | Email + password, `hashlib.scrypt` (stdlib), server-side sessions in SQLite, httpOnly cookies, no email sending |
| Frontend | Vanilla ES modules + pdf.js (v1) |
| External | OpenRouter (one server-side key), NCBI E-utils, PMC, OpenAlex, Unpaywall, Europe PMC |
| Deploy | One Docker container + Caddy (HTTPS) on the institutional VM, nightly backup of DB + PDF volume |

Dependencies: `jsonschema` (v1). No new ones.

---

## Must have

### Auth and roles
- M1. Email + password login, logout, server-side sessions.
- M2. Three roles — superadmin and project manager (account-level), curator (per project) — enforced server-side on every endpoint.

### Curator
- M3. Work queue: "my papers" + unassigned pool, filter by status.
- M4. Paper view: PDF viewer, evidence click-to-highlight, list and card modes **(v1)**.
- M5. Per-field edit and confirm, immutable `aiValue` kept alongside **(v1)**.
- M6. Select text in PDF to set a field value **(v1)**.
- M7. Only the assigned curator (or whoever claimed it from the pool) can edit a paper.
- M8. Every edit and confirm records who and when.
- M9. Paper status: unassigned → in progress → submitted, or excluded / unextractable with a reason.
- M10. Autosave to server throughout.

### Project manager
Everything a curator can do, plus:
- M11. Create projects; the creator becomes that project's manager.
- M12. Schema editor: features, system prompt, model, parse engine **(v1)**.
- M13. Add curators to the project from existing user accounts.
- M14. Add papers: single PMID, file of PMIDs, single PDF, bulk PDF, per-row progress, retry, PDF fallback **(v1)** — run as a background job.
- M15. Duplicate detection on content hash and PMID **(v1)**.
- M16. Trigger extraction in bulk as a background job, with per-paper status, error and retry.
- M17. Assign papers to curators, or leave them in the pool; reassign.

### Super admin
Everything a project manager can do, plus:
- M18. Create user accounts, set their role, reset passwords manually, deactivate users and memberships.
- M19. Archive and read every project.
- M20. Assign additional project managers to a project.

### Platform
- M21. Background job runner (queued / running / done / failed / cancelled, error text).
- M22. Docker + Caddy deploy on the institutional VM.
- M23. Nightly backup of DB and PDFs, with a tested restore.

---

## Should have

- S1. Cost estimate shown before confirming a bulk extraction (pages × token price, prices from config).
- S2. Cancel a running batch.
- S3. Project dashboard: paper counts by status, per-curator progress, failed fetches, failed extractions.
- S4. Schema lock: once any extraction exists, the schema can't be edited; copy the project to change it.

---

## Could have

- C1. Duplicate detection on DOI.
- C2. Invite link: one project, curator role only, expiring, revocable, use-capped; superadmin can view and revoke all.
- C3. Superadmin job-queue view: view, retry, cancel across projects.
- C4. Error tracking (Sentry) and structured logs.
- C5. Analytics: AI vs curated agreement per field and per model, curation throughput over time.
