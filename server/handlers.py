"""Endpoint handlers. Each: fn(ctx, req, params, body_bytes) -> None (writes response).

By the time a handler runs, app._dispatch has already made sure there is a
logged-in, active user (`req.user`) unless the route is @public.

Project-scoped handlers are wrapped in @project_route(min_role) and get an extra
`proj` argument: fn(ctx, proj, req, params, body). The wrapper checks the user's
role in THAT project on every request (M2, NFR-2): not a member -> 404, exactly
as if the project did not exist; a curator on a manager route -> 403. The
project id travels in the PATH rather than as server-side "current project"
state — someone with two projects open in two tabs is an obvious thing to do.

Global login / sign-up / admin routes live in server/auth_handlers.py.
"""
from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

from . import (
    auth,
    auth_handlers,
    config_loader,
    export,
    feature_sheet,
    features as feat,
    importer,
    jobs as jobs_mod,
    membership,
    ncbi,
    papers as papers_mod,
    projects as projects_mod,
    responses,
    util,
    workflow,
)
from . import schema_builder
from .access import account_roles, copy_markers, superadmin_only
from .extraction import run_extraction
from .net import site_url

# Decision 6: starting extraction spends money on the one shared OpenRouter
# key, so it needs at least this project role. One line to change.
EXTRACT_MIN_ROLE = "manager"


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


def _extraction_enabled(ctx) -> bool:
    # Only the server's own key exists now (NFR-7): there is no personal key.
    return bool(ctx.secrets.openrouter_api_key)


def project_route(min_role: str = "curator"):
    """Resolve {pid} to a Project and check the caller's role in it."""

    def deco(fn):
        def wrapped(ctx, req, params, body):
            pid = params["pid"]
            role = membership.project_role(ctx.db, req.user, pid)
            proj = ctx.projects.get(pid) if role else None
            if proj is None:
                # Same answer for "doesn't exist" and "not yours": an outsider
                # can't even confirm the project exists.
                return responses.send_error_json(req, 404, f"no project {pid}")
            if not membership.at_least(role, min_role):
                return responses.send_error_json(
                    req, 403, "only this project's managers can do that"
                )
            req.project_role = role
            return fn(ctx, proj, req, params, body)

        wrapped.__name__ = fn.__name__
        return copy_markers(fn, wrapped)

    return deco


def _permissions(ctx, role) -> dict:
    """What the UI should offer. A convenience only — every one of these is
    enforced again on the server when the action is attempted."""
    manager = membership.at_least(role, "manager")
    return {
        "manage": manager,
        "addPapers": manager,
        "extract": membership.at_least(role, EXTRACT_MIN_ROLE) and _extraction_enabled(ctx),
        "export": manager,
        "assign": manager,
    }


def _workflow_error(req, exc):
    status = getattr(exc, "status", 400)
    return responses.send_error_json(req, status, str(exc))


# --------------------------------------------------------------------------- #
# Global (no project)
# --------------------------------------------------------------------------- #
def h_config(ctx, req, params, body):
    responses.send_json(
        req,
        {
            "limits": _limits(ctx),
            "featureTypes": config_loader.FEATURE_TYPES,
            "extractionEnabled": _extraction_enabled(ctx),
            "hasTemplate": ctx.template is not None,
        },
    )


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
    """Only the projects the caller belongs to; a superadmin sees all (M19)."""
    user = req.user
    if user["account_role"] == "superadmin":
        projects = projects_mod.list_projects(ctx.db)
        roles = {}
    else:
        mine = membership.memberships(ctx.db, user["id"])
        roles = {m["projectId"]: m["role"] for m in mine}
        projects = projects_mod.list_projects(ctx.db, only_ids=set(roles))
    for p in projects:
        p["myRole"] = roles.get(p["id"], "manager")
    responses.send_json(req, {
        "projects": projects,
        "canCreate": user["account_role"] in ("superadmin", "manager"),
    })


@account_roles("superadmin", "manager")
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

    user = req.user
    try:
        project = projects_mod.create_project(
            ctx.db, data.get("name"), data.get("description"), doc,
            created_by={"id": user["id"], "name": user["name"], "email": user["email"]},
        )
    except projects_mod.ProjectError as exc:
        return responses.send_error_json(req, 400, str(exc))
    responses.send_json(req, project, 201)


