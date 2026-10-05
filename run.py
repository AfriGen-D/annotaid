#!/usr/bin/env python3
"""annotaid entrypoint.

    python run.py                 # serve on http://127.0.0.1:8765
    python run.py --port 9000
    python run.py --keys ../.keys # dotenv-style key file with OPENROUTER_API_KEY

Binds to localhost only so the API key is never exposed on the network.
"""
from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# allow `python run.py` from inside annotaid/ by making the package importable
sys.path.insert(0, os.path.dirname(HERE))

from annotaid.server import app as app_mod  # noqa: E402
from annotaid.server import projects as projects_mod  # noqa: E402
from annotaid.server.config_loader import (  # noqa: E402
    ConfigError,
    load_config,
    load_secrets,
)
from annotaid.server.db import Db  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description="annotaid — local biocuration gold-standard tool")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--config", default=os.path.join(HERE, "config", "schema.json"),
                    help="starter template a NEW project is seeded from "
                         "(each project owns its own config once created)")
    ap.add_argument("--keys", default=os.path.join(os.path.dirname(HERE), ".keys"),
                    help="dotenv-style key file (default: repo-root .keys)")
    ap.add_argument("--data-dir", default=os.path.join(HERE, "data"))
    ap.add_argument("--static-dir", default=os.path.join(HERE, "static"))
    ap.add_argument("--no-secrets", action="store_true",
                    help="start without requiring OPENROUTER_API_KEY (extraction disabled)")
    args = ap.parse_args(argv)

    # The template is only needed to CREATE a project, so a broken one warns
    # rather than killing a server that could still serve existing projects.
    template = None
    try:
        template = load_config(args.config)
    except ConfigError as exc:
        print(f"warning: starter template unusable ({exc})")
        print("         existing projects still work; creating one needs a config.")

    if args.no_secrets:
        from annotaid.server.config_loader import Secrets
        secrets = Secrets(openrouter_api_key="")
    else:
        try:
            secrets = load_secrets(args.keys)
        except ConfigError as exc:
            raise SystemExit(f"Secrets error: {exc}")

    db_path = os.path.join(args.data_dir, "annotaid.db")
    blob_dir = os.path.join(args.data_dir, "pdfs")
    db = Db(db_path)
    ctx = app_mod.Context(
        secrets=secrets,
        db=db,
        projects=projects_mod.ProjectRegistry(db, blob_dir),
        static_dir=args.static_dir,
        template=template,
    )
    server = app_mod.build_server(ctx, args.host, args.port)

    n_proj = len(projects_mod.list_projects(db))
    print(f"annotaid serving http://{args.host}:{args.port}  "
          f"({n_proj} project(s), db={db_path})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
