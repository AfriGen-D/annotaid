"""Paper curation workflow: who is responsible for a paper and how far it got.

    unassigned --claim / assign--> in_progress --submit--> submitted
                                   in_progress --exclude--> excluded | unextractable
    in_progress --unassign (manager)--> unassigned
    submitted / excluded / unextractable --reopen (manager)--> in_progress
                                         (or unassigned if nobody holds it)

Only the assignee may edit a paper, and only while it is in progress (M7). A
manager changes who is responsible by reassigning, not by editing around them.
Every transition is written to `events` with who, when and why (M8, M9).

Each transition is a single UPDATE whose WHERE clause states the state it
expects. If someone else got there first, zero rows change and the caller gets
a clear error instead of a silent overwrite — this is what makes "claim" safe
when two curators click at the same moment (NFR-11).
"""
from __future__ import annotations

import json

from . import util
from .auth import AuthError
from .db import Db
from .membership import at_least, log_event

UNASSIGNED = "unassigned"
IN_PROGRESS = "in_progress"
SUBMITTED = "submitted"
EXCLUDED = "excluded"
UNEXTRACTABLE = "unextractable"
STATUSES = (UNASSIGNED, IN_PROGRESS, SUBMITTED, EXCLUDED, UNEXTRACTABLE)
FINISHED = (SUBMITTED, EXCLUDED, UNEXTRACTABLE)
MAX_REASON = 2000


class WorkflowError(AuthError):
    """`status` is the HTTP status the handler should answer with."""

    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


def _paper(c, project_id: str, uid: str):
    row = c.execute("SELECT * FROM papers WHERE project_id=? AND uid=?",
                    (project_id, uid)).fetchone()
    if row is None:
        raise WorkflowError(f"no paper {uid} in this project", 404)
    return row


def _name(c, user_id):
    if not user_id:
        return None
    row = c.execute("SELECT name FROM users WHERE id=?", (user_id,)).fetchone()
    return row["name"] if row else user_id


def _set(c, project_id, uid, expect_where, expect_params, *, assignee, status,
         reason, actor) -> None:
    n = c.execute(
        "UPDATE papers SET assignee_id=?, curation_status=?, status_reason=?, "
        "status_by=?, status_at=? WHERE project_id=? AND uid=? AND " + expect_where,
        (assignee, status, reason, actor, util.iso_now(), project_id, uid, *expect_params),
    ).rowcount
    if n != 1:
        raise WorkflowError("this paper changed in the meantime — reload and try again")


# --------------------------------------------------------------------------- #
# Who may edit
# --------------------------------------------------------------------------- #
def edit_block_reason(db: Db, user, project_id: str, uid: str):
    """None if `user` may edit this paper's curated values; else why not (M7)."""
    row = db.query_one(
        "SELECT p.assignee_id, p.curation_status, u.name AS assignee_name FROM papers p "
        "LEFT JOIN users u ON u.id = p.assignee_id WHERE p.project_id=? AND p.uid=?",
        (project_id, uid),
    )
    if row is None:
        return f"no paper {uid} in this project"
    if row["assignee_id"] != user["id"]:
        if row["assignee_id"] is None:
            return "this paper isn't assigned to you — claim it first"
        return f"this paper is assigned to {row['assignee_name']}"
    if row["curation_status"] != IN_PROGRESS:
        return f"this paper is {row['curation_status'].replace('_', ' ')} — a manager must reopen it first"
    return None


def setup_block_reason(db: Db, user, role, project_id: str, uid: str):
    """For paper SET-UP (confirming its identity, declaring its group rows):
    a project manager may always do it — extraction is manager-only and needs
    both done first — otherwise the same rule as editing."""
    if at_least(role, "manager"):
        return None
    return edit_block_reason(db, user, project_id, uid)


# --------------------------------------------------------------------------- #
# Transitions
# --------------------------------------------------------------------------- #
def claim(db: Db, user, project_id: str, uid: str) -> None:
    """A member takes a paper from the pool (M3)."""
    with db.write() as c:
        row = _paper(c, project_id, uid)
        if row["assignee_id"] is not None or row["curation_status"] != UNASSIGNED:
            holder = _name(c, row["assignee_id"])
            raise WorkflowError(
                f"already taken by {holder}" if holder else "this paper isn't in the pool"
            )
        _set(c, project_id, uid, "assignee_id IS NULL AND curation_status=?", (UNASSIGNED,),
             assignee=user["id"], status=IN_PROGRESS, reason=None, actor=user["id"])
        log_event(c, project_id, uid, user["id"], "claim")


