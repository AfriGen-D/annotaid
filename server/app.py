"""ThreadingHTTPServer + request handler. Stdlib only."""
from __future__ import annotations

import traceback
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from . import handlers, responses
from .config_loader import Config, Secrets
from .router import Router
from .store import Store

MAX_BODY = 64 * 1024 * 1024  # 64 MB cap (large PDFs)


@dataclass
class Context:
    config: Config
    secrets: Secrets
    store: Store
    static_dir: str


def make_handler(ctx: Context, router: Router):
    class Handler(BaseHTTPRequestHandler):
        server_version = "annotaid/0.1"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # quieter default logging
            pass

        def _read_body(self) -> bytes:
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length <= 0:
                return b""
            if length > MAX_BODY:
                return b""
            return self.rfile.read(length)

        def _dispatch(self, method: str):
            path = urlparse(self.path).path
            handler, params = router.match(method, path)
            if handler is None:
                if method == "GET" and not path.startswith("/api/"):
                    return responses.serve_static(self, ctx.static_dir, path)
                return responses.send_error_json(self, 404, "not found")
            body = self._read_body() if method in ("POST", "PUT") else b""
            try:
                handler(ctx, self, params, body)
            except BrokenPipeError:
                pass
            except Exception as exc:  # noqa: BLE001
                traceback.print_exc()
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


def build_server(ctx: Context, host: str, port: int) -> ThreadingHTTPServer:
    router = Router()
    handlers.register(router)
    handler_cls = make_handler(ctx, router)
    return ThreadingHTTPServer((host, port), handler_cls)
