#!/usr/bin/env python3
"""annotaid entrypoint.

    python run.py                 # serve on http://127.0.0.1:8765
    python run.py --port 9000
    python run.py --keys /etc/annotaid/env

The env file (dotenv style) holds the server's secrets and settings:

    OPENROUTER_API_KEY=...           # extraction; without it extraction is off
    NCBI_API_KEY=...                 # optional, raises the NCBI rate limit
    UNPAYWALL_EMAIL=...              # optional, enables the Unpaywall source
    ANNOTAID_ADMIN_EMAIL=...         # first superadmin, created at start-up if
    ANNOTAID_ADMIN_PASSWORD=...      #   none exists (must change at first login)
    ANNOTAID_ADMIN_NAME=...          # optional
    ANNOTAID_ALLOWED_ORIGINS=...     # optional, comma-separated extra origins
    ANNOTAID_COOKIE_SECURE=auto      # auto | always | never

Binds to localhost only: in production nginx sits in front and terminates
HTTPS. Run exactly ONE instance per data directory — the write lock that keeps
concurrent saves from overwriting each other only works inside one process.
"""
from __future__ import annotations

import argparse
import os
import signal
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# allow `python run.py` from inside annotaid/ by making the package importable
sys.path.insert(0, os.path.dirname(HERE))

from annotaid.server import VERSION  # noqa: E402
from annotaid.server import app as app_mod  # noqa: E402
from annotaid.server import auth  # noqa: E402
from annotaid.server import projects as projects_mod  # noqa: E402
from annotaid.server import util  # noqa: E402
from annotaid.server.backup import Backups  # noqa: E402
from annotaid.server.config_loader import (  # noqa: E402
    ConfigError,
    Secrets,
    load_config,
)
from annotaid.server.db import Db  # noqa: E402
from annotaid.server.jobs import JobRunner  # noqa: E402


def _secrets_from_env() -> Secrets:
    return Secrets(
        openrouter_api_key=os.environ.get("OPENROUTER_API_KEY", "").strip(),
        ncbi_api_key=os.environ.get("NCBI_API_KEY", "").strip(),
        unpaywall_email=os.environ.get("UNPAYWALL_EMAIL", "").strip(),
    )


def main(argv=None):
    ap = argparse.ArgumentParser(description="annotaid — AI-assisted biocuration")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--config", default=os.path.join(HERE, "config", "schema.json"),
                    help="starter template a NEW project is seeded from "
                         "(each project owns its own config once created)")
    ap.add_argument("--keys", default=os.path.join(os.path.dirname(HERE), ".keys"),
                    help="dotenv-style env file (default: repo-root .keys)")
    ap.add_argument("--data-dir", default=os.path.join(HERE, "data"))
    ap.add_argument("--static-dir", default=os.path.join(HERE, "static"))
    ap.add_argument("--no-secrets", action="store_true",
                    help="ignore OPENROUTER_API_KEY (extraction disabled)")
    ap.add_argument("--no-background", action="store_true",
                    help="don't start the job runner / nightly backup threads (tests)")
    args = ap.parse_args(argv)

    # The template is only needed to CREATE a project, so a broken one warns
    # rather than killing a server that could still serve existing projects.
    template = None
    try:
        template = load_config(args.config)
    except ConfigError as exc:
        print(f"warning: starter template unusable ({exc})")
        print("         existing projects still work; creating one needs a config.")

    # Always read the env file: it carries the bootstrap admin and settings
    # too, not just the API key.
    util.load_env_file(args.keys)
    secrets = _secrets_from_env()
    if args.no_secrets:
        secrets = Secrets(openrouter_api_key="", ncbi_api_key=secrets.ncbi_api_key,
                          unpaywall_email=secrets.unpaywall_email)
    if not secrets.openrouter_api_key:
        print("note: no OPENROUTER_API_KEY — AI extraction is disabled.")

    cookie_mode = (os.environ.get("ANNOTAID_COOKIE_SECURE") or "auto").strip().lower()
    if cookie_mode not in ("auto", "always", "never"):
        raise SystemExit("ANNOTAID_COOKIE_SECURE must be auto, always or never")
    origins = frozenset(
        o.strip().rstrip("/") for o in (os.environ.get("ANNOTAID_ALLOWED_ORIGINS") or "").split(",")
        if o.strip()
    )

    db_path = os.path.join(args.data_dir, "annotaid.db")
    blob_dir = os.path.join(args.data_dir, "pdfs")
    db = Db(db_path)
    auth.bootstrap_superadmin(db, os.environ)

    registry = projects_mod.ProjectRegistry(db, blob_dir)
    ctx = app_mod.Context(
        secrets=secrets,
        db=db,
        projects=registry,
        static_dir=args.static_dir,
        template=template,
        allowed_origins=origins,
        cookie_secure=cookie_mode,
        data_dir=args.data_dir,
        version=VERSION,
    )
    ctx.jobs = JobRunner(db, registry, secrets, errors=ctx.errors)
    ctx.backups = Backups(db_path, os.path.join(args.data_dir, "backups"), errors=ctx.errors)
    if not args.no_background:
        ctx.jobs.start()
        ctx.backups.start()

    server = app_mod.build_server(ctx, args.host, args.port)

    def _shutdown(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _shutdown)  # systemd stop -> clean shutdown

    n_proj = len(projects_mod.list_projects(db))
    print(f"annotaid {VERSION} serving http://{args.host}:{args.port}  "
          f"({n_proj} project(s), db={db_path})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        ctx.jobs.stop()
        ctx.backups.stop()
        server.server_close()


if __name__ == "__main__":
    main()