def assign(db: Db, actor, project_id: str, uid: str, assignee_id) -> None:
    """A manager assigns, reassigns, or (assignee_id=None) returns a paper to
    the pool (M17). Finished papers must be reopened first."""
    with db.write() as c:
        row = _paper(c, project_id, uid)
        if row["curation_status"] in FINISHED:
            raise WorkflowError(
                f"this paper is {row['curation_status']} — reopen it before reassigning"
            )
        if assignee_id:
            member = c.execute(
                "SELECT 1 FROM project_members m JOIN users u ON u.id=m.user_id "
                "WHERE m.project_id=? AND m.user_id=? AND u.state='active'",
                (project_id, assignee_id),
            ).fetchone()
            if member is None:
                raise WorkflowError("that person isn't an active member of this project", 400)
        if row["assignee_id"] == (assignee_id or None):
            return
        _set(c, project_id, uid, "curation_status=? AND assignee_id IS ?",
             (row["curation_status"], row["assignee_id"]),
             assignee=assignee_id or None,
             status=IN_PROGRESS if assignee_id else UNASSIGNED,
             reason=None, actor=actor["id"])
        log_event(c, project_id, uid, actor["id"], "assign" if assignee_id else "unassign",
                  fromAssignee=row["assignee_id"], toAssignee=assignee_id or None)


def _require_holder(row, user):
    if row["assignee_id"] != user["id"]:
        raise WorkflowError("only the curator this paper is assigned to can do that", 403)
    if row["curation_status"] != IN_PROGRESS:
        raise WorkflowError(f"this paper is {row['curation_status'].replace('_', ' ')}")


def submit(db: Db, user, project_id: str, uid: str) -> None:
    with db.write() as c:
        row = _paper(c, project_id, uid)
        _require_holder(row, user)
        _set(c, project_id, uid, "assignee_id=? AND curation_status=?",
             (user["id"], IN_PROGRESS),
             assignee=user["id"], status=SUBMITTED, reason=None, actor=user["id"])
        log_event(c, project_id, uid, user["id"], "submit")


def exclude(db: Db, user, project_id: str, uid: str, status: str, reason) -> None:
    """Excluded (out of scope) or unextractable (in scope, info not reported).
    A written reason is required so the decision can be reviewed (M9)."""
    if status not in (EXCLUDED, UNEXTRACTABLE):
        raise WorkflowError("status must be 'excluded' or 'unextractable'", 400)
    reason = str(reason or "").strip()
    if not reason:
        raise WorkflowError("give a reason, so the decision can be reviewed later", 400)
    if len(reason) > MAX_REASON:
        raise WorkflowError(f"reason is too long (max {MAX_REASON} characters)", 400)
    with db.write() as c:
        row = _paper(c, project_id, uid)
        _require_holder(row, user)
        _set(c, project_id, uid, "assignee_id=? AND curation_status=?",
             (user["id"], IN_PROGRESS),
             assignee=user["id"], status=status, reason=reason, actor=user["id"])
        log_event(c, project_id, uid, user["id"], status, reason=reason)


def reopen(db: Db, actor, project_id: str, uid: str, reason=None) -> None:
    """A manager sends a finished paper back to work. Not in the SRS; without
    it one wrong click on "submit" would be permanent. Logged like the rest."""
    reason = str(reason or "").strip()[:MAX_REASON] or None
    with db.write() as c:
        row = _paper(c, project_id, uid)
        if row["curation_status"] not in FINISHED:
            raise WorkflowError("only a submitted, excluded or unextractable paper can be reopened")
        holder_active = row["assignee_id"] and c.execute(
            "SELECT 1 FROM project_members m JOIN users u ON u.id=m.user_id "
            "WHERE m.project_id=? AND m.user_id=? AND u.state='active'",
            (project_id, row["assignee_id"]),
        ).fetchone()
        assignee = row["assignee_id"] if holder_active else None
        _set(c, project_id, uid, "curation_status=?", (row["curation_status"],),
             assignee=assignee, status=IN_PROGRESS if assignee else UNASSIGNED,
             reason=None, actor=actor["id"])
        log_event(c, project_id, uid, actor["id"], "reopen",
                  fromStatus=row["curation_status"], reason=reason)


def history(db: Db, project_id: str, uid: str) -> list:
    names = {r["id"]: r["name"] for r in db.query("SELECT id, name FROM users")}
    out = []
    for r in db.query(
        "SELECT * FROM events WHERE project_id=? AND uid=? ORDER BY id", (project_id, uid)
    ):
        try:
            detail = json.loads(r["detail_json"] or "{}")
        except ValueError:
            detail = {}
        for k in ("fromAssignee", "toAssignee"):
            if detail.get(k):
                detail[k + "Name"] = names.get(detail[k], detail[k])
        out.append({"at": r["at"], "action": r["action"], "actor": r["actor"],
                    "actorName": names.get(r["actor"], r["actor"]), **detail})
    return out