# --------------------------------------------------------------------------- #
# Project metadata
# --------------------------------------------------------------------------- #
@project_route("manager")
def h_project_get(ctx, proj, req, params, body):
    """The portable document — this is what a manager hands to another manager."""
    doc = projects_mod.portable_document(ctx.db, proj.id)
    if doc is None:
        return responses.send_error_json(req, 404, f"no project {proj.id}")
    responses.send_json(req, doc)


@project_route("manager")
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


@superadmin_only
@project_route("manager")
def h_project_archive(ctx, proj, req, params, body):
    try:
        projects_mod.archive_project(ctx.db, proj.id)
    except projects_mod.ProjectError as exc:
        return responses.send_error_json(req, 404, str(exc))
    responses.send_json(req, {"ok": True})


@project_route()
def h_project_config(ctx, proj, req, params, body):
    doc = proj.config.public_dict()
    doc["limits"] = _limits(ctx)
    doc["project"] = proj.meta()
    doc["myRole"] = req.project_role
    doc["permissions"] = _permissions(ctx, req.project_role)
    responses.send_json(req, doc)


# --------------------------------------------------------------------------- #
# Curation state
# --------------------------------------------------------------------------- #
@project_route()
def h_state(ctx, proj, req, params, body):
    user = req.user
    responses.send_json(
        req,
        {
            "papers": proj.store.list_papers(),
            "runs": proj.store.list_all_runs(),
            "ui": proj.store.load_state(user["id"]),
            "me": {"id": user["id"], "name": user["name"], "role": req.project_role},
            "permissions": _permissions(ctx, req.project_role),
            # id -> name for attribution ("edited by ...") and assignees. Names
            # only; deactivated accounts included, so old edits stay named.
            "users": auth.names_by_id(ctx.db),
            "members": membership.list_members(ctx.db, proj.id),
        },
    )


@project_route()
def h_save_state(ctx, proj, req, params, body):
    try:
        data = _json_body(body)
    except json.JSONDecodeError:
        return responses.send_error_json(req, 400, "invalid JSON")
    proj.store.save_state(req.user["id"], data if isinstance(data, dict) else {})
    responses.send_json(req, {"ok": True})


@project_route()
def h_papers_list(ctx, proj, req, params, body):
    responses.send_json(req, {"papers": proj.store.list_papers()})


@project_route("manager")
def h_upload(ctx, proj, req, params, body):
    if not body:
        return responses.send_error_json(req, 400, "empty upload")
    filename = req.headers.get("X-Filename", "upload.pdf")
    # Set when the manager reached the Upload tab from a failed "fetch by PMID"
    # row — they already told us which paper this PDF is for, so prefill it
    # instead of guessing from the filename. Still only a *suggestion*.
    suggested = req.headers.get("X-Suggested-Pmid", "")
    try:
        paper = papers_mod.store_upload(proj.store, body, filename, suggested)
    except papers_mod.PaperError as exc:
        return responses.send_error_json(req, 400, str(exc))
    proj.store.set_added_by(paper["uid"], req.user["id"])
    responses.send_json(req, proj.store.load_paper(paper["uid"]), 201)


@project_route("manager")
def h_fetch_pmid(ctx, proj, req, params, body):
    """Synchronous single-PMID fetch. Superseded by the import job (POST .../jobs);
    kept until the browser has moved over."""
    try:
        data = _json_body(body)
    except json.JSONDecodeError:
        return responses.send_error_json(req, 400, "invalid JSON")
    pmid = str(data.get("pmid", "")).strip()
    if not pmid.isdigit():
        return responses.send_error_json(req, 400, "PMID must be numeric")
    job = {"created_by": req.user["id"], "params": {}}
    state, outcome, result, error = jobs_mod.run_import_item(ctx.jobs, proj, job, pmid)
    if state == "done" and result and outcome != "duplicate":
        return responses.send_json(req, proj.store.load_paper(result["uid"]), 201)
    if outcome == "duplicate":
        return responses.send_error_json(req, 409, error or f"PMID {pmid} is already in this project")
    return responses.send_error_json(req, 502, error or "fetch failed")


@project_route()
def h_identity(ctx, proj, req, params, body):
    """Resolve a paper's identity: confirm a PMID, or record that it has none
    (optionally with a DOI instead). Set-up, not curation: a project manager may
    always do it; otherwise only the paper's assignee."""
    try:
        data = _json_body(body)
    except json.JSONDecodeError:
        return responses.send_error_json(req, 400, "invalid JSON")

    uid = params["uid"]
    blocked = workflow.setup_block_reason(ctx.db, req.user, req.project_role, proj.id, uid)
    if blocked:
        return responses.send_error_json(req, 403, blocked)
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


