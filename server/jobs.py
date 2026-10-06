"""Background jobs: PMID import (M14) and AI extraction (M16), run by one worker
thread inside the server process (M21, Section 2.5).

The browser creates a job and polls it; the work no longer depends on the tab
staying open. A job is a list of items (one PMID, or one paper), each with its
own state and outcome, so a 50-paper batch shows 50 rows and a failure in one
does not stop the rest.

    job:  queued -> running -> done | failed | cancelled
    item: waiting -> running -> done | failed | skipped | cancelled

Cancel sets a flag; items already running finish, waiting ones are cancelled.
Retry makes a NEW job from the failed items, so items that succeeded are never
re-run and the original job stays as a record.

Restart: items that were running when the process died are marked failed
("interrupted by server restart") and their job carries on with whatever was
still waiting. No item is ever left looking busy forever.

All writes go through Db.write(), i.e. the same process-wide lock as every
curation save — which is why there must be exactly one server process
(NFR-11).
"""
from __future__ import annotations

import json
import secrets
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor

from . import ai_settings, extraction, ncbi, papers as papers_mod, util
from .db import Db

KINDS = ("import", "extract")
# Items of one job run this many at a time (decision 10). Extraction already
# fans out up to 4 model calls per paper, so 3 papers = up to 12 OpenRouter
# calls in flight; drop EXTRACT_CONCURRENCY to 2 if OpenRouter starts throttling.
IMPORT_CONCURRENCY = 3
EXTRACT_CONCURRENCY = 3
MAX_ITEMS = 500
POLL_SECONDS = 2.0


class JobError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------- #
# Item executors. Each returns (state, outcome, result_dict, error_text) and
# must not raise for an ordinary per-item failure.
# --------------------------------------------------------------------------- #
def run_import_item(runner, proj, job, pmid: str):
    pmid = str(pmid).strip()
    store = proj.store
    if not pmid.isdigit():
        return "failed", "invalid", None, "PMID must be numeric"
    existing = store.pmid_taken_by(pmid)
    if existing:
        return "done", "duplicate", {"uid": existing}, None
    secrets_ = runner.secrets
    try:
        pdf = ncbi.fetch_fulltext_pdf(pmid, secrets_.ncbi_api_key, secrets_.unpaywall_email)
    except ncbi.NcbiError as exc:
        msg = str(exc)
        if "not found" in msg:
            return "failed", "not_found", None, msg
        if msg.startswith(("PubMed request failed", "could not reach")):
            return "failed", "error", None, msg
        # No open-access PDF. The project may allow curating from the abstract.
        if proj.config.allow_extraction_on_abstract:
            try:
                abstract = ncbi.fetch_abstract(pmid, secrets_.ncbi_api_key)
                paper = papers_mod.store_and_confirm_from_abstract(store, pmid, abstract)
            except (ncbi.NcbiError, papers_mod.PaperError) as exc2:
                return "failed", "no_pdf", None, f"{msg} — and no abstract either ({exc2})"
            store.set_added_by(paper["uid"], job["created_by"])
            return "done", "added_abstract", {"uid": paper["uid"]}, None
        return "failed", "no_pdf", None, msg
    try:
        paper = papers_mod.store_and_confirm_from_pmid(store, pdf, pmid)
    except papers_mod.PaperError as exc:
        if "already" in str(exc):
            return "done", "duplicate", None, str(exc)
        return "failed", "error", None, str(exc)
    store.set_added_by(paper["uid"], job["created_by"])
    return "done", "added", {"uid": paper["uid"]}, None


def run_extract_item(runner, proj, job, uid: str):
    params = job["params"]
    paper = proj.store.load_paper_by_ident(uid)
    if paper is None:
        return "failed", "error", None, f"no paper {uid} in this project"
    stage = params.get("stage") or ("discover" if proj.config.groups() else "curate")
    settings = ai_settings.get(runner.db, proj.config)
    model = ai_settings.model(settings)
    if model is None:
        return "failed", "error", None, "no default AI model is configured"
    if stage == "discover":
        try:
            items = extraction.discover_group_items(
                proj.config, runner.secrets, proj.store, paper, model,
                settings["parseEngine"], context=proj.prompt_context(),
            )
        except ValueError as exc:
            return "failed", "error", None, str(exc)
        return "done", "identified", {"uid": paper["uid"], "items": len(items)}, None

    if proj.config.groups() and (
        paper.get("assigneeId") != job["created_by"]
        or paper.get("curationStatus") != "in_progress"
    ):
        return (
            "failed", "error", None,
            "the paper is no longer assigned to the curator who started AI curation",
        )

    before = {r["modelId"]: r.get("requestedAt") for r in proj.store.list_runs(paper["uid"])}
    try:
        runs = extraction.run_extraction(
            proj.config, runner.secrets, proj.store, paper,
            [model["slug"]], params.get("promptId"), settings["parseEngine"],
            force=bool(params.get("force")), context=proj.prompt_context(),
            requested_by=job["created_by"],
            models_override=settings["models"],
        )
    except ValueError as exc:
        return "failed", "error", None, str(exc)
    fresh = [r for r in runs if before.get(r["modelId"]) != r.get("requestedAt")]
    failed = [r for r in fresh if r.get("status") == "failed"]
    result = {"uid": paper["uid"], "models": [r["modelId"] for r in fresh], "stage": "curate"}
    if failed:
        errs = "; ".join(f"{r['modelId']}: {r.get('error') or 'failed'}" for r in failed)
        return "failed", "error", result, errs
    if not fresh:
        return "skipped", "already_extracted", result, None
    return "done", "extracted", result, None


