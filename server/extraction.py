"""Run extraction for (paper × model): call OpenRouter, validate, build records.

One extraction run is produced per (paper × model). aiValue is written once and
equals value at creation; the curator edits value later (autosave preserves aiValue).
"""
from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor

from . import openrouter, schema_builder, util, validation
from .config_loader import Config, Secrets
from .store import Store

MAX_WORKERS = 4


def _blank_features(features: list) -> dict:
    out = {}
    for f in features:
        empty = schema_builder.empty_value(f)
        out[f["name"]] = {
            "type": f["type"],
            "aiValue": empty,
            "value": copy.deepcopy(empty),
            "present": False,
            "evidence": None,
            "evidenceMatch": "none",
            "confirmed": False,
            "editedAt": None,
            "confirmedAt": None,
        }
    return out


def _features_from_canonical(canonical: dict, features: list) -> dict:
    out = {}
    for f in features:
        node = canonical.get(f["name"], {}) if canonical else {}
        value = node.get("value", schema_builder.empty_value(f))
        out[f["name"]] = {
            "type": f["type"],
            "aiValue": copy.deepcopy(value),
            "value": copy.deepcopy(value),
            "present": bool(node.get("present", False)),
            "evidence": node.get("evidence"),
            "evidenceMatch": "none",  # computed client-side against the pdf.js text layer
            "confirmed": False,
            "editedAt": None,
            "confirmedAt": None,
        }
    return out


def extract_one(
    config: Config,
    secrets: Secrets,
    paper: dict,
    model: dict,
    prompt: dict,
    parse_engine: str,
) -> dict:
    pmid = paper["pmid"]
    features = config.features
    schema = schema_builder.build_response_schema(features)
    prompt_text = schema_builder.build_prompt(prompt["text"], features)

    run = {
        "pmid": pmid,
        "modelId": model["slug"],
        "modelLabel": model["label"],
        "promptId": prompt["id"],
        "source": "openrouter",
        "parseEngine": parse_engine,
        "requestedAt": util.iso_now(),
        "completedAt": None,
        "status": "failed",
        "error": None,
        "features": _blank_features(features),
    }

    pdf_path = paper.get("_pdf_path")
    try:
        messages = openrouter.build_messages(prompt_text, pdf_path, paper["filename"])
        payload = openrouter.build_payload(
            model["slug"],
            messages,
            schema,
            parse_engine,
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
                parse_engine,
                send_response_format=model.get("supportsStructuredOutput", False),
            )
            text2 = openrouter.call(retry_payload, secrets.openrouter_api_key)
            canonical, status, errs = validation.process(text2, features, schema)

        run["status"] = status
        if canonical is not None:
            run["features"] = _features_from_canonical(canonical, features)
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
    store: Store,
    paper: dict,
    model_slugs: list,
    prompt_id: str | None,
    parse_engine: str | None,
    force: bool = False,
) -> list:
    if not paper.get("pmid"):
        raise ValueError("paper has no confirmed PMID")

    prompt = config.prompt(prompt_id) if prompt_id else config.default_prompt()
    if prompt is None:
        raise ValueError(f"unknown promptId: {prompt_id}")
    engine = parse_engine or config.default_parse_engine()
    if engine not in config.parse_engines:
        raise ValueError(f"unknown parseEngine: {engine}")

    pdf_path = store.resolve_pdf(paper["uid"])
    if not pdf_path:
        raise ValueError("no stored PDF for this paper")
    paper = {**paper, "_pdf_path": pdf_path}

    models = [config.model(s) for s in model_slugs]
    if any(m is None for m in models):
        missing = [s for s, m in zip(model_slugs, models) if m is None]
        raise ValueError(f"unknown model(s): {missing}")

    # decide which need running (protect curator edits unless force)
    to_run, reused = [], []
    for m in models:
        existing = store.load_run(paper["pmid"], m["slug"])
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
                        config, secrets, paper, m, prompt, engine
                    ),
                    to_run,
                )
            )
        for run in fresh:
            run.pop("_pdf_path", None)
            store.save_run(run)

    # record models on the paper
    stored_paper = store.load_paper(paper["uid"])
    if stored_paper is not None:
        seen = set(stored_paper.get("models", []))
        for m in models:
            seen.add(m["slug"])
        stored_paper["models"] = sorted(seen)
        store.save_paper(stored_paper)

    by_slug = {r["modelId"]: r for r in (reused + fresh)}
    return [by_slug[m["slug"]] for m in models if m["slug"] in by_slug]
