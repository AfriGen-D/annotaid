"""The local curator's own identity: name, email, and an optional personal
OpenRouter key. One row per server instance ('singleton') — this app has no
login, so "the current user" is whoever is at this browser, asked once on
first run by the home-screen identity modal (static/js/userIdentity.js).

The key, if set, overrides the server's own .keys/env-configured OpenRouter
key for extraction calls (see server/handlers.h_extract) — someone running
annotaid without access to server-level secrets can still supply their own.
It is NEVER sent back to the browser once saved: get_identity() only reports
whether one is set, the same rule Config.public_dict() follows for secrets.
"""
from __future__ import annotations

from . import util
from .db import Db

_ROW_ID = "singleton"
MAX_NAME = 120
MAX_EMAIL = 200


class IdentityError(Exception):
    pass


def _clean_name(name) -> str:
    name = str(name or "").strip()
    if not name:
        raise IdentityError("name is required")
    if len(name) > MAX_NAME:
        raise IdentityError(f"name is too long (max {MAX_NAME} characters)")
    return name


def _clean_email(email) -> str:
    email = str(email or "").strip()
    if not email:
        raise IdentityError("email is required")
    if len(email) > MAX_EMAIL:
        raise IdentityError(f"email is too long (max {MAX_EMAIL} characters)")
    # Deliberately permissive shape check — rejecting a real address a strict
    # regex doesn't like is worse than accepting one a regex would ban.
    if "@" not in email or email.startswith("@") or email.endswith("@"):
        raise IdentityError(f"{email!r} doesn't look like an email address")
    return email


def _row(db: Db):
    return db.query_one("SELECT * FROM app_identity WHERE id=?", (_ROW_ID,))


def get_identity(db: Db) -> dict:
    """set=False is the first-run signal the home screen blocks the modal on."""
    row = _row(db)
    if row is None:
        return {"set": False, "name": "", "email": "", "hasOpenRouterKey": False}
    return {
        "set": True,
        "name": row["name"],
        "email": row["email"],
        "hasOpenRouterKey": bool(row["openrouter_key"]),
    }


def get_openrouter_key(db: Db) -> str:
    """The raw key, for building an OpenRouter request server-side only — never
    put this in an API response."""
    row = _row(db)
    return (row["openrouter_key"] or "") if row else ""


def save_identity(db: Db, name, email, openrouter_key=None, clear_key: bool = False) -> dict:
    """openrouter_key=None leaves whatever is stored untouched: the field is
    masked once set, so the browser never has the real value to send back
    unedited. Pass clear_key=True to remove it explicitly instead."""
    name = _clean_name(name)
    email = _clean_email(email)

    existing = _row(db)
    if clear_key:
        key = None
    elif openrouter_key is not None and str(openrouter_key).strip():
        key = str(openrouter_key).strip()
    else:
        key = existing["openrouter_key"] if existing else None

    now = util.iso_now()
    with db.write() as c:
        c.execute(
            """
            INSERT INTO app_identity (id, name, email, openrouter_key, updated_at)
            VALUES (?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
              name=excluded.name, email=excluded.email,
              openrouter_key=excluded.openrouter_key, updated_at=excluded.updated_at
            """,
            (_ROW_ID, name, email, key, now),
        )
    return get_identity(db)
