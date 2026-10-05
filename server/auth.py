"""Accounts, passwords and server-side sessions. Stdlib only.

Passwords are salted scrypt hashes (hashlib.scrypt, NFR-3). Sessions live in the
database and the browser only ever holds an opaque random token in an httpOnly
cookie; the table stores sha256(token), so a copy of the database does not hand
out live logins. Logging out, resetting a password or deactivating a user deletes
the rows, which ends access immediately (NFR-4) rather than when a token expires.

There is no email anywhere: accounts come from sign-up (approved by a
superadmin) or are created by a superadmin, and forgotten passwords are reset by
a superadmin, who hands over a temporary password themselves.
"""
from __future__ import annotations

import collections
import hashlib
import hmac
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

from . import util
from .db import Db

ACCOUNT_ROLES = ("superadmin", "manager", "user")
STATES = ("pending", "active", "rejected", "deactivated")

MIN_PASSWORD = 10
MAX_PASSWORD = 256
MAX_NAME = 120
MAX_EMAIL = 200

SESSION_IDLE = timedelta(hours=12)
SESSION_ABSOLUTE = timedelta(days=7)
# last_seen_at is a write under the global lock; autosave is chatty, so only
# touch it when it is this stale.
TOUCH_EVERY = timedelta(seconds=60)

# scrypt cost. n=2**14, r=8 uses 16 MB, inside hashlib's 32 MB default maxmem.
_N, _R, _P = 2 ** 14, 8, 1


class AuthError(Exception):
    """A problem the user can act on; the message is safe to show them."""


# --------------------------------------------------------------------------- #
# Passwords
# --------------------------------------------------------------------------- #
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_hex, dk_hex = stored.split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(
            password.encode("utf-8"), salt=bytes.fromhex(salt_hex),
            n=int(n), r=int(r), p=int(p), dklen=len(dk_hex) // 2,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk.hex(), dk_hex)


# Burned on a login for an unknown email, so "no such user" and "wrong password"
# take the same time and cannot be told apart by timing.
_DUMMY_HASH = hash_password(secrets.token_hex(8))


def temp_password() -> str:
    """Readable temporary password for a superadmin to hand over (14 chars)."""
    alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(14))


def _check_password(password) -> str:
    password = str(password or "")
    if len(password) < MIN_PASSWORD:
        raise AuthError(f"password must be at least {MIN_PASSWORD} characters")
    if len(password) > MAX_PASSWORD:
        raise AuthError(f"password is too long (max {MAX_PASSWORD} characters)")
    return password


def _clean_name(name) -> str:
    name = " ".join(str(name or "").split())
    if not name:
        raise AuthError("name is required")
    if len(name) > MAX_NAME:
        raise AuthError(f"name is too long (max {MAX_NAME} characters)")
    return name


def clean_email(email) -> str:
    email = str(email or "").strip().lower()
    if not email:
        raise AuthError("email is required")
    if len(email) > MAX_EMAIL:
        raise AuthError(f"email is too long (max {MAX_EMAIL} characters)")
    local, _, domain = email.partition("@")
    if not local or "." not in domain or " " in email:
        raise AuthError(f"{email!r} doesn't look like an email address")
    return email


def _check_role(role) -> str:
    if role not in ACCOUNT_ROLES:
        raise AuthError(f"unknown account role {role!r}")
    return role


# --------------------------------------------------------------------------- #
# Users
# --------------------------------------------------------------------------- #
def public_user(row) -> dict:
    """The shape that crosses /api. Never includes the hash."""
    if row is None:
        return None
    return {
        "id": row["id"],
        "email": row["email"],
        "name": row["name"],
        "role": row["account_role"],
        "state": row["state"],
        "mustChangePassword": bool(row["must_change_password"]),
        "createdAt": row["created_at"],
        "lastLoginAt": row["last_login_at"],
    }


def get_user(db: Db, user_id: str):
    return db.query_one("SELECT * FROM users WHERE id=?", (user_id,))


