"""Per-project durable store, backed by SQLite (server/db.py).

Replaces the old JSON-file Store. The method surface is deliberately kept close to
it so papers.py / extraction.py / importer.py / export.py keep working: the dict
shapes returned here are the SAME shapes that used to be JSON files, and the same
shapes that cross /api to the browser.

The one real signature change is that run methods key on `uid`, not `pmid` — a
paper may legitimately have no PubMed ID, while `uid` (sha256 of the PDF bytes)
exists the moment the file lands and never changes. See db.py for why `pmid` is not
a column on `runs`.
"""
from __future__ import annotations

import json
import os

from . import util
from .db import Db

# A paper's identity state. 'pending' is the only one that blocks curation —
# the curator must resolve identity first, but that resolution no longer has to
# be a PMID.
STATUS_PENDING = "pending"
STATUS_CONFIRMED = "confirmed"
STATUS_NONE = "none"
IDENTITY_STATUSES = (STATUS_PENDING, STATUS_CONFIRMED, STATUS_NONE)

PMID_SOURCES = ("prefill-confirmed", "manual", "pmid-fetch")


class ProjectStore:
    def __init__(self, db: Db, blob_dir: str, project_id: str):
        self.db = db
        self.project_id = project_id
        self.pdfs = os.path.abspath(blob_dir)
        os.makedirs(self.pdfs, exist_ok=True)

    # ---- PDFs (shared, content-addressed pool) -------------------------- #
    def pdf_path(self, uid: str) -> str:
        return os.path.join(self.pdfs, util.safe_filename(uid) + ".pdf")

    def save_pdf(self, uid: str, data: bytes) -> None:
        # uid IS sha256(data)[:12], so an identical PDF in another project is the
        # same file — writing it again is a harmless no-op overwrite.
        util.atomic_write_bytes(self.pdf_path(uid), data)

    def resolve_pdf(self, ident: str):
        uid = self.uid_for(ident)
        if not uid:
            return None
        p = self.pdf_path(uid)
        return p if os.path.isfile(p) else None

    # ---- identity resolution -------------------------------------------- #
    def uid_for(self, ident: str):
        """Resolve a uid OR pmid (within this project) to a uid."""
        if not ident:
            return None
        row = self.db.query_one(
            "SELECT uid FROM papers WHERE project_id=? AND (uid=? OR pmid=?)",
            (self.project_id, ident, ident),
        )
        return row["uid"] if row else None

    def pmid_for_uid(self, uid: str):
        return self.db.scalar(
            "SELECT pmid FROM papers WHERE project_id=? AND uid=?",
            (self.project_id, uid),
        )

    def pmid_taken_by(self, pmid: str):
        """uid of the paper holding this PMID in THIS project, else None."""
        return self.db.scalar(
            "SELECT uid FROM papers WHERE project_id=? AND pmid=?",
            (self.project_id, pmid),
        )

    def doi_taken_by(self, doi: str):
        return self.db.scalar(
            "SELECT uid FROM papers WHERE project_id=? AND doi=?",
            (self.project_id, doi),
        )

    def set_identity(self, uid: str, *, status: str, pmid=None, doi=None,
                     source=None) -> None:
        """Commit a paper's identity. Replaces the old set_pmid().

        The (project_id, pmid) and (project_id, doi) partial unique indexes are
        what reject a collision — this does not pre-check, it lets the database
        say no (see papers.py for the friendly error).
        """
        if status not in IDENTITY_STATUSES:
            raise ValueError(f"unknown identity status: {status}")
        with self.db.write() as c:
            c.execute(
                "UPDATE papers SET pmid=?, doi=?, pmid_status=?, pmid_source=? "
                "WHERE project_id=? AND uid=?",
                (pmid or None, doi or None, status, source, self.project_id, uid),
            )

    # ---- Papers --------------------------------------------------------- #
    def save_paper(self, paper: dict) -> None:
        """Upsert a whole paper record (same semantics as the old file write)."""
        with self.db.write() as c:
            c.execute(
                """
                INSERT INTO papers (project_id, uid, pmid, doi, pmid_status,
                                    suggested_pmid, filename, pmid_source, added_at,
                                    has_pdf, abstract_text)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(project_id, uid) DO UPDATE SET
                  pmid=excluded.pmid, doi=excluded.doi,
                  pmid_status=excluded.pmid_status,
                  suggested_pmid=excluded.suggested_pmid,
                  filename=excluded.filename,
                  pmid_source=excluded.pmid_source
                """,
                (
                    self.project_id,
                    paper["uid"],
                    paper.get("pmid") or None,
                    paper.get("doi") or None,
                    paper.get("pmidStatus") or STATUS_PENDING,
                    paper.get("suggestedPmid") or "",
                    paper.get("filename") or f"{paper['uid']}.pdf",
                    paper.get("pmidSource"),
                    paper.get("addedAt") or util.iso_now(),
                    # Not in the ON CONFLICT clause: has_pdf/abstract_text are set
                    # once at creation (a paper's content kind never changes) and
                    # every later save_paper() call is an identity update only.
                    1 if paper.get("hasPdf", True) else 0,
                    paper.get("abstractText"),
                ),
            )

    def set_added_by(self, uid: str, user_id: str) -> None:
        """Stamp who brought a paper in. First writer wins: re-adding an
        existing paper must not take the credit from whoever added it."""
        with self.db.write() as c:
            c.execute(
                "UPDATE papers SET added_by=? WHERE project_id=? AND uid=? AND added_by IS NULL",
                (user_id, self.project_id, uid),
            )

    def paper_row(self, uid: str):
        """The raw papers row (workflow checks need assignee/status)."""
        return self.db.query_one(
            "SELECT * FROM papers WHERE project_id=? AND uid=?", (self.project_id, uid)
        )

    def load_paper(self, uid: str):
        row = self.db.query_one(
            "SELECT * FROM papers WHERE project_id=? AND uid=?",
            (self.project_id, uid),
        )
        if row is None:
            return None
        return _paper_dict(row, self._models_for(uid))

    # ---- the one repeating group's reviewed row identity ---------------- #
    def load_group_items(self, uid: str) -> list:
        """[{"rowId","label","evidence"?}, ...] in review order. Every run
        addresses its group rows by these same rowIds."""
        raw = self.db.scalar(
            "SELECT group_items_json FROM papers WHERE project_id=? AND uid=?",
            (self.project_id, uid),
        )
        try:
            return json.loads(raw or "[]")
        except json.JSONDecodeError:
            return []

    def save_group_items(self, uid: str, items: list, *, reviewed=None, discovered=None) -> list:
        """Replace the discovered/reviewed list wholesale. A rowId the caller
        sends that we don't recognise is
        treated as new — never trust the client to invent the id an existing
        row keeps; an unrecognised or missing one gets a fresh server id, so
        an item already extracted against keeps its identity across an edit
        that only reorders or renames its neighbours."""
        existing_ids = {it.get("rowId") for it in self.load_group_items(uid)}
        out = []
        for raw in items or []:
            label = str((raw or {}).get("label") or "").strip()
            if not label:
                continue  # a blank row names nothing — just drop it
            rid = (raw or {}).get("rowId")
            if not rid or rid not in existing_ids:
                rid = util.short_id()
            evidence = str((raw or {}).get("evidence") or "").strip() or None
            item = {"rowId": rid, "label": label}
            if evidence:
                item["evidence"] = evidence
            out.append(item)
        with self.db.write() as c:
            if reviewed is None and discovered is None:
                c.execute(
                    "UPDATE papers SET group_items_json=? WHERE project_id=? AND uid=?",
                    (json.dumps(out, ensure_ascii=False), self.project_id, uid),
                )
            else:
                current = c.execute(
                    "SELECT group_items_reviewed, group_items_discovered FROM papers "
                    "WHERE project_id=? AND uid=?", (self.project_id, uid)
                ).fetchone()
                next_reviewed = bool(reviewed) if reviewed is not None else bool(current["group_items_reviewed"])
                next_discovered = bool(discovered) if discovered is not None else bool(current["group_items_discovered"])
                c.execute(
                    "UPDATE papers SET group_items_json=?, group_items_reviewed=?, group_items_discovered=? "
                    "WHERE project_id=? AND uid=?",
                    (json.dumps(out, ensure_ascii=False), 1 if next_reviewed else 0,
                     1 if next_discovered else 0,
                     self.project_id, uid),
                )
        return out

    def load_paper_by_ident(self, ident: str):
        uid = self.uid_for(ident)
        return self.load_paper(uid) if uid else None

    def list_papers(self) -> list:
        rows = self.db.query(
            "SELECT * FROM papers WHERE project_id=? ORDER BY added_at, uid",
            (self.project_id,),
        )
        by_uid = self._models_by_uid()
        return [_paper_dict(r, by_uid.get(r["uid"], [])) for r in rows]

    def _models_for(self, uid: str) -> list:
        rows = self.db.query(
            "SELECT DISTINCT model_id FROM runs WHERE project_id=? AND uid=? "
            "ORDER BY model_id",
            (self.project_id, uid),
        )
        return [r["model_id"] for r in rows]

    def _models_by_uid(self) -> dict:
        """Derived rather than stored: the old code hand-maintained
        paper["models"] in two places and could drift."""
        out = {}
        for r in self.db.query(
            "SELECT DISTINCT uid, model_id FROM runs WHERE project_id=? "
            "ORDER BY uid, model_id",
            (self.project_id,),
        ):
            out.setdefault(r["uid"], []).append(r["model_id"])
        return out

    # ---- Runs ----------------------------------------------------------- #
    _CELL_SQL = """
        INSERT INTO run_features (project_id, uid, model_id, row_id, name, type,
            ai_value_json, value_json, present, evidence, evidence_match,
            confirmed, edited_at, confirmed_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """

    def _cell_params(self, uid, model_id, row_id, name, f):
        return (
            self.project_id, uid, model_id, row_id, name,
            f.get("type") or "string",
            json.dumps(f.get("aiValue")),
            json.dumps(f.get("value")),
            1 if f.get("present") else 0,
            f.get("evidence"),
            f.get("evidenceMatch") or "none",
            1 if f.get("confirmed") else 0,
            f.get("editedAt"), f.get("confirmedAt"),
        )

    def save_run(self, run: dict) -> None:
        """Write a run and (re)write all of its cells, group rows and group
        state, atomically."""
        uid = run["uid"]
        model_id = run["modelId"]
        feats = run.get("features") or {}
        groups = run.get("groups") or {}
        with self.db.write() as c:
            c.execute(
                """
                INSERT INTO runs (project_id, uid, model_id, model_label, prompt_id,
                                  prompt_hash, prompt_text, source, parse_engine,
                                  requested_at, completed_at, status, error,
                                  imported_from, requested_by)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(project_id, uid, model_id) DO UPDATE SET
                  model_label=excluded.model_label, prompt_id=excluded.prompt_id,
                  prompt_hash=excluded.prompt_hash, prompt_text=excluded.prompt_text,
                  source=excluded.source, parse_engine=excluded.parse_engine,
                  requested_at=excluded.requested_at,
                  completed_at=excluded.completed_at,
                  status=excluded.status, error=excluded.error,
                  imported_from=excluded.imported_from,
                  requested_by=excluded.requested_by
                """,
                (
                    self.project_id, uid, model_id,
                    run.get("modelLabel") or "",
                    run.get("promptId"), run.get("promptHash"), run.get("promptText"),
                    run.get("source"), run.get("parseEngine"),
                    run.get("requestedAt"), run.get("completedAt"),
                    run.get("status") or "failed",
                    run.get("error"), run.get("importedFrom"),
                    run.get("requestedBy"),
                ),
            )
            # Replace every child wholesale: a re-run supersedes its own previous
            # output, and a changed feature list must not leave orphans behind.
            # run_features has no foreign key to run_rows — paper-level cells
            # carry row_id='' and so have no parent row — which is exactly why
            # these three deletes have to stay together.
            for table in ("run_features", "run_rows", "run_groups"):
                c.execute(
                    f"DELETE FROM {table} "
                    "WHERE project_id=? AND uid=? AND model_id=?",
                    (self.project_id, uid, model_id),
                )

            cells = [self._cell_params(uid, model_id, "", n, f) for n, f in feats.items()]
            for gname, gnode in groups.items():
                present = 1 if gnode.get("present") else 0
                c.execute(
                    "INSERT INTO run_groups (project_id, uid, model_id, group_name, "
                    "present, ai_present, confirmed, confirmed_at) VALUES (?,?,?,?,?,?,?,?)",
                    (self.project_id, uid, model_id, gname, present, present, 0, None),
                )
                for i, row in enumerate(gnode.get("rows") or []):
                    rid = row.get("rowId") or util.short_id()
                    c.execute(
                        "INSERT INTO run_rows (project_id, uid, model_id, row_id, "
                        "group_name, position, origin, deleted_at) VALUES (?,?,?,?,?,?,?,?)",
                        (self.project_id, uid, model_id, rid, gname,
                         row.get("position", i), row.get("origin") or "ai",
                         row.get("deletedAt")),
                    )
                    for n, f in (row.get("features") or {}).items():
                        cells.append(self._cell_params(uid, model_id, rid, n, f))
            c.executemany(self._CELL_SQL, cells)

    def load_run(self, uid: str, model_id: str, with_prompt_text: bool = False):
        found = self._list_runs(uid, with_prompt_text, model_id=model_id)
        return found[0] if found else None

    def list_runs(self, uid: str, with_prompt_text: bool = False) -> list:
        return self._list_runs(uid, with_prompt_text)

    def list_all_runs(self, with_prompt_text: bool = False) -> list:
        return self._list_runs(None, with_prompt_text)

    def _list_runs(self, uid, with_prompt_text: bool, model_id=None) -> list:
        """uid=None lists every run in the project. Four queries, never N+1:
        runs, cells, group rows, group state — each grouped in Python."""
        where, child_where, params = "WHERE r.project_id=? ", "WHERE project_id=? ", [self.project_id]
        if uid:
            where += "AND r.uid=? "
            child_where += "AND uid=? "
            params.append(uid)
        if model_id:
            where += "AND r.model_id=? "
            child_where += "AND model_id=? "
            params.append(model_id)
        params = tuple(params)

        rows = self.db.query(
            "SELECT r.*, p.pmid AS pmid FROM runs r "
            "JOIN papers p ON p.project_id=r.project_id AND p.uid=r.uid "
            + where + "ORDER BY r.uid, r.model_id",
            params,
        )
        if not rows:
            return []

        cells = {}
        for r in self.db.query("SELECT * FROM run_features " + child_where, params):
            cells.setdefault((r["uid"], r["model_id"], r["row_id"]), {})[
                r["name"]
            ] = _feature_dict(r)

        row_defs = {}
        for r in self.db.query(
            "SELECT * FROM run_rows " + child_where
            + "ORDER BY group_name, position, row_id", params
        ):
            row_defs.setdefault((r["uid"], r["model_id"]), []).append(r)

        group_state = {}
        for r in self.db.query("SELECT * FROM run_groups " + child_where, params):
            group_state.setdefault((r["uid"], r["model_id"]), {})[r["group_name"]] = r

        out = []
        for r in rows:
            key = (r["uid"], r["model_id"])
            groups = {}

            def node(gname):
                return groups.setdefault(gname, {
                    "present": False, "aiPresent": False,
                    "confirmed": False, "confirmedAt": None, "confirmedBy": None,
                    "rows": [],
                })

            for gr in row_defs.get(key, []):
                node(gr["group_name"])["rows"].append({
                    "rowId": gr["row_id"],
                    "origin": gr["origin"],
                    "position": gr["position"],
                    # Deleted rows ARE sent to the client: a rejected AI row is
                    # audit data, the curator may want to undo, and hiding them
                    # would make the confirmed counts lie.
                    "deletedAt": gr["deleted_at"],
                    "features": cells.get((r["uid"], r["model_id"], gr["row_id"]), {}),
                })
            for gname, st in (group_state.get(key) or {}).items():
                g = node(gname)
                g["present"] = bool(st["present"])
                g["aiPresent"] = bool(st["ai_present"])
                g["confirmed"] = bool(st["confirmed"])
                g["confirmedAt"] = st["confirmed_at"]
                g["confirmedBy"] = st["confirmed_by"]

            out.append(_run_dict(
                r, cells.get((r["uid"], r["model_id"], ""), {}), groups, with_prompt_text
            ))
        return out

    # ---- curator edits -------------------------------------------------- #
    def merge_run_edits(self, uid: str, model_id: str, incoming_features: dict,
                        incoming_groups: dict = None, group_defs: dict = None,
                        user_id: str = None) -> dict:
        """Apply curator edits, PRESERVING aiValue + type from the database.

        Stamps editedAt when a value changes and confirmedAt on confirm
        transitions. aiValue immutability is the whole audit signal — the pair
        (what the AI said, what the curator settled on) is the product.

        `group_defs` comes from features.group_cell_templates(): the store needs
        a declared type and an empty value to create a CURATOR-added row, but it
        must not import the config to get them.

        `user_id` is stamped as edited_by / confirmed_by alongside the
        timestamps (M8): the last person to change or confirm each value.
        """
        with self.db.write() as c:
            exists = c.execute(
                "SELECT 1 FROM runs WHERE project_id=? AND uid=? AND model_id=?",
                (self.project_id, uid, model_id),
            ).fetchone()
            if exists is None:
                raise KeyError(f"no run for {uid}/{model_id}")

            now = util.iso_now()
            self._merge_cells(c, uid, model_id, "", incoming_features or {}, now, user_id)
            for gname, gnode in (incoming_groups or {}).items():
                self._merge_group(
                    c, uid, model_id, gname, gnode or {},
                    (group_defs or {}).get(gname) or {}, now, user_id,
                )
        return self.load_run(uid, model_id)

    def _merge_cells(self, c, uid, model_id, row_id, incoming, now, user_id=None) -> None:
        """Merge edits into cells that already exist. Never invents a cell: the
        AI (or the config) decides which fields exist, not the payload."""
        rows = c.execute(
            "SELECT * FROM run_features "
            "WHERE project_id=? AND uid=? AND model_id=? AND row_id=?",
            (self.project_id, uid, model_id, row_id),
        ).fetchall()
        for row in rows:
            inc = (incoming or {}).get(row["name"])
            if inc is None:
                continue

            cur_value = json.loads(row["value_json"])
            new_value = inc.get("value", cur_value)
            value_changed = json.dumps(new_value, sort_keys=True) != json.dumps(
                cur_value, sort_keys=True
            )

            was_confirmed = bool(row["confirmed"])
            now_confirmed = bool(inc.get("confirmed", was_confirmed))

            edited_at = now if value_changed else row["edited_at"]
            edited_by = user_id if value_changed else row["edited_by"]
            if now_confirmed and not was_confirmed:
                confirmed_at, confirmed_by = now, user_id
            elif not now_confirmed:
                confirmed_at, confirmed_by = None, None
            else:
                confirmed_at, confirmed_by = row["confirmed_at"], row["confirmed_by"]

            c.execute(
                """
                UPDATE run_features SET value_json=?, present=?, evidence=?,
                  evidence_match=?, confirmed=?, edited_at=?, confirmed_at=?,
                  edited_by=?, confirmed_by=?
                WHERE project_id=? AND uid=? AND model_id=? AND row_id=? AND name=?
                """,
                (
                    json.dumps(new_value),
                    1 if inc.get("present", bool(row["present"])) else 0,
                    inc.get("evidence", row["evidence"]),
                    inc.get("evidenceMatch", row["evidence_match"]) or "none",
                    1 if now_confirmed else 0,
                    edited_at, confirmed_at, edited_by, confirmed_by,
                    self.project_id, uid, model_id, row_id, row["name"],
                ),
            )

    def _merge_group(self, c, uid, model_id, gname, gnode, templates, now, user_id=None) -> None:
        existing = {
            r["row_id"]: r
            for r in c.execute(
                "SELECT * FROM run_rows WHERE project_id=? AND uid=? AND model_id=? "
                "AND group_name=?",
                (self.project_id, uid, model_id, gname),
            ).fetchall()
        }
        scope = (self.project_id, uid, model_id)

        for pos, inc_row in enumerate(gnode.get("rows") or []):
            rid = inc_row.get("rowId")
            deleted = bool(inc_row.get("deleted"))

            if rid and rid in existing:
                cur = existing[rid]
                if deleted:
                    if cur["origin"] == "ai":
                        # A row the AI proposed and the curator rejected is a
                        # FALSE POSITIVE. Erase it and that count is gone, so it
                        # is only ever soft-deleted.
                        c.execute(
                            "UPDATE run_rows SET deleted_at=COALESCE(deleted_at, ?) "
                            "WHERE project_id=? AND uid=? AND model_id=? AND row_id=?",
                            (now, *scope, rid),
                        )
                    else:
                        # Curator-added and curator-removed: never a model claim,
                        # so there is no signal to preserve and no reason to keep
                        # it as noise.
                        c.execute(
                            "DELETE FROM run_features WHERE project_id=? AND uid=? "
                            "AND model_id=? AND row_id=?", (*scope, rid),
                        )
                        c.execute(
                            "DELETE FROM run_rows WHERE project_id=? AND uid=? "
                            "AND model_id=? AND row_id=?", (*scope, rid),
                        )
                    continue

                c.execute(
                    "UPDATE run_rows SET position=?, deleted_at=NULL "
                    "WHERE project_id=? AND uid=? AND model_id=? AND row_id=?",
                    (pos, *scope, rid),
                )
                self._merge_cells(c, uid, model_id, rid,
                                  inc_row.get("features") or {}, now, user_id)

            elif not deleted:
                if not templates:
                    continue  # unknown group — nothing safe to write
                rid = util.short_id()
                c.execute(
                    "INSERT INTO run_rows (project_id, uid, model_id, row_id, "
                    "group_name, position, origin, deleted_at) "
                    "VALUES (?,?,?,?,?,?,'curator',NULL)",
                    (*scope, rid, gname, pos),
                )
                inc_cells = inc_row.get("features") or {}
                for fname, tpl in templates.items():
                    cell = inc_cells.get(fname) or {}
                    value = cell.get("value", tpl["empty"])
                    changed = json.dumps(value, sort_keys=True) != json.dumps(
                        tpl["empty"], sort_keys=True
                    )
                    confirmed = bool(cell.get("confirmed"))
                    # aiValue is the type's EMPTY value, not the curator's input:
                    # the AI proposed nothing in this row, and "AI said nothing,
                    # human supplied this" is the truthful audit pair.
                    c.execute(self._CELL_SQL, (
                        *scope, rid, fname, tpl["type"],
                        json.dumps(tpl["empty"]), json.dumps(value),
                        1 if cell.get("present", changed) else 0,
                        cell.get("evidence"),
                        cell.get("evidenceMatch") or "none",
                        1 if confirmed else 0,
                        now if changed else None,
                        now if confirmed else None,
                    ))
                    if changed or confirmed:
                        c.execute(
                            "UPDATE run_features SET edited_by=?, confirmed_by=? "
                            "WHERE project_id=? AND uid=? AND model_id=? AND row_id=? AND name=?",
                            (user_id if changed else None, user_id if confirmed else None,
                             *scope, rid, fname),
                        )

        if "present" not in gnode and "confirmed" not in gnode:
            return
        st = c.execute(
            "SELECT * FROM run_groups WHERE project_id=? AND uid=? AND model_id=? "
            "AND group_name=?", (*scope, gname),
        ).fetchone()
        was_confirmed = bool(st["confirmed"]) if st else False
        now_confirmed = bool(gnode.get("confirmed", was_confirmed))
        present = 1 if gnode.get(
            "present", bool(st["present"]) if st else False
        ) else 0
        if now_confirmed and not was_confirmed:
            confirmed_at, confirmed_by = now, user_id
        elif not now_confirmed:
            confirmed_at, confirmed_by = None, None
        else:
            confirmed_at = st["confirmed_at"] if st else now
            confirmed_by = st["confirmed_by"] if st else user_id

        if st:
            c.execute(
                "UPDATE run_groups SET present=?, confirmed=?, confirmed_at=?, confirmed_by=? "
                "WHERE project_id=? AND uid=? AND model_id=? AND group_name=?",
                (present, 1 if now_confirmed else 0, confirmed_at, confirmed_by, *scope, gname),
            )
        else:
            c.execute(
                "INSERT INTO run_groups (project_id, uid, model_id, group_name, "
                "present, ai_present, confirmed, confirmed_at, confirmed_by) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (*scope, gname, present, present,
                 1 if now_confirmed else 0, confirmed_at, confirmed_by),
            )

    # ---- UI state ------------------------------------------------------- #
    def load_state(self, user_id: str) -> dict:
        raw = self.db.scalar(
            "SELECT state_json FROM ui_state WHERE project_id=? AND user_id=?",
            (self.project_id, user_id),
        )
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    def save_state(self, user_id: str, state: dict) -> None:
        with self.db.write() as c:
            c.execute(
                "INSERT INTO ui_state (project_id, user_id, state_json) VALUES (?,?,?) "
                "ON CONFLICT(project_id, user_id) DO UPDATE SET state_json=excluded.state_json",
                (self.project_id, user_id, json.dumps(state or {})),
            )


