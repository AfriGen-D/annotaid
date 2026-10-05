"""Login, sign-up, join links, the caller's own account, and the superadmin's
admin API. Project-scoped routes are in server/handlers.py.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone

from . import auth, membership, projects as projects_mod, responses
from .net import client_ip, site_url  # noqa: F401 — site_url used by handlers
from .access import allow_must_change, public, superadmin_only


def _json(body):
    if not body:
        return {}
    data = json.loads(body.decode("utf-8"))
    return data if isinstance(data, dict) else {}


def _bad_json(req):
    return responses.send_error_json(req, 400, "invalid JSON")


def _me_payload(ctx, user) -> dict:
    out = auth.public_user(user)
    mine = membership.memberships(ctx.db, user["id"])
    out["memberships"] = mine
    out["canCreateProjects"] = user["account_role"] in ("superadmin", "manager")
    return out


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #
@public
def h_auth_page(ctx, req, params, body):
    """/login, /signup and /join/<token> share one small page; authPages.js
    picks the form from the path."""
    responses.serve_static(req, ctx.static_dir, "/auth.html")


@allow_must_change
def h_account_page(ctx, req, params, body):
    responses.serve_static(req, ctx.static_dir, "/account.html")


@superadmin_only
def h_admin_page(ctx, req, params, body):
    responses.serve_static(req, ctx.static_dir, "/admin.html")


# --------------------------------------------------------------------------- #
# Login / logout / sign-up
# --------------------------------------------------------------------------- #
@public
def h_login(ctx, req, params, body):
    try:
        data = _json(body)
    except ValueError:
        return _bad_json(req)
    email = str(data.get("email") or "").strip().lower()
    ip = client_ip(req)
    if auth.LOGIN_FAILS_PER_EMAIL.blocked(email) or auth.LOGIN_FAILS_PER_IP.blocked(ip):
        return responses.send_error_json(
            req, 429, "too many failed attempts — wait 15 minutes and try again"
        )
    try:
        user = auth.authenticate(ctx.db, email, data.get("password"))
    except auth.AuthError as exc:
        if str(exc) == auth.LOGIN_FAILED:
            auth.LOGIN_FAILS_PER_EMAIL.hit(email)
            auth.LOGIN_FAILS_PER_IP.hit(ip)
        return responses.send_error_json(req, 401, str(exc))
    auth.LOGIN_FAILS_PER_EMAIL.clear(email)
    token = auth.create_session(ctx.db, user["id"], ip, req.headers.get("User-Agent", ""))
    user = auth.get_user(ctx.db, user["id"])
    responses.send_json(
        req, {"user": _me_payload(ctx, user)},
        extra={"Set-Cookie": responses.session_cookie(token, req.cookie_secure())},
    )


@public
def h_logout(ctx, req, params, body):
    auth.delete_session(ctx.db, req.session_token())
    responses.send_json(
        req, {"ok": True},
        extra={"Set-Cookie": responses.clear_session_cookie(req.cookie_secure())},
    )


@public
def h_signup(ctx, req, params, body):
    try:
        data = _json(body)
    except ValueError:
        return _bad_json(req)
    ip = client_ip(req)
    if auth.SIGNUPS_PER_IP.blocked(ip):
        return responses.send_error_json(req, 429, "too many sign-ups from here — try again later")
    link_id = None
    token = data.get("joinToken")
    if token:
        try:
            link_id = membership.link_id_for_token(ctx.db, str(token))
        except membership.MembershipError as exc:
            return responses.send_error_json(req, 400, str(exc))
    try:
        user = auth.signup(ctx.db, data.get("name"), data.get("email"), data.get("password"),
                           pending_link_id=link_id)
    except auth.AuthError as exc:
        return responses.send_error_json(req, 400, str(exc))
    auth.SIGNUPS_PER_IP.hit(ip)
    responses.send_json(req, {"pending": True, "email": user["email"]}, 201)


# --------------------------------------------------------------------------- #
# Join links (public description; joining needs a session)
# --------------------------------------------------------------------------- #
@public
def h_join_info(ctx, req, params, body):
    info = membership.describe_token(ctx.db, params["token"])
    info["loggedIn"] = req.user is not None
    responses.send_json(req, info)


def h_join(ctx, req, params, body):
    try:
        pid = membership.redeem(ctx.db, token=params["token"], user_id=req.user["id"])
    except membership.MembershipError as exc:
        return responses.send_error_json(req, 400, str(exc))
    responses.send_json(req, {"projectId": pid})


# --------------------------------------------------------------------------- #
# The caller's own account
# --------------------------------------------------------------------------- #
@allow_must_change
def h_me(ctx, req, params, body):
    responses.send_json(req, _me_payload(ctx, req.user))


@allow_must_change
def h_change_password(ctx, req, params, body):
    try:
        data = _json(body)
    except ValueError:
        return _bad_json(req)
    try:
        auth.change_password(ctx.db, req.user["id"], data.get("current"), data.get("new"),
                             keep_token_hash=req.session_hash)
    except auth.AuthError as exc:
        return responses.send_error_json(req, 400, str(exc))
    responses.send_json(req, {"ok": True})


# --------------------------------------------------------------------------- #
# Admin (superadmin): users, projects, system
# --------------------------------------------------------------------------- #
@superadmin_only
def h_admin_users(ctx, req, params, body):
    responses.send_json(req, {"users": auth.list_users(ctx.db)})


@superadmin_only
def h_admin_user_create(ctx, req, params, body):
    try:
        data = _json(body)
    except ValueError:
        return _bad_json(req)
    try:
        user, pw = auth.create_user(ctx.db, data.get("name"), data.get("email"),
                                    data.get("role") or "user", req.user["id"])
    except auth.AuthError as exc:
        return responses.send_error_json(req, 400, str(exc))
    # The temporary password is shown ONCE, to the superadmin, who hands it
    # over themselves (there is no email). It must be changed at first login.
    responses.send_json(req, {"user": user, "tempPassword": pw}, 201)


@superadmin_only
def h_admin_user_action(ctx, req, params, body):
    try:
        data = _json(body)
    except ValueError:
        return _bad_json(req)
    uid, action, actor = params["id"], params["action"], req.user["id"]
    out = {}
    try:
        if action in ("approve", "reject"):
            link_id = auth.decide_signup(ctx.db, uid, action == "approve",
                                         data.get("role") or "user", actor)
            if link_id:
                # Signed up through a join link: add them to that project now.
                try:
                    out["joinedProjectId"] = membership.redeem(ctx.db, link_id=link_id, user_id=uid)
                except membership.MembershipError as exc:
                    out["joinError"] = str(exc)
        elif action == "role":
            auth.set_role(ctx.db, uid, data.get("role"), actor)
        elif action == "deactivate":
            auth.set_active(ctx.db, uid, False, actor)
        elif action == "reactivate":
            auth.set_active(ctx.db, uid, True, actor)
        elif action == "reset-password":
            out["tempPassword"] = auth.reset_password(ctx.db, uid)
        else:
            return responses.send_error_json(req, 404, "not found")
    except auth.AuthError as exc:
        return responses.send_error_json(req, 400, str(exc))
    out["user"] = auth.public_user(auth.get_user(ctx.db, uid))
    responses.send_json(req, out)


@superadmin_only
def h_admin_projects(ctx, req, params, body):
    projects = projects_mod.list_projects(ctx.db, include_archived=True)
    for p in projects:
        p["managers"] = [m for m in membership.list_members(ctx.db, p["id"])
                         if m["role"] == "manager"]
    responses.send_json(req, {"projects": projects})


@superadmin_only
def h_admin_project_action(ctx, req, params, body):
    pid, action = params["pid"], params["action"]
    try:
        if action == "archive":
            projects_mod.archive_project(ctx.db, pid)
        elif action == "unarchive":
            projects_mod.unarchive_project(ctx.db, pid)
        else:
            return responses.send_error_json(req, 404, "not found")
    except projects_mod.ProjectError as exc:
        return responses.send_error_json(req, 404, str(exc))
    responses.send_json(req, {"ok": True})


def _system(ctx) -> dict:
    db_path = ctx.db.path
    size = sum(os.path.getsize(p) for p in (db_path, db_path + "-wal")
               if os.path.exists(p))
    backups = ctx.backups.list() if ctx.backups else []
    counts = {r["state"]: r["n"] for r in ctx.db.query(
        "SELECT state, COUNT(*) AS n FROM users GROUP BY state")}
    jobs = {r["state"]: r["n"] for r in ctx.db.query(
        "SELECT state, COUNT(*) AS n FROM jobs WHERE state IN ('queued','running') GROUP BY state")}
    return {
        "version": ctx.version,
        "dbSize": size,
        "extractionEnabled": bool(ctx.secrets.openrouter_api_key),
        "backups": backups[:14],
        "lastBackup": backups[0] if backups else None,
        "errors": ctx.errors.list(),
        "users": counts,
        "jobs": jobs,
    }


@superadmin_only
def h_admin_system(ctx, req, params, body):
    responses.send_json(req, _system(ctx))


@superadmin_only
def h_admin_backup(ctx, req, params, body):
    if ctx.backups is None:
        return responses.send_error_json(req, 400, "backups are not configured")
    info = ctx.backups.run_now()
    responses.send_json(req, {"backup": info, "system": _system(ctx)})


@superadmin_only
def h_admin_snapshot(ctx, req, params, body):
    """A fresh, consistent copy of the database, as a download — the
    superadmin's own off-server copy. Contains password HASHES and every
    project's data: treat the file accordingly."""
    if ctx.backups is None:
        return responses.send_error_json(req, 400, "backups are not configured")
    # A temporary copy, not one of the kept backups: downloading repeatedly
    # must not push the nightly copies out of the retention window.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    fd, path = tempfile.mkstemp(prefix="annotaid-snapshot-", suffix=".db")
    os.close(fd)
    try:
        ctx.backups.copy_to(path)
        responses.send_file(req, path, "application/vnd.sqlite3", extra={
            "Content-Disposition": f'attachment; filename="annotaid-snapshot-{stamp}.db"',
            "Cache-Control": "no-store",
        })
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
def register(router):
    # pages
    router.add("GET", "/login", h_auth_page)
    router.add("GET", "/signup", h_auth_page)
    router.add("GET", "/join/{token}", h_auth_page)
    router.add("GET", "/account", h_account_page)
    router.add("GET", "/admin", h_admin_page)

    # session
    router.add("POST", "/api/login", h_login)
    router.add("POST", "/api/logout", h_logout)
    router.add("POST", "/api/signup", h_signup)
    router.add("GET", "/api/join/{token}", h_join_info)
    router.add("POST", "/api/join/{token}", h_join)
    router.add("GET", "/api/me", h_me)
    router.add("POST", "/api/me/password", h_change_password)

    # admin
    router.add("GET", "/api/admin/users", h_admin_users)
    router.add("POST", "/api/admin/users", h_admin_user_create)
    router.add("POST", "/api/admin/users/{id}/{action}", h_admin_user_action)
    router.add("GET", "/api/admin/projects", h_admin_projects)
    router.add("POST", "/api/admin/projects/{pid}/{action}", h_admin_project_action)
    router.add("GET", "/api/admin/system", h_admin_system)
    router.add("POST", "/api/admin/backup", h_admin_backup)
    router.add("GET", "/api/admin/snapshot", h_admin_snapshot)