def get_user_by_email(db: Db, email: str):
    return db.query_one("SELECT * FROM users WHERE email=?", (str(email or "").strip(),))


def list_users(db: Db, state: str | None = None) -> list:
    if state:
        rows = db.query("SELECT * FROM users WHERE state=? ORDER BY created_at", (state,))
    else:
        rows = db.query("SELECT * FROM users ORDER BY state='pending' DESC, name COLLATE NOCASE")
    return [public_user(r) for r in rows]


def names_by_id(db: Db) -> dict:
    """{user_id: name} for every account — attribution survives deactivation."""
    return {r["id"]: r["name"] for r in db.query("SELECT id, name FROM users")}


def _insert_user(c, *, email, name, password, role, state, must_change,
                 decided_by=None, pending_link_id=None) -> str:
    uid = "u_" + secrets.token_hex(6)
    now = util.iso_now()
    try:
        c.execute(
            "INSERT INTO users (id, email, name, password_hash, account_role, state, "
            "must_change_password, pending_link_id, created_at, decided_at, decided_by) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (uid, email, name, hash_password(password), role, state,
             1 if must_change else 0, pending_link_id, now,
             now if state != "pending" else None, decided_by),
        )
    except Exception as exc:  # sqlite3.IntegrityError on users_email
        if "UNIQUE" in str(exc).upper():
            raise AuthError("an account with that email already exists")
        raise
    return uid


def signup(db: Db, name, email, password, pending_link_id=None) -> dict:
    """Self sign-up. The account starts PENDING: it cannot log in until a
    superadmin approves it. No email is verified, so approval is the check."""
    email = clean_email(email)
    name = _clean_name(name)
    password = _check_password(password)
    with db.write() as c:
        uid = _insert_user(c, email=email, name=name, password=password,
                           role="user", state="pending", must_change=False,
                           pending_link_id=pending_link_id)
    return public_user(get_user(db, uid))


def create_user(db: Db, name, email, role, actor_id: str) -> tuple:
    """Superadmin-created account, active at once, with a temporary password the
    superadmin hands over. Returns (user, temp_password)."""
    email = clean_email(email)
    name = _clean_name(name)
    role = _check_role(role)
    pw = temp_password()
    with db.write() as c:
        uid = _insert_user(c, email=email, name=name, password=pw, role=role,
                           state="active", must_change=True, decided_by=actor_id)
    return public_user(get_user(db, uid)), pw


def decide_signup(db: Db, user_id: str, approve: bool, role: str, actor_id: str):
    """Approve (with an account role) or reject a pending sign-up. Returns the
    user row's pending_link_id so the caller can redeem it on approval."""
    row = get_user(db, user_id)
    if row is None:
        raise AuthError("no such user")
    if row["state"] != "pending":
        raise AuthError(f"that account is {row['state']}, not pending")
    role = _check_role(role or "user")
    with db.write() as c:
        c.execute(
            "UPDATE users SET state=?, account_role=?, decided_at=?, decided_by=?, "
            "pending_link_id=NULL WHERE id=? AND state='pending'",
            ("active" if approve else "rejected", role, util.iso_now(), actor_id, user_id),
        )
    return row["pending_link_id"] if approve else None


def _active_superadmins(db: Db) -> int:
    return db.scalar(
        "SELECT COUNT(*) FROM users WHERE account_role='superadmin' AND state='active'"
    ) or 0


def set_role(db: Db, user_id: str, role: str, actor_id: str) -> dict:
    role = _check_role(role)
    row = get_user(db, user_id)
    if row is None:
        raise AuthError("no such user")
    if (row["account_role"] == "superadmin" and role != "superadmin"
            and row["state"] == "active" and _active_superadmins(db) <= 1):
        raise AuthError("you can't demote the last superadmin")
    with db.write() as c:
        c.execute("UPDATE users SET account_role=? WHERE id=?", (role, user_id))
    return public_user(get_user(db, user_id))