# --------------------------------------------------------------------------- #
# Row -> API dict. These shapes are the contract with the browser and with
# export.py / importer.py — keep them stable.
# --------------------------------------------------------------------------- #
def _paper_dict(row, models: list) -> dict:
    return {
        "uid": row["uid"],
        "pmid": row["pmid"],
        "doi": row["doi"],
        "pmidStatus": row["pmid_status"],
        "suggestedPmid": row["suggested_pmid"] or "",
        "filename": row["filename"],
        "pmidSource": row["pmid_source"],
        "addedAt": row["added_at"],
        "models": models,
        "hasPdf": bool(row["has_pdf"]),
        "abstractText": row["abstract_text"],
        "groupItems": _load_group_items_from_row(row),
        "groupItemsDiscovered": bool(row["group_items_discovered"]),
        "groupItemsReviewed": bool(row["group_items_reviewed"]),
        # Curation workflow (server/workflow.py).
        "assigneeId": row["assignee_id"],
        "curationStatus": row["curation_status"],
        "statusReason": row["status_reason"],
        "statusBy": row["status_by"],
        "statusAt": row["status_at"],
        "addedBy": row["added_by"],
    }


def _load_group_items_from_row(row) -> list:
    try:
        return json.loads(row["group_items_json"] or "[]")
    except json.JSONDecodeError:
        return []


