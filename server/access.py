"""Route access markers, read by the gate in app._dispatch.

Every route needs a logged-in, active user unless it is marked @public. The gate
runs BEFORE the handler, so a handler that forgets to check is still covered —
the default is closed, not open (NFR-1).
"""
from __future__ import annotations


def public(fn):
    """Reachable without a session (login, sign-up, join pages and their APIs)."""
    fn.public = True
    return fn


def allow_must_change(fn):
    """Reachable by a user who must change their password first (their own
    profile, the password change itself, logout)."""
    fn.allow_must_change = True
    return fn


def superadmin_only(fn):
    fn.superadmin_only = True
    return fn


def account_roles(*roles):
    """Restrict a GLOBAL route to these account roles (e.g. who may create a
    project). Project routes use handlers.project_route(min_role=...) instead."""
    def deco(fn):
        fn.account_roles = roles
        return fn
    return deco


def copy_markers(src, dst):
    for attr in ("public", "allow_must_change", "superadmin_only", "account_roles"):
        if hasattr(src, attr):
            setattr(dst, attr, getattr(src, attr))
    return dst
