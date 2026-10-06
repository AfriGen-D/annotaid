"""Server-wide AI model and PDF-processing settings.

Feature schemas and prompts belong to projects. Models and parsing engines are
deployment choices, so a superadmin manages them once here instead of asking a
project manager to understand OpenRouter details on every extraction.
"""
from __future__ import annotations

import json

from . import util

KEY = "ai"
PARSE_ENGINES = ("pdf-text", "mistral-ocr", "native")


class AiSettingsError(ValueError):
    pass


def _fallback(config=None) -> dict:
    models = [dict(m) for m in getattr(config, "models", [])]
    default_model = models[0]["slug"] if models else ""
    engine = config.default_parse_engine() if config is not None else "pdf-text"
    return {"models": models, "defaultModel": default_model, "parseEngine": engine}


def validate(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise AiSettingsError("AI settings must be an object")
    models = []
    seen_slugs, seen_labels = set(), set()
    for item in raw.get("models") or []:
        if not isinstance(item, dict):
            raise AiSettingsError("each model must be an object")
        slug = str(item.get("slug") or "").strip()
        label = str(item.get("label") or "").strip() or slug
        if not slug:
            raise AiSettingsError("every model needs an OpenRouter slug")
        if slug in seen_slugs:
            raise AiSettingsError(f"duplicate model slug: {slug}")
        if label.casefold() in seen_labels:
            raise AiSettingsError(f"duplicate model label: {label}")
        seen_slugs.add(slug)
        seen_labels.add(label.casefold())
        models.append({
            "slug": slug,
            "label": label,
            "supportsStructuredOutput": bool(item.get("supportsStructuredOutput", False)),
        })
    if not models:
        raise AiSettingsError("add at least one model")
    default_model = str(raw.get("defaultModel") or "").strip()
    if default_model not in seen_slugs:
        raise AiSettingsError("choose a default model from the model list")
    engine = str(raw.get("parseEngine") or "pdf-text").strip()
    if engine not in PARSE_ENGINES:
        raise AiSettingsError(f"unknown PDF processing engine: {engine}")
    return {"models": models, "defaultModel": default_model, "parseEngine": engine}


def get(db, fallback_config=None) -> dict:
    row = db.query_one("SELECT value_json FROM app_settings WHERE key=?", (KEY,))
    if row is not None:
        try:
            return validate(json.loads(row["value_json"]))
        except (ValueError, json.JSONDecodeError):
            pass
    return _fallback(fallback_config)


def save(db, raw: dict, user_id: str | None) -> dict:
    value = validate(raw)
    with db.write() as c:
        c.execute(
            "INSERT INTO app_settings (key,value_json,updated_at,updated_by) VALUES (?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, "
            "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
            (KEY, json.dumps(value, ensure_ascii=False), util.iso_now(), user_id),
        )
    return value


def model(settings: dict, slug: str | None = None):
    wanted = slug or settings.get("defaultModel")
    return next((m for m in settings.get("models") or [] if m.get("slug") == wanted), None)
