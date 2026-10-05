"""ThreadingHTTPServer + request handler + the access gate. Stdlib only.

Every request passes through Handler._dispatch, which is the ONE place that
decides whether it may proceed (NFR-1, NFR-2):

    1. match the route (or fall through to a static file)
    2. resolve the session cookie -> req.user (an active account) or None
    3. POST/PUT: the Origin must be this site (CSRF)
    4. not @public and no user          -> 401 (API) / redirect to /login (page)
    5. must change password             -> only @allow_must_change routes
    6. @superadmin_only / @account_roles -> 403 otherwise
    7. project routes additionally check membership (handlers.project_route)
"""
from __future__ import annotations

import collections
import threading
import traceback
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, urlparse

from . import auth, handlers, responses, util
from .net import cookie_secure as _cookie_secure, forwarded_proto
from .config_loader import Config, Secrets
from .db import Db
from .projects import ProjectRegistry
from .router import Router

MAX_BODY = 64 * 1024 * 1024  # 64 MB cap (large PDFs)

# Static files reachable without a session: only what the login / sign-up /
# join pages themselves load. pdf.js, the app's own modules and every PDF stay
# behind the gate.
PUBLIC_STATIC = ("/css/", "/js/authPages.js", "/favicon.ico")


class ErrorLog:
    """The last N server errors, for the admin System tab — whoever runs this
    cannot read the server's logs. Records type, message, method, path and time
    only: never a request body (it might be a login)."""

    def __init__(self, size: int = 50):
        self._items = collections.deque(maxlen=size)
        self._lock = threading.Lock()

    def add(self, method: str, path: str, exc: BaseException) -> None:
        with self._lock:
            self._items.appendleft({
                "at": util.iso_now(), "method": method, "path": path,
                "type": type(exc).__name__, "message": str(exc)[:500],
            })

    def list(self) -> list:
        with self._lock:
            return list(self._items)


@dataclass
class Context:
    """There is no global `config` or `store` any more — a feature set and a paper
    library both belong to a project, resolved per request via `projects`."""

    secrets: Secrets
    db: Db
    projects: ProjectRegistry
    static_dir: str
    # Starter config used to seed a new project. Optional: the server is fully
    # usable with existing projects even if the template file is broken.
    template: Config | None = None
    # Extra origins allowed to POST, besides the site's own (Origin == the Host
    # header). From ANNOTAID_ALLOWED_ORIGINS; usually unnecessary.
    allowed_origins: frozenset = frozenset()
    # 'auto': Secure cookie when the request came over HTTPS (nginx sets
    # X-Forwarded-Proto); 'always' / 'never' force it.
    cookie_secure: str = "auto"
    errors: ErrorLog = field(default_factory=ErrorLog)
    jobs: object = None      # server.jobs.JobRunner
    backups: object = None   # server.backup.Backups
    data_dir: str = ""
    version: str = ""


def _origin_ok(ctx: Context, req) -> bool:
    """CSRF guard for state-changing requests. Browsers send Origin on every
    cross-origin POST (and Chrome/Firefox on same-origin ones too); fall back to
    Referer. Neither present -> refuse. 'null' (sandboxed iframes, odd
    redirects) is refused explicitly."""
    origin = req.headers.get("Origin")
    if origin is None:
        ref = req.headers.get("Referer")
        if not ref:
            return False
        u = urlparse(ref)
        origin = f"{u.scheme}://{u.netloc}"
    origin = origin.strip().rstrip("/")
    if not origin or origin == "null":
        return False
    if origin in ctx.allowed_origins:
        return True
    host = (req.headers.get("Host") or "").strip()
    if not host:
        return False
    proto = forwarded_proto(req) or "http"
    return origin == f"{proto}://{host}"


