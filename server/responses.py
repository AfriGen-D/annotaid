"""Response helpers + safe static file serving."""
from __future__ import annotations

import json
import mimetypes
import os

mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")


def send_json(handler, obj, status: int = 200, extra=None) -> None:
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    for k, v in (extra or {}).items():
        handler.send_header(k, v)
    handler.end_headers()
    handler.wfile.write(body)


def redirect(handler, location: str, status: int = 302) -> None:
    handler.send_response(status)
    handler.send_header("Location", location)
    handler.send_header("Content-Length", "0")
    handler.end_headers()


SESSION_COOKIE = "annotaid_session"


def session_cookie(token: str, secure: bool, max_age: int = None) -> str:
    """Set-Cookie value. HttpOnly so page scripts can't read it; SameSite=Lax so
    other sites can't ride it on a POST; host-only (no Domain), so the test and
    production domains each keep their own logins. Secure whenever the request
    arrived over HTTPS (see app._cookie_secure)."""
    parts = [f"{SESSION_COOKIE}={token}", "Path=/", "HttpOnly", "SameSite=Lax"]
    if secure:
        parts.append("Secure")
    if max_age is not None:
        parts.append(f"Max-Age={max_age}")
    return "; ".join(parts)


def clear_session_cookie(secure: bool) -> str:
    return session_cookie("", secure, max_age=0)


def send_file(handler, path: str, content_type: str, extra=None, chunk: int = 1 << 20) -> None:
    """Stream a file in chunks — for files too big to read into memory at once
    (the database snapshot)."""
    size = os.path.getsize(path)
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(size))
    for k, v in (extra or {}).items():
        handler.send_header(k, v)
    handler.end_headers()
    with open(path, "rb") as fh:
        while True:
            buf = fh.read(chunk)
            if not buf:
                break
            handler.wfile.write(buf)


def send_bytes(handler, data: bytes, content_type: str, status: int = 200, extra=None) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(data)))
    for k, v in (extra or {}).items():
        handler.send_header(k, v)
    handler.end_headers()
    handler.wfile.write(data)


def send_text(handler, text: str, content_type: str, status: int = 200, extra=None) -> None:
    send_bytes(handler, text.encode("utf-8"), content_type, status, extra)


def send_error_json(handler, status: int, message: str) -> None:
    send_json(handler, {"error": message}, status)


def serve_static(handler, static_dir: str, url_path: str) -> None:
    rel = url_path.lstrip("/")
    if rel == "" or rel.endswith("/"):
        rel = rel + "index.html"
    target = os.path.realpath(os.path.join(static_dir, rel))
    root = os.path.realpath(static_dir)
    if not (target == root or target.startswith(root + os.sep)):
        send_error_json(handler, 403, "forbidden")
        return
    if not os.path.isfile(target):
        send_error_json(handler, 404, "not found")
        return
    ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
    with open(target, "rb") as fh:
        data = fh.read()
    send_bytes(handler, data, ctype)
