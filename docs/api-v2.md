# AnnotAid v2 — HTTP API

Every route needs a signed-in, **active** account unless marked **public**.
Without one: API → `401 {"error"}`, page → `302 /login?next=…`.
A user who must change their password gets `403 {"code":"must_change_password"}`
(pages: `302 /account?force=1`) everywhere except the routes marked **pw-ok**.

Every `POST` must carry an `Origin` (or `Referer`) of this site, or it gets `403`.
Browsers do this automatically for `fetch`.

Errors are always `{"error": "message safe to show the user"}`.

Roles: **SA** = superadmin (account role; manager of every project),
**PM** = manager of this project, **C** = curator in this project.
On `/api/projects/{pid}/…` a non-member gets **404** (as if the project didn't exist);
a curator on a PM route gets **403**.

## Pages

| Path | Who | Serves |
|---|---|---|
| `/login`, `/signup`, `/join/{token}` | public | `auth.html` (+ `js/authPages.js`) |
| `/account` | pw-ok | `account.html` |
| `/admin` | SA | `admin.html` |
| `/` | any | `home.html` |
| `/p/{pid}` | any (the app then 404s if not a member) | `index.html` |

Public static files: `/css/*`, `/js/authPages.js`. Everything else needs a session.

## Session and account

| Method | Path | Who | Body | Response |
|---|---|---|---|---|
| POST | `/api/login` | public | `{email, password}` | `{user: Me}` + `Set-Cookie`. `401` wrong credentials / pending / rejected / deactivated (message says which, only after the right password). `429` throttled. |
| POST | `/api/logout` | public | `{}` | `{ok}`; clears the cookie and deletes the session |
| POST | `/api/signup` | public | `{name, email, password, joinToken?}` | `201 {pending: true, email}` |
| GET | `/api/join/{token}` | public | – | `{state: valid\|expired\|revoked\|used_up\|invalid, projectName?, role?, loggedIn}` |
| POST | `/api/join/{token}` | any | `{}` | `{projectId}` |
| GET | `/api/me` | pw-ok | – | `Me` |
| POST | `/api/me/password` | pw-ok | `{current, new}` | `{ok}`; other sessions end |

`Me = {id, email, name, role: superadmin|manager|user, state, mustChangePassword, createdAt, lastLoginAt, memberships: [{projectId, role}], canCreateProjects}`

## Admin (SA)

| Method | Path | Body | Response |
|---|---|---|---|
| GET | `/api/admin/users` | – | `{users: [User]}` (pending first) |
| POST | `/api/admin/users` | `{name, email, role}` | `201 {user, tempPassword}`: show the password once |
| POST | `/api/admin/users/{id}/approve` | `{role}` | `{user, joinedProjectId?, joinError?}` |
| POST | `/api/admin/users/{id}/reject` | `{}` | `{user}` |
| POST | `/api/admin/users/{id}/role` | `{role}` | `{user}` |
| POST | `/api/admin/users/{id}/deactivate` | `{}` | `{user}`; their sessions end |
| POST | `/api/admin/users/{id}/reactivate` | `{}` | `{user}` |
| POST | `/api/admin/users/{id}/reset-password` | `{}` | `{user, tempPassword}`; their sessions end |
| GET | `/api/admin/projects` | – | `{projects: [Project + archivedAt, managers: [Member]]}` incl. archived |
| POST | `/api/admin/projects/{pid}/archive` / `unarchive` | `{}` | `{ok}` |
| GET | `/api/admin/system` | – | `{version, dbSize, extractionEnabled, backups: [{file,size,at}], lastBackup, errors: [{at,method,path,type,message}], users: {state: n}, jobs: {state: n}}` |
| POST | `/api/admin/backup` | `{}` | `{backup, system}` |
| GET | `/api/admin/snapshot` | – | file download (`.db`), a fresh consistent copy |

`User = {id, email, name, role, state: pending|active|rejected|deactivated, mustChangePassword, createdAt, lastLoginAt}`

Guard rails (400 with a message): you can't deactivate yourself, and you can't deactivate or demote the last superadmin.

## Projects

| Method | Path | Who | Notes |
|---|---|---|---|
| GET | `/api/projects` | any | `{projects: [Project + myRole], canCreate}`. Only your projects; SA sees all. |
| POST | `/api/projects` | account role SA or manager | as before; the creator becomes its PM |
| GET | `/api/projects/{pid}/config` | C | as before + `myRole`, `permissions` |
| GET/POST | `/api/projects/{pid}` | PM | portable document / update |
| GET | `/api/projects/{pid}/config-file` | PM | download |
| POST | `/api/projects/{pid}/archive` | SA | |
| GET | `/api/projects/{pid}/export?format=csv\|json` | PM | audit JSON now includes `users` and `events` |

`permissions = {manage, addPapers, extract, export, assign}` (booleans, a UI convenience only).

## Curation state

| Method | Path | Who | Notes |
|---|---|---|---|
| GET | `/api/projects/{pid}/state` | C | `{papers, runs, ui, me: {id, name, role}, permissions, users: {id: name}, members: [Member]}` |
| POST | `/api/projects/{pid}/state` | C | per-user UI state |
| GET | `/api/projects/{pid}/papers` | C | |
| POST | `/api/projects/{pid}/papers` | PM | PDF upload (`X-Filename`, `X-Suggested-Pmid`), synchronous |
| GET | `/api/projects/{pid}/pdf/{id}` | C | |
| GET | `/api/projects/{pid}/runs/{ident}` | C | |
| POST | `/api/projects/{pid}/runs/{ident}` | **assignee, in progress** | autosave; `403 {error}` explains why not (e.g. "this paper is assigned to Cora") |
| POST | `/api/projects/{pid}/papers/{uid}/identity` | PM or assignee | |
| POST | `/api/projects/{pid}/papers/{uid}/group-items` | PM or assignee | |
| POST | `/api/projects/{pid}/import` | PM | model-output JSON import (synchronous) |
| POST | `/api/projects/{pid}/papers/fetch-by-pmid` | PM | **deprecated**, use an import job |
| POST | `/api/projects/{pid}/extract` | PM | **deprecated**, use an extract job |

Paper (new fields): `assigneeId, curationStatus: unassigned|in_progress|submitted|excluded|unextractable, statusReason, statusBy, statusAt, addedBy`.
Feature cell (new): `editedBy, confirmedBy` (user ids → names via `state.users`). Run: `requestedBy`. Group: `confirmedBy`.
`Member = {userId, name, email, role: manager|curator, state, addedAt, inProgress}`

## Workflow

| Method | Path | Who | Body | Effect |
|---|---|---|---|---|
| POST | `…/papers/{uid}/claim` | C | `{}` | unassigned → in progress, assigned to me. `409` if someone got there first. |
| POST | `…/papers/{uid}/assign` | PM | `{assigneeId \| null}` | assign / reassign / back to pool. Not for finished papers (reopen first). |
| POST | `…/papers/{uid}/submit` | assignee | `{}` | in progress → submitted |
| POST | `…/papers/{uid}/exclude` | assignee | `{status: excluded\|unextractable, reason}` | reason required |
| POST | `…/papers/{uid}/reopen` | PM | `{reason?}` | finished → in progress (same assignee if still active) |
| GET | `…/papers/{uid}/history` | C | – | `{events: [{at, action, actor, actorName, ...detail}]}` |

All return `{paper}` (the updated paper).

## Background jobs

| Method | Path | Who | Body |
|---|---|---|---|
| POST | `/api/projects/{pid}/jobs` | PM | import: `{kind:"import", refs:[pmid,...]}`; extract: `{kind:"extract", refs:[uid\|pmid,...], models:[slug], promptId?, parseEngine?, force?}` |
| GET | `/api/projects/{pid}/jobs` | C | `{jobs: [Job]}`, newest 20, without items |
| GET | `/api/projects/{pid}/jobs/{jid}` | C | `Job` with `items` |
| POST | `/api/projects/{pid}/jobs/{jid}/cancel` | PM | `Job` |
| POST | `/api/projects/{pid}/jobs/{jid}/retry` | PM | `201 Job`: a NEW job containing only the failed/cancelled items |

`Job = {id, kind, state: queued|running|done|failed|cancelled, params, total, counts: {waiting, running, done, failed, skipped, cancelled}, error, retryOf, createdBy, createdAt, startedAt, finishedAt, items?: [Item]}`
`Item = {seq, ref, state: waiting|running|done|failed|skipped|cancelled, outcome, result, error}`

Import outcomes: `added` (`result.uid`), `added_abstract`, `duplicate` (state `done`: not an error), `no_pdf` (offer "upload the PDF for this PMID"), `not_found`, `invalid`, `error`, `interrupted`.
Extract outcomes: `extracted`, `already_extracted` (state `skipped`), `error`, `interrupted`.

Poll `GET …/jobs/{jid}` about every 2 s while `state` is `queued` or `running`.

## Team and join links

| Method | Path | Who | Body / response |
|---|---|---|---|
| GET | `/api/projects/{pid}/members` | C | `{members: [Member]}` |
| GET | `/api/projects/{pid}/directory` | PM | `{users: [{id, name, email}]}`: active accounts |
| POST | `/api/projects/{pid}/members` | PM (role manager: SA only) | `{userId, role}` → `{members}` |
| POST | `/api/projects/{pid}/members/{userId}/remove` | PM (a manager: SA only) | → `{released, members}`; their in-progress papers go back to the pool |
| GET | `/api/projects/{pid}/links` | PM | `{links: [Link]}` |
| POST | `/api/projects/{pid}/links` | PM | `{days?: 1-90 (7), maxUses?}` → `201 Link + url` (**url shown once**) |
| POST | `/api/projects/{pid}/links/{lid}/revoke` | PM | `Link` |

`Link = {id, role, createdAt, createdBy, expiresAt, maxUses, uses, revokedAt, state: valid|expired|revoked|used_up}`
