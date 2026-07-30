"""Paper intake: upload, suggested-PMID, confirm-PMID (brief §5.2).

The suggestion is NEVER auto-finalised — the curator must confirm the PMID before
it commits, because a numeric filename may not be a valid PMID.
"""
from __future__ import annotations

import os

from . import util
from .store import Store


class PaperError(Exception):
    pass


def suggested_pmid(filename: str) -> str:
    """Prefill only if the filename stem is purely numeric, else empty."""
    stem = os.path.splitext(os.path.basename(filename))[0].strip()
    return stem if stem.isdigit() else ""


def store_upload(store: Store, pdf_bytes: bytes, filename: str) -> dict:
    if not pdf_bytes.startswith(b"%PDF"):
        raise PaperError("not a PDF (missing %PDF header)")
    uid = util.sha256_hex(pdf_bytes)[:12]
    store.save_pdf(uid, pdf_bytes)

    existing = store.load_paper(uid)
    if existing:
        return existing  # dedupe: same bytes -> same paper

    paper = {
        "uid": uid,
        "pmid": None,
        "suggestedPmid": suggested_pmid(filename),
        "filename": os.path.basename(filename) or f"{uid}.pdf",
        "pmidSource": None,
        "addedAt": util.iso_now(),
        "models": [],
    }
    store.save_paper(paper)
    return paper


def confirm_pmid(store: Store, uid: str, pmid: str, source: str) -> dict:
    paper = store.load_paper(uid)
    if paper is None:
        raise PaperError(f"no paper with uid {uid}")
    pmid = str(pmid).strip()
    if not pmid.isdigit():
        raise PaperError("PMID must be numeric")
    if source not in ("prefill-confirmed", "manual"):
        source = "manual"

    taken = store.pmid_taken_by(pmid)
    if taken and taken != uid:
        raise PaperError(f"PMID {pmid} is already assigned to another paper")

    paper["pmid"] = pmid
    paper["pmidSource"] = source
    store.set_pmid(uid, pmid)
    store.save_paper(paper)
    return paper
