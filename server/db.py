"""SQLite connection management + schema (replaces the JSON file store).

Why SQLite: a project owns its own papers, runs and per-feature curation state, and
the home screen wants counts across every project. As one-file-per-record JSON that
is a directory walk per project per page load; as tables it is one query.

Layout under data_dir:
    annotaid.db          everything except PDF bytes
    pdfs/<uid>.pdf       shared, content-addressed blob pool

PDF bytes stay on the filesystem on purpose: openrouter.build_messages reads a
path, and h_pdf streams the file to the browser. As BLOBs both paths would have to
round-trip multi-MB buffers through memory for no gain. Because a uid IS
sha256(bytes)[:12], the same PDF added to two projects is one file.

Threading: ThreadingHTTPServer spawns a thread per request, so connections are
thread-local (a sqlite3 connection may not be shared across threads). That means a
fresh connection per request, which is fine — sqlite3.connect is microseconds and
journal_mode=WAL is persisted in the file header, so only the per-connection
pragmas are re-applied. Writes additionally take a process-wide lock, because
read-modify-write (merge_run_edits) must be serialised and run_extraction writes
runs from a ThreadPoolExecutor.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager

SCHEMA_VERSION = 5

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS projects (
  id              TEXT PRIMARY KEY,
  name            TEXT NOT NULL,
  description     TEXT NOT NULL DEFAULT '',
  config_json     TEXT NOT NULL,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL,
  archived_at     TEXT,
  -- Provenance, not curation config: who was signed in (server/identity.py)
  -- when the project was created. NULL for a project created before this
  -- column existed, or if no identity had been set yet. Stamped once at
  -- create_project() time and never updated afterwards.
  created_by_name  TEXT,
  created_by_email TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS projects_name ON projects(name);

-- The local curator's own identity: name, email, and an optional personal
-- OpenRouter key (server/identity.py). One row ('singleton') — this app has
-- no login, so "the current user" is whoever is at this browser, asked once
-- on first run (brief: the home-screen identity modal).
CREATE TABLE IF NOT EXISTS app_identity (
  id             TEXT PRIMARY KEY,
  name           TEXT NOT NULL,
  email          TEXT NOT NULL,
  openrouter_key TEXT,
  updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS papers (
  project_id     TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  uid            TEXT NOT NULL,
  pmid           TEXT,
  doi            TEXT,
  pmid_status    TEXT NOT NULL DEFAULT 'pending',
  suggested_pmid TEXT NOT NULL DEFAULT '',
  filename       TEXT NOT NULL,
  pmid_source    TEXT,
  added_at       TEXT NOT NULL,
  -- has_pdf=0 papers exist only when a project has opted into
  -- allowExtractionOnAbstract and "fetch by PMID" found an abstract but no
  -- open-access full text. uid is then sha256("abstract:"+pmid)[:12] rather
  -- than sha256(pdf bytes) — there are no bytes to hash — so it stays stable
  -- and content-addressed within its own namespace.
  has_pdf        INTEGER NOT NULL DEFAULT 1,
  abstract_text  TEXT,
  -- Curator-declared identity for a project's one repeating group (e.g. which
  -- variants/haplotypes this paper reports on), set BEFORE extraction runs and
  -- never touched by the AI. JSON array of {"rowId","label"}, ordered; a label
  -- may repeat (the same variant reported for two sub-populations is two
  -- entries). Paper-scoped rather than run-scoped because it does not depend
  -- on which model ran — every model's run addresses the SAME rows by rowId.
  -- See server/extraction.py and server/project_store.load_group_items.
  group_items_json TEXT NOT NULL DEFAULT '[]',
  PRIMARY KEY (project_id, uid)
);
-- Replaces index.json's pmidToUid map AND the pmid_taken_by check, enforced by the
-- database. Partial, so any number of identity-pending papers (pmid IS NULL)
-- coexist — which is exactly what papers with no PubMed ID need.
CREATE UNIQUE INDEX IF NOT EXISTS papers_pmid
  ON papers(project_id, pmid) WHERE pmid IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS papers_doi
  ON papers(project_id, doi) WHERE doi IS NOT NULL;

-- Keyed on uid, not pmid: a paper may legitimately have no PubMed ID, and uid
-- exists from the moment the PDF lands and never changes. `pmid` is deliberately
-- NOT a column here — it is joined in from papers when a run is read, so
-- confirming a PMID after extraction cannot leave stale run records behind.
CREATE TABLE IF NOT EXISTS runs (
  project_id    TEXT NOT NULL,
  uid           TEXT NOT NULL,
  model_id      TEXT NOT NULL,
  model_label   TEXT NOT NULL DEFAULT '',
  prompt_id     TEXT,
  prompt_hash   TEXT,
  prompt_text   TEXT,
  source        TEXT,
  parse_engine  TEXT,
  requested_at  TEXT,
  completed_at  TEXT,
  status        TEXT NOT NULL DEFAULT 'failed',
  error         TEXT,
  imported_from TEXT,
  PRIMARY KEY (project_id, uid, model_id),
  FOREIGN KEY (project_id, uid)
    REFERENCES papers(project_id, uid) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS runs_project ON runs(project_id);

-- One entry of a repeating group (e.g. one genetic variant) within a run.
--
-- row_id is generated once and never reused, because a curator inserts, deletes
-- and reorders rows: matching them by array index is how you silently reassign
-- one row's edits to a different variant.
--
-- origin + deleted_at are what turn ordinary curation into a row-level
-- benchmark. An AI row the curator deletes is a FALSE POSITIVE and a curator row
-- the AI never proposed is a MISS; erase either and you have destroyed the
-- count. So AI rows are soft-deleted. Curator-added rows carry no such signal
-- (they were never a model claim), so those are removed outright rather than
-- accumulating as noise.
CREATE TABLE IF NOT EXISTS run_rows (
  project_id TEXT NOT NULL,
  uid        TEXT NOT NULL,
  model_id   TEXT NOT NULL,
  row_id     TEXT NOT NULL,
  group_name TEXT NOT NULL,
  position   INTEGER NOT NULL DEFAULT 0,
  origin     TEXT NOT NULL DEFAULT 'ai',    -- 'ai' | 'curator'
  deleted_at TEXT,
  PRIMARY KEY (project_id, uid, model_id, row_id),
  FOREIGN KEY (project_id, uid, model_id)
    REFERENCES runs(project_id, uid, model_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS run_rows_group
  ON run_rows(project_id, uid, model_id, group_name, position);

-- Per-group state that is not a row: whether the paper reports any entries at
-- all, and whether the curator has VERIFIED that. "I checked and there are none"
-- is real curation work and a different fact from "nobody has looked".
CREATE TABLE IF NOT EXISTS run_groups (
  project_id   TEXT NOT NULL,
  uid          TEXT NOT NULL,
  model_id     TEXT NOT NULL,
  group_name   TEXT NOT NULL,
  present      INTEGER NOT NULL DEFAULT 0,
  ai_present   INTEGER NOT NULL DEFAULT 0,  -- immutable, like ai_value on a cell
  confirmed    INTEGER NOT NULL DEFAULT 0,
  confirmed_at TEXT,
  PRIMARY KEY (project_id, uid, model_id, group_name),
  FOREIGN KEY (project_id, uid, model_id)
    REFERENCES runs(project_id, uid, model_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS run_features (
  project_id     TEXT NOT NULL,
  uid            TEXT NOT NULL,
  model_id       TEXT NOT NULL,
  -- '' means the paper itself; otherwise a run_rows.row_id
  row_id         TEXT NOT NULL DEFAULT '',
  name           TEXT NOT NULL,
  type           TEXT NOT NULL,
  -- JSON-encoded: a feature value may be string | number | bool | null | array,
  -- so no single typed column fits. json round-trips floats exactly, which
  -- matters because ai_value beside value IS the audit signal.
  ai_value_json  TEXT NOT NULL,
  value_json     TEXT NOT NULL,
  present        INTEGER NOT NULL DEFAULT 0,
  evidence       TEXT,
  evidence_match TEXT NOT NULL DEFAULT 'none',
  confirmed      INTEGER NOT NULL DEFAULT 0,
  edited_at      TEXT,
  confirmed_at   TEXT,
  PRIMARY KEY (project_id, uid, model_id, row_id, name),
  FOREIGN KEY (project_id, uid, model_id)
    REFERENCES runs(project_id, uid, model_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS run_features_project ON run_features(project_id);
CREATE INDEX IF NOT EXISTS run_features_row
  ON run_features(project_id, uid, model_id, row_id);

CREATE TABLE IF NOT EXISTS ui_state (
  project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
  state_json TEXT NOT NULL DEFAULT '{}'
);
"""