@project_route()
def h_save_group_items(ctx, proj, req, params, body):
    """The declared row identity for the project's one repeating group (e.g.
    which variants this paper reports on) — set BEFORE extraction, never
    proposed by the AI. Set-up, like h_identity: managers, or the assignee."""
    try:
        data = _json_body(body)
    except json.JSONDecodeError:
        return responses.send_error_json(req, 400, "invalid JSON")
    uid = proj.store.uid_for(params["uid"])
    if not uid:
        return responses.send_error_json(req, 404, f"no paper {params['uid']}")
    blocked = workflow.setup_block_reason(ctx.db, req.user, req.project_role, proj.id, uid)
    if blocked:
        return responses.send_error_json(req, 403, blocked)
    items = data.get("items")
    if not isinstance(items, list):
        return responses.send_error_json(req, 400, "items must be an array")
    saved = proj.store.save_group_items(uid, items)
    responses.send_json(req, {"items": saved})


@project_route()
def h_pdf(ctx, proj, req, params, body):
    """PDFs only ever leave through here: logged in AND on the project (NFR-6)."""
    path = proj.store.resolve_pdf(params["id"])
    if not path:
        return responses.send_error_json(req, 404, "no PDF")
    with open(path, "rb") as fh:
        responses.send_bytes(req, fh.read(), "application/pdf",
                             extra={"Cache-Control": "private, max-age=3600"})


@project_route(EXTRACT_MIN_ROLE)
def h_extract(ctx, proj, req, params, body):
    """Synchronous single-paper extraction. Superseded by the extraction job
    (POST .../jobs); kept until the browser has moved over."""
    try:
        data = _json_body(body)
    except json.JSONDecodeError:
        return responses.send_error_json(req, 400, "invalid JSON")
    if not _extraction_enabled(ctx):
        return responses.send_error_json(req, 400, "extraction is not configured on this server")
    ident = str(data.get("uid") or data.get("pmid") or "").strip()
    paper = proj.store.load_paper_by_ident(ident)
    if paper is None:
        return responses.send_error_json(req, 404, f"no paper {ident} in this project")
    models = data.get("models") or []
    if not models:
        return responses.send_error_json(req, 400, "no models specified")
    try:
        runs = run_extraction(
            proj.config, ctx.secrets, proj.store, paper, models,
            data.get("promptId"), data.get("parseEngine"),
            force=bool(data.get("force")), context=proj.prompt_context(),
            requested_by=req.user["id"],
        )
    except ValueError as exc:
        return responses.send_error_json(req, 400, str(exc))
    responses.send_json(req, {"runs": runs})


@project_route("manager")
def h_import(ctx, proj, req, params, body):
    try:
        data = _json_body(body)
    except json.JSONDecodeError:
        return responses.send_error_json(req, 400, "invalid JSON")
    items = data.get("items") or []
    summary = importer.import_items(proj.store, proj.config, items)
    responses.send_json(req, summary)


@project_route()
def h_runs(ctx, proj, req, params, body):
    uid = proj.store.uid_for(params["ident"])
    if not uid:
        return responses.send_error_json(req, 404, f"no paper {params['ident']}")
    responses.send_json(req, {"runs": proj.store.list_runs(uid)})


@project_route()
def h_save_run(ctx, proj, req, params, body):
    """Autosave of curated values. Only the paper's assignee, while it is in
    progress (M7) — enforced here, whatever the UI shows."""
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
    blocked = workflow.edit_block_reason(ctx.db, req.user, proj.id, uid)
    if blocked:
        return responses.send_error_json(req, 403, blocked)
    try:
        run = proj.store.merge_run_edits(
            uid, model_id,
            data.get("features") or {},
            data.get("groups") or {},
            # The store needs a declared type and an empty value to create a
            # curator-added row, but must not import the config to get them.
            feat.group_cell_templates(proj.config.features),
            user_id=req.user["id"],
        )
    except KeyError as exc:
        return responses.send_error_json(req, 404, str(exc))
    # The merged run comes back because the SERVER assigns row ids and origins.
    # A curator-added row is sent with rowId:null; without adopting the reply the
    # client would send it as new again on the next save and duplicate it.
    responses.send_json(req, {"ok": True, "savedAt": util.iso_now(), "run": run})


