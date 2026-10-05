"""Endpoint handlers. Each: fn(ctx, req, params, body_bytes) -> None (writes response).

Project-scoped handlers are wrapped in @project_route and get an extra `proj`
argument: fn(ctx, proj, req, params, body). The project id travels in the PATH
rather than as server-side "current project" state — a curator with two projects
open in two tabs is an obvious thing to do, and a mutable global on a threaded
server would silently cross their work over.
"""
from __future__ import annotations

import dataclasses
import json
from urllib.parse import parse_qs, urlparse

from . import (
    config_loader,
    export,
    feature_sheet,
    features as feat,
    identity as identity_mod,
    importer,
    ncbi,
    papers as papers_mod,
    projects as projects_mod,
    responses,
    util,
)
from . import schema_builder
from .extraction import run_extraction


def _json_body(body_bytes):
    if not body_bytes:
        return {}
    return json.loads(body_bytes.decode("utf-8"))


def _query(req):
    return {k: v[0] for k, v in parse_qs(urlparse(req.path).query).items()}


def _limits(ctx) -> dict:
    """How many PMID fetches the "Add Paper(s)" modal may run at once. The real
    NCBI rate limit is enforced server-side in ncbi._request; this is only a
    responsiveness knob — browsers cap HTTP/1.1 at ~6 connections per origin, so
    stay under it or a batch starves /api/pdf and the static assets.
    Derived here (not in Config.public_dict) because only ctx.secrets knows
    whether a key is configured; the key itself never leaves Secrets."""
    return {"pmidFetchConcurrency": 4 if ctx.secrets.ncbi_api_key else 3}


def project_route(fn):
    """Resolve {pid} to a Project (metadata + parsed Config + its own store)."""

    def wrapped(ctx, req, params, body):
        proj = ctx.projects.get(params["pid"])
        if proj is None:
            return responses.send_error_json(
                req, 404, f"no project {params['pid']}"
            )
        return fn(ctx, proj, req, params, body)

    wrapped.__name__ = fn.__name__
    return wrapped


# --------------------------------------------------------------------------- #
# Global (no project)
# --------------------------------------------------------------------------- #
def h_config(ctx, req, params, body):
    responses.send_json(
        req,
        {
            "limits": _limits(ctx),
            "featureTypes": config_loader.FEATURE_TYPES,
            # A personal key (server/identity.py) works just as well as the
            # server's own .keys/env one — see h_extract, which prefers it.
            "extractionEnabled": bool(ctx.secrets.openrouter_api_key)
                or bool(identity_mod.get_openrouter_key(ctx.db)),
            "hasTemplate": ctx.template is not None,
        },
    )


# --------------------------------------------------------------------------- #
# The local curator's own identity (name, email, optional personal OpenRouter
# key) — see server/identity.py. Not project-scoped: one identity per running
# instance, asked for once by the home screen's first-run modal.
# --------------------------------------------------------------------------- #
def h_identity_get(ctx, req, params, body):
    responses.send_json(req, identity_mod.get_identity(ctx.db))


def h_identity_save(ctx, req, params, body):
    try:
        data = _json_body(body)
    except json.JSONDecodeError:
        return responses.send_error_json(req, 400, "invalid JSON")
    try:
        result = identity_mod.save_identity(
            ctx.db, data.get("name"), data.get("email"),
            openrouter_key=data.get("openrouterKey"),
            clear_key=bool(data.get("clearOpenrouterKey")),
        )
    except identity_mod.IdentityError as exc:
        return responses.send_error_json(req, 400, str(exc))
    responses.send_json(req, result)


def h_home_shell(ctx, req, params, body):
    """GET / — the project picker. A separate document from the curation app:
    index.html boots straight into the three-pane UI and cannot do that before it
    knows which project it is in."""
    responses.serve_static(req, ctx.static_dir, "/home.html")


def h_app_shell(ctx, req, params, body):
    """GET /p/<pid> — serve the curation SPA. The id is read back off the path by
    main.js; the project is not resolved here, so a bad id lands in the app and
    is redirected home with a message rather than 404-ing a blank page."""
    responses.serve_static(req, ctx.static_dir, "/index.html")