class DbError(RuntimeError):
    pass


class Db:
    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.RLock()
        self._init_schema()

    # ---- connections ---------------------------------------------------- #
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            # isolation_level=None -> autocommit; transactions are explicit below.
            c = sqlite3.connect(self.path, timeout=15, isolation_level=None)
            c.row_factory = sqlite3.Row
            # WAL is persisted in the file header (set once), but foreign_keys and
            # busy_timeout are PER CONNECTION and easy to forget — the ON DELETE
            # CASCADEs above are silently inert without foreign_keys=ON.
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA busy_timeout=15000")
            self._local.conn = c
        return c

    @contextmanager
    def write(self):
        """Serialised, explicit transaction. Rolls back on any exception."""
        with self._write_lock:
            c = self.conn()
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
            except BaseException:
                c.execute("ROLLBACK")
                raise
            else:
                c.execute("COMMIT")

    # ---- reads ---------------------------------------------------------- #
    def query(self, sql: str, params=()) -> list:
        return self.conn().execute(sql, params).fetchall()

    def query_one(self, sql: str, params=()):
        return self.conn().execute(sql, params).fetchone()

    def scalar(self, sql: str, params=()):
        row = self.query_one(sql, params)
        return row[0] if row is not None else None

    # ---- schema --------------------------------------------------------- #
    def _init_schema(self) -> None:
        with self._write_lock:
            c = self.conn()

            # The version check comes FIRST. Running the DDL against an older
            # schema fails inside SQLite with something like "no such column:
            # row_id" — technically true, and useless to whoever has to act on
            # it. Ask the version before touching anything.
            found = None
            if c.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_meta'"
            ).fetchone():
                row = c.execute(
                    "SELECT value FROM schema_meta WHERE key='version'"
                ).fetchone()
                found = row[0] if row else None
            if found is not None and int(found) != SCHEMA_VERSION:
                raise DbError(
                    f"database {self.path} is schema version {found}, this "
                    f"build expects {SCHEMA_VERSION}. There is no migrator: "
                    f"delete the file (and its -wal/-shm siblings) to start "
                    f"fresh, or point --data-dir somewhere else."
                )

            # executescript() implicitly COMMITs any pending transaction, so it
            # must NOT run inside self.write(). The DDL is all IF NOT EXISTS,
            # hence idempotent and safe to run unwrapped on every startup.
            c.executescript(SCHEMA_SQL)

            if found is None:
                with self.write() as w:      # RLock is reentrant
                    w.execute(
                        "INSERT OR REPLACE INTO schema_meta(key, value) "
                        "VALUES('version', ?)", (str(SCHEMA_VERSION),),
                    )
