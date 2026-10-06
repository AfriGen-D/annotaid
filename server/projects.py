"""Projects: CRUD, home-screen stats, and the registry the handlers resolve through.

A project owns its feature set and its papers. Its configuration is stored as one
validated JSON document rather than normalised into tables, because a config is
always read whole, is never queried across projects, and — most importantly — is
the artifact that gets handed from a project manager to a curator. Normalising it
would buy nothing and cost the meta-schema validation we already have.

The portable document, assembled on read and split on write so each field has
exactly one source of truth:

    { "annotaidProject": 1, "name": ..., "description": ...,
      "createdBy": { "name": ..., "email": ... },
      "settings": { "allowPapersWithoutPmid": false },
      "features": [...],            # leaves, and groups holding their own leaves
      "models": [...], "prompts": [{label, text}],
      "parseEngines": [...], "defaults": {...}, "export": {...} }

`name` and `description` are not decoration: they compose into the extraction
prompt (see schema_builder.build_prompt), alongside the feature descriptions.
"""
from __future__ import annotations

import json
import secrets as _secrets
import threading

from . import util
from .config_loader import Config, ConfigError, parse_config
from .db import Db
from .project_store import ProjectStore

DOC_VERSION = 1
MAX_NAME = 120
MAX_DESCRIPTION = 4000

# Fields of the portable document that belong to the config half. Anything not
# listed here is DROPPED on create/update — so a new key must be added here or it
# silently never persists.
CONFIG_KEYS = ("features", "models", "prompts", "parseEngines", "defaults",
               "export", "settings")
SHAREABLE_CONFIG_KEYS = ("features", "prompts", "export", "settings")


class ProjectError(Exception):
    pass


def new_id() -> str:
    return _secrets.token_hex(6)  # 12 hex chars, same shape as a paper uid


# --------------------------------------------------------------------------- #
# Read
# --------------------------------------------------------------------------- #
def get_project_row(db: Db, pid: str):
    return db.query_one(
        "SELECT * FROM projects WHERE id=? AND archived_at IS NULL", (pid,)
    )


def _leaf_count(features_raw) -> int:
    """Total curatable values a config declares: paper-level features PLUS every
    field nested inside a group. The home screen shows one number for "what does
    this project curate", so a group's fields count the same as a paper-level
    one rather than being folded into a separate "N groups" figure.

    Walks the raw JSON rather than going through features.parse_features: this
    runs once per project on every /api/projects list, and a full parse (with
    its jsonschema pass) is unnecessary work for a count.
    """
    total = 0
    for f in features_raw or []:
        if f.get("type") == "group":
            total += len(f.get("features") or [])
        else:
            total += 1
    return total


def list_projects(db: Db, only_ids=None, include_archived: bool = False) -> list:
    """Home-screen payload. One query — cheap because run_features is relational
    rather than buried inside a JSON blob per run.

    only_ids: restrict to these project ids (the caller's memberships); None
    means every project (superadmin). include_archived: the admin view.
    """
    if only_ids is not None and not only_ids:
        return []
    where = "WHERE 1=1" if include_archived else "WHERE p.archived_at IS NULL"
    params = ()
    if only_ids is not None:
        ids = sorted(only_ids)
        where += " AND p.id IN (" + ",".join("?" * len(ids)) + ")"
        params = tuple(ids)
    rows = db.query(
        """
        SELECT p.id, p.name, p.description, p.created_at, p.updated_at, p.config_json,
          p.archived_at, p.created_by_name,
          (SELECT COUNT(*) FROM project_members WHERE project_id=p.id)              AS members,
          (SELECT COUNT(*) FROM papers WHERE project_id=p.id
             AND curation_status='submitted')                                      AS finished,
          (SELECT COUNT(*) FROM papers WHERE project_id=p.id)                       AS papers,
          (SELECT COUNT(*) FROM papers WHERE project_id=p.id
             AND pmid_status='pending')                                             AS pending,
          (SELECT COUNT(*) FROM runs WHERE project_id=p.id)                         AS runs,
          (SELECT COUNT(*) FROM run_features WHERE project_id=p.id
             AND confirmed=1)                                                       AS confirmed,
          (SELECT COUNT(*) FROM run_features WHERE project_id=p.id)                 AS features_total
        FROM projects p
        """ + where + """
        ORDER BY p.updated_at DESC
        """,
        params,
    )
    out = []
    for r in rows:
        # Feature/model counts come from the stored document, so a project with no
        # papers yet still advertises what it is set up to curate.
        try:
            doc = json.loads(r["config_json"])
        except json.JSONDecodeError:
            doc = {}
        out.append(
            {
                "id": r["id"],
                "name": r["name"],
                "description": r["description"],
                "createdAt": r["created_at"],
                "updatedAt": r["updated_at"],
                "archivedAt": r["archived_at"],
                "createdByName": r["created_by_name"],
                # One number, combining paper-level features and every field
                # nested under a group — the group boundary is an authoring/
                # curation detail, not something a manager scanning project
                # cards needs to mentally add up.
                "featureCount": _leaf_count(doc.get("features")),
                "groupCount": sum(
                    1 for f in (doc.get("features") or [])
                    if f.get("type") == "group"
                ),
                "modelCount": len(doc.get("models") or []),
                "stats": {
                    "papers": r["papers"],
                    "pending": r["pending"],
                    "runs": r["runs"],
                    "confirmed": r["confirmed"],
                    "featuresTotal": r["features_total"],
                    "finished": r["finished"],
                    "members": r["members"],
                },
            }
        )
    return out