def h_template(ctx, req, params, body):
    """The starter feature set a new project can be seeded from.

    A separate endpoint rather than part of /api/config: it is a whole config
    document and only the creation flow needs it, so the home screen should not
    pay for it on every load.
    """
    if ctx.template is None:
        return responses.send_error_json(req, 404, "no starter template is loaded")
    doc = ctx.template.portable_dict()
    doc["annotaidProject"] = projects_mod.DOC_VERSION
    responses.send_json(req, doc)


def h_feature_template(ctx, req, params, body):
    """A fill-in-the-blanks CSV a project manager can build a feature set in,
    without touching JSON or the visual editor at all."""
    responses.send_text(
        req, feature_sheet.build_template_csv(), "text/csv; charset=utf-8",
        extra={"Content-Disposition": 'attachment; filename="annotaid_feature_template.csv"'},
    )


def h_parse_feature_sheet(ctx, req, params, body):
    """The other half: a filled-in copy of that template -> a draft features
    list, in the SAME shape the visual editor edits. The Features step swaps it
    in and re-renders the editor, so a spreadsheet is just another way to seed
    it — the manager reviews and can still tweak by hand before creating.

    200 + ok:false for a problem with the FILE's content (a bad type keyword, a
    row referencing an undeclared group) — that is the answer to "is this
    sheet usable", not a request failure. A body that isn't readable as text at
    all is the one case that is actually a bad request.
    """
    try:
        text = (body or b"").decode("utf-8-sig")
    except UnicodeDecodeError:
        return responses.send_error_json(req, 400, "that file isn't readable as text/CSV")
    try:
        parsed = feature_sheet.parse_sheet_csv(text)
    except (feature_sheet.SheetError, feat.FeatureError) as exc:
        return responses.send_json(req, {"ok": False, "error": str(exc)})
    responses.send_json(req, {"ok": True, "features": parsed})


def h_validate_config(ctx, req, params, body):
    """Validate a draft project config and preview the prompt it produces.

    The feature editor posts its draft here rather than reimplementing the rules
    in JavaScript: two implementations of one schema drift, and then the editor
    happily accepts a config the server rejects. One source of truth.

    An invalid draft is 200 with ok:false — being invalid is the ANSWER to the
    question asked, not an HTTP failure.
    """
    try:
        data = _json_body(body)
    except json.JSONDecodeError:
        return responses.send_error_json(req, 400, "invalid JSON")

    doc = {k: v for k, v in (data.get("config") or {}).items()
           if k in projects_mod.CONFIG_KEYS}
    try:
        cfg = config_loader.parse_config(doc, label="draft")
    except config_loader.ConfigError as exc:
        return responses.send_json(req, {"ok": False, "error": str(exc)})

    name = str(data.get("name") or "Untitled project").strip()
    description = str(data.get("description") or "").strip()
    prompt = cfg.default_prompt()
    preview = ""
    if prompt:
        preview = schema_builder.build_prompt(
            prompt["text"], cfg.features,
            projects_mod.prompt_context(name, description),
        )
    responses.send_json(req, {
        "ok": True,
        "paths": cfg.feature_names(),
        "groupCount": len(cfg.groups()),
        "promptPreview": preview,
    })


def h_projects_list(ctx, req, params, body):
    responses.send_json(req, {"projects": projects_mod.list_projects(ctx.db)})


def h_project_create(ctx, req, params, body):
    try:
        data = _json_body(body)
    except json.JSONDecodeError:
        return responses.send_error_json(req, 400, "invalid JSON")

    doc = data.get("config")
    if doc is None:
        # PLACEHOLDER seed. The real creation flow (define features, or import a
        # shared config document) is the next step; until then a new project
        # starts from the starter template so it is immediately usable.
        if ctx.template is None:
            return responses.send_error_json(
                req, 400,
                "no starter template is loaded — supply a project config to create from",
            )
        doc = ctx.template.portable_dict()

    ident = identity_mod.get_identity(ctx.db)
    created_by = {"name": ident["name"], "email": ident["email"]} if ident["set"] else None
    try:
        project = projects_mod.create_project(
            ctx.db, data.get("name"), data.get("description"), doc,
            created_by=created_by,
        )
    except projects_mod.ProjectError as exc:
        return responses.send_error_json(req, 400, str(exc))
    responses.send_json(req, project, 201)