EXECUTORS = {"import": run_import_item, "extract": run_extract_item}
CONCURRENCY = {"import": IMPORT_CONCURRENCY, "extract": EXTRACT_CONCURRENCY}


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #
def _job_dict(row, counts=None) -> dict:
    try:
        params = json.loads(row["params_json"] or "{}")
    except ValueError:
        params = {}
    out = {
        "id": row["id"], "projectId": row["project_id"], "kind": row["kind"],
        "state": row["state"], "params": params, "total": row["total"],
        "cancelRequested": bool(row["cancel_requested"]), "error": row["error"],
        "retryOf": row["retry_of"], "createdBy": row["created_by"],
        "createdAt": row["created_at"], "startedAt": row["started_at"],
        "finishedAt": row["finished_at"],
    }
    if counts is not None:
        out["counts"] = counts
    return out


def _counts(db: Db, job_id: str) -> dict:
    c = {s: 0 for s in ("waiting", "running", "done", "failed", "skipped", "cancelled")}
    for r in db.query("SELECT state, COUNT(*) AS n FROM job_items WHERE job_id=? GROUP BY state",
                      (job_id,)):
        c[r["state"]] = r["n"]
    return c


def get_job(db: Db, project_id: str, job_id: str, with_items: bool = True):
    row = db.query_one("SELECT * FROM jobs WHERE id=? AND project_id=?", (job_id, project_id))
    if row is None:
        return None
    out = _job_dict(row, _counts(db, job_id))
    if with_items:
        items = []
        for r in db.query("SELECT * FROM job_items WHERE job_id=? ORDER BY seq", (job_id,)):
            try:
                result = json.loads(r["result_json"]) if r["result_json"] else None
            except ValueError:
                result = None
            items.append({
                "seq": r["seq"], "ref": r["ref"], "state": r["state"],
                "outcome": r["outcome"], "result": result, "error": r["error"],
                "startedAt": r["started_at"], "finishedAt": r["finished_at"],
            })
        out["items"] = items
    return out