def _run_dict(row, features: dict, groups: dict, with_prompt_text: bool) -> dict:
    out = {
        "uid": row["uid"],
        "pmid": row["pmid"],          # joined from papers, never stored on the run
        "modelId": row["model_id"],
        "modelLabel": row["model_label"],
        "promptId": row["prompt_id"],
        "promptHash": row["prompt_hash"],
        "source": row["source"],
        "parseEngine": row["parse_engine"],
        "requestedAt": row["requested_at"],
        "completedAt": row["completed_at"],
        "status": row["status"],
        "error": row["error"],
        # Paper-level cells. Repeating groups live in `groups`, each holding its
        # own rows — mirroring the config, where a group carries its own
        # `features` list.
        "features": features,
        "groups": groups,
    }
    if row["imported_from"]:
        out["importedFrom"] = row["imported_from"]
    if row["requested_by"]:
        out["requestedBy"] = row["requested_by"]
    # The composed prompt can be a few KB; it is audit material, not something
    # every /api/state needs to ship for every run.
    if with_prompt_text:
        out["promptText"] = row["prompt_text"]
    return out


def _feature_dict(row) -> dict:
    return {
        "type": row["type"],
        "aiValue": json.loads(row["ai_value_json"]),
        "value": json.loads(row["value_json"]),
        "present": bool(row["present"]),
        "evidence": row["evidence"],
        "evidenceMatch": row["evidence_match"],
        "confirmed": bool(row["confirmed"]),
        "editedAt": row["edited_at"],
        "confirmedAt": row["confirmed_at"],
        "editedBy": row["edited_by"],
        "confirmedBy": row["confirmed_by"],
    }