# --------------------------------------------------------------------------- #
# Project metadata
# --------------------------------------------------------------------------- #
@project_route
def h_project_get(ctx, proj, req, params, body):
    """The portable document — this is what a manager hands to a curator."""
    doc = projects_mod.portable_document(ctx.db, proj.id)
    if doc is None:
        return responses.send_error_json(req, 404, f"no project {proj.id}")
    responses.send_json(req, doc)


@project_route
def h_project_update(ctx, proj, req, params, body):
    try:
        data = _json_body(body)
    except json.JSONDecodeError:
        return responses.send_error_json(req, 400, "invalid JSON")
    try:
        updated = projects_mod.update_project(
            ctx.db, proj.id,
            name=data.get("name"),
            description=data.get("description"),
            config_doc=data.get("config"),
        )
    except projects_mod.ProjectError as exc:
        return responses.send_error_json(req, 400, str(exc))
    responses.send_json(req, updated)


@project_route
def h_project_archive(ctx, proj, req, params, body):
    try:
        projects_mod.archive_project(ctx.db, proj.id)
    except projects_mod.ProjectError as exc:
        return responses.send_error_json(req, 404, str(exc))
    responses.send_json(req, {"ok": True})


@project_route
def h_project_config(ctx, proj, req, params, body):
    doc = proj.config.public_dict()
    doc["limits"] = _limits(ctx)
    doc["project"] = proj.meta()
    responses.send_json(req, doc)


# --------------------------------------------------------------------------- #
# Curation state
# --------------------------------------------------------------------------- #
@project_route
def h_state(ctx, proj, req, params, body):
    responses.send_json(
        req,
        {
            "papers": proj.store.list_papers(),
            "runs": proj.store.list_all_runs(),
            "ui": proj.store.load_state(),
        },
    )


@project_route
def h_save_state(ctx, proj, req, params, body):
    try:
        data = _json_body(body)
    except json.JSONDecodeError:
        return responses.send_error_json(req, 400, "invalid JSON")
    proj.store.save_state(data if isinstance(data, dict) else {})
    responses.send_json(req, {"ok": True})


@project_route
def h_papers_list(ctx, proj, req, params, body):
    responses.send_json(req, {"papers": proj.store.list_papers()})


@project_route
def h_upload(ctx, proj, req, params, body):
    if not body:
        return responses.send_error_json(req, 400, "empty upload")
    filename = req.headers.get("X-Filename", "upload.pdf")
    # Set when the curator reached the Upload tab from a failed "fetch by PMID"
    # row — they already told us which paper this PDF is for, so prefill it
    # instead of guessing from the filename. Still only a *suggestion*.
    suggested = req.headers.get("X-Suggested-Pmid", "")
    try:
        paper = papers_mod.store_upload(proj.store, body, filename, suggested)
    except papers_mod.PaperError as exc:
        return responses.send_error_json(req, 400, str(exc))
    responses.send_json(req, paper, 201)


@project_route
def h_fetch_pmid(ctx, proj, req, params, body):
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
        # allowExtractionOnAbstract: no open-access full text, but the project
        # has opted into curating from the abstract when that happens.
        if not proj.config.allow_extraction_on_abstract:
            return responses.send_error_json(req, 502, str(exc))
        try:
            abstract_text = ncbi.fetch_abstract(pmid, ctx.secrets.ncbi_api_key)
        except ncbi.NcbiError as abstract_exc:
            return responses.send_error_json(
                req, 502,
                f"{exc} — and no abstract could be fetched either ({abstract_exc})",
            )
        try:
            paper = papers_mod.store_and_confirm_from_abstract(proj.store, pmid, abstract_text)
        except papers_mod.PaperError as paper_exc:
            return responses.send_error_json(req, 409, str(paper_exc))
        return responses.send_json(req, paper, 201)
    try:
        paper = papers_mod.store_and_confirm_from_pmid(proj.store, pdf_bytes, pmid)
    except papers_mod.PaperError as exc:
        return responses.send_error_json(req, 409, str(exc))
    responses.send_json(req, paper, 201)