def list_jobs(db: Db, project_id: str, limit: int = 20) -> list:
    rows = db.query("SELECT * FROM jobs WHERE project_id=? ORDER BY created_at DESC, id LIMIT ?",
                    (project_id, limit))
    return [_job_dict(r, _counts(db, r["id"])) for r in rows]


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
class JobRunner:
    def __init__(self, db: Db, projects, secrets, executors=None, errors=None):
        self.db = db
        self.projects = projects
        self.secrets = secrets
        self.executors = executors or EXECUTORS
        self.errors = errors           # app.ErrorLog, optional
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None

    # ---- creating ------------------------------------------------------- #
    def create(self, project_id: str, kind: str, refs: list, params: dict,
               created_by: str, retry_of: str = None) -> dict:
        if kind not in KINDS:
            raise JobError(f"unknown job kind {kind!r}")
        clean, seen = [], set()
        for r in refs or []:
            r = str(r).strip()
            if r and r not in seen:
                seen.add(r)
                clean.append(r)
        if not clean:
            raise JobError("nothing to do — the list is empty")
        if len(clean) > MAX_ITEMS:
            raise JobError(f"too many items in one job (max {MAX_ITEMS})")
        job_id = "j_" + secrets.token_hex(6)
        now = util.iso_now()
        with self.db.write() as c:
            c.execute(
                "INSERT INTO jobs (id, project_id, kind, state, params_json, total, "
                "retry_of, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (job_id, project_id, kind, "queued", json.dumps(params or {}),
                 len(clean), retry_of, created_by, now),
            )
            c.executemany(
                "INSERT INTO job_items (job_id, seq, ref) VALUES (?,?,?)",
                [(job_id, i, r) for i, r in enumerate(clean)],
            )
        self._wake.set()
        return get_job(self.db, project_id, job_id)

    def cancel(self, project_id: str, job_id: str) -> dict:
        with self.db.write() as c:
            row = c.execute("SELECT state FROM jobs WHERE id=? AND project_id=?",
                            (job_id, project_id)).fetchone()
            if row is None:
                raise JobError("no such job", 404)
            if row["state"] in ("done", "failed", "cancelled"):
                raise JobError(f"this job has already finished ({row['state']})", 409)
            c.execute("UPDATE jobs SET cancel_requested=1 WHERE id=?", (job_id,))
            if row["state"] == "queued":
                # Never started: cancel everything right here.
                now = util.iso_now()
                c.execute("UPDATE job_items SET state='cancelled', finished_at=? "
                          "WHERE job_id=? AND state='waiting'", (now, job_id))
                c.execute("UPDATE jobs SET state='cancelled', finished_at=? WHERE id=?",
                          (now, job_id))
        return get_job(self.db, project_id, job_id)

    def retry(self, project_id: str, job_id: str, created_by: str) -> dict:
        job = get_job(self.db, project_id, job_id)
        if job is None:
            raise JobError("no such job", 404)
        if job["state"] in ("queued", "running"):
            raise JobError("this job is still running", 409)
        refs = [it["ref"] for it in job["items"] if it["state"] in ("failed", "cancelled")]
        if not refs:
            raise JobError("nothing failed in this job — nothing to retry", 409)
        return self.create(project_id, job["kind"], refs, job["params"], created_by,
                           retry_of=job_id)

    # ---- running -------------------------------------------------------- #
    def recover(self) -> None:
        """Called once at start-up, before the worker thread runs."""
        now = util.iso_now()
        with self.db.write() as c:
            c.execute(
                "UPDATE job_items SET state='failed', outcome='interrupted', "
                "error='interrupted by server restart', finished_at=? WHERE state='running'",
                (now,),
            )
            c.execute("UPDATE jobs SET state='queued' WHERE state='running'")

    def start(self) -> None:
        self.recover()
        self._thread = threading.Thread(target=self._loop, name="annotaid-jobs", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                worked = self.run_next()
            except Exception as exc:  # noqa: BLE001 — the worker must not die
                traceback.print_exc()
                if self.errors:
                    self.errors.add("JOB", "worker", exc)
                worked = False
            if not worked:
                self._wake.wait(POLL_SECONDS)
                self._wake.clear()

    def run_next(self) -> bool:
        """Run the oldest queued job to completion. -> False if none was queued.
        Public so tests can drive the runner without the thread."""
        row = self.db.query_one(
            "SELECT * FROM jobs WHERE state='queued' ORDER BY created_at, id LIMIT 1"
        )
        if row is None:
            return False
        job_id = row["id"]
        with self.db.write() as c:
            c.execute("UPDATE jobs SET state='running', started_at=COALESCE(started_at, ?) "
                      "WHERE id=?", (util.iso_now(), job_id))
        job = {"id": job_id, "created_by": row["created_by"],
               "params": json.loads(row["params_json"] or "{}")}

        proj = self.projects.get(row["project_id"])
        if proj is None:
            self._finish(job_id, "failed", "the project no longer exists or is archived")
            return True

        execute = self.executors[row["kind"]]
        items = self.db.query("SELECT seq, ref FROM job_items WHERE job_id=? AND state='waiting' "
                              "ORDER BY seq", (job_id,))

        def one(item):
            if self._cancel_requested(job_id) or self._stop.is_set():
                return
            with self.db.write() as c:
                n = c.execute("UPDATE job_items SET state='running', started_at=? "
                              "WHERE job_id=? AND seq=? AND state='waiting'",
                              (util.iso_now(), job_id, item["seq"])).rowcount
            if not n:
                return
            try:
                state, outcome, result, error = execute(self, proj, job, item["ref"])
            except Exception as exc:  # noqa: BLE001 — one bad item must not sink the job
                traceback.print_exc()
                if self.errors:
                    self.errors.add("JOB", f"{row['kind']}:{item['ref']}", exc)
                state, outcome, result, error = "failed", "error", None, f"{type(exc).__name__}: {exc}"
            with self.db.write() as c:
                c.execute(
                    "UPDATE job_items SET state=?, outcome=?, result_json=?, error=?, "
                    "finished_at=? WHERE job_id=? AND seq=?",
                    (state, outcome, json.dumps(result) if result is not None else None,
                     (error or None) and str(error)[:2000], util.iso_now(), job_id, item["seq"]),
                )

        workers = max(1, min(CONCURRENCY.get(row["kind"], 1), len(items) or 1))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(one, items))

        if self._stop.is_set():
            return True  # shutting down: leave the job for recover() on next start
        if self._cancel_requested(job_id):
            now = util.iso_now()
            with self.db.write() as c:
                c.execute("UPDATE job_items SET state='cancelled', finished_at=? "
                          "WHERE job_id=? AND state='waiting'", (now, job_id))
            self._finish(job_id, "cancelled")
            return True
        counts = _counts(self.db, job_id)
        ok = counts["done"] + counts["skipped"]
        self._finish(job_id, "failed" if counts["failed"] and not ok else "done")
        return True

    def _cancel_requested(self, job_id: str) -> bool:
        return bool(self.db.scalar("SELECT cancel_requested FROM jobs WHERE id=?", (job_id,)))

    def _finish(self, job_id: str, state: str, error: str = None) -> None:
        with self.db.write() as c:
            c.execute("UPDATE jobs SET state=?, error=COALESCE(?, error), finished_at=? WHERE id=?",
                      (state, error, util.iso_now(), job_id))
