"""Response helpers + safe static file serving."""
from __future__ import annotations

import json
import mimetypes
import os

mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")


def send_json(handler, obj, status: int = 200) -> None:
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


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