def portable_document(db: Db, pid: str):
    """The full shareable document for one project."""
    row = get_project_row(db, pid)
    if row is None:
        return None
    try:
        doc = json.loads(row["config_json"])
    except json.JSONDecodeError as exc:
        raise ProjectError(f"project {pid} has an unreadable config: {exc}")
    return {
        "annotaidProject": DOC_VERSION,
        "name": row["name"],
        "description": row["description"],
        # Provenance (server/identity.py), not curation config — None for a
        # project created before the identity modal existed, or if no identity
        # was set at creation time.
        "createdBy": (
            {"name": row["created_by_name"], "email": row["created_by_email"]}
            if row["created_by_name"] else None
        ),
        **{k: doc[k] for k in SHAREABLE_CONFIG_KEYS if k in doc},
    }


# --------------------------------------------------------------------------- #
# Write
# --------------------------------------------------------------------------- #
def _clean_name(name) -> str:
    name = str(name or "").strip()
    if not name:
        raise ProjectError("a project needs a name")
    if len(name) > MAX_NAME:
        raise ProjectError(f"name is too long (max {MAX_NAME} characters)")
    return name


def _clean_description(description) -> str:
    text = str(description or "").strip()
    if len(text) > MAX_DESCRIPTION:
        raise ProjectError(f"description is too long (max {MAX_DESCRIPTION} characters)")
    return text


def create_project(db: Db, name, description, config_doc: dict, created_by: dict | None = None) -> dict:
    """Create a project. The config is validated BEFORE insert, so the database
    can never hold a config that would fail to load.

    created_by: {"id", "name", "email"} of the logged-in user, stamped by the
    caller (h_project_create). The creator is also made the project's first
    manager (M11), in the same transaction.
    """
    name = _clean_name(name)
    description = _clean_description(description)

    config_part = {k: v for k, v in (config_doc or {}).items() if k in CONFIG_KEYS}
    try:
        # Round-tripping through parse_config normalises model labels and prompt
        # ids exactly as a file-based config would be normalised.
        config = parse_config(config_part, label=f"project {name!r}")
    except ConfigError as exc:
        raise ProjectError(str(exc))

    pid = new_id()
    now = util.iso_now()
    created_by_id = (created_by or {}).get("id") or None
    created_by_name = (created_by or {}).get("name") or None
    created_by_email = (created_by or {}).get("email") or None
    try:
        with db.write() as c:
            c.execute(
                "INSERT INTO projects (id, name, description, config_json, "
                "created_at, updated_at, created_by, created_by_name, created_by_email) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (pid, name, description,
                 json.dumps(config.portable_dict(), ensure_ascii=False), now, now,
                 created_by_id, created_by_name, created_by_email),
            )
            if created_by_id:
                c.execute(
                    "INSERT INTO project_members (project_id, user_id, role, added_at, added_by) "
                    "VALUES (?,?,?,?,?)", (pid, created_by_id, "manager", now, created_by_id),
                )
    except Exception as exc:  # sqlite3.IntegrityError on the unique name index
        if "projects_name" in str(exc) or "UNIQUE" in str(exc).upper():
            raise ProjectError(f"a project named {name!r} already exists")
        raise
    return {"id": pid, "name": name, "description": description,
            "createdAt": now, "updatedAt": now}


