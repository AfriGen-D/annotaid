"""What the request looked like from the browser's side, through nginx.

nginx terminates HTTPS and forwards to this process over plain HTTP on
127.0.0.1, adding X-Forwarded-Proto / X-Forwarded-For. Those headers are only
believed when the socket peer is local — nothing else could have set them.
"""
from __future__ import annotations

_LOCAL = ("127.0.0.1", "::1")


def _from_proxy(req) -> bool:
    return bool(req.client_address) and req.client_address[0] in _LOCAL


def forwarded_proto(req) -> str:
    if not _from_proxy(req):
        return ""
    return (req.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()


def client_ip(req) -> str:
    """The real client: the first X-Forwarded-For entry behind nginx."""
    peer = req.client_address[0] if req.client_address else ""
    if peer in _LOCAL:
        fwd = (req.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        if fwd:
            return fwd
    return peer


def cookie_secure(mode: str, req) -> bool:
    """mode 'auto': Secure whenever the browser used HTTPS (nginx says so);
    'always' / 'never' force it."""
    if mode == "always":
        return True
    if mode == "never":
        return False
    return forwarded_proto(req) == "https"


def site_url(req, path: str) -> str:
    """Absolute URL on whichever host the request came in on (the test and
    production domains alike) — for links shown to the user."""
    host = req.headers.get("Host") or "localhost"
    proto = forwarded_proto(req) or "http"
    return f"{proto}://{host}{path}"