@project_route("manager")
def h_project_config_download(ctx, proj, req, params, body):
    """The project card's "download config file" menu item — the exact
    portable document (server/projects.portable_document) forced to a file
    download, so a project manager can hand it to another manager without
    touching the New Project stepper's import route."""
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


@project_route("manager")
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
    # Attribution ids -> who they are (M8). Never includes password data.
    doc["users"] = {
        r["id"]: {"name": r["name"], "email": r["email"]}
        for r in ctx.db.query("SELECT id, name, email FROM users")
    }
    doc["events"] = [
        {"at": r["at"], "uid": r["uid"], "actor": r["actor"], "action": r["action"],
         "detail": json.loads(r["detail_json"] or "{}")}
        for r in ctx.db.query("SELECT * FROM events WHERE project_id=? ORDER BY id", (proj.id,))
    ]
    responses.send_text(
        req, json.dumps(doc, indent=2, ensure_ascii=False),
        "application/json; charset=utf-8",
        extra={"Content-Disposition":
               f'attachment; filename="annotaid_{slug}_audit.json"'},
    )


# --------------------------------------------------------------------------- #
# Workflow: claim / assign / submit / exclude / reopen (M3, M7, M9, M17)
# --------------------------------------------------------------------------- #
def _paper_uid(proj, params):
    return proj.store.uid_for(params["uid"])


def _reply_paper(req, proj, uid):
    responses.send_json(req, {"paper": proj.store.load_paper(uid)})


@project_route()
def h_claim(ctx, proj, req, params, body):
    uid = _paper_uid(proj, params)
    if not uid:
        return responses.send_error_json(req, 404, f"no paper {params['uid']}")
    try:
        workflow.claim(ctx.db, req.user, proj.id, uid)
    except workflow.WorkflowError as exc:
        return _workflow_error(req, exc)
    _reply_paper(req, proj, uid)


@project_route("manager")
def h_assign(ctx, proj, req, params, body):
    try:
        data = _json_body(body)
    except json.JSONDecodeError:
        return responses.send_error_json(req, 400, "invalid JSON")
    uid = _paper_uid(proj, params)
    if not uid:
        return responses.send_error_json(req, 404, f"no paper {params['uid']}")
    try:
        workflow.assign(ctx.db, req.user, proj.id, uid, data.get("assigneeId") or None)
    except workflow.WorkflowError as exc:
        return _workflow_error(req, exc)
    _reply_paper(req, proj, uid)


@project_route()
def h_submit(ctx, proj, req, params, body):
    uid = _paper_uid(proj, params)
    if not uid:
        return responses.send_error_json(req, 404, f"no paper {params['uid']}")
    try:
        workflow.submit(ctx.db, req.user, proj.id, uid)
    except workflow.WorkflowError as exc:
        return _workflow_error(req, exc)
    _reply_paper(req, proj, uid)


@project_route()
def h_exclude(ctx, proj, req, params, body):
    try:
        data = _json_body(body)
    except json.JSONDecodeError:
        return responses.send_error_json(req, 400, "invalid JSON")
    uid = _paper_uid(proj, params)
    if not uid:
        return responses.send_error_json(req, 404, f"no paper {params['uid']}")
    try:
        workflow.exclude(ctx.db, req.user, proj.id, uid, data.get("status"), data.get("reason"))
    except workflow.WorkflowError as exc:
        return _workflow_error(req, exc)
    _reply_paper(req, proj, uid)


@project_route("manager")
def h_reopen(ctx, proj, req, params, body):
    try:
        data = _json_body(body)
    except json.JSONDecodeError:
        return responses.send_error_json(req, 400, "invalid JSON")
    uid = _paper_uid(proj, params)
    if not uid:
        return responses.send_error_json(req, 404, f"no paper {params['uid']}")
    try:
        workflow.reopen(ctx.db, req.user, proj.id, uid, data.get("reason"))
    except workflow.WorkflowError as exc:
        return _workflow_error(req, exc)
    _reply_paper(req, proj, uid)


@project_route()
def h_paper_history(ctx, proj, req, params, body):
    uid = _paper_uid(proj, params)
    if not uid:
        return responses.send_error_json(req, 404, f"no paper {params['uid']}")
    responses.send_json(req, {"events": workflow.history(ctx.db, proj.id, uid)})