def update_project(db: Db, pid: str, *, name=None, description=None,
                   config_doc=None) -> dict:
    row = get_project_row(db, pid)
    if row is None:
        raise ProjectError(f"no project {pid}")

    new_name = row["name"] if name is None else _clean_name(name)
    new_desc = row["description"] if description is None else _clean_description(description)
    config_json = row["config_json"]

    if config_doc is not None:
        config_part = {k: v for k, v in config_doc.items() if k in CONFIG_KEYS}
        try:
            config = parse_config(config_part, label=f"project {new_name!r}")
        except ConfigError as exc:
            raise ProjectError(str(exc))
        config_json = json.dumps(config.portable_dict(), ensure_ascii=False)

    now = util.iso_now()
    try:
        with db.write() as c:
            c.execute(
                "UPDATE projects SET name=?, description=?, config_json=?, "
                "updated_at=? WHERE id=?",
                (new_name, new_desc, config_json, now, pid),
            )
    except Exception as exc:
        if "projects_name" in str(exc) or "UNIQUE" in str(exc).upper():
            raise ProjectError(f"a project named {new_name!r} already exists")
        raise
    return {"id": pid, "name": new_name, "description": new_desc, "updatedAt": now}


def unarchive_project(db: Db, pid: str) -> None:
    row = db.query_one("SELECT id FROM projects WHERE id=? AND archived_at IS NOT NULL", (pid,))
    if row is None:
        raise ProjectError(f"no archived project {pid}")
    now = util.iso_now()
    with db.write() as c:
        c.execute("UPDATE projects SET archived_at=NULL, updated_at=? WHERE id=?", (now, pid))


def archive_project(db: Db, pid: str) -> None:
    """Hide a project from the home screen. Deliberately NOT a delete: its papers,
    runs and curation work stay in the database and its PDFs stay in the shared
    pool (other projects may reference the same content-addressed files)."""
    row = get_project_row(db, pid)
    if row is None:
        raise ProjectError(f"no project {pid}")
    now = util.iso_now()
    with db.write() as c:
        c.execute("UPDATE projects SET archived_at=?, updated_at=? WHERE id=?",
                  (now, now, pid))


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
def prompt_context(name: str, description: str) -> str:
    """The project's contribution to the extraction prompt.

    A module function, not just a method, because the feature editor previews
    this for a project that does not exist yet — and a preview that disagreed
    with the real run would be worse than no preview.
    """
    lines = [f"Curation project: {name}"]
    if description:
        lines.append(description)
    return "\n".join(lines)


class Project:
    """One resolved project: metadata + parsed Config + its own store."""

    __slots__ = ("id", "name", "description", "config", "store", "updated_at")

    def __init__(self, pid, name, description, config, store, updated_at):
        self.id = pid
        self.name = name
        self.description = description
        self.config = config
        self.store = store
        self.updated_at = updated_at

    def prompt_context(self) -> str:
        """The name and description are semantic input to the model, not just UI
        chrome — they tell it what this curation effort is actually about."""
        return prompt_context(self.name, self.description)

    def meta(self) -> dict:
        return {"id": self.id, "name": self.name, "description": self.description}


class ProjectRegistry:
    """Resolves a project id to a Project, caching the parsed Config.

    The cache is keyed on the row's updated_at, so editing a project's config
    invalidates it without any explicit invalidation call — and we do not re-run
    jsonschema on every single request.
    """

    def __init__(self, db: Db, blob_dir: str):
        self.db = db
        self.blob_dir = blob_dir
        self._cache = {}  # pid -> (updated_at, Config)
        self._lock = threading.Lock()

    def get(self, pid: str):
        row = get_project_row(self.db, pid)
        if row is None:
            return None
        config = self._config_for(row)
        if config is None:
            return None
        return Project(
            row["id"], row["name"], row["description"], config,
            ProjectStore(self.db, self.blob_dir, row["id"]), row["updated_at"],
        )

    def _config_for(self, row):
        pid, stamp = row["id"], row["updated_at"]
        with self._lock:
            cached = self._cache.get(pid)
            if cached and cached[0] == stamp:
                return cached[1]
        try:
            config = parse_config(
                json.loads(row["config_json"]), label=f"project {row['name']!r}"
            )
        except (ConfigError, json.JSONDecodeError):
            # create_project/update_project validate before writing, so this means
            # the row was tampered with. Treat it as unresolvable rather than
            # crashing every request for every project.
            return None
        with self._lock:
            self._cache[pid] = (stamp, config)
        return config
