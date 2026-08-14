"""Endpoint handlers. Each: fn(ctx, req, params, body_bytes) -> None (writes response)."""
from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

from . import export, importer, ncbi, papers as papers_mod, responses, util
from .extraction import run_extraction


def _json_body(body_bytes):
    if not body_bytes:
        return {}
    return json.loads(body_bytes.decode("utf-8"))


def _query(req):
    return {k: v[0] for k, v in parse_qs(urlparse(req.path).query).items()}


# --------------------------------------------------------------------------- #
def h_config(ctx, req, params, body):
    doc = ctx.config.public_dict()
    # How many PMID fetches the "Add Paper(s)" modal may run at once. The real
    # NCBI rate limit is enforced server-side in ncbi._request; this is only a
    # responsiveness knob — browsers cap HTTP/1.1 at ~6 connections per origin,
    # so stay under it or a batch starves /api/pdf and the static assets.
    # Derived here (not in Config.public_dict) because only ctx.secrets knows
    # whether a key is configured; the key itself never leaves Secrets.
    doc["limits"] = {"pmidFetchConcurrency": 4 if ctx.secrets.ncbi_api_key else 3}
    responses.send_json(req, doc)


def h_state(ctx, req, params, body):
    responses.send_json(
        req,
        {
            "papers": ctx.store.list_papers(),
            "runs": ctx.store.list_all_runs(),
            "ui": ctx.store.load_state(),
        },
    )


def h_papers_list(ctx, req, params, body):
    responses.send_json(req, {"papers": ctx.store.list_papers()})


def h_upload(ctx, req, params, body):
    if not body:
        return responses.send_error_json(req, 400, "empty upload")
    filename = req.headers.get("X-Filename", "upload.pdf")
    # Set when the curator reached the Upload tab from a failed "fetch by PMID"
    # row — they already told us which paper this PDF is for, so prefill it
    # instead of guessing from the filename. Still only a *suggestion*.
    suggested = req.headers.get("X-Suggested-Pmid", "")
    try:
        paper = papers_mod.store_upload(ctx.store, body, filename, suggested)
    except papers_mod.PaperError as exc:
        return responses.send_error_json(req, 400, str(exc))
    responses.send_json(req, paper, 201)


def h_fetch_pmid(ctx, req, params, body):
    try:
        data = _json_body(body)
    except json.JSONDecodeError:
        return responses.send_error_json(req, 400, "invalid JSON")
    pmid = str(data.get("pmid", "")).strip()
    if not pmid.isdigit():
        return responses.send_error_json(req, 400, "PMID must be numeric")
    try:
        pdf_bytes = ncbi.fetch_fulltext_pdf(
            pmid, ctx.secrets.ncbi_api_key, ctx.secrets.unpaywall_email
        )
    except ncbi.NcbiError as exc:
        return responses.send_error_json(req, 502, str(exc))
    try:
        paper = papers_mod.store_and_confirm_from_pmid(ctx.store, pdf_bytes, pmid)
    except papers_mod.PaperError as exc:
        return responses.send_error_json(req, 409, str(exc))
    responses.send_json(req, paper, 201)


def h_confirm_pmid(ctx, req, params, body):
    try:
        data = _json_body(body)
    except json.JSONDecodeError:
        return responses.send_error_json(req, 400, "invalid JSON")
    try:
        paper = papers_mod.confirm_pmid(
            ctx.store, params["uid"], data.get("pmid", ""), data.get("source", "manual")
        )
    except papers_mod.PaperError as exc:
        msg = str(exc)
        status = 409 if "already assigned" in msg else 400
        return responses.send_error_json(req, status, msg)
    responses.send_json(req, paper)


def h_pdf(ctx, req, params, body):
    path = ctx.store.resolve_pdf(params["id"])
    if not path:
        return responses.send_error_json(req, 404, "no PDF")
    with open(path, "rb") as fh:
        responses.send_bytes(req, fh.read(), "application/pdf")


def h_extract(ctx, req, params, body):
    try:
        data = _json_body(body)
    except json.JSONDecodeError:
        return responses.send_error_json(req, 400, "invalid JSON")
    pmid = str(data.get("pmid", "")).strip()
    paper = ctx.store.load_paper_by_ident(pmid)
    if paper is None or not paper.get("pmid"):
        return responses.send_error_json(req, 404, f"no confirmed paper with PMID {pmid}")
    models = data.get("models") or []
    if not models:
        return responses.send_error_json(req, 400, "no models specified")
    try:
        runs = run_extraction(
            ctx.config,
            ctx.secrets,
            ctx.store,
            paper,
            models,
            data.get("promptId"),
            data.get("parseEngine"),
            force=bool(data.get("force")),
        )
    except ValueError as exc:
        return responses.send_error_json(req, 400, str(exc))
    responses.send_json(req, {"runs": runs})


def h_import(ctx, req, params, body):
    try:
        data = _json_body(body)
    except json.JSONDecodeError:
        return responses.send_error_json(req, 400, "invalid JSON")
    items = data.get("items") or []
    summary = importer.import_items(ctx.store, ctx.config, items)
    responses.send_json(req, summary)


def h_runs(ctx, req, params, body):
    responses.send_json(req, {"runs": ctx.store.list_runs(params["pmid"])})


def h_save_run(ctx, req, params, body):
    try:
        data = _json_body(body)
    except json.JSONDecodeError:
        return responses.send_error_json(req, 400, "invalid JSON")
    model_id = data.get("modelId")
    if not model_id:
        return responses.send_error_json(req, 400, "modelId required")
    try:
        ctx.store.merge_run_edits(params["pmid"], model_id, data.get("features", {}))
    except KeyError as exc:
        return responses.send_error_json(req, 404, str(exc))
    responses.send_json(req, {"ok": True, "savedAt": util.iso_now()})


def h_save_state(ctx, req, params, body):
    try:
        data = _json_body(body)
    except json.JSONDecodeError:
        return responses.send_error_json(req, 400, "invalid JSON")
    ctx.store.save_state(data if isinstance(data, dict) else {})
    responses.send_json(req, {"ok": True})


def h_export(ctx, req, params, body):
    fmt = _query(req).get("format", "json")
    if fmt == "csv":
        text = export.export_csv(ctx.store, ctx.config)
        return responses.send_text(
            req, text, "text/csv; charset=utf-8",
            extra={"Content-Disposition": 'attachment; filename="annotaid_export.csv"'},
        )
    from . import util

    doc = export.export_audit_json(ctx.store, ctx.config)
    doc["generatedAt"] = util.iso_now()
    responses.send_text(
        req, json.dumps(doc, indent=2, ensure_ascii=False), "application/json; charset=utf-8",
        extra={"Content-Disposition": 'attachment; filename="annotaid_audit.json"'},
    )


def register(router):
    router.add("GET", "/api/config", h_config)
    router.add("GET", "/api/state", h_state)
    router.add("POST", "/api/state", h_save_state)
    router.add("GET", "/api/papers", h_papers_list)
    router.add("POST", "/api/papers", h_upload)
    router.add("POST", "/api/papers/fetch-by-pmid", h_fetch_pmid)
    router.add("POST", "/api/papers/{uid}/pmid", h_confirm_pmid)
    router.add("GET", "/api/pdf/{id}", h_pdf)
    router.add("POST", "/api/extract", h_extract)
    router.add("POST", "/api/import", h_import)
    router.add("GET", "/api/runs/{pmid}", h_runs)
    router.add("POST", "/api/runs/{pmid}", h_save_run)
    router.add("GET", "/api/export", h_export)