# --------------------------------------------------------------------------- #
# Background jobs (M14, M16, M21)
# --------------------------------------------------------------------------- #
@project_route()
def h_jobs_create(ctx, proj, req, params, body):
    try:
        data = _json_body(body)
    except json.JSONDecodeError:
        return responses.send_error_json(req, 400, "invalid JSON")
    kind = data.get("kind")
    refs = data.get("refs") or []
    if not isinstance(refs, list):
        return responses.send_error_json(req, 400, "refs must be a list")
    role = req.project_role
    params_ = {}
    if kind == "import":
        if not membership.at_least(role, "manager"):
            return responses.send_error_json(req, 403, "only this project's managers can add papers")
        bad = [r for r in refs if not str(r).strip().isdigit()]
        if bad:
            return responses.send_error_json(req, 400, f"not a PMID: {', '.join(map(str, bad[:5]))}")
    elif kind == "extract":
        if not membership.at_least(role, EXTRACT_MIN_ROLE):
            return responses.send_error_json(req, 403, "only this project's managers can run extraction")
        if not _extraction_enabled(ctx):
            return responses.send_error_json(req, 400, "extraction is not configured on this server")
        models = data.get("models") or []
        if not models:
            return responses.send_error_json(req, 400, "choose at least one model")
        unknown = [m for m in models if proj.config.model(m) is None]
        if unknown:
            return responses.send_error_json(req, 400, f"unknown model(s): {', '.join(unknown)}")
        uids = []
        for r in refs:
            uid = proj.store.uid_for(str(r))
            if not uid:
                return responses.send_error_json(req, 400, f"no paper {r} in this project")
            uids.append(uid)
        refs = uids
        params_ = {"models": models, "promptId": data.get("promptId"),
                   "parseEngine": data.get("parseEngine"), "force": bool(data.get("force"))}
    else:
        return responses.send_error_json(req, 400, "kind must be 'import' or 'extract'")
    try:
        job = ctx.jobs.create(proj.id, kind, refs, params_, req.user["id"])
    except jobs_mod.JobError as exc:
        return responses.send_error_json(req, exc.status, str(exc))
    responses.send_json(req, job, 201)


@project_route()
def h_jobs_list(ctx, proj, req, params, body):
    responses.send_json(req, {"jobs": jobs_mod.list_jobs(ctx.db, proj.id)})


@project_route()
def h_job_get(ctx, proj, req, params, body):
    job = jobs_mod.get_job(ctx.db, proj.id, params["jid"])
    if job is None:
        return responses.send_error_json(req, 404, "no such job")
    responses.send_json(req, job)


@project_route("manager")
def h_job_cancel(ctx, proj, req, params, body):
    try:
        job = ctx.jobs.cancel(proj.id, params["jid"])
    except jobs_mod.JobError as exc:
        return responses.send_error_json(req, exc.status, str(exc))
    responses.send_json(req, job)


@project_route("manager")
def h_job_retry(ctx, proj, req, params, body):
    try:
        job = ctx.jobs.retry(proj.id, params["jid"], req.user["id"])
    except jobs_mod.JobError as exc:
        return responses.send_error_json(req, exc.status, str(exc))
    responses.send_json(req, job, 201)


# --------------------------------------------------------------------------- #
# Team (M13, M20) and join links
# --------------------------------------------------------------------------- #
@project_route()
def h_members(ctx, proj, req, params, body):
    responses.send_json(req, {"members": membership.list_members(ctx.db, proj.id)})


@project_route("manager")
def h_directory(ctx, proj, req, params, body):
    """Active accounts a manager can add (they don't create accounts, M13)."""
    responses.send_json(req, {"users": membership.directory(ctx.db)})


@project_route("manager")
def h_member_add(ctx, proj, req, params, body):
    try:
        data = _json_body(body)
    except json.JSONDecodeError:
        return responses.send_error_json(req, 400, "invalid JSON")
    role = data.get("role") or "curator"
    if role == "manager" and req.user["account_role"] != "superadmin":
        return responses.send_error_json(req, 403, "only a superadmin can add project managers")
    current = ctx.db.scalar("SELECT role FROM project_members WHERE project_id=? AND user_id=?",
                            (proj.id, data.get("userId")))
    if current == "manager" and req.user["account_role"] != "superadmin":
        return responses.send_error_json(req, 403, "only a superadmin can change a manager's role")
    try:
        membership.add_member(ctx.db, proj.id, data.get("userId"), role, req.user["id"])
    except membership.MembershipError as exc:
        return responses.send_error_json(req, 400, str(exc))
    responses.send_json(req, {"members": membership.list_members(ctx.db, proj.id)})


