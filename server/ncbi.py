"""Fetch a paper's full-text PDF by PMID — stdlib urllib port of the finder chain
in scripts/fetch_pubmed.py (same project, `requests`-based CLI), minus its
Sci-Hub last resort: that one is explicitly opt-in/CLI-only and not appropriate
for an in-app "fetch by PMID" action.

Order: verify the PMID exists on PubMed, resolve its PMC id / DOI via efetch,
then try open-access sources for an actual PDF, stopping at the first hit:
  PMC-based (need a PMC id): AWS S3 PMC Cloud Service, PMC OA tar.gz, the
  citation_pdf_url meta tag on the PMC article page.
  Broad (need only a PMID/DOI): OpenAlex, Unpaywall (needs UNPAYWALL_EMAIL).
  PMC fallbacks: Europe PMC render, NCBI direct article PDF URL.

Most subscription-only papers are simply not retrievable this way — that is
expected and surfaced as a normal NcbiError, not a crash, so the curator can
fall back to manual upload.
"""
from __future__ import annotations

import io
import json
import re
import tarfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
USER_AGENT = "annotaid/0.1 (biocuration tool; python urllib)"

# --- NCBI rate limiting -----------------------------------------------------
# NCBI allows 3 requests/second per IP without an API key, 10 with one. That is
# a per-IP budget, so it has to be enforced here rather than in the client: the
# "Add Paper(s)" modal fires several fetches concurrently and the server is a
# ThreadingHTTPServer, so those genuinely run in parallel.
#
# Only NCBI's own hosts are gated. www.ncbi.nlm.nih.gov is included because the
# finder chain hits it too (oa.fcgi, the PMC article page, the direct /pdf/
# URL). Everything else in the chain — the PMC S3 bucket, the ftp tarballs,
# OpenAlex, Unpaywall, Europe PMC — is a different operator with a different
# budget, and throttling multi-MB downloads to 3/s would slow the happy path
# for nothing.
_RATE_LIMITED_HOSTS = {"eutils.ncbi.nlm.nih.gov", "www.ncbi.nlm.nih.gov"}
_INTERVAL_NO_KEY = 1.0 / 3.0
_INTERVAL_WITH_KEY = 1.0 / 10.0
_SAFETY = 1.10  # ~10% headroom for clock jitter


class _MinIntervalGate:
    """Hands out request start-slots at least `interval` apart. Thread-safe.

    The sleep happens OUTSIDE the lock: each caller reserves a distinct future
    slot and then waits for its own slot, so N threads still achieve 1/interval
    throughput instead of queueing on the mutex.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._next_at = 0.0

    def wait(self, interval: float) -> None:
        with self._lock:
            start = max(time.monotonic(), self._next_at)
            self._next_at = start + interval
        delay = start - time.monotonic()
        if delay > 0:
            time.sleep(delay)


_gate = _MinIntervalGate()

# _request() is called from eight places, half of them inside best-effort
# `except Exception: pass` blocks, and it has no view of ctx.secrets — so the
# rate is read from here rather than threaded through every call site.
# fetch_fulltext_pdf() sets it; every thread writes the same constant for the
# life of the process, so the unsynchronised write is benign.
_has_api_key = False


class NcbiError(RuntimeError):
    """Message is safe to show directly to the curator."""


def _request(url: str, params: dict | None = None, timeout: int = 15, headers: dict | None = None) -> bytes:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    if (urllib.parse.urlsplit(url).hostname or "") in _RATE_LIMITED_HOSTS:
        _gate.wait((_INTERVAL_WITH_KEY if _has_api_key else _INTERVAL_NO_KEY) * _SAFETY)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _is_pdf(data: bytes) -> bool:
    return data[:4] == b"%PDF"


def _download_pdf_url(url: str, timeout: int = 20):
    """Fetch a URL and return bytes if it's a valid PDF, else None (best-effort)."""
    try:
        data = _request(url, timeout=timeout)
        if _is_pdf(data):
            return data
    except Exception:  # noqa: BLE001 — any failure just means "try the next source"
        pass
    return None