def set_active(db: Db, user_id: str, active: bool, actor_id: str) -> dict:
    row = get_user(db, user_id)
    if row is None:
        raise AuthError("no such user")
    if row["state"] in ("pending", "rejected"):
        raise AuthError(f"that account is {row['state']}; approve or reject it instead")
    if not active:
        if user_id == actor_id:
            raise AuthError("you can't deactivate yourself")
        if row["account_role"] == "superadmin" and _active_superadmins(db) <= 1:
            raise AuthError("you can't deactivate the last superadmin")
    with db.write() as c:
        c.execute("UPDATE users SET state=? WHERE id=?",
                  ("active" if active else "deactivated", user_id))
        if not active:
            c.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
    return public_user(get_user(db, user_id))


def reset_password(db: Db, user_id: str) -> str:
    """Superadmin reset: new temporary password, must change at next login, and
    every existing session for the account ends."""
    if get_user(db, user_id) is None:
        raise AuthError("no such user")
    pw = temp_password()
    with db.write() as c:
        c.execute("UPDATE users SET password_hash=?, must_change_password=1 WHERE id=?",
                  (hash_password(pw), user_id))
        c.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
    return pw


def change_password(db: Db, user_id: str, current, new, keep_token_hash: str = None) -> None:
    """The user's own change. Other sessions end; the current one is kept so they
    are not thrown out of the tab they did it from."""
    row = get_user(db, user_id)
    if row is None or not verify_password(str(current or ""), row["password_hash"]):
        raise AuthError("current password is incorrect")
    new = _check_password(new)
    if new == current:
        raise AuthError("the new password must be different from the current one")
    with db.write() as c:
        c.execute("UPDATE users SET password_hash=?, must_change_password=0 WHERE id=?",
                  (hash_password(new), user_id))
        c.execute("DELETE FROM sessions WHERE user_id=? AND token_hash IS NOT ?",
                  (user_id, keep_token_hash))


# --------------------------------------------------------------------------- #
# Login + sessions
# --------------------------------------------------------------------------- #
LOGIN_FAILED = "email or password is incorrect"


def authenticate(db: Db, email, password):
    """-> user row. Raises AuthError with a message that never says WHICH of the
    two was wrong (M1). A pending/rejected/deactivated account only learns its
    state after giving the right password."""
    try:
        email = clean_email(email)
    except AuthError:
        raise AuthError(LOGIN_FAILED)
    row = get_user_by_email(db, email)
    if row is None:
        verify_password(str(password or ""), _DUMMY_HASH)
        raise AuthError(LOGIN_FAILED)
    if not verify_password(str(password or ""), row["password_hash"]):
        raise AuthError(LOGIN_FAILED)
    if row["state"] == "pending":
        raise AuthError("your account is waiting for approval by an administrator")
    if row["state"] == "rejected":
        raise AuthError("this account request was not approved")
    if row["state"] == "deactivated":
        raise AuthError("this account has been deactivated")
    return row


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def create_session(db: Db, user_id: str, ip: str = "", user_agent: str = "") -> str:
    """-> the raw token for the cookie. Only its hash is stored."""
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    with db.write() as c:
        c.execute(
            "INSERT INTO sessions (token_hash, user_id, created_at, last_seen_at, "
            "expires_at, ip, user_agent) VALUES (?,?,?,?,?,?,?)",
            (_token_hash(token), user_id, _ts(now), _ts(now),
             _ts(now + SESSION_ABSOLUTE), (ip or "")[:64], (user_agent or "")[:300]),
        )
        c.execute("UPDATE users SET last_login_at=? WHERE id=?", (_ts(now), user_id))
        # Opportunistic cleanup; there is no cron.
        c.execute("DELETE FROM sessions WHERE expires_at < ?", (_ts(now),))
    return token