@project_route
def h_identity(ctx, proj, req, params, body):
    """Resolve a paper's identity: confirm a PMID, or record that it has none
    (optionally with a DOI instead). Was h_confirm_pmid."""
    try:
        data = _json_body(body)
    except json.JSONDecodeError:
        return responses.send_error_json(req, 400, "invalid JSON")

    uid = params["uid"]
    try:
        if data.get("status") == "none" or data.get("noPmid"):
            # Project policy, set by whoever defined the project. Enforced here
            # as well as hidden in the UI: a policy that only exists in the
            # client is not a policy.
            if not proj.config.allow_papers_without_pmid:
                return responses.send_error_json(
                    req, 400,
                    "this project requires a confirmed PubMed ID for every paper",
                )
            paper = papers_mod.mark_no_pmid(proj.store, uid, data.get("doi", ""))
        else:
            paper = papers_mod.confirm_pmid(
                proj.store, uid, data.get("pmid", ""), data.get("source", "manual")
            )
    except papers_mod.PaperError as exc:
        msg = str(exc)
        status = 409 if "already assigned" in msg else 400
        return responses.send_error_json(req, status, msg)
    responses.send_json(req, paper)


@project_route
def h_save_group_items(ctx, proj, req, params, body):
    """The curator's own declared row identity for the project's one repeating
    group (e.g. which variants/haplotypes this paper reports on) — set here,
    BEFORE extraction, never proposed by the AI. See server/extraction.py."""
    try:
        data = _json_body(body)
    except json.JSONDecodeError:
        return responses.send_error_json(req, 400, "invalid JSON")
    uid = proj.store.uid_for(params["uid"])
    if not uid:
        return responses.send_error_json(req, 404, f"no paper {params['uid']}")
    items = data.get("items")
    if not isinstance(items, list):
        return responses.send_error_json(req, 400, "items must be an array")
    saved = proj.store.save_group_items(uid, items)
    responses.send_json(req, {"items": saved})


@project_route
def h_pdf(ctx, proj, req, params, body):
    path = proj.store.resolve_pdf(params["id"])
    if not path:
        return responses.send_error_json(req, 404, "no PDF")
    with open(path, "rb") as fh:
        responses.send_bytes(req, fh.read(), "application/pdf")


@project_route
def h_extract(ctx, proj, req, params, body):
    try:
        data = _json_body(body)
    except json.JSONDecodeError:
        return responses.send_error_json(req, 400, "invalid JSON")
    # Keyed on uid: a paper may legitimately have no PMID.
    ident = str(data.get("uid") or data.get("pmid") or "").strip()
    paper = proj.store.load_paper_by_ident(ident)
    if paper is None:
        return responses.send_error_json(req, 404, f"no paper {ident} in this project")
    models = data.get("models") or []
    if not models:
        return responses.send_error_json(req, 400, "no models specified")
    # A personal key (server/identity.py) takes priority over the server's own
    # .keys/env one — it's the more specific credential, and it's how someone
    # without access to server-level secrets can still run extraction.
    secrets = ctx.secrets
    user_key = identity_mod.get_openrouter_key(ctx.db)
    if user_key:
        secrets = dataclasses.replace(secrets, openrouter_api_key=user_key)
    try:
        runs = run_extraction(
            proj.config,
            secrets,
            proj.store,
            paper,
            models,
            data.get("promptId"),
            data.get("parseEngine"),
            force=bool(data.get("force")),
            context=proj.prompt_context(),
        )
    except ValueError as exc:
        return responses.send_error_json(req, 400, str(exc))
    responses.send_json(req, {"runs": runs})


@project_route
def h_import(ctx, proj, req, params, body):
    try:
        data = _json_body(body)
    except json.JSONDecodeError:
        return responses.send_error_json(req, 400, "invalid JSON")
    items = data.get("items") or []
    summary = importer.import_items(proj.store, proj.config, items)
    responses.send_json(req, summary)


@project_route
def h_runs(ctx, proj, req, params, body):
    uid = proj.store.uid_for(params["ident"])
    if not uid:
        return responses.send_error_json(req, 404, f"no paper {params['ident']}")
    responses.send_json(req, {"runs": proj.store.list_runs(uid)})


