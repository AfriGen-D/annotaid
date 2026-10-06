"""Run extraction for (paper × model): call OpenRouter, validate, build records.

One extraction run is produced per (paper × model). aiValue is written once and
equals value at creation; the curator edits value later (autosave preserves aiValue).
"""
from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor

from . import features as feat, openrouter, project_store, schema_builder, util, validation
from .config_loader import Config, Secrets
from .project_store import ProjectStore

MAX_WORKERS = 4


def _paper_source(config: Config, store: ProjectStore, paper: dict) -> dict:
    """Attach the PDF path or abstract text used by either extraction pass."""
    pdf_path = store.resolve_pdf(paper["uid"])
    if pdf_path:
        return {**paper, "_pdf_path": pdf_path}
    if paper.get("hasPdf", True) is False and paper.get("abstractText"):
        if not config.allow_extraction_on_abstract:
            raise ValueError(
                "this paper has no stored PDF (abstract-only) and the project "
                "does not allow extraction on abstract"
            )
        return {**paper, "_abstract_text": paper["abstractText"]}
    raise ValueError("no stored PDF for this paper")


def _messages_for_paper(prompt_text: str, paper: dict, parse_engine: str):
    if paper.get("_pdf_path"):
        return (
            openrouter.build_messages(prompt_text, paper["_pdf_path"], paper["filename"]),
            parse_engine,
        )
    return openrouter.build_messages_abstract(prompt_text, paper.get("_abstract_text") or ""), None


def discover_group_items(
    config: Config,
    secrets: Secrets,
    store: ProjectStore,
    paper: dict,
    model: dict,
    parse_engine: str,
    context: str | None = None,
) -> list:
    """Pass 1: ask the model only which repeating-group entries exist.

    The result deliberately remains unreviewed. A curator may rename, add or
    remove identifiers, then explicitly approve the list before pass 2.
    """
    groups = config.groups()
    if not groups:
        return []
    paper = _paper_source(config, store, paper)
    group = groups[0]
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["items"],
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["label", "evidence"],
                    "properties": {
                        "label": {"type": "string"},
                        "evidence": {"type": ["string", "null"]},
                    },
                },
            }
        },
    }
    prompt = (
        "Identify the distinct entries reported for the repeating group "
        f'\"{group.display}\". {group.description or ""}\n\n'
        "Return the identifier exactly as written in the paper. Identifiers may use "
        "different valid systems, including dbSNP rs identifiers, EVA accession IDs, "
        "HGVS nomenclature, haplotype names, or another domain-specific format. Do not "
        "normalise, merge, invent, or discard an identifier because its format differs. "
        f"Return at most {group.max_items} entries. If none are reported, return an empty list.\n\n"
        "For each entry, include a short verbatim quote containing the identifier.\n"
        "Return ONLY JSON: {\"items\":[{\"label\":\"exact identifier\","
        "\"evidence\":\"verbatim quote or null\"}]}"
    )
    if context:
        prompt += "\n\nProject context:\n" + context.strip()
    messages, engine = _messages_for_paper(prompt, paper, parse_engine)
    payload = openrouter.build_payload(
        model["slug"], messages, schema, engine,
        send_response_format=model.get("supportsStructuredOutput", False),
    )
    try:
        raw = validation.extract_json(openrouter.call(payload, secrets.openrouter_api_key))
        if not isinstance(raw, dict) or not isinstance(raw.get("items"), list):
            raise ValueError('response must be an object with an "items" array')
    except (ValueError, openrouter.OpenRouterError) as exc:
        raise ValueError(f"could not identify {group.display} entries: {exc}") from exc
    # A deliberate re-discovery should not orphan existing run rows merely
    # because the same label came back without its server rowId.
    previous = store.load_group_items(paper["uid"])
    by_label = {}
    for old in previous:
        by_label.setdefault(old.get("label"), []).append(old.get("rowId"))

    items, seen = [], set()
    for item in raw.get("items") or []:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        if not label or label in seen:
            continue
        seen.add(label)
        evidence = str(item.get("evidence") or "").strip() or None
        item_out = {"label": label, "evidence": evidence}
        old_ids = by_label.get(label) or []
        if len(old_ids) == 1 and old_ids[0]:
            item_out["rowId"] = old_ids[0]
        items.append(item_out)
        if len(items) >= group.max_items:
            break
    return store.save_group_items(paper["uid"], items, reviewed=False, discovered=True)


