"""Paper intake: upload, suggested-PMID, and identity resolution (brief §5.2).

A suggestion is NEVER auto-finalised — the curator must confirm the PMID before it
commits, because a numeric filename may not be a valid PMID.

Most papers have a PubMed ID, but not all do. So a paper carries an identity
*state* rather than just a pmid:

    pending    -- the curator hasn't decided yet; curation is blocked
    confirmed  -- pmid is set and verified
    none       -- deliberately has no PMID (may carry a doi); curation proceeds

The gate is on `pending`, not on the absence of a pmid: the original intent was
"resolve a paper's identity before curating it", and that intent survives without
forcing the answer to be a PubMed ID.
"""
from __future__ import annotations

import os
import re

from . import util
from .project_store import (
    STATUS_CONFIRMED,
    STATUS_NONE,
    STATUS_PENDING,
    ProjectStore,
)

# Deliberately permissive: DOIs are '10.<registrant>/<suffix>' and the suffix may
# contain almost anything. We check the shape, not a registry.
_DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")


class PaperError(Exception):
    pass


def suggested_pmid(filename: str) -> str:
    """Prefill only if the filename stem is purely numeric, else empty."""
    stem = os.path.splitext(os.path.basename(filename))[0].strip()
    return stem if stem.isdigit() else ""


def normalise_doi(raw: str) -> str:
    """Strip the common URL / 'doi:' wrappers people paste, then shape-check."""
    doi = str(raw or "").strip()
    doi = re.sub(r"^(https?://)?(dx\.)?doi\.org/", "", doi, flags=re.I)
    doi = re.sub(r"^doi:\s*", "", doi, flags=re.I)
    doi = doi.strip().rstrip(".")
    if not doi:
        return ""
    if not _DOI_RE.match(doi):
        raise PaperError(f"{doi!r} doesn't look like a DOI (expected e.g. 10.1038/s41588-024-01234)")
    return doi


def store_upload(store: ProjectStore, pdf_bytes: bytes, filename: str,
                 suggested: str = "") -> dict:
    """`suggested` is an explicit PMID the curator already named (they came here
    from a failed fetch-by-PMID row). It only prefills the confirm box — an
    uploaded PDF has NOT been verified against PubMed, so it is never committed.
    """
    if not pdf_bytes.startswith(b"%PDF"):
        raise PaperError("not a PDF (missing %PDF header)")
    uid = util.sha256_hex(pdf_bytes)[:12]
    store.save_pdf(uid, pdf_bytes)

    existing = store.load_paper(uid)
    if existing:
        return existing  # dedupe: same bytes -> same paper, within this project

    suggested = str(suggested).strip()
    paper = {
        "uid": uid,
        "pmid": None,
        "doi": None,
        "pmidStatus": STATUS_PENDING,
        "suggestedPmid": suggested if suggested.isdigit() else suggested_pmid(filename),
        "filename": os.path.basename(filename) or f"{uid}.pdf",
        "pmidSource": None,
        "addedAt": util.iso_now(),
        "models": [],
    }
    store.save_paper(paper)
    return paper


def confirm_pmid(store: ProjectStore, uid: str, pmid: str, source: str) -> dict:
    paper = store.load_paper(uid)
    if paper is None:
        raise PaperError(f"no paper with uid {uid}")
    pmid = str(pmid).strip()
    if not pmid.isdigit():
        raise PaperError("PMID must be numeric")
    if source not in ("prefill-confirmed", "manual", "pmid-fetch"):
        source = "manual"

    # PMIDs are unique PER PROJECT — the same paper may legitimately be curated
    # in another project at the same time.
    taken = store.pmid_taken_by(pmid)
    if taken and taken != uid:
        raise PaperError(f"PMID {pmid} is already assigned to another paper in this project")

    store.set_identity(uid, status=STATUS_CONFIRMED, pmid=pmid, source=source)
    return store.load_paper(uid)


def mark_no_pmid(store: ProjectStore, uid: str, doi: str = "") -> dict:
    """Record that a paper has no PubMed ID, optionally with a DOI instead.

    This UNBLOCKS curation — it is a real answer to "what is this paper?", not a
    way of skipping the question.
    """
    paper = store.load_paper(uid)
    if paper is None:
        raise PaperError(f"no paper with uid {uid}")

    clean = normalise_doi(doi)
    if clean:
        taken = store.doi_taken_by(clean)
        if taken and taken != uid:
            raise PaperError(f"DOI {clean} is already assigned to another paper in this project")

    store.set_identity(uid, status=STATUS_NONE, doi=clean or None, source=None)
    return store.load_paper(uid)


def store_and_confirm_from_pmid(store: ProjectStore, pdf_bytes: bytes, pmid: str) -> dict:
    """Used by the "fetch by PMID" flow: since the PMID was just verified against
    PubMed itself (server/ncbi.py), it is pre-confirmed here rather than asking
    the curator to re-confirm what they just typed.
    """
    if not pdf_bytes.startswith(b"%PDF"):
        raise PaperError("fetched file is not a PDF (missing %PDF header)")
    pmid = str(pmid).strip()
    uid = util.sha256_hex(pdf_bytes)[:12]
    store.save_pdf(uid, pdf_bytes)

    existing = store.load_paper(uid)
    if existing:
        if existing.get("pmid") and existing["pmid"] != pmid:
            raise PaperError(f"this PDF is already stored under PMID {existing['pmid']}")
        if existing.get("pmid") == pmid:
            return existing
        return confirm_pmid(store, uid, pmid, "pmid-fetch")

    taken = store.pmid_taken_by(pmid)
    if taken:
        raise PaperError(f"PMID {pmid} is already assigned to another paper in this project")

    paper = {
        "uid": uid,
        "pmid": None,
        "doi": None,
        "pmidStatus": STATUS_PENDING,
        "suggestedPmid": pmid,
        "filename": f"PMID{pmid}.pdf",
        "pmidSource": None,
        "addedAt": util.iso_now(),
        "models": [],
        "hasPdf": True,
    }
    store.save_paper(paper)
    return confirm_pmid(store, uid, pmid, "pmid-fetch")


def store_and_confirm_from_abstract(store: ProjectStore, pmid: str, abstract_text: str) -> dict:
    """allowExtractionOnAbstract fallback: used when fetch-by-PMID could not find
    an open-access full-text PDF (server/ncbi.fetch_fulltext_pdf raised) but the
    project has opted into extracting from the abstract alone.

    Mirrors store_and_confirm_from_pmid, except there are no PDF bytes to hash
    for a uid — the abstract text IS the paper's content, so uid is derived from
    "abstract:<pmid>" instead. Like the PDF path, the PMID was just verified
    against PubMed itself, so it is pre-confirmed here.
    """
    pmid = str(pmid).strip()
    uid = util.sha256_hex(f"abstract:{pmid}".encode())[:12]

    existing = store.load_paper(uid)
    if existing:
        return existing  # dedupe: same PMID abstract fetched twice -> same paper

    taken = store.pmid_taken_by(pmid)
    if taken:
        raise PaperError(f"PMID {pmid} is already assigned to another paper in this project")

    paper = {
        "uid": uid,
        "pmid": None,
        "doi": None,
        "pmidStatus": STATUS_PENDING,
        "suggestedPmid": pmid,
        "filename": f"PMID{pmid}-abstract.txt",
        "pmidSource": None,
        "addedAt": util.iso_now(),
        "models": [],
        "hasPdf": False,
        "abstractText": abstract_text,
    }
    store.save_paper(paper)
    return confirm_pmid(store, uid, pmid, "pmid-fetch")
