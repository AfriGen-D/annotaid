"""Durable JSON store on disk (brief §9).

Layout under data_dir:
    pdfs/<uid>.pdf
    papers/<uid>.json                 Paper records
    runs/<pmid>__<modelSlug>.json     one Extraction-run per (paper × model)
    index.json                        uid <-> pmid map
    state.json                        transient UI niceties

Writes are atomic (temp + os.replace) and serialised with a lock, since the server
is threaded and autosave is read-modify-write.
"""
from __future__ import annotations

import json
import os
import threading

from . import util


class Store:
    def __init__(self, data_dir: str):
        self.dir = os.path.abspath(data_dir)
        self.pdfs = os.path.join(self.dir, "pdfs")
        self.papers = os.path.join(self.dir, "papers")
        self.runs = os.path.join(self.dir, "runs")
        self.index_path = os.path.join(self.dir, "index.json")
        self.state_path = os.path.join(self.dir, "state.json")
        self._lock = threading.RLock()
        for d in (self.pdfs, self.papers, self.runs):
            os.makedirs(d, exist_ok=True)

    # ---- index (uid <-> pmid) ------------------------------------------- #
    def _index(self) -> dict:
        return util.read_json(self.index_path, {"uidToPmid": {}, "pmidToUid": {}})

    def uid_for(self, ident: str):
        """Resolve a uid OR pmid to a uid."""
        with self._lock:
            idx = self._index()
            if ident in idx["uidToPmid"]:
                return ident
            return idx["pmidToUid"].get(ident)

    def pmid_for_uid(self, uid: str):
        with self._lock:
            return self._index()["uidToPmid"].get(uid)

    def set_pmid(self, uid: str, pmid: str) -> None:
        with self._lock:
            idx = self._index()
            idx["uidToPmid"][uid] = pmid
            idx["pmidToUid"][pmid] = uid
            util.atomic_write_json(self.index_path, idx)

    def pmid_taken_by(self, pmid: str):
        with self._lock:
            return self._index()["pmidToUid"].get(pmid)

    # ---- PDFs ----------------------------------------------------------- #
    def pdf_path(self, uid: str) -> str:
        return os.path.join(self.pdfs, util.safe_filename(uid) + ".pdf")

    def save_pdf(self, uid: str, data: bytes) -> None:
        util.atomic_write_bytes(self.pdf_path(uid), data)

    def resolve_pdf(self, ident: str):
        uid = self.uid_for(ident)
        if not uid:
            return None
        p = self.pdf_path(uid)
        return p if os.path.isfile(p) else None

    # ---- Papers --------------------------------------------------------- #
    def _paper_path(self, uid: str) -> str:
        return os.path.join(self.papers, util.safe_filename(uid) + ".json")

    def save_paper(self, paper: dict) -> None:
        with self._lock:
            util.atomic_write_json(self._paper_path(paper["uid"]), paper)

    def load_paper(self, uid: str):
        return util.read_json(self._paper_path(uid))

    def load_paper_by_ident(self, ident: str):
        uid = self.uid_for(ident)
        return self.load_paper(uid) if uid else None

    def list_papers(self) -> list:
        out = []
        if os.path.isdir(self.papers):
            for fn in sorted(os.listdir(self.papers)):
                if fn.endswith(".json"):
                    p = util.read_json(os.path.join(self.papers, fn))
                    if p:
                        out.append(p)
        out.sort(key=lambda p: p.get("addedAt", ""))
        return out

    # ---- Runs ----------------------------------------------------------- #
    def _run_path(self, pmid: str, model_id: str) -> str:
        fn = f"{util.safe_filename(pmid)}__{util.safe_filename(model_id)}.json"
        return os.path.join(self.runs, fn)

    def save_run(self, run: dict) -> None:
        with self._lock:
            util.atomic_write_json(self._run_path(run["pmid"], run["modelId"]), run)

    def load_run(self, pmid: str, model_id: str):
        return util.read_json(self._run_path(pmid, model_id))

    def list_runs(self, pmid: str) -> list:
        prefix = util.safe_filename(pmid) + "__"
        out = []
        if os.path.isdir(self.runs):
            for fn in sorted(os.listdir(self.runs)):
                if fn.startswith(prefix) and fn.endswith(".json"):
                    r = util.read_json(os.path.join(self.runs, fn))
                    if r:
                        out.append(r)
        return out

    def list_all_runs(self) -> list:
        out = []
        if os.path.isdir(self.runs):
            for fn in sorted(os.listdir(self.runs)):
                if fn.endswith(".json"):
                    r = util.read_json(os.path.join(self.runs, fn))
                    if r:
                        out.append(r)
        return out

    def merge_run_edits(self, pmid: str, model_id: str, incoming_features: dict) -> dict:
        """Apply curator edits, PRESERVING aiValue + type from disk (immutability).

        Stamps editedAt when a value changes and confirmedAt on confirm transitions.
        """
        with self._lock:
            run = self.load_run(pmid, model_id)
            if run is None:
                raise KeyError(f"no run for {pmid}/{model_id}")
            now = util.iso_now()
            feats = run.get("features", {})
            for name, incoming in (incoming_features or {}).items():
                cur = feats.get(name)
                if cur is None:
                    continue  # never invent features not proposed by the AI
                # immutable fields kept from disk:
                ai_value = cur.get("aiValue")
                ftype = cur.get("type")

                new_value = incoming.get("value", cur.get("value"))
                value_changed = json.dumps(new_value, sort_keys=True) != json.dumps(
                    cur.get("value"), sort_keys=True
                )

                was_confirmed = bool(cur.get("confirmed"))
                now_confirmed = bool(incoming.get("confirmed", was_confirmed))

                cur["type"] = ftype
                cur["aiValue"] = ai_value
                cur["value"] = new_value
                if "present" in incoming:
                    cur["present"] = incoming["present"]
                if "evidence" in incoming:
                    cur["evidence"] = incoming["evidence"]
                if "evidenceMatch" in incoming:
                    cur["evidenceMatch"] = incoming["evidenceMatch"]
                cur["confirmed"] = now_confirmed

                if value_changed:
                    cur["editedAt"] = now
                if now_confirmed and not was_confirmed:
                    cur["confirmedAt"] = now
                elif not now_confirmed:
                    cur["confirmedAt"] = None
                feats[name] = cur
            run["features"] = feats
            self.save_run(run)
            return run

    # ---- UI state ------------------------------------------------------- #
    def load_state(self) -> dict:
        return util.read_json(self.state_path, {})

    def save_state(self, state: dict) -> None:
        with self._lock:
            util.atomic_write_json(self.state_path, state)