@project_route("manager")
def h_member_remove(ctx, proj, req, params, body):
    target = params["userId"]
    role = ctx.db.scalar("SELECT role FROM project_members WHERE project_id=? AND user_id=?",
                         (proj.id, target))
    if role == "manager" and req.user["account_role"] != "superadmin":
        return responses.send_error_json(req, 403, "only a superadmin can remove a project manager")
    try:
        released = membership.remove_member(ctx.db, proj.id, target, req.user["id"])
    except membership.MembershipError as exc:
        return responses.send_error_json(req, 400, str(exc))
    responses.send_json(req, {"released": released,
                              "members": membership.list_members(ctx.db, proj.id)})


@project_route("manager")
def h_links(ctx, proj, req, params, body):
    responses.send_json(req, {"links": membership.list_links(ctx.db, proj.id)})


@project_route("manager")
def h_link_create(ctx, proj, req, params, body):
    try:
        data = _json_body(body)
    except json.JSONDecodeError:
        return responses.send_error_json(req, 400, "invalid JSON")
    try:
        link, token = membership.create_link(ctx.db, proj.id, req.user["id"],
                                             days=data.get("days"), max_uses=data.get("maxUses"))
    except membership.MembershipError as exc:
        return responses.send_error_json(req, 400, str(exc))
    # The full URL is shown ONCE; afterwards only the link's metadata exists.
    link["url"] = site_url(req, f"/join/{token}")
    responses.send_json(req, link, 201)


@project_route("manager")
def h_link_revoke(ctx, proj, req, params, body):
    try:
        link = membership.revoke_link(ctx.db, proj.id, params["lid"], req.user["id"])
    except membership.MembershipError as exc:
        return responses.send_error_json(req, 404, str(exc))
    responses.send_json(req, link)


# --------------------------------------------------------------------------- #
def register(router):
    auth_handlers.register(router)

    # global
    router.add("GET", "/api/config", h_config)
    router.add("GET", "/api/projects", h_projects_list)
    router.add("POST", "/api/projects", h_project_create)
    router.add("POST", "/api/validate-config", h_validate_config)
    router.add("GET", "/api/template", h_template)
    router.add("GET", "/api/feature-template", h_feature_template)
    router.add("POST", "/api/parse-feature-sheet", h_parse_feature_sheet)

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

    # workflow
    router.add("POST", "/api/projects/{pid}/papers/{uid}/claim", h_claim)
    router.add("POST", "/api/projects/{pid}/papers/{uid}/assign", h_assign)
    router.add("POST", "/api/projects/{pid}/papers/{uid}/submit", h_submit)
    router.add("POST", "/api/projects/{pid}/papers/{uid}/exclude", h_exclude)
    router.add("POST", "/api/projects/{pid}/papers/{uid}/reopen", h_reopen)
    router.add("GET", "/api/projects/{pid}/papers/{uid}/history", h_paper_history)

    # jobs
    router.add("POST", "/api/projects/{pid}/jobs", h_jobs_create)
    router.add("GET", "/api/projects/{pid}/jobs", h_jobs_list)
    router.add("GET", "/api/projects/{pid}/jobs/{jid}", h_job_get)
    router.add("POST", "/api/projects/{pid}/jobs/{jid}/cancel", h_job_cancel)
    router.add("POST", "/api/projects/{pid}/jobs/{jid}/retry", h_job_retry)

    # team
    router.add("GET", "/api/projects/{pid}/members", h_members)
    router.add("GET", "/api/projects/{pid}/directory", h_directory)
    router.add("POST", "/api/projects/{pid}/members", h_member_add)
    router.add("POST", "/api/projects/{pid}/members/{userId}/remove", h_member_remove)
    router.add("GET", "/api/projects/{pid}/links", h_links)
    router.add("POST", "/api/projects/{pid}/links", h_link_create)
    router.add("POST", "/api/projects/{pid}/links/{lid}/revoke", h_link_revoke)

    # page shells
    router.add("GET", "/", h_home_shell)
    router.add("GET", "/p/{pid}", h_app_shell)
