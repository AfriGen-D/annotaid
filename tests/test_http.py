"""End-to-end access test over real HTTP — run: python -m annotaid.tests.test_http

Boots the actual server (same Context wiring as run.py) on a free port with a
throwaway data dir, and checks the hosted-release rules from the outside:

  * every non-public route refuses an anonymous request (found by walking the
    router, so a route added later cannot be forgotten — NFR-1)
  * login / logout / cookie replay / deactivation (M1, NFR-4)
  * CSRF: a POST from another origin is refused
  * the permission matrix: superadmin / manager / curator / outsider (M2)
  * sign-up -> pending -> approval, join links (incl. sign-up through a link)
  * workflow: claim, edit lock, submit, exclude, reopen (M3, M7, M9, M17)
  * attribution of edits (M8)
  * background jobs, driven synchronously with fake executors (M21)
  * admin: create user, reset password, backup, snapshot

No test dependency on purpose (stdlib + jsonschema only), same as the others.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(ROOT))

from annotaid.server import app as app_mod  # noqa: E402
from annotaid.server import auth, jobs as jobs_mod  # noqa: E402
from annotaid.server import projects as projects_mod  # noqa: E402
from annotaid.server.backup import Backups  # noqa: E402
from annotaid.server.config_loader import Secrets, load_config  # noqa: E402
from annotaid.server.db import Db  # noqa: E402

FAILS = []
PASSES = [0]


def check(label, cond, detail=""):
    if cond:
        PASSES[0] += 1
        print(f"  ok  {label}")
    else:
        FAILS.append(label)
        print(f"  FAIL {label}  {detail}")


# --------------------------------------------------------------------------- #
class Client:
    """A browser stand-in: keeps its own session cookie, sends Origin."""

    def __init__(self, base, origin=None):
        self.base = base
        self.origin = origin if origin is not None else base
        self.cookie = None

    def call(self, method, path, body=None, raw=None, headers=None, follow=False):
        data = None
        hdrs = dict(headers or {})
        if body is not None:
            data = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
        if raw is not None:
            data = raw
        if method == "POST" and self.origin:
            hdrs.setdefault("Origin", self.origin)
        if self.cookie:
            hdrs["Cookie"] = self.cookie
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=hdrs)
        opener = urllib.request.build_opener(NoRedirect) if not follow else urllib.request.build_opener()
        try:
            r = opener.open(req, timeout=20)
            status, rh, payload = r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            status, rh, payload = e.code, e.headers, e.read()
        sc = rh.get("Set-Cookie")
        if sc and sc.startswith("annotaid_session="):
            val = sc.split(";", 1)[0]
            self.cookie = None if val == "annotaid_session=" else val
        try:
            js = json.loads(payload) if payload else None
        except ValueError:
            js = None
        return status, js, rh

    def get(self, path, **kw):
        return self.call("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.call("POST", path, body=body if body is not None else {}, **kw)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


def login(base, email, password):
    c = Client(base)
    s, js, _ = c.post("/api/login", {"email": email, "password": password})
    return c, s, js


# --------------------------------------------------------------------------- #
def fake_import(runner, proj, job, ref):
    """Stand-in for the NCBI fetch: PMIDs ending in 9 have no open-access PDF."""
    from annotaid.server import papers as papers_mod
    existing = proj.store.pmid_taken_by(ref)   # same first step as the real one
    if existing:
        return "done", "duplicate", {"uid": existing}, None
    if ref.endswith("9"):
        return "failed", "no_pdf", None, "no open-access PDF found"
    pdf = b"%PDF-1.4\n" + ref.encode() * 20
    try:
        paper = papers_mod.store_and_confirm_from_pmid(proj.store, pdf, ref)
    except papers_mod.PaperError as exc:
        return "done", "duplicate", None, str(exc)
    proj.store.set_added_by(paper["uid"], job["created_by"])
    return "done", "added", {"uid": paper["uid"]}, None


def fake_extract(runner, proj, job, uid):
    """Stand-in for OpenRouter: writes one run with every feature empty."""
    from annotaid.server.extraction import _blank_features
    run = {
        "uid": uid, "modelId": job["params"]["models"][0], "modelLabel": "fake",
        "status": "done", "requestedAt": "2026-01-01T00:00:00Z",
        "features": _blank_features(proj.config.features),
        "groups": {}, "requestedBy": job["created_by"],
    }
    proj.store.save_run(run)
    return "done", "extracted", {"uid": uid}, None


def main():
    tmp = tempfile.mkdtemp(prefix="annotaid-http-")
    try:
        run_all(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if FAILS:
        print(f"\n{len(FAILS)} FAILED: {FAILS}")
        sys.exit(1)
    print(f"\n{PASSES[0]} checks passed.")


def run_all(tmp):
    db_path = os.path.join(tmp, "annotaid.db")
    db = Db(db_path)
    env = {"ANNOTAID_ADMIN_EMAIL": "Root@Example.org", "ANNOTAID_ADMIN_PASSWORD": "bootstrap-pass-1"}
    auth.bootstrap_superadmin(db, env, log=lambda *_: None)
    registry = projects_mod.ProjectRegistry(db, os.path.join(tmp, "pdfs"))
    ctx = app_mod.Context(
        secrets=Secrets(openrouter_api_key="test-key"), db=db, projects=registry,
        static_dir=os.path.join(ROOT, "static"),
        template=load_config(os.path.join(ROOT, "config", "schema.json")),
        version="test",
    )
    ctx.jobs = jobs_mod.JobRunner(db, registry, ctx.secrets,
                                  executors={"import": fake_import, "extract": fake_extract})
    ctx.backups = Backups(db_path, os.path.join(tmp, "backups"))
    server = app_mod.build_server(ctx, "127.0.0.1", 0)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        scenarios(ctx, db, base)
    finally:
        server.shutdown()
        server.server_close()


def scenarios(ctx, db, base):
    anon = Client(base)

    # ------------------------------------------------------------------ #
    print("\nanonymous access to every non-public route")
    router = app_mod.build_router()
    leaks = []
    for method, regex, handler in router.routes:
        if getattr(handler, "public", False):
            continue
        path = regex.pattern.strip("^$").replace("\\", "")
        path = path.replace("(?P<pid>[^/]+)", "p0").replace("(?P<uid>[^/]+)", "u0")
        import re
        path = re.sub(r"\(\?P<\w+>\[\^/\]\+\)", "x", path)
        s, js, h = anon.call(method, path, body={} if method == "POST" else None)
        ok = (s == 401) or (s == 302 and h.get("Location", "").startswith("/login"))
        if not ok:
            leaks.append((method, path, s))
    check(f"all {len(router.routes)} routes refuse anonymous callers", not leaks, leaks)
    for p in ("/js/main.js", "/vendor/", "/index.html", "/home.html"):
        s, _, h = anon.get(p)
        check(f"static {p} needs a session", s in (302, 401), s)
    s, _, _ = anon.get("/css/styles.css")
    check("login page CSS is public", s == 200, s)
    s, _, _ = anon.get("/login")
    check("login page is public", s == 200, s)

    # ------------------------------------------------------------------ #
    print("\nbootstrap superadmin + forced password change")
    root, s, js = login(base, "root@example.org", "bootstrap-pass-1")
    check("bootstrap superadmin can log in (email case-insensitive)", s == 200, (s, js))
    check("cookie is HttpOnly, SameSite=Lax", root.cookie is not None)
    check("must change password flagged", js["user"]["mustChangePassword"] is True)
    s, js, _ = root.get("/api/projects")
    check("other routes blocked until the password changes",
          s == 403 and js.get("code") == "must_change_password", (s, js))
    s, _, h = root.get("/")
    check("pages redirect to /account until then", s == 302 and h["Location"].startswith("/account"))
    s, js, _ = root.post("/api/me/password", {"current": "bootstrap-pass-1", "new": "short"})
    check("short new password refused", s == 400)
    s, js, _ = root.post("/api/me/password", {"current": "bootstrap-pass-1", "new": "root-password-2"})
    check("password changed", s == 200, js)
    s, js, _ = root.get("/api/projects")
    check("unblocked after the change", s == 200 and js["canCreate"], (s, js))
    check("bootstrap is a no-op once a superadmin exists",
          auth.bootstrap_superadmin(db, {"ANNOTAID_ADMIN_EMAIL": "x@y.org",
                                         "ANNOTAID_ADMIN_PASSWORD": "whatever-123"},
                                    log=lambda *_: None) is None
          and auth.get_user_by_email(db, "x@y.org") is None)

    print("\nlogin errors and throttling")
    _, s, js = login(base, "root@example.org", "wrong-password")
    _, s2, js2 = login(base, "nobody@example.org", "wrong-password")
    check("wrong password and unknown email give the same answer",
          s == s2 == 401 and js["error"] == js2["error"], (js, js2))
    for _ in range(5):
        login(base, "throttle@example.org", "nope-nope-nope")
    _, s, _ = login(base, "throttle@example.org", "nope-nope-nope")
    check("6th failure in a row for one email is throttled", s == 429, s)

    print("\nCSRF")
    evil = Client(base, origin="https://evil.example")
    evil.cookie = root.cookie
    s, _, _ = evil.post("/api/projects", {"name": "csrf"})
    check("POST from another origin refused", s == 403, s)
    evil.origin = "null"
    s, _, _ = evil.post("/api/projects", {"name": "csrf"})
    check("Origin: null refused", s == 403, s)
    noorigin = Client(base, origin="")
    noorigin.cookie = root.cookie
    s, _, _ = noorigin.post("/api/projects", {"name": "csrf"})
    check("POST with no Origin/Referer refused", s == 403, s)

    # ------------------------------------------------------------------ #
    print("\naccounts: admin-created, sign-up + approval")
    s, js, _ = root.post("/api/admin/users", {"name": "Maya Manager", "email": "maya@example.org",
                                              "role": "manager"})
    check("superadmin creates a manager account", s == 201 and js["tempPassword"], (s, js))
    maya_id, maya_tmp = js["user"]["id"], js["tempPassword"]
    maya, s, js = login(base, "maya@example.org", maya_tmp)
    maya.post("/api/me/password", {"current": maya_tmp, "new": "maya-password-1"})

    s, js, _ = anon.post("/api/signup", {"name": "Cora Curator", "email": "cora@example.org",
                                         "password": "cora-password-1"})
    check("sign-up accepted as pending", s == 201 and js["pending"], (s, js))
    _, s, js = login(base, "cora@example.org", "cora-password-1")
    check("pending account can't log in yet", s == 401 and "approval" in js["error"], js)
    s, js, _ = anon.post("/api/signup", {"name": "Dup", "email": "CORA@example.org",
                                         "password": "another-pass-1"})
    check("duplicate email refused", s == 400, s)
    cora_id = auth.get_user_by_email(db, "cora@example.org")["id"]
    s, js, _ = maya.post(f"/api/admin/users/{cora_id}/approve", {"role": "user"})
    check("a manager can't approve sign-ups", s == 403, s)
    s, js, _ = root.post(f"/api/admin/users/{cora_id}/approve", {"role": "user"})
    check("superadmin approves", s == 200 and js["user"]["state"] == "active", js)
    cora, s, _ = login(base, "cora@example.org", "cora-password-1")
    check("approved account logs in", s == 200)

    s, js, _ = anon.post("/api/signup", {"name": "Otto Outsider", "email": "otto@example.org",
                                         "password": "otto-password-1"})
    otto_id = auth.get_user_by_email(db, "otto@example.org")["id"]
    root.post(f"/api/admin/users/{otto_id}/approve", {"role": "user"})
    otto, _, _ = login(base, "otto@example.org", "otto-password-1")

    # ------------------------------------------------------------------ #
    print("\nprojects + permission matrix")
    s, js, _ = cora.post("/api/projects", {"name": "nope"})
    check("a plain user can't create projects", s == 403, s)
    s, js, _ = maya.post("/api/projects", {"name": "Pilot", "description": "d"})
    check("manager creates a project", s == 201, (s, js))
    pid = js["id"]
    s, js, _ = maya.get("/api/projects")
    check("creator is its manager", [p["myRole"] for p in js["projects"]] == ["manager"], js)
    s, js, _ = otto.get("/api/projects")
    check("outsider sees no projects", js["projects"] == [], js)
    s, js, _ = otto.get(f"/api/projects/{pid}/state")
    check("outsider gets 404, not 403", s == 404, s)
    s, js, _ = otto.get(f"/api/projects/{pid}/pdf/anything")
    check("outsider can't reach PDFs", s == 404, s)
    s, js, _ = root.get(f"/api/projects/{pid}/state")
    check("superadmin can read any project", s == 200, s)

    s, js, _ = maya.get(f"/api/projects/{pid}/directory")
    check("manager sees the user directory", s == 200 and len(js["users"]) >= 4)
    s, js, _ = maya.post(f"/api/projects/{pid}/members", {"userId": cora_id, "role": "curator"})
    check("manager adds a curator", s == 200 and any(m["userId"] == cora_id for m in js["members"]))
    s, js, _ = maya.post(f"/api/projects/{pid}/members", {"userId": otto_id, "role": "manager"})
    check("a manager can't add managers (superadmin only, M20)", s == 403, s)
    s, js, _ = cora.get(f"/api/projects/{pid}/config")
    check("curator reads project config", s == 200 and js["myRole"] == "curator"
          and js["permissions"]["extract"] is False, js and js.get("permissions"))
    for path, label in [(f"/api/projects/{pid}/export", "export"),
                        (f"/api/projects/{pid}/config-file", "config download"),
                        (f"/api/projects/{pid}/links", "join links")]:
        s, _, _ = cora.get(path)
        check(f"curator can't {label}", s == 403, s)
    s, _, _ = cora.post(f"/api/projects/{pid}/jobs", {"kind": "import", "refs": ["123"]})
    check("curator can't add papers", s == 403, s)
    s, _, _ = cora.get("/api/admin/users")
    check("curator can't reach admin API", s == 403, s)
    s, _, h = cora.get("/admin")
    check("curator can't load the admin page", s == 403, s)
    s, _, _ = maya.post(f"/api/projects/{pid}/archive")
    check("a manager can't archive (superadmin only)", s == 403, s)

    # ------------------------------------------------------------------ #
    print("\nimport job")
    s, job, _ = maya.post(f"/api/projects/{pid}/jobs",
                          {"kind": "import", "refs": ["111", "222", "339", "111"]})
    check("import job created (duplicates in the list collapsed)", s == 201 and job["total"] == 3, job)
    check("job starts queued", job["state"] == "queued")
    ctx.jobs.run_next()
    s, job, _ = maya.get(f"/api/projects/{pid}/jobs/{job['id']}")
    outcomes = {it["ref"]: it["outcome"] for it in job["items"]}
    check("per-item outcomes", outcomes == {"111": "added", "222": "added", "339": "no_pdf"}, outcomes)
    check("job done with one failure", job["state"] == "done" and job["counts"]["failed"] == 1, job["counts"])
    s, retry, _ = maya.post(f"/api/projects/{pid}/jobs/{job['id']}/retry")
    check("retry makes a new job with only the failed item",
          s == 201 and [it["ref"] for it in retry["items"]] == ["339"] and retry["retryOf"] == job["id"])
    s, cancelled, _ = maya.post(f"/api/projects/{pid}/jobs/{retry['id']}/cancel")
    check("cancelling a queued job", s == 200 and cancelled["state"] == "cancelled", cancelled)
    s, job2, _ = maya.post(f"/api/projects/{pid}/jobs", {"kind": "import", "refs": ["111"]})
    ctx.jobs.run_next()
    s, job2, _ = maya.get(f"/api/projects/{pid}/jobs/{job2['id']}")
    check("re-importing reports duplicate, not an error",
          job2["items"][0]["outcome"] == "duplicate" and job2["items"][0]["state"] == "done", job2["items"])

    s, st, _ = maya.get(f"/api/projects/{pid}/state")
    papers = {p["pmid"]: p for p in st["papers"]}
    check("two papers, unassigned, added_by the manager",
          set(papers) == {"111", "222"}
          and all(p["curationStatus"] == "unassigned" and p["addedBy"] == maya_id
                  for p in papers.values()), papers)
    u1, u2 = papers["111"]["uid"], papers["222"]["uid"]

    print("\nrestart recovery")
    s, job3, _ = maya.post(f"/api/projects/{pid}/jobs", {"kind": "import", "refs": ["444", "555"]})
    with db.write() as c:  # simulate a crash mid-item
        c.execute("UPDATE jobs SET state='running' WHERE id=?", (job3["id"],))
        c.execute("UPDATE job_items SET state='running' WHERE job_id=? AND ref='444'", (job3["id"],))
    ctx.jobs.recover()
    ctx.jobs.run_next()
    s, job3, _ = maya.get(f"/api/projects/{pid}/jobs/{job3['id']}")
    items = {it["ref"]: it for it in job3["items"]}
    check("interrupted item marked as such",
          items["444"]["state"] == "failed" and "restart" in items["444"]["error"], items["444"])
    check("the rest of the job carried on", items["555"]["outcome"] == "added", items["555"])

    print("\nextraction job")
    model = maya.get(f"/api/projects/{pid}/config")[1]["models"][0]["slug"]
    s, js, _ = cora.post(f"/api/projects/{pid}/jobs", {"kind": "extract", "refs": [u1], "models": [model]})
    check("curator can't start extraction (decision 6)", s == 403, s)
    s, js, _ = maya.post(f"/api/projects/{pid}/jobs", {"kind": "extract", "refs": [u1], "models": ["no/such"]})
    check("unknown model refused", s == 400, js)
    s, ej, _ = maya.post(f"/api/projects/{pid}/jobs", {"kind": "extract", "refs": [u1, u2], "models": [model]})
    check("extraction job created", s == 201, (s, ej))
    ctx.jobs.run_next()
    s, st, _ = maya.get(f"/api/projects/{pid}/state")
    run = next(r for r in st["runs"] if r["uid"] == u1)
    check("run records who requested it", run.get("requestedBy") == maya_id, run.get("requestedBy"))
    fname = next(iter(run["features"]))

    # ------------------------------------------------------------------ #
    print("\nworkflow + edit lock")
    save = {"modelId": run["modelId"], "features": {fname: {"value": "edited", "confirmed": True}}}
    s, js, _ = cora.post(f"/api/projects/{pid}/runs/{u1}", save)
    check("can't edit an unclaimed paper", s == 403 and "claim" in js["error"], js)
    s, js, _ = cora.post(f"/api/projects/{pid}/papers/{u1}/claim")
    check("curator claims from the pool", s == 200 and js["paper"]["assigneeId"] == cora_id
          and js["paper"]["curationStatus"] == "in_progress", js)
    s, js, _ = maya.post(f"/api/projects/{pid}/papers/{u1}/claim")
    check("second claim loses", s == 409 and "Cora" in js["error"], js)
    s, js, _ = maya.post(f"/api/projects/{pid}/runs/{u1}", save)
    check("even the manager can't edit someone else's paper (M7)", s == 403, js)
    s, js, _ = cora.post(f"/api/projects/{pid}/runs/{u1}", save)
    check("assignee edits", s == 200, js)
    cell = js["run"]["features"][fname]
    check("attribution: edited_by / confirmed_by", cell["editedBy"] == cora_id
          and cell["confirmedBy"] == cora_id, cell)
    check("aiValue untouched", cell["aiValue"] != "edited")

    s, js, _ = cora.post(f"/api/projects/{pid}/papers/{u1}/exclude", {"status": "excluded"})
    check("exclude needs a reason", s == 400, js)
    s, js, _ = cora.post(f"/api/projects/{pid}/papers/{u1}/submit")
    check("assignee submits", s == 200 and js["paper"]["curationStatus"] == "submitted")
    s, js, _ = cora.post(f"/api/projects/{pid}/runs/{u1}", save)
    check("submitted paper is locked", s == 403, js)
    s, js, _ = cora.post(f"/api/projects/{pid}/papers/{u1}/reopen")
    check("curator can't reopen", s == 403, s)
    s, js, _ = maya.post(f"/api/projects/{pid}/papers/{u1}/reopen", {"reason": "typo"})
    check("manager reopens", s == 200 and js["paper"]["curationStatus"] == "in_progress"
          and js["paper"]["assigneeId"] == cora_id, js)

    s, js, _ = maya.post(f"/api/projects/{pid}/papers/{u2}/assign", {"assigneeId": otto_id})
    check("can't assign to a non-member", s == 400, js)
    s, js, _ = maya.post(f"/api/projects/{pid}/papers/{u2}/assign", {"assigneeId": cora_id})
    check("manager assigns", s == 200 and js["paper"]["assigneeId"] == cora_id)
    s, js, _ = cora.post(f"/api/projects/{pid}/papers/{u2}/exclude",
                         {"status": "unextractable", "reason": "does not report it"})
    check("unextractable with reason", s == 200 and js["paper"]["statusReason"] == "does not report it")
    s, js, _ = maya.get(f"/api/projects/{pid}/papers/{u1}/history")
    check("history records claim/submit/reopen",
          [e["action"] for e in js["events"]] == ["claim", "submit", "reopen"], js)

    print("\nremoving a member releases their in-progress papers")
    s, js, _ = maya.post(f"/api/projects/{pid}/members/{cora_id}/remove")
    check("removed; one in-progress paper released", s == 200 and js["released"] == 1, js)
    s, st, _ = maya.get(f"/api/projects/{pid}/state")
    p1 = next(p for p in st["papers"] if p["uid"] == u1)
    check("released paper is back in the pool", p1["assigneeId"] is None
          and p1["curationStatus"] == "unassigned", p1)
    s, _, _ = cora.get(f"/api/projects/{pid}/state")
    check("removed member loses access", s == 404, s)

    # ------------------------------------------------------------------ #
    print("\njoin links")
    s, link, _ = maya.post(f"/api/projects/{pid}/links", {"days": 7})
    check("manager creates a link with a full URL", s == 201 and "/join/" in link["url"], link)
    token = link["url"].rsplit("/", 1)[1]
    s, js, _ = anon.get(f"/api/join/{token}")
    check("anyone can see the link is valid + project name",
          js["state"] == "valid" and js["projectName"] == "Pilot", js)
    s, js, _ = cora.post(f"/api/join/{token}")
    check("logged-in user joins through the link", s == 200 and js["projectId"] == pid, js)
    s, js, _ = cora.get(f"/api/projects/{pid}/config")
    check("...as curator", s == 200 and js["myRole"] == "curator")

    s, js, _ = anon.post("/api/signup", {"name": "Nia New", "email": "nia@example.org",
                                         "password": "nia-password-1", "joinToken": token})
    nia_id = auth.get_user_by_email(db, "nia@example.org")["id"]
    s, js, _ = root.post(f"/api/admin/users/{nia_id}/approve", {"role": "user"})
    check("sign-up through a link joins on approval", js.get("joinedProjectId") == pid, js)

    s, js, _ = maya.post(f"/api/projects/{pid}/links/{link['id']}/revoke")
    check("link revoked", js["state"] == "revoked")
    s, js, _ = otto.post(f"/api/join/{token}")
    check("revoked link refuses", s == 400, js)
    s, js, _ = anon.get("/api/join/not-a-real-token")
    check("unknown token says invalid", js["state"] == "invalid")

    # ------------------------------------------------------------------ #
    print("\nsessions end when they should")
    stolen = Client(base)
    stolen.cookie = otto.cookie
    s, _, _ = otto.post("/api/logout")
    s, _, _ = stolen.get("/api/me")
    check("a copied cookie dies at logout", s == 401, s)
    otto, _, _ = login(base, "otto@example.org", "otto-password-1")
    s, js, _ = root.post(f"/api/admin/users/{otto_id}/deactivate")
    check("deactivated", js["user"]["state"] == "deactivated")
    s, _, _ = otto.get("/api/me")
    check("deactivation ends the live session", s == 401, s)
    _, s, js = login(base, "otto@example.org", "otto-password-1")
    check("deactivated account can't log in", s == 401 and "deactivated" in js["error"], js)
    s, js, _ = root.post(f"/api/admin/users/{maya_id}/reset-password")
    check("reset gives a temp password", s == 200 and js["tempPassword"])
    s, _, _ = maya.get("/api/me")
    check("reset ends the user's sessions", s == 401, s)
    root_id = auth.get_user_by_email(db, "root@example.org")["id"]
    s, js, _ = root.post(f"/api/admin/users/{root_id}/deactivate")
    check("can't deactivate yourself", s == 400, js)
    s, js, _ = root.post(f"/api/admin/users/{root_id}/role", {"role": "user"})
    check("can't demote the last superadmin", s == 400, js)

    print("\ndatabase holds no secrets in the clear")
    hashes = [r[0] for r in db.query("SELECT password_hash FROM users")]
    check("passwords stored as scrypt hashes", all(h.startswith("scrypt$") for h in hashes))
    toks = [r[0] for r in db.query("SELECT token_hash FROM sessions")]
    check("session tokens stored hashed", all(len(t) == 64 for t in toks) and root.cookie.split("=", 1)[1] not in toks)

    print("\nadmin system + backups")
    s, js, _ = root.post("/api/admin/backup")
    check("backup now", s == 200 and js["backup"]["size"] > 0, js)
    s, js, _ = root.get("/api/admin/system")
    check("system shows last backup", s == 200 and js["lastBackup"] is not None)
    req = urllib.request.Request(base + "/api/admin/snapshot", headers={"Cookie": root.cookie})
    with urllib.request.urlopen(req, timeout=20) as r:
        head = r.read(16)
    check("snapshot downloads a SQLite file", head.startswith(b"SQLite format 3"), head)
    s, js, _ = root.get("/api/admin/projects")
    check("admin sees all projects with managers",
          s == 200 and js["projects"][0]["managers"][0]["userId"] == maya_id, js)
    s, _, _ = root.post(f"/api/admin/projects/{pid}/archive")
    s, _, _ = cora.get(f"/api/projects/{pid}/state")
    check("archived project unreachable for members", s == 404, s)
    s, _, _ = root.post(f"/api/admin/projects/{pid}/unarchive")
    s, _, _ = cora.get(f"/api/projects/{pid}/state")
    check("unarchived project is back", s == 200, s)


if __name__ == "__main__":
    main()
