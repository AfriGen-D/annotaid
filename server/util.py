"""Small stdlib-only helpers shared across the backend.

Several of these are ported from scripts/pipeline.py (dotenv reader, slugify,
MODEL=LABEL parsing) so the OpenRouter behaviour matches the existing CLI.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import tempfile
from datetime import datetime, timezone


# --------------------------------------------------------------------------- #
# Secrets / env
# --------------------------------------------------------------------------- #
def load_env_file(path: str) -> None:
    """Load simple KEY=value lines into os.environ (does not overwrite existing).

    Ported verbatim from scripts/pipeline.py. Blank lines and `#` comments are
    skipped; surrounding quotes are stripped from values.
    """
    if not path or not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip('"').strip("'")
            os.environ.setdefault(key, val)


# --------------------------------------------------------------------------- #
# Names / ids
# --------------------------------------------------------------------------- #
def slugify(name: str) -> str:
    """'anthropic/claude-sonnet-5' -> 'anthropic_claude_sonnet_5'. (ported)"""
    return re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")


def parse_model_arg(spec: str) -> tuple[str, str]:
    """'slug' or 'slug=label' -> (slug, label). Label defaults to slugify(slug).

    Honours notes.md: if no label is provided, fall back to a derived label.
    """
    slug, sep, label = spec.partition("=")
    slug = slug.strip()
    label = label.strip() if sep and label.strip() else slugify(slug)
    return slug, label


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def prompt_id(text: str) -> str:
    """Stable id identifying the exact prompt used (brief §6 promptId)."""
    return "sha256:" + sha256_hex(text.encode("utf-8"))[:16]


def short_id(nbytes: int = 4) -> str:
    """A short opaque id (8 hex chars by default).

    Used for group row ids. Rows need an identity that survives insert, delete
    and reorder — an array index does not, and matching rows by index is how you
    silently reassign one curator's edits to a different variant.
    """
    return secrets.token_hex(nbytes)


_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_filename(name: str) -> str:
    """Make a string safe to use as a single path segment (no separators)."""
    name = name.replace("/", "_").replace("\\", "_")
    return _UNSAFE.sub("_", name).strip("_") or "unnamed"


# --------------------------------------------------------------------------- #
# Time
# --------------------------------------------------------------------------- #
def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Atomic writes (crash-safe autosave)
# --------------------------------------------------------------------------- #
def atomic_write_bytes(path: str, data: bytes) -> None:
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def atomic_write_json(path: str, obj) -> None:
    atomic_write_bytes(
        path, json.dumps(obj, indent=2, ensure_ascii=False).encode("utf-8")
    )


def read_json(path: str, default=None):
    if not os.path.isfile(path):
        return default
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)
