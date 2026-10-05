"""Import existing model-output JSONs into extraction-run records.

Handles the two shapes already in the corpus:
  (A) rich per-model file: { [pubmed_id], <field>: {value, evidence, location} }
      filename encodes pmid + model, e.g. 17957323_pdf_Claude_fable5_extra.json
  (B) nested aggregated:   { <pmid>: { <curator>: { <field>: <bare value> } } }
      (no evidence -> evidenceMatch stays "none")

An imported run attaches to an EXISTING paper (matched by PMID); papers without a
stored PDF are skipped and reported, since verification needs the PDF.
"""
from __future__ import annotations

import copy
import os
import re

from . import features as feat, util, validation
from .config_loader import Config
from .project_store import ProjectStore

_PMID_RE = re.compile(r"(\d{5,9})")


def _pmid_from_filename(filename: str):
    stem = os.path.splitext(os.path.basename(filename))[0]
    m = _PMID_RE.match(stem)
    return m.group(1) if m else None


def _model_from_filename(filename: str, pmid: str):
    stem = os.path.splitext(os.path.basename(filename))[0]
    rest = stem
    if pmid and stem.startswith(pmid):
        rest = stem[len(pmid) :]
    rest = re.sub(r"^[_-]*(pdf|xml)[_-]*", "", rest, flags=re.I)
    return util.slugify(rest) or "imported"


def _run_from_fields(uid, model_id, features_obj, config: Config, source_label):
    """Build an extraction-run record from a flat {field: value|node} object."""
    feats = {}
    for f in feat.leaves(config.features):
        name = f.name
        if name in features_obj:
            canon = validation.coerce_feature(features_obj[name], f)
        else:
            canon = {"present": False, "value": f.empty_value(), "evidence": None}
        feats[name] = {
            "type": f.type,
            "aiValue": copy.deepcopy(canon["value"]),
            "value": copy.deepcopy(canon["value"]),
            "present": canon["present"],
            "evidence": canon["evidence"],
            "evidenceMatch": "none",
            "confirmed": False,
            "editedAt": None,
            "confirmedAt": None,
        }
    now = util.iso_now()
    return {
        "uid": uid,
        "modelId": model_id,
        "modelLabel": model_id,
        "promptId": "import",
        "promptHash": None,   # not our prompt — the output came from elsewhere
        "promptText": None,
        "source": "import",
        "parseEngine": "native",
        "requestedAt": now,
        "completedAt": now,
        "status": "ok",
        "error": None,
        "features": feats,
        # The import formats are flat {field: value} files, so they carry nothing
        # for a repeating group. Empty-and-absent is the truthful record of that:
        # the file asserted no entries, rather than us pretending it did.
        "groups": {g.name: {"present": False, "rows": []}
                   for g in feat.groups(config.features)},
        "importedFrom": source_label,
    }


def _looks_nested(data) -> bool:
    """True if data is {pmid: {curator: {...}}} rather than a single flat record."""
    if not isinstance(data, dict):
        return False
    for k, v in data.items():
        if k.isdigit() and isinstance(v, dict):
            inner = next(iter(v.values()), None)
            if isinstance(inner, dict):
                return True
    return False


def import_items(store: ProjectStore, config: Config, items: list) -> dict:
    """items: [{filename, data, modelId?, pmid?}]. Returns a summary."""
    written, skipped, warnings = 0, 0, []

    for item in items:
        filename = item.get("filename", "upload.json")
        data = item.get("data")
        if data is None:
            skipped += 1
            warnings.append(f"{filename}: no data")
            continue

        records = []  # (pmid, model_id, fields_obj)
        if _looks_nested(data):
            for pmid, curators in data.items():
                if not (pmid.isdigit() and isinstance(curators, dict)):
                    continue
                for curator, fields in curators.items():
                    if isinstance(fields, dict):
                        records.append((pmid, util.slugify(curator), fields))
        else:
            pmid = item.get("pmid") or (
                str(data.get("pubmed_id")) if isinstance(data, dict) and data.get("pubmed_id") else None
            ) or _pmid_from_filename(filename)
            model_id = item.get("modelId") or _model_from_filename(filename, pmid or "")
            fields = {k: v for k, v in data.items() if k != "pubmed_id"} if isinstance(data, dict) else {}
            records.append((pmid, model_id, fields))

        for pmid, model_id, fields in records:
            if not pmid:
                skipped += 1
                warnings.append(f"{filename}: could not determine PMID")
                continue
            # An import is matched to a paper by PMID, so a paper deliberately
            # marked as having no PMID cannot be an import target — there is
            # nothing in the incoming file to match it on.
            paper = store.load_paper_by_ident(pmid)
            if paper is None or not paper.get("pmid"):
                skipped += 1
                warnings.append(f"{filename}: no paper with confirmed PMID {pmid} (upload the PDF first)")
                continue
            # runs are keyed on uid, not pmid (a paper may have no PMID at all)
            run = _run_from_fields(paper["uid"], model_id, fields, config, filename)
            store.save_run(run)
            # paper["models"] is derived from the runs table — no write-back.
            written += 1

    return {"written": written, "skipped": skipped, "warnings": warnings}