def _cell(leaf, node: dict) -> dict:
    """One curated cell. aiValue is written once here and never again — the pair
    (what the AI said, what the curator settled on) IS the audit signal."""
    value = node.get("value", leaf.empty_value()) if node else leaf.empty_value()
    return {
        "type": leaf.type,
        "aiValue": copy.deepcopy(value),
        "value": copy.deepcopy(value),
        "present": bool(node.get("present", False)) if node else False,
        "evidence": node.get("evidence") if node else None,
        "evidenceMatch": "none",  # computed client-side against the pdf.js text layer
        "confirmed": False,
        "editedAt": None,
        "confirmedAt": None,
    }


def _blank_features(features: list) -> dict:
    """Paper-level cells only, all empty. Used when a call fails outright, so the
    curator still sees the full field list rather than a missing key."""
    return {f.name: _cell(f, None) for f in feat.leaves(features)}


def _blank_groups(features: list, group_items: list) -> dict:
    """Seed blank rows from the curator-reviewed pass-1 list on failure."""
    groups = feat.groups(features)
    if not groups:
        return {}
    g = groups[0]  # at most one group per project (features.parse_features)
    rows = [
        {
            "rowId": item["rowId"],
            "origin": "ai",
            "deletedAt": None,
            "position": i,
            "features": {c.name: _cell(c, None) for c in g.features},
        }
        for i, item in enumerate(group_items)
    ]
    return {g.name: {"present": bool(rows), "rows": rows}}


def _features_from_canonical(canonical: dict, features: list) -> dict:
    out = {}
    for f in feat.leaves(features):
        out[f.name] = _cell(f, (canonical or {}).get(f.name) or {})
    return out


def _groups_from_canonical(canonical: dict, features: list, group_items: list) -> dict:
    """Turn the group's canonical items into rows — matched POSITIONALLY against
    the reviewed pass-1 list, never by a pass-2 identifier (there
    isn't one any more: schema_builder.build_prompt asks for value-only entries
    in a fixed order once group_items is known). A declared item the model
    didn't return becomes a blank row rather than vanishing; anything the model
    returned past the declared count is simply never looked at — it doesn't
    correspond to any row the curator asked for.

    origin="ai" describes who supplied the VALUES, not who invented the row —
    the row's existence is always the curator's (its identity came from
    group_items), only its content can be AI- or curator-authored. This keeps
    the existing false-positive/miss bookkeeping in project_store._merge_group
    meaningful for a row a curator adds by hand later.
    """
    out = {}
    groups = feat.groups(features)
    if not groups:
        return out
    g = groups[0]
    node = (canonical or {}).get(g.name) or {}
    ai_rows = node.get("items") or []
    rows = []
    for i, item in enumerate(group_items):
        cells = ai_rows[i] if i < len(ai_rows) else {}
        rows.append({
            "rowId": item["rowId"],
            "origin": "ai",
            "deletedAt": None,
            "position": i,
            "features": {c.name: _cell(c, (cells or {}).get(c.name) or {})
                         for c in g.features},
        })
    out[g.name] = {"present": bool(rows), "rows": rows}
    return out