def _fetch_from_tgz(url: str, timeout: int = 30):
    try:
        data = _request(url, timeout=timeout)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            pdf_members = [m for m in tar.getmembers() if m.name.endswith(".pdf")]
            if not pdf_members:
                return None
            extracted = tar.extractfile(pdf_members[0])
            raw = extracted.read() if extracted else None
            if raw and _is_pdf(raw):
                return raw
    except Exception:  # noqa: BLE001
        pass
    return None


def _eutils_params(extra: dict, api_key: str) -> dict:
    if api_key:
        extra["api_key"] = api_key
    return extra


def _verify_pmid_exists(pmid: str, api_key: str) -> None:
    body = _request(
        f"{EUTILS}/esummary.fcgi",
        params=_eutils_params({"db": "pubmed", "id": pmid, "retmode": "json"}, api_key),
        timeout=15,
    )
    try:
        obj = json.loads(body)
        result = obj["result"]
        uids = result["uids"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise NcbiError(f"PubMed lookup for PMID {pmid} returned an unexpected response") from exc
    if pmid not in uids:
        raise NcbiError(f"PMID {pmid} was not found on PubMed")
    # NCBI still echoes an invalid id back into `uids`, but marks its own record
    # with an "error" key (e.g. "cannot get document summary") — that's the
    # actual "doesn't exist" signal.
    rec = result.get(pmid)
    if isinstance(rec, dict) and rec.get("error"):
        raise NcbiError(f"PMID {pmid} was not found on PubMed")


def _get_article_ids(pmid: str, api_key: str):
    """Return (pmc_id, doi) from efetch PubMedData/ArticleIdList. Either may be None."""
    body = _request(
        f"{EUTILS}/efetch.fcgi",
        params=_eutils_params({"db": "pubmed", "id": pmid, "rettype": "xml", "retmode": "xml"}, api_key),
        timeout=20,
    )
    pmc_id = doi = None
    try:
        root = ET.fromstring(body)
        for article_id in root.findall("./PubmedArticle/PubmedData/ArticleIdList/ArticleId"):
            id_type = article_id.get("IdType")
            text = (article_id.text or "").strip()
            if id_type == "pmc" and not pmc_id:
                pmc_id = text[3:] if text.upper().startswith("PMC") else text
            elif id_type == "doi" and not doi:
                doi = text
    except ET.ParseError:
        pass
    return pmc_id, doi


def _find_pdf(pmid: str, pmc_id: str | None, doi: str | None, unpaywall_email: str):
    """Finder chain — returns bytes at the first successful download, else None."""

    if pmc_id:
        data = _download_pdf_url(f"https://pmc-oa-opendata.s3.amazonaws.com/PMC{pmc_id}.1/PMC{pmc_id}.1.pdf")
        if data:
            return data

        try:
            body = _request(
                "https://www.ncbi.nlm.nih.gov/pmc/utils/oa/oa.fcgi",
                params={"id": f"PMC{pmc_id}"}, timeout=15,
            )
            link = ET.fromstring(body).find(".//link[@format='tgz']")
            if link is not None and link.get("href"):
                tgz_url = link.get("href", "").replace(
                    "ftp://ftp.ncbi.nlm.nih.gov/pub/pmc/",
                    "https://ftp.ncbi.nlm.nih.gov/pub/pmc/deprecated/",
                )
                data = _fetch_from_tgz(tgz_url)
                if data:
                    return data
        except Exception:  # noqa: BLE001
            pass

        try:
            page = _request(f"https://www.ncbi.nlm.nih.gov/pmc/articles/PMC{pmc_id}", timeout=15)
            text = page.decode("utf-8", errors="ignore")
            m = re.search(
                r'<meta[^>]+?name=["\']citation_pdf_url["\'][^>]+?content=["\']([^"\']+)["\']'
                r'|<meta[^>]+?content=["\']([^"\']+)["\'][^>]+?name=["\']citation_pdf_url["\']',
                text, re.IGNORECASE,
            )
            pdf_url = (m.group(1) or m.group(2)) if m else None
            if pdf_url:
                data = _download_pdf_url(pdf_url)
                if data:
                    return data
        except Exception:  # noqa: BLE001
            pass

    try:
        body = _request(
            f"https://api.openalex.org/works/pmid:{pmid}",
            params={"select": "best_oa_location,locations"},
            headers={"User-Agent": f"annotaid/0.1 (mailto:{unpaywall_email or 'research@example.com'})"},
            timeout=10,
        )
        obj = json.loads(body)
        pdf_urls = []
        best = obj.get("best_oa_location") or {}
        if best.get("pdf_url"):
            pdf_urls.append(best["pdf_url"])
        for loc in obj.get("locations") or []:
            u = loc.get("pdf_url")
            if u and u not in pdf_urls:
                pdf_urls.append(u)
        for pdf_url in pdf_urls:
            data = _download_pdf_url(pdf_url)
            if data:
                return data
    except Exception:  # noqa: BLE001
        pass

    if doi and unpaywall_email:
        try:
            body = _request(
                f"https://api.unpaywall.org/v2/{doi}",
                params={"email": unpaywall_email}, timeout=10,
            )
            obj = json.loads(body)
            pdf_urls = []
            best = obj.get("best_oa_location") or {}
            if best.get("url_for_pdf"):
                pdf_urls.append(best["url_for_pdf"])
            for loc in obj.get("oa_locations") or []:
                u = loc.get("url_for_pdf")
                if u and u not in pdf_urls:
                    pdf_urls.append(u)
            for pdf_url in pdf_urls:
                data = _download_pdf_url(pdf_url)
                if data:
                    return data
        except Exception:  # noqa: BLE001
            pass

    if pmc_id:
        data = _download_pdf_url(f"https://europepmc.org/backend/ptpmcrender.fcgi?accid=PMC{pmc_id}&blobtype=pdf")
        if data:
            return data
        data = _download_pdf_url(f"https://www.ncbi.nlm.nih.gov/pmc/articles/PMC{pmc_id}/pdf/")
        if data:
            return data

    return None


def fetch_abstract(pmid: str, api_key: str = "") -> str:
    """Plain-text abstract for a PMID, used as the allowExtractionOnAbstract
    fallback when fetch_fulltext_pdf() finds no open-access PDF.

    Raises NcbiError (message safe to show to the curator) if PubMed doesn't
    know the PMID, or the record has no abstract (e.g. a letter or erratum).
    """
    global _has_api_key
    _has_api_key = bool(api_key)

    pmid = str(pmid).strip()
    if not pmid.isdigit():
        raise NcbiError("PMID must be numeric")

    try:
        _verify_pmid_exists(pmid, api_key)
        body = _request(
            f"{EUTILS}/efetch.fcgi",
            params=_eutils_params(
                {"db": "pubmed", "id": pmid, "rettype": "abstract", "retmode": "text"},
                api_key,
            ),
            timeout=20,
        )
    except NcbiError:
        raise
    except urllib.error.HTTPError as exc:
        raise NcbiError(f"PubMed request failed (HTTP {exc.code})") from exc
    except OSError as exc:
        raise NcbiError(f"could not reach PubMed: {exc}") from exc

    text = body.decode("utf-8", errors="ignore").strip()
    if not text:
        raise NcbiError(f"PMID {pmid} has no abstract on PubMed")
    return text


def fetch_fulltext_pdf(pmid: str, api_key: str = "", unpaywall_email: str = "") -> bytes:
    """Verify the PMID is real, then try every open-access source for its PDF.

    Raises NcbiError (message safe to show to the curator) if PubMed doesn't
    know the PMID, or if no source returned an actual PDF.
    """
    global _has_api_key
    _has_api_key = bool(api_key)

    pmid = str(pmid).strip()
    if not pmid.isdigit():
        raise NcbiError("PMID must be numeric")

    try:
        _verify_pmid_exists(pmid, api_key)
        pmc_id, doi = _get_article_ids(pmid, api_key)
    except NcbiError:
        raise
    except urllib.error.HTTPError as exc:
        raise NcbiError(f"PubMed request failed (HTTP {exc.code})") from exc
    except OSError as exc:
        raise NcbiError(f"could not reach PubMed: {exc}") from exc

    data = _find_pdf(pmid, pmc_id, doi, unpaywall_email)
    if data is None:
        sources = [s for s in (f"PMC{pmc_id}" if pmc_id else None, f"DOI:{doi}" if doi else None) if s]
        detail = f" (checked {', '.join(sources)})" if sources else ""
        raise NcbiError(
            f"no open-access full-text PDF could be found for PMID {pmid}{detail} — "
            "it is likely behind a paywall"
        )
    return data
