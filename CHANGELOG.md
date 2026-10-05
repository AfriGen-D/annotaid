# AnnotAid — Changelog

## Baseline — before the changes introduced over the past few days

State as of the first presentation ("Good evening everybody... In this presentation, I will introduce AnnotAid", `references/pres1.txt`, slides in `AnnotAid - an introduction.pdf`) and the commit history up to `6e85d31`:

- **Core mechanism, unchanged since:** an LLM does a first pass on a supplied paper, identifying the sections relevant to each curated feature and proposing a value; the curator confirms or corrects it on the interface; the curator downloads the curated values and adds them to a shared datasheet. This description from the first presentation is still exactly how the tool works today — everything since has been architecture and workflow around this same core.
- Single local project, single JSON config — no multi-project support, no per-project SQLite store, no concept of a project manager configuring something to hand to curators.
- Interface was two panes: Documents List + PDF viewer, plus the extraction results.
- "Add a paper" — one PMID at a time (paste and load) or one PDF upload — no batch/stepper flow, no PMID+variant paste format.
- "Run AI extraction" was a single action against one paper, one model — no multi-model checklist.
- Extracted feature set was the general-purpose kind (e.g. `countries` as a plain array-of-strings value) rather than the later statistical/study-field rework.
- Per value: "AI proposed: X", the supporting evidence quote highlighted back onto the PDF, click to jump to it, edit, confirm.
- Output: download as CSV.
- Live-demo script for this version: load the (empty) site, introduce the three sections, add a PDF (show both options), copy a PMID, load the paper, show the viewer, run AI extraction, show results, show the highlight, click to add, edit a value, confirm, download as CSV.
- Later, still pre-"past few days": multi-paper stepper for adding papers, model dropdown replaced by a header dropdown + run-dot checklist, feature set reworked to statistical/study fields (p-value, countries, population description, sample size, mixed population), PMID-fetch modal with extraction spinner and PDF-value picker, tooltips on feature descriptions.

## Developer meeting

Presented AnnotAid to the team — first to introduce the tool and get feedback (the first presentation above), then separately to request permission to host it on a project server (`references/pres2.txt`). That second presentation framed v1 (this local-first, single-user app) as the proven core, and proposed a v2: a cloud application for one institution running multiple concurrent projects, with three roles —

- **Super Admin:** create/archive/read every project, manage user accounts, assign project managers, a schema editor with immutable versions (schema forks on edit once a run exists, with a 3-paper preview before committing a version), a model registry with per-model pricing and spend dashboard, job-queue control, a global audit log, and invite-link management.
- **Project Manager:** add curators (directly or via invite link), add papers in bulk (single PMID / file of PMIDs / single or bulk PDF, with per-row progress and PDF fallback, duplicate detection on content hash/PMID/DOI), trigger bulk extraction with a cost estimate shown before confirming, assign papers or leave them pooled, a per-curator progress dashboard, CSV/audit-JSON export.
- **Curator:** a work queue, the same paper view (PDF viewer, evidence click-to-highlight, list/card modes), per-field edit-and-confirm with the AI's original value always retained alongside, select-text-in-PDF to set a value directly, flag a paper as excluded/unextractable with a reason, submit, autosave throughout.

Proposed stack for that v2: FastAPI + Pydantic backend, PostgreSQL with Alembic migrations and JSONB for polymorphic field values, a Postgres-backed job queue (procrastinate) with retries/idempotency/cancellation, MinIO (S3-compatible) file storage, session-cookie auth (argon2id, no email sending), a React/TypeScript/Vite/TanStack Query/Tailwind frontend porting v1's pdf.js evidence-highlighting, Server-Sent Events for progress, the same external services as v1 (OpenRouter, NCBI E-utils, PMC, OpenAlex, Unpaywall, Europe PMC), deployed via Docker Compose on an institutional VM with Caddy and Sentry.

The response to the tool itself was positive. Hosting a server for it was not going to be granted as part of the project.

## Follow-up

Redesigned the collaboration model to work without a server, keeping v1's local-first core rather than building the v2 cloud stack:

- The tool runs entirely locally — each person downloads it and runs their own instance.
- A project manager creates a project and configures it (features, models, prompts, etc.) — the multi-project, config-driven layer that v1 never had.
- The project is shared with curators as its config file — that's the artifact that gets handed over, not a hosted link or account.
- Curators load the shared config locally, get the paper ids to curate from a shared Google Sheet, add those papers (by PMID or PDF), and curate.
- Curated values are downloaded and pasted back into that same shared Google Sheet. The download-to-shared-sheet step itself isn't new — it's in the first presentation's original description of the tool — what's new is closing the loop: the sheet also now supplies the task list (which paper ids to curate), and a shared project config is what makes multiple people's local instances actually the *same* project rather than independent copies.

---

**Sourcing:** Baseline and Developer Meeting are drawn directly from `references/pres1.txt`, `references/pres2.txt`, `references/AnnotAid - an introduction.pdf`, and the real git history (`c5ec2be` init through `6e85d31`). Follow-up is written from the description given directly for this changelog, since no reference document covers the post-meeting decision itself.