def make_handler(ctx: Context, router: Router):
    class Handler(BaseHTTPRequestHandler):
        server_version = "annotaid"
        sys_version = ""
        protocol_version = "HTTP/1.1"

        # Set by _dispatch for the handlers.
        user = None            # users row of the logged-in, active account
        session_hash = None    # sha256 of this request's session token

        def log_message(self, fmt, *args):  # quieter default logging
            pass

        def cookie_secure(self) -> bool:
            return _cookie_secure(ctx.cookie_secure, self)

        def session_token(self) -> str:
            raw = self.headers.get("Cookie")
            if not raw:
                return ""
            try:
                c = SimpleCookie()
                c.load(raw)
            except Exception:  # noqa: BLE001 — a malformed cookie is no cookie
                return ""
            m = c.get(responses.SESSION_COOKIE)
            return m.value if m else ""

        def _deny(self, status: int, message: str, is_api: bool, path: str, code=None):
            if not is_api and status == 401:
                nxt = path if path not in ("/", "") else ""
                return responses.redirect(self, "/login" + (f"?next={quote(nxt)}" if nxt else ""))
            if not is_api and code == "must_change_password":
                return responses.redirect(self, "/account?force=1")
            body = {"error": message}
            if code:
                body["code"] = code
            return responses.send_json(self, body, status)

        def _dispatch(self, method: str):
            parsed = urlparse(self.path)
            path = parsed.path
            is_api = path.startswith("/api/")

            body = b""
            if method == "POST":
                length = int(self.headers.get("Content-Length", 0) or 0)
                if length > MAX_BODY:
                    # Don't leave the unread body in the socket: the next
                    # keep-alive request would be parsed out of it.
                    self.close_connection = True
                    return responses.send_json(
                        self, {"error": "upload too large (max 64 MB)"}, 413,
                        extra={"Connection": "close"},
                    )
                if length > 0:
                    body = self.rfile.read(length)

            handler, params = router.match(method, path)
            if handler is None and (method != "GET" or is_api):
                return responses.send_error_json(self, 404, "not found")

            found = auth.resolve_session(ctx.db, self.session_token())
            self.user, self.session_hash = found if found else (None, None)

            if method == "POST" and not _origin_ok(ctx, self):
                return responses.send_error_json(
                    self, 403, "request refused: it did not come from this site"
                )

            if handler is None:
                is_public = path.startswith(PUBLIC_STATIC)
            else:
                is_public = getattr(handler, "public", False)

            if not is_public:
                if self.user is None:
                    return self._deny(401, "not logged in", is_api, path)
                # Static files (handler None) are code, not actions: the
                # account page needs its scripts to change the password at all.
                if (self.user["must_change_password"] and handler is not None
                        and not getattr(handler, "allow_must_change", False)):
                    return self._deny(403, "you must change your password first",
                                      is_api, path, code="must_change_password")
                if (getattr(handler, "superadmin_only", False)
                        and self.user["account_role"] != "superadmin"):
                    return self._deny(403, "administrators only", is_api, path)
                roles = getattr(handler, "account_roles", None)
                if roles and self.user["account_role"] not in roles:
                    return self._deny(403, "your account can't do that", is_api, path)

            if handler is None:
                return responses.serve_static(self, ctx.static_dir, path)
            try:
                handler(ctx, self, params, body)
            except BrokenPipeError:
                pass
            except Exception as exc:  # noqa: BLE001
                traceback.print_exc()
                ctx.errors.add(method, path, exc)
                try:
                    responses.send_error_json(self, 500, f"{type(exc).__name__}: {exc}")
                except Exception:  # noqa: BLE001
                    pass

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_PUT(self):
            self._dispatch("POST")  # treat PUT like POST for autosave convenience

    return Handler


def build_router() -> Router:
    router = Router()
    handlers.register(router)
    return router


def build_server(ctx: Context, host: str, port: int) -> ThreadingHTTPServer:
    router = build_router()
    handler_cls = make_handler(ctx, router)
    server = ThreadingHTTPServer((host, port), handler_cls)
    server.daemon_threads = True
    return server