def resolve_session(db: Db, token: str):
    """-> (user_row, token_hash) for a live session of an ACTIVE user, else None."""
    if not token or len(token) > 200:
        return None
    th = _token_hash(token)
    row = db.query_one(
        "SELECT s.last_seen_at, s.expires_at, u.* FROM sessions s "
        "JOIN users u ON u.id = s.user_id WHERE s.token_hash=?", (th,),
    )
    if row is None:
        return None
    now = datetime.now(timezone.utc)
    try:
        last_seen = _parse_ts(row["last_seen_at"])
        expires = _parse_ts(row["expires_at"])
    except ValueError:
        return None
    if row["state"] != "active" or now >= expires or now - last_seen >= SESSION_IDLE:
        delete_session(db, token)
        return None
    if now - last_seen >= TOUCH_EVERY:
        with db.write() as c:
            c.execute("UPDATE sessions SET last_seen_at=? WHERE token_hash=?", (_ts(now), th))
    return row, th


def delete_session(db: Db, token: str) -> None:
    if not token:
        return
    with db.write() as c:
        c.execute("DELETE FROM sessions WHERE token_hash=?", (_token_hash(token),))


# --------------------------------------------------------------------------- #
# Throttling (in memory: a restart forgets it, which is fine at this scale)
# --------------------------------------------------------------------------- #
class Throttle:
    """At most `limit` hits per key in a sliding `window` of seconds."""

    def __init__(self, limit: int, window: float):
        self.limit = limit
        self.window = window
        self._hits = collections.defaultdict(collections.deque)
        self._lock = threading.Lock()

    def blocked(self, key: str) -> bool:
        with self._lock:
            q = self._hits.get(key)
            if not q:
                return False
            cutoff = time.monotonic() - self.window
            while q and q[0] < cutoff:
                q.popleft()
            return len(q) >= self.limit

    def hit(self, key: str) -> None:
        with self._lock:
            self._hits[key].append(time.monotonic())

    def clear(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)


LOGIN_FAILS_PER_EMAIL = Throttle(5, 15 * 60)
LOGIN_FAILS_PER_IP = Throttle(30, 15 * 60)
SIGNUPS_PER_IP = Throttle(5, 60 * 60)


# --------------------------------------------------------------------------- #
# First superadmin (decision 3: from the env file)
# --------------------------------------------------------------------------- #
def bootstrap_superadmin(db: Db, env: dict, log=print) -> None:
    """If there is no active superadmin, create one from ANNOTAID_ADMIN_EMAIL /
    ANNOTAID_ADMIN_PASSWORD (+ optional ANNOTAID_ADMIN_NAME). Does nothing once a
    superadmin exists, so the values can safely stay in the file.

    If an account with that email already exists it is promoted, reactivated and
    given the env password — the way back in if the only superadmin was lost.
    The password must be changed at first login either way.
    """
    if _active_superadmins(db) > 0:
        return
    email = (env.get("ANNOTAID_ADMIN_EMAIL") or "").strip()
    password = env.get("ANNOTAID_ADMIN_PASSWORD") or ""
    name = (env.get("ANNOTAID_ADMIN_NAME") or "").strip() or "Administrator"
    if not email or not password:
        log("warning: no active superadmin, and ANNOTAID_ADMIN_EMAIL / "
            "ANNOTAID_ADMIN_PASSWORD are not set — nobody can administer this "
            "server until they are (then restart).")
        return
    try:
        email = clean_email(email)
        _check_password(password)
    except AuthError as exc:
        log(f"warning: superadmin bootstrap skipped: {exc}")
        return
    row = get_user_by_email(db, email)
    with db.write() as c:
        if row is None:
            _insert_user(c, email=email, name=name, password=password,
                         role="superadmin", state="active", must_change=True)
        else:
            c.execute(
                "UPDATE users SET account_role='superadmin', state='active', "
                "password_hash=?, must_change_password=1 WHERE id=?",
                (hash_password(password), row["id"]),
            )
            c.execute("DELETE FROM sessions WHERE user_id=?", (row["id"],))
    log(f"superadmin {email} set up from the environment (must change password at first login)")
