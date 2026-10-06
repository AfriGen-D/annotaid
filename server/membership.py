"""Project membership, project-level roles and join links.

A user's access to a project is decided here and nowhere else: the route
decorator (handlers.project_route) asks project_role() on every request (M2,
NFR-2). A superadmin is treated as a manager of every project (M19).

Join links: a project manager mints a link; anyone who opens it while logged in
joins as a curator, and someone without an account signs up through it and is
added the moment a superadmin approves them. Only sha256(token) is stored.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone

from . import util
from .auth import AuthError
from .db import Db

PROJECT_ROLES = ("manager", "curator")
# Rank for "at least this role" checks.
_RANK = {"curator": 1, "manager": 2}

LINK_DEFAULT_DAYS = 7
LINK_MAX_DAYS = 90


class MembershipError(AuthError):
    pass


# --------------------------------------------------------------------------- #
# Roles
# --------------------------------------------------------------------------- #
def project_role(db: Db, user, project_id: str):
    """'manager' | 'curator' | None. `user` is a users row (or dict with
    account_role + id). Superadmins are managers everywhere."""
    if user is None:
        return None
    if user["account_role"] == "superadmin":
        return "manager"
    return db.scalar(
        "SELECT role FROM project_members WHERE project_id=? AND user_id=?",
        (project_id, user["id"]),
    )


def at_least(role, needed: str) -> bool:
    return role is not None and _RANK.get(role, 0) >= _RANK[needed]


def memberships(db: Db, user_id: str) -> list:
    return [
        {"projectId": r["project_id"], "role": r["role"]}
        for r in db.query(
            "SELECT project_id, role FROM project_members WHERE user_id=?", (user_id,)
        )
    ]


def project_ids_for(db: Db, user_id: str) -> set:
    return {m["projectId"] for m in memberships(db, user_id)}


def log_event(c, project_id, uid, actor, action, **detail) -> None:
    """Append to the audit trail (events). `c` is an open write transaction."""
    c.execute(
        "INSERT INTO events (project_id, uid, at, actor, action, detail_json) "
        "VALUES (?,?,?,?,?,?)",
        (project_id, uid, util.iso_now(), actor, action,
         json.dumps(detail, ensure_ascii=False)),
    )


# --------------------------------------------------------------------------- #
# Members
# --------------------------------------------------------------------------- #
def list_members(db: Db, project_id: str) -> list:
    rows = db.query(
        "SELECT m.user_id, m.role, m.added_at, u.name, u.email, u.state, "
        "  (SELECT COUNT(*) FROM papers p WHERE p.project_id=m.project_id "
        "     AND p.assignee_id=m.user_id AND p.curation_status='in_progress') AS in_progress "
        "FROM project_members m JOIN users u ON u.id=m.user_id "
        "WHERE m.project_id=? ORDER BY m.role, u.name COLLATE NOCASE",
        (project_id,),
    )
    return [{
        "userId": r["user_id"], "name": r["name"], "email": r["email"],
        "role": r["role"], "state": r["state"], "addedAt": r["added_at"],
        "inProgress": r["in_progress"],
    } for r in rows]


def add_member(db: Db, project_id: str, user_id: str, role: str, actor_id,
               via_link_id: str = None) -> None:
    """Add, or change the role of, a member. The target must be an active
    account. Upsert, so re-adding someone just updates their role."""
    if role not in PROJECT_ROLES:
        raise MembershipError(f"unknown project role {role!r}")
    user = db.query_one("SELECT state FROM users WHERE id=?", (user_id,))
    if user is None or user["state"] != "active":
        raise MembershipError("only active accounts can be added to a project")
    with db.write() as c:
        before = c.execute(
            "SELECT role FROM project_members WHERE project_id=? AND user_id=?",
            (project_id, user_id),
        ).fetchone()
        if before and before["role"] == "manager" and role != "manager":
            _guard_last_manager(c, project_id, user_id)
        c.execute(
            "INSERT INTO project_members (project_id, user_id, role, added_at, added_by, via_link_id) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(project_id, user_id) DO UPDATE SET role=excluded.role",
            (project_id, user_id, role, util.iso_now(), actor_id, via_link_id),
        )
        log_event(c, project_id, None, actor_id,
                  "member_joined" if via_link_id else ("member_role" if before else "member_added"),
                  user=user_id, role=role, link=via_link_id)


def _guard_last_manager(c, project_id, user_id) -> None:
    n = c.execute(
        "SELECT COUNT(*) FROM project_members WHERE project_id=? AND role='manager' "
        "AND user_id != ?", (project_id, user_id),
    ).fetchone()[0]
    if n == 0:
        raise MembershipError("a project needs at least one manager")


def remove_member(db: Db, project_id: str, user_id: str, actor_id) -> int:
    """Remove someone from a project. Their in-progress papers go back to the
    pool (otherwise nobody could ever edit them again, M7). Submitted papers
    keep their assignee: that is history, not a lock.
    Returns how many papers were released."""
    with db.write() as c:
        row = c.execute(
            "SELECT role FROM project_members WHERE project_id=? AND user_id=?",
            (project_id, user_id),
        ).fetchone()
        if row is None:
            raise MembershipError("that person is not on this project")
        if row["role"] == "manager":
            _guard_last_manager(c, project_id, user_id)
        released = c.execute(
            "SELECT uid FROM papers WHERE project_id=? AND assignee_id=? "
            "AND curation_status='in_progress'", (project_id, user_id),
        ).fetchall()
        now = util.iso_now()
        for p in released:
            c.execute(
                "UPDATE papers SET assignee_id=NULL, curation_status='unassigned', "
                "status_by=?, status_at=?, status_reason=NULL WHERE project_id=? AND uid=?",
                (actor_id, now, project_id, p["uid"]),
            )
            log_event(c, project_id, p["uid"], actor_id, "unassign",
                      fromAssignee=user_id, reason="member removed")
        c.execute("DELETE FROM project_members WHERE project_id=? AND user_id=?",
                  (project_id, user_id))
        log_event(c, project_id, None, actor_id, "member_removed",
                  user=user_id, released=len(released))
    return len(released)


def directory(db: Db) -> list:
    """Active accounts a manager can pick from when staffing a project (M13)."""
    return [
        {"id": r["id"], "name": r["name"], "email": r["email"]}
        for r in db.query(
            "SELECT id, name, email FROM users WHERE state='active' ORDER BY name COLLATE NOCASE"
        )
    ]


# --------------------------------------------------------------------------- #
# Join links
# --------------------------------------------------------------------------- #
def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def create_link(db: Db, project_id: str, actor_id: str, days=None, max_uses=None) -> tuple:
    """-> (link dict, raw token). The token is shown once; only its hash is kept."""
    try:
        days = int(days) if days not in (None, "") else LINK_DEFAULT_DAYS
    except (TypeError, ValueError):
        raise MembershipError("expiry must be a number of days")
    if not 1 <= days <= LINK_MAX_DAYS:
        raise MembershipError(f"expiry must be between 1 and {LINK_MAX_DAYS} days")
    if max_uses in ("", None):
        max_uses = None
    else:
        try:
            max_uses = int(max_uses)
        except (TypeError, ValueError):
            raise MembershipError("maximum uses must be a whole number")
        if max_uses < 1:
            raise MembershipError("maximum uses must be at least 1")
    token = secrets.token_urlsafe(32)
    link_id = "l_" + secrets.token_hex(5)
    now = _now()
    with db.write() as c:
        c.execute(
            "INSERT INTO join_links (id, project_id, token_hash, role, created_by, "
            "created_at, expires_at, max_uses) VALUES (?,?,?,?,?,?,?,?)",
            (link_id, project_id, _hash(token), "curator", actor_id, _ts(now),
             _ts(now + timedelta(days=days)), max_uses),
        )
        log_event(c, project_id, None, actor_id, "link_created", link=link_id,
                  days=days, maxUses=max_uses)
    return get_link(db, project_id, link_id), token


def _link_state(row) -> str:
    if row["revoked_at"]:
        return "revoked"
    if row["expires_at"] <= _ts(_now()):
        return "expired"
    if row["max_uses"] is not None and row["uses"] >= row["max_uses"]:
        return "used_up"
    return "valid"


def _link_dict(row) -> dict:
    return {
        "id": row["id"], "role": row["role"], "createdAt": row["created_at"],
        "createdBy": row["created_by"], "expiresAt": row["expires_at"],
        "maxUses": row["max_uses"], "uses": row["uses"],
        "revokedAt": row["revoked_at"], "state": _link_state(row),
    }


def get_link(db: Db, project_id: str, link_id: str):
    row = db.query_one("SELECT * FROM join_links WHERE id=? AND project_id=?",
                       (link_id, project_id))
    return _link_dict(row) if row else None


def list_links(db: Db, project_id: str) -> list:
    return [_link_dict(r) for r in db.query(
        "SELECT * FROM join_links WHERE project_id=? ORDER BY created_at DESC", (project_id,)
    )]


def revoke_link(db: Db, project_id: str, link_id: str, actor_id: str) -> dict:
    with db.write() as c:
        n = c.execute(
            "UPDATE join_links SET revoked_at=COALESCE(revoked_at, ?) WHERE id=? AND project_id=?",
            (util.iso_now(), link_id, project_id),
        ).rowcount
        if not n:
            raise MembershipError("no such link")
        log_event(c, project_id, None, actor_id, "link_revoked", link=link_id)
    return get_link(db, project_id, link_id)


def lookup_token(db: Db, token: str):
    """-> (link row, project row) for a token, whatever its state; None if the
    token matches nothing or its project is archived."""
    if not token or len(token) > 200:
        return None
    row = db.query_one("SELECT * FROM join_links WHERE token_hash=?", (_hash(token),))
    if row is None:
        return None
    proj = db.query_one("SELECT id, name FROM projects WHERE id=? AND archived_at IS NULL",
                        (row["project_id"],))
    if proj is None:
        return None
    return row, proj


def describe_token(db: Db, token: str) -> dict:
    """What the public /join page may show: validity + project name, no more."""
    found = lookup_token(db, token)
    if found is None:
        return {"state": "invalid"}
    link, proj = found
    return {"state": _link_state(link), "projectName": proj["name"], "role": link["role"]}


def redeem(db: Db, token: str = None, user_id: str = None, link_id: str = None) -> str:
    """Use a link (by raw token, or by id for a sign-up being approved) to add
    `user_id` as a curator. -> project id. Joining a project you are already
    on does not consume a use and never downgrades a manager."""
    if token is not None:
        found = lookup_token(db, token)
        if found is None:
            raise MembershipError("this invite link is not valid")
        link = found[0]
    else:
        link = db.query_one("SELECT * FROM join_links WHERE id=?", (link_id,))
        if link is None:
            raise MembershipError("this invite link no longer exists")
    if _link_state(link) != "valid":
        raise MembershipError(
            "this invite link is no longer valid — ask the project manager for a new one"
        )
    pid = link["project_id"]
    existing = db.scalar("SELECT role FROM project_members WHERE project_id=? AND user_id=?",
                         (pid, user_id))
    if existing:
        return pid
    add_member(db, pid, user_id, link["role"], actor_id=user_id, via_link_id=link["id"])
    with db.write() as c:
        c.execute("UPDATE join_links SET uses=uses+1 WHERE id=?", (link["id"],))
    return pid


def link_id_for_token(db: Db, token: str):
    """For sign-up through a link: remember WHICH link, to redeem on approval.
    A dead link is refused up front rather than silently dropped later."""
    found = lookup_token(db, token)
    if found is None or _link_state(found[0]) != "valid":
        raise MembershipError(
            "this invite link is no longer valid — ask the project manager for a new one"
        )
    return found[0]["id"]