def extract_one(
    config: Config,
    secrets: Secrets,
    paper: dict,
    model: dict,
    prompt: dict,
    parse_engine: str,
    context: str | None = None,
    group_items: list | None = None,
) -> dict:
    """group_items is the curator-reviewed pass-1 identity list, or [] when the
    project has no group (or the reviewed result was genuinely empty)."""
    features = config.features
    group_items = group_items or []
    schema = schema_builder.build_response_schema(features)
    groups = feat.groups(features)
    prompt_group_items = (
        {groups[0].name: [it["label"] for it in group_items]} if groups else None
    )
    prompt_text = schema_builder.build_prompt(prompt["text"], features, context, prompt_group_items)

    run = {
        "uid": paper["uid"],
        "modelId": model["slug"],
        "modelLabel": model["label"],
        # promptId identifies the TEMPLATE the curator selected; promptHash
        # identifies the prompt that was actually sent. They differ because the
        # feature descriptions and the project's own description compose into the
        # final prompt — two projects sharing one template do NOT share a prompt.
        # Without this split, editing a feature description would silently change
        # what was asked while leaving every past run's prompt id untouched.
        "promptId": prompt["id"],
        "promptHash": util.prompt_id(prompt_text),
        "promptText": prompt_text,
        "source": "openrouter",
        "parseEngine": parse_engine,
        "requestedAt": util.iso_now(),
        "completedAt": None,
        "status": "failed",
        "error": None,
        "features": _blank_features(features),
        "groups": _blank_groups(features, group_items),
    }

    pdf_path = paper.get("_pdf_path")
    abstract_text = paper.get("_abstract_text")
    try:
        if pdf_path:
            messages = openrouter.build_messages(prompt_text, pdf_path, paper["filename"])
            engine_for_payload = parse_engine
        else:
            # requested parse_engine never ran — nothing was parsed — so the
            # audit trail should say what actually happened, not what was asked.
            run["parseEngine"] = "abstract"
            # allowExtractionOnAbstract path: no PDF, so no file-parser plugin
            # either — there's nothing for OpenRouter to parse.
            messages = openrouter.build_messages_abstract(prompt_text, abstract_text)
            engine_for_payload = None
        payload = openrouter.build_payload(
            model["slug"],
            messages,
            schema,
            engine_for_payload,
            send_response_format=model.get("supportsStructuredOutput", False),
        )
        text = openrouter.call(payload, secrets.openrouter_api_key)
        canonical, status, errs = validation.process(text, features, schema)

        if status == "failed":
            # one repair retry, feeding the validator errors back to the model
            retry_messages = messages + [
                {"role": "assistant", "content": text},
                {
                    "role": "user",
                    "content": (
                        "Your previous output was invalid: "
                        + "; ".join(errs)
                        + ". Return ONLY JSON matching the required shape, no prose."
                    ),
                },
            ]
            retry_payload = openrouter.build_payload(
                model["slug"],
                retry_messages,
                schema,
                engine_for_payload,
                send_response_format=model.get("supportsStructuredOutput", False),
            )
            text2 = openrouter.call(retry_payload, secrets.openrouter_api_key)
            canonical, status, errs = validation.process(text2, features, schema)

        run["status"] = status
        if canonical is not None:
            run["features"] = _features_from_canonical(canonical, features)
            run["groups"] = _groups_from_canonical(canonical, features, group_items)
        if status == "failed":
            run["error"] = "; ".join(errs) if errs else "validation failed"
    except openrouter.OpenRouterError as exc:
        run["status"] = "failed"
        run["error"] = str(exc)
    except Exception as exc:  # noqa: BLE001
        run["status"] = "failed"
        run["error"] = f"{type(exc).__name__}: {exc}"

    run["completedAt"] = util.iso_now()
    return run


def run_extraction(
    config: Config,
    secrets: Secrets,
    store: ProjectStore,
    paper: dict,
    model_slugs: list,
    prompt_id: str | None,
    parse_engine: str | None,
    force: bool = False,
    context: str | None = None,
    requested_by: str | None = None,
    models_override: list | None = None,
) -> list:
    # A paper needs a RESOLVED identity, which no longer has to be a PMID — a
    # paper deliberately marked as having none (optionally with a DOI) is
    # perfectly curatable. Only 'pending' means the curator hasn't decided yet.
    if paper.get("pmidStatus") == project_store.STATUS_PENDING:
        raise ValueError("paper identity is unresolved — confirm a PMID, or mark it as having none")

    prompt = config.prompt(prompt_id) if prompt_id else config.default_prompt()
    if prompt is None:
        raise ValueError(f"unknown promptId: {prompt_id}")
    engine = parse_engine or config.default_parse_engine()
    paper = _paper_source(config, store, paper)

    available = models_override if models_override is not None else config.models
    models = [next((m for m in available if m.get("slug") == s), None) for s in model_slugs]
    if any(m is None for m in models):
        missing = [s for s, m in zip(model_slugs, models) if m is None]
        raise ValueError(f"unknown model(s): {missing}")

    # Pass 2 may run only after the assignee reviewed pass 1's identifier list.
    # An approved empty list is valid: the paper can still have global fields.
    groups = config.groups()
    group_items = []
    if groups:
        group = groups[0]
        group_items = store.load_group_items(paper["uid"])
        if not paper.get("groupItemsReviewed"):
            raise ValueError(
                f'review the discovered "{group.display}" entries before running full extraction'
            )

    # decide which need running (protect curator edits unless force)
    to_run, reused = [], []
    for m in models:
        existing = store.load_run(paper["uid"], m["slug"])
        if existing and existing.get("status") != "failed" and not force:
            reused.append(existing)
        else:
            to_run.append(m)

    fresh = []
    if to_run:
        with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(to_run))) as pool:
            fresh = list(
                pool.map(
                    lambda m: extract_one(
                        config, secrets, paper, m, prompt, engine, context, group_items
                    ),
                    to_run,
                )
            )
        for run in fresh:
            run.pop("_pdf_path", None)
            run["requestedBy"] = requested_by
            store.save_run(run)
        # A freshly saved run carries no pmid field (it is joined in on read), so
        # re-read it rather than shipping the in-memory dict to the client.
        fresh = [store.load_run(paper["uid"], r["modelId"]) for r in fresh]

    # paper["models"] is DERIVED from the runs table now — no write-back needed.
    by_slug = {r["modelId"]: r for r in (reused + fresh)}
    return [by_slug[m["slug"]] for m in models if m["slug"] in by_slug]