@project_route
def h_save_run(ctx, proj, req, params, body):
    try:
        data = _json_body(body)
    except json.JSONDecodeError:
        return responses.send_error_json(req, 400, "invalid JSON")
    model_id = data.get("modelId")
    if not model_id:
        return responses.send_error_json(req, 400, "modelId required")
    uid = proj.store.uid_for(params["ident"])
    if not uid:
        return responses.send_error_json(req, 404, f"no paper {params['ident']}")
    try:
        run = proj.store.merge_run_edits(
            uid, model_id,
            data.get("features") or {},
            data.get("groups") or {},
            # The store needs a declared type and an empty value to create a
            # curator-added row, but must not import the config to get them.
            feat.group_cell_templates(proj.config.features),
        )
    except KeyError as exc:
        return responses.send_error_json(req, 404, str(exc))
    # The merged run comes back because the SERVER assigns row ids and origins.
    # A curator-added row is sent with rowId:null; without adopting the reply the
    # client would send it as new again on the next save and duplicate it.
    responses.send_json(req, {"ok": True, "savedAt": util.iso_now(), "run": run})


@project_route
def h_project_config_download(ctx, proj, req, params, body):
    """The project card's "download config file" menu item — the exact
    portable document (server/projects.portable_document) forced to a file
    download, so a project manager can hand it to a curator or another
    manager without touching the New Project stepper's import route."""
    doc = projects_mod.portable_document(ctx.db, proj.id)
    if doc is None:
        return responses.send_error_json(req, 404, f"no project {proj.id}")
    slug = util.slugify(proj.name) or "project"
    responses.send_text(
        req, json.dumps(doc, indent=2, ensure_ascii=False),
        "application/json; charset=utf-8",
        extra={"Content-Disposition":
               f'attachment; filename="annotaid_{slug}_config.json"'},
    )


@project_route
def h_export(ctx, proj, req, params, body):
    fmt = _query(req).get("format", "json")
    slug = util.slugify(proj.name) or "project"
    if fmt == "csv":
        text = export.export_csv(proj.store, proj.config)
        return responses.send_text(
            req, text, "text/csv; charset=utf-8",
            extra={"Content-Disposition":
                   f'attachment; filename="annotaid_{slug}.csv"'},
        )
    doc = export.export_audit_json(proj.store, proj.config, proj)
    doc["generatedAt"] = util.iso_now()
    responses.send_text(
        req, json.dumps(doc, indent=2, ensure_ascii=False),
        "application/json; charset=utf-8",
        extra={"Content-Disposition":
               f'attachment; filename="annotaid_{slug}_audit.json"'},
    )


# --------------------------------------------------------------------------- #
def register(router):
    # global
    router.add("GET", "/api/config", h_config)
    router.add("GET", "/api/projects", h_projects_list)
    router.add("POST", "/api/projects", h_project_create)
    router.add("POST", "/api/validate-config", h_validate_config)
    router.add("GET", "/api/template", h_template)
    router.add("GET", "/api/feature-template", h_feature_template)
    router.add("POST", "/api/parse-feature-sheet", h_parse_feature_sheet)
    router.add("GET", "/api/identity", h_identity_get)
    router.add("POST", "/api/identity", h_identity_save)

    # project metadata
    router.add("GET", "/api/projects/{pid}", h_project_get)
    router.add("POST", "/api/projects/{pid}", h_project_update)
    router.add("POST", "/api/projects/{pid}/archive", h_project_archive)
    router.add("GET", "/api/projects/{pid}/config", h_project_config)
    router.add("GET", "/api/projects/{pid}/config-file", h_project_config_download)

    # curation
    router.add("GET", "/api/projects/{pid}/state", h_state)
    router.add("POST", "/api/projects/{pid}/state", h_save_state)
    router.add("GET", "/api/projects/{pid}/papers", h_papers_list)
    router.add("POST", "/api/projects/{pid}/papers", h_upload)
    router.add("POST", "/api/projects/{pid}/papers/fetch-by-pmid", h_fetch_pmid)
    router.add("POST", "/api/projects/{pid}/papers/{uid}/identity", h_identity)
    router.add("POST", "/api/projects/{pid}/papers/{uid}/group-items", h_save_group_items)
    router.add("GET", "/api/projects/{pid}/pdf/{id}", h_pdf)
    router.add("POST", "/api/projects/{pid}/extract", h_extract)
    router.add("POST", "/api/projects/{pid}/import", h_import)
    router.add("GET", "/api/projects/{pid}/runs/{ident}", h_runs)
    router.add("POST", "/api/projects/{pid}/runs/{ident}", h_save_run)
    router.add("GET", "/api/projects/{pid}/export", h_export)

    # page shells
    router.add("GET", "/", h_home_shell)
    router.add("GET", "/p/{pid}", h_app_shell)
