---
marp: true
paginate: true
theme: default
size: 16:9
title: AnnotAid v2 — hosted release scope
style: |
  section { font-size: 24px; padding: 48px 64px; }
  section.dense { font-size: 17px; padding: 32px 56px; }
  section.dense h2 { font-size: 28px; margin-bottom: 8px; }
  section.dense table { font-size: 15px; }
  section.dense th, section.dense td { padding: 3px 8px; }
  section.dense pre { font-size: 14px; margin: 6px 0; }
  h1 { font-size: 40px; color: #1d3557; }
  h2 { font-size: 32px; color: #1d3557; }
  table { font-size: 20px; }
  th { background: #e9eef5; }
  code { font-size: 0.9em; }
  .in { color: #1b7f3b; font-weight: 600; }
  .out { color: #b3261e; font-weight: 600; }
  .later { color: #8a5a00; font-weight: 600; }
  .ask { background: #fff4d6; border-left: 6px solid #e0a800; padding: 6px 14px; }
  .small { font-size: 0.8em; color: #555; }
---

# AnnotAid v2 — hosted release

## What goes in, what stays out, and the decisions needed before code

Scope review · no code written yet

<span class="small">Every slide with a yellow box needs a yes/no from you. They are collected again at the end.</span>

---

## The constraints we're designing for

| Constraint | What it means for the design |
|---|---|
| Existing Rocky Linux 9.5 server, other live apps, nginx already running | We fit in beside them: one systemd service on a localhost port, nginx in front. No Docker, no Caddy. |
| **You have no direct server access** | Everything server-side goes into a one-page runbook a sysadmin follows. Admin tasks (users, passwords) need a **web UI**, not a CLI. You also can't read logs, so the app has to show its own errors. |
| HTTPS | Handled by the existing nginx. NFR-5 is the sysadmin's job, not ours. |
| Python | Rocky 9's default is 3.9. The current code parses as 3.9. ✔ |
| Dependencies | Standard library + `jsonschema` only (unchanged) |
| Time | 1 day build + 1 day test **(see the honest estimate on slide 5)** |
| Scale | ~10–15 accounts, ~2 people working at once |

---

## Step zero: the multi-project version isn't committed yet

The app you've been building (projects, per-project SQLite store, feature editor, home screen) lives in three places:

- `stash@{0}` ("WIP on v1"): 28 files changed, including the new `handlers.py` with `/api/projects/{pid}/...` routes and the removal of `store.py`
- **untracked** files on `main`: `server/db.py`, `projects.py`, `project_store.py`, `features.py`, `feature_sheet.py`, `identity.py`, `static/home.html`, 8 JS modules, `tests/`
- `main` itself is still the old single-store v1

All of v2 is built on that foundation. Right now one `git stash drop` or `git clean` would delete it.

<div class="ask">

**Decision 1:** which branch does the foundation get committed to (`dev`, `v1`, or a new `v2`)? That's where v2 work happens.

</div>

---

## Two releases, not one

Think of it as a building. **R1 locks the doors and gets the lifts working.** **R2 adds the sign-up sheet that says who sits at which desk.**

| | Contents | Goes live |
|---|---|---|
| **R1: Hosted & safe** | Login, sessions, auth gate, server-only API key, admin UI, project membership (manager / curator), attribution, **background jobs for import + extraction**, deploy runbook, in-app backup | After the test day |
| **R2: Workflow** | Paper assignment, claim from pool, status lifecycle, work queue, "only the assignee edits" lock | A few days later, as an upgrade |

**Trick that makes R2 cheap to deploy:** R1 creates *every* new table and column, including R2's. The current `db.py` refuses to start on a schema mismatch and has no migrator. If the R2 columns already exist, R2 is a code-only upgrade with no data migration on a server you can't reach.

---

<!-- _class: dense -->

## Honest estimate

Rough build hours with me writing the code and you reviewing. I can't promise these to better than ±50%.

| # | Item | Release | Hours |
|---|---|---|---|
| 0 | Commit foundation to a branch | R1 | 0.25 |
| 1 | Schema v6: all new tables/columns (R1 + R2) + a minimal migrator for future versions | R1 | 1 |
| 2 | Users, sessions, login / logout / change password | R1 | 2 |
| 3 | Auth gate + membership check + role matrix | R1 | 1.5 |
| 4 | Server-only OpenRouter key; logged-in user replaces the identity modal | R1 | 1 |
| 5 | Admin UI (users, all projects, team panel) + first-admin bootstrap | R1 | 3 |
| 6 | Attribution (who edited / confirmed) wired through | R1 | 1 |
| 7 | Job runner + PMID-import job + extraction job + jobs panel | R1 | 4.5 |
| 8 | Deploy pack: runbook, systemd unit, nginx block, backup thread, error viewer | R1 | 1.5 |
| | **R1 total** | | **≈ 16 h ≈ 2 build days** |
| 9 | Assignment, claim, status lifecycle, work queue, edit lock, history | R2 | 4–5 |

<div class="ask">

**Decision 2:** R1 doesn't fit in one day. Either take 2 build days, **or** ship items 0–6 + 8 first (≈ 11 h) and deploy jobs (item 7) as the first upgrade. Jobs add no schema because item 1 already created it.

</div>

---

<!-- _class: dense -->

## In / Out at a glance

| <span class="in">IN (R1)</span> | <span class="later">IN (R2)</span> | <span class="out">OUT of this release</span> |
|---|---|---|
| Email + password login, logout | Assign / reassign papers (M17) | Docker, Caddy, CI |
| Server-side sessions, httpOnly + Secure cookies | Claim from the pool (M3) | Email of any kind: invites, "forgot password", verification |
| Every page, API call and PDF behind login | Status: unassigned → in progress → submitted / excluded / unextractable (M9) | Cost estimates, spend dashboard |
| Server-only OpenRouter key | Work queue: "my papers" + pool + status filter | Schema versioning / locking |
| Admin UI: users, roles, reset password, deactivate | Only the assignee can edit (M7) | Full per-edit history (we keep the *last* editor per field) |
| Superadmin sees and archives all projects | Assignment / status history log | 2FA, SSO / university login |
| Project team: managers + curators | Manager can reopen a submitted paper | More than one server process |
| Background jobs: PMID import, extraction | | Live push updates (SSE / websockets); we poll instead |
| Last-editor attribution per field | | Global cross-project job queue view |
| Login throttling, CSRF protection | | Self-registration |
| Nightly DB backup + in-app error viewer | | Moving existing local data automatically (see slide 19) |

---

## Roles: three of them, two levels

**Account level** (on the user):

- **superadmin**: everything, in every project, plus the admin UI
- **manager**: may *create* projects. Creating one makes you its manager.
- **user**: can only be added to projects by someone else

**Project level** (membership of a project):

- **manager** of project X: runs X (schema, papers, jobs, team, export)
- **curator** in project X: curates papers in X

One person can manage project A and curate in project B.

**Not a member?** Every `/api/projects/{pid}/...` call returns **404, not 403**, so outsiders can't even confirm the project exists (M2: "without revealing data").

---

<!-- _class: dense -->

## Permission matrix: what we test against on day 2

| Action (endpoint group) | Superadmin | Project manager | Curator | Non-member | Anonymous |
|---|---|---|---|---|---|
| Log in / out, change own password | ✔ | ✔ | ✔ | ✔ | login only |
| List projects | all | own | own | own (none) | 401 |
| Create project | ✔ | ✔ (account role) | ✗ | ✗ | 401 |
| Edit schema / settings | ✔ | ✔ | ✗ | 404 | 401 |
| Archive / unarchive project | ✔ | ✗ | ✗ | 404 | 401 |
| Add / remove curators | ✔ | ✔ | ✗ | 404 | 401 |
| Add / remove project managers (M20) | ✔ | ✗ | ✗ | 404 | 401 |
| View papers, PDFs, runs | ✔ | ✔ | ✔ | 404 | 401 |
| Add papers (upload, PMID import job) | ✔ | ✔ | ✗ | 404 | 401 |
| Start / cancel / retry extraction job | ✔ | ✔ | ✗ ⓓ | 404 | 401 |
| Edit / confirm values (autosave) | R1: any member · R2: assignee only ⓓ | | | 404 | 401 |
| Assign / reassign, reopen (R2) | ✔ | ✔ | ✗ | 404 | 401 |
| Claim, submit, exclude (R2) | as assignee | as assignee | as assignee | 404 | 401 |
| Export CSV / audit JSON | ✔ | ✔ | ✗ ⓓ | 404 | 401 |
| Admin UI (users) | ✔ | ✗ 403 | ✗ 403 | ✗ 403 | 401 |

<span class="small">ⓓ = a decision on slide 21. Enforced on the server: the gate sits in `app._dispatch`, and the membership check in `@project_route`. Hiding buttons in the UI is a convenience only.</span>

---

<!-- _class: dense -->

## Authentication: how it works

- **Tables:** `users` (email, name, scrypt hash, account role, active, must-change-password, last login) and `sessions`
- **Passwords:** `hashlib.scrypt`, random 16-byte salt per user, minimum 10 characters. Nobody can read a password back, including a superadmin.
- **Session token:** random 32 bytes in the cookie. The DB stores only its **SHA-256**, so a copy of the database doesn't hand out live logins.
- **Cookie:** `HttpOnly; Secure; SameSite=Lax; Path=/`. `Secure` is always on in production, with a `--dev` flag to turn it off for local http testing.
- **Expiry:** 12 h idle, 7 days absolute. Logout, password reset and deactivation delete the session rows immediately.
- **Login errors:** always "email or password is incorrect". After 5 failures per email in 15 minutes there's a short lockout (held in memory).
- **CSRF:** `SameSite=Lax`, plus every POST and PUT (autosave) must carry an `Origin` matching the host. nginx must pass `Host` through (it's in the runbook).
- **The gate:** one check in `app._dispatch`. Only `/login`, `POST /api/login` and the login page's own CSS/JS are public. Everything else without a session gets `401` (API) or a redirect to `/login?next=…` (pages). That includes `pdf.js` and every PDF.

---

## The identity modal and the personal API key go away

Today (`server/identity.py`): one `singleton` row holds a name, an email and an **optional personal OpenRouter key**, which overrides the server key in `h_extract`.

In v2:

- "Who am I" becomes **the logged-in user**. A new `GET /api/me` replaces `/api/identity` and returns the user plus their project memberships.
- The OpenRouter key exists **only** in the server's env file (NFR-7). The `app_identity` table is dropped.
- Code that changes: `identity.py` (removed), `h_config` ("extraction available" now depends only on the server key), `h_extract`, `static/js/userIdentity.js` and `identityBox.js` (replaced by a small "signed in as … · change password · log out" menu)
- `projects.created_by_name/email` becomes `created_by` (a user id)

<span class="small">That's four files plus the UI, not a 30-minute job. Estimated at 1 h in item 4.</span>

---

<!-- _class: dense -->

## Admin UI (`/admin`, superadmin only)

**Users tab**
- List: name, email, account role, active, last login
- **Create user**: name, email, role → the app generates a **temporary password, shown once** with a copy button. You pass it on yourself. The user must change it at first login.
- **Reset password**: new temporary password, all their sessions killed
- **Deactivate / reactivate**: sessions killed instantly. The user is never deleted, so their name stays on their edits (M18, NFR-10).
- Change account role
- Guard rails: you can't deactivate yourself or demote the last superadmin

**Projects tab**: every project including archived, open any of them (M19), archive / unarchive, add or remove managers (M20)

**System tab**: app version, DB size, **last backup time** + a **Back up now** button, **last 50 server errors** (since you can't read the logs), and an optional DB snapshot download ⓓ

**Team panel** (inside each project, for its managers): add existing users as curator or manager, remove them (M13)

---

## The first superadmin: the chicken-and-egg problem

The admin UI creates users, but someone has to log in first, and you can't run commands on the server.

| Option | How | Risk |
|---|---|---|
| **A. Env-file bootstrap** (recommended) | Sysadmin adds `ANNOTAID_ADMIN_EMAIL` + `ANNOTAID_ADMIN_PASSWORD` to the same env file as the OpenRouter key. On startup, **if no superadmin exists**, the app creates one with must-change-password set. Ignored afterwards. | Password sits in a root-readable file until you change it, and the forced change makes it useless after that |
| B. `/setup` page while `users` is empty | First person to open the URL becomes superadmin | Whoever gets there first owns the system. On a public URL that's a race you might lose. |

<div class="ask">

**Decision 3:** use option A?

</div>

---

## Background jobs: the idea

Right now, adding 50 PMIDs means the **browser** fires 50 requests, 3 at a time, and extraction runs inside the HTTP request. Close the tab and the work stops.

A job runner turns that into a **to-do list the server owns**:

```
 browser ──POST /jobs──▶  jobs + job_items rows (state: queued)
                               │
               one worker thread picks the oldest queued job
                               │
          runs its items, up to N at a time ──▶ writes progress per item
                               │
 browser ──GET /jobs/{id} every 2 s──▶ sees each row: waiting · running · done · failed
```

- One process, one worker thread (NFR-11). It runs **inside** the existing server, so there's no new service to install.
- Writes go through the existing `Db` write lock, the same as everything else
- Survives the user leaving the page. Doesn't survive a server restart mid-item (next slide).

---

<!-- _class: dense -->

## Background jobs: the details

**Tables:** `jobs(id, project_id, kind, state, params_json, total, n_done, n_failed, cancel_requested, error, created_by, created/started/finished_at)` and `job_items(job_id, seq, ref, state, outcome, error, started/finished_at)`

| | **PMID import job** (M14) | **Extraction job** (M16) |
|---|---|---|
| Input | List of PMIDs (one pasted, a list, or a file) | Paper uids + models + prompt + parse engine + force |
| Per item | NCBI verify → open-access PDF chain → store → dedup | `run_extraction()` as today (its 4-wide per-model pool stays; no extra pool on top) |
| Outcomes | `added` · `duplicate` (not an error, M15) · `no_pdf` · `failed: reason` | `done` · `skipped` (a good run exists, not forced) · `failed: reason` |
| `no_pdf` row | Shows **Upload PDF for this PMID**, using the existing upload with `X-Suggested-Pmid` | n/a |
| Items in parallel | N = 3 (NCBI rate limiter already thread-safe) ⓓ | N = 3 ⓓ |

**States:** job `queued → running → done | failed | cancelled`; item `waiting → running → done | failed | skipped`
**Cancel:** sets a flag. Running items finish, waiting ones become `cancelled`.
**Retry:** "Retry failed" makes a **new job** from the failed refs, so successful items never re-run and the old job stays as a record.
**Restart:** on startup, any `running` item is marked `failed: interrupted by server restart`, and its job becomes retryable. No silent half-states.
**Endpoints:** `POST /api/projects/{pid}/jobs`, `GET …/jobs`, `GET …/jobs/{id}`, `POST …/jobs/{id}/cancel`, `POST …/jobs/{id}/retry`

---

## Jobs: what stays synchronous, and the word "import"

"Import" means two different things in this code:

| Feature | Today | v2 |
|---|---|---|
| Add papers by PMID (`fetch-by-pmid`) | One request per PMID, browser-driven | **Job**, even for a single PMID, so there's one path |
| Upload PDF(s) | One request per file | **Stays synchronous.** The bytes have to come from the browser anyway, and storing them takes under a second. |
| Run AI extraction | One request per paper | **Job**, for one paper or many |
| Import model-output JSONs (`/api/import`) | One request | Stays synchronous, managers only |

The UI keeps the existing stepper and modal. The rows just get their state from the job instead of from in-flight fetches. A **Jobs** panel in each project lists recent jobs with progress, so you can leave and come back.

---

## Attribution (M8)

The question it answers: *"who decided this value, and when?"*

| Where | New columns |
|---|---|
| `run_features` (each cell) | `edited_by`, `confirmed_by` (next to the existing `edited_at`, `confirmed_at`) |
| `run_groups` | `confirmed_by` |
| `papers` | `added_by` |
| `runs` | `requested_by` (who started the extraction) |
| `projects` | `created_by` |

- Stored as user ids. Users are never deleted, so the names always resolve.
- The audit JSON export includes names and emails
- **Last** editor per field, not a full history of every keystroke. Full history is out of scope for this release.
- `aiValue` is still written once and never changed (NFR-9, unchanged)

---

<!-- _class: dense -->

## R2: assignment and status

**On `papers`:** `assignee_id`, `status`, `status_reason`, `status_by`, `status_at`, plus a `paper_events` table (who changed assignment or status, from → to, why, when)

```
                claim / assign                 submit
 unassigned ─────────────────────▶ in progress ─────────▶ submitted
     ▲          reassign keeps it ▲     │  exclude / unextractable (reason required)
     └──── unassign (manager) ────┘     └──────────────▶ excluded · unextractable
                    reopen (manager, logged) ◀── submitted / excluded / unextractable
```

- **Claim is atomic:** `UPDATE … SET assignee_id=? WHERE assignee_id IS NULL`, then check one row changed. Two curators clicking at once can't both win.
- **Edit lock (M7):** saving runs, group items and PMID identity checks `assignee == me AND status == in progress`. Otherwise you get 403 "assigned to <name>". A manager who wants to edit reassigns the paper to themselves.
- **Work queue (M3):** the paper list splits into **My papers** and **Pool**, with a status filter and a status badge on each row
- **Reopen** isn't in the SRS, but without it one wrong "submit" is permanent ⓓ

---

<!-- _class: dense -->

## Deployment runbook (handed to the sysadmin)

| Item | Value |
|---|---|
| Code / data | `/opt/annotaid` (code), `/var/lib/annotaid` (DB, PDF blobs, backups), owned by a dedicated `annotaid` user |
| Python | system `python3` (3.9), or `python3.11` from dnf ⓓ; venv + `jsonschema` (wheel shipped with the release in case PyPI is blocked) |
| Secrets | `/etc/annotaid/env`, mode 0600: `OPENROUTER_API_KEY`, `NCBI_API_KEY`, `UNPAYWALL_EMAIL`, bootstrap admin |
| Service | systemd unit, `--host 127.0.0.1 --port <free port>`, `Restart=on-failure`, **exactly one instance** (NFR-11) |
| nginx | server block (or location), `proxy_pass http://127.0.0.1:<port>`, `client_max_body_size 64m` (matches `MAX_BODY`), `proxy_read_timeout 300s`, pass `Host` / `X-Forwarded-For` / `X-Forwarded-Proto`, HTTP → HTTPS redirect |
| **SELinux** | Rocky enforces it. nginx → local port needs `setsebool -P httpd_can_network_connect 1` (or label the port). This is the classic "502 Bad Gateway for no reason". |
| Outbound | OpenRouter, NCBI E-utils + web, PMC + its open-data store, OpenAlex, Unpaywall, Europe PMC (NFR-8) |
| Backups | The app writes a nightly `sqlite3` backup (online, safe while running) to `/var/lib/annotaid/backups`, keeping 14. Sysadmin includes `/var/lib/annotaid` in the host's existing backups (that covers the PDFs). |
| Upgrades | Release tarball → unpack → `systemctl restart annotaid` |

---

## Where it lives, and what data it starts with

**URL.** The frontend uses absolute paths everywhere (`/api/...`, `/p/{pid}`, and the JS/CSS/vendor assets).

- **Own subdomain** (e.g. `annotaid.<institution>`): works as is
- **Sub-path** (e.g. `<host>/annotaid/`): needs a base-path setting threaded through `api.js`, the HTML shells and the redirects. About +2 h, and another thing to test.

**Data at go-live.** The server DB starts empty at schema v6.

- Projects move across with the existing **download config file → create project from file** flow
- Papers get re-added with a PMID import job, which goes fast on the new runner
- **Curated values from local instances don't move automatically.** If any local curation work must survive, it needs a one-off migration script (~2 h).

<div class="ask">

**Decision 4:** subdomain or sub-path? **Decision 5:** start fresh, or does local curation work have to come over?

</div>

---

<!-- _class: dense -->

## Test day plan (day 2)

| # | Test | Passes when |
|---|---|---|
| 1 | Anonymous `curl` of **every** route in the route table + one PDF URL + one static asset (e.g. pdf.js) | All 401 / redirect to login |
| 2 | Log out, then replay the old cookie | 401 |
| 3 | Deactivate a user while they're logged in | Their next request gets 401 |
| 4 | Walk the permission matrix (slide 8) with 4 accounts: superadmin, manager, curator, outsider | Every cell matches; the outsider gets 404 on project URLs |
| 5 | Copy the DB file and look for passwords / session tokens | Only scrypt hashes and SHA-256 token hashes |
| 6 | Import 30 PMIDs, close the tab, come back | Job finished; duplicates reported as `duplicate`; `no_pdf` rows offer upload |
| 7 | Restart the server mid-extraction-job | Interrupted items say so; "retry failed" re-runs only those |
| 8 | Two curators autosaving in the same project for 30 min | No lost edits; `edited_by` correct |
| 9 | Press **Back up now** in the System tab, then start a fresh instance on that file | Projects, papers, runs, users all there |
| 10 | Admin: create user → temp password → forced change → reset → deactivate | Each step works; sessions die where they should |
| 11 | Browser devtools on any page | No OpenRouter key anywhere in responses |

---

<!-- _class: dense -->

## Decisions needed from you

| # | Question | My recommendation |
|---|---|---|
| 1 | Branch to commit the stashed + untracked foundation to | New `v2` branch, from `v1` + the stash |
| 2 | R1 takes ~2 build days. Extend, or ship jobs as the first upgrade? | Ship 0–6 + 8 on day 1, jobs next. Only if the deadline is fixed. |
| 3 | First superadmin | Env-file bootstrap (option A) |
| 4 | Subdomain or sub-path | Subdomain |
| 5 | Carry over local curation data? | Start fresh, unless real curation exists locally |
| 6 | Who may start extraction (it costs money on one shared key)? | Managers + superadmin only |
| 7 | Can curators export? | No, managers only |
| 8 | In R1 (before assignment), may any member edit any paper? | Yes, with attribution recorded. R2 adds the lock. |
| 9 | Can a manager reopen a submitted / excluded paper? | Yes, logged in `paper_events` |
| 10 | Items in parallel per job | 3 |
| 11 | DB snapshot download in the admin UI? | Yes, superadmin only. Your only off-server copy. |
| 12 | Python 3.9 (system) or 3.11 | 3.11 if the sysadmin agrees; 3.9 works |
| 13 | Who does upgrades, and how fast? | Agree a contact + a tarball + restart procedure **before** go-live |

---

# After your review

1. You answer the 13 decisions (on this deck, or just in chat)
2. I update the deck to match. It becomes the build checklist.
3. Commit the foundation (decision 1), then build R1 in the order of slide 5
4. Day 2: the test plan on slide 20, then hand the runbook to the sysadmin

<span class="small">Source documents: `final_requirements` (SRS, M1–M23, NFR-1–17), `specs.md`, the stashed `server/handlers.py` route table, `server/db.py` (schema v5).</span>
