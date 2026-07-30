"""Load and meta-validate config/schema.json, and load secrets from .keys.

The config is config-driven (brief §8): it declares the feature list + types, the
models, the prompt(s), and the CSV export mapping. Secrets live only in memory and
are never serialised into an /api response.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from jsonschema import Draft202012Validator

from . import util

FEATURE_TYPES = [
    "string",
    "boolean",
    "number",
    "enum",
    "array<string>",
    "array<number>",
]

# Meta-schema: validate the shape of schema.json itself on load (fail fast).
CONFIG_META_SCHEMA = {
    "type": "object",
    "required": ["features", "models"],
    "additionalProperties": True,
    "properties": {
        "features": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["name", "type"],
                "additionalProperties": True,
                "properties": {
                    "name": {"type": "string", "minLength": 1},
                    "type": {"enum": FEATURE_TYPES},
                    "nullable": {"type": "boolean"},
                    "description": {"type": "string"},
                    "enumValues": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                    },
                },
            },
        },
        "models": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["slug"],
                "additionalProperties": True,
                "properties": {
                    "slug": {"type": "string", "minLength": 1},
                    "label": {"type": "string"},
                    "supportsStructuredOutput": {"type": "boolean"},
                },
            },
        },
        "prompts": {"type": "array"},
        "parseEngines": {"type": "array", "items": {"type": "string"}},
        "defaults": {"type": "object"},
        "export": {"type": "object"},
    },
}


class ConfigError(Exception):
    pass


@dataclass
class Config:
    features: list  # list[dict] normalized feature defs
    models: list  # list[dict]: {slug, label, supportsStructuredOutput}
    prompts: list  # list[dict]: {id, label, text}
    parse_engines: list
    defaults: dict
    export: dict
    raw: dict = field(default_factory=dict)

    # ---- convenience lookups ----
    def feature(self, name: str):
        for f in self.features:
            if f["name"] == name:
                return f
        return None

    def feature_names(self) -> list:
        return [f["name"] for f in self.features]

    def model(self, slug: str):
        for m in self.models:
            if m["slug"] == slug:
                return m
        return None

    def prompt(self, prompt_id: str):
        for p in self.prompts:
            if p["id"] == prompt_id:
                return p
        return None

    def default_prompt(self):
        return self.prompts[0] if self.prompts else None

    def default_parse_engine(self) -> str:
        return self.defaults.get("parseEngine") or (
            self.parse_engines[0] if self.parse_engines else "pdf-text"
        )

    def public_dict(self) -> dict:
        """Config for GET /api/config — NO secrets ever included."""
        return {
            "features": [
                {
                    "name": f["name"],
                    "type": f["type"],
                    "nullable": f.get("nullable", True),
                    "description": f.get("description", ""),
                    **({"enumValues": f["enumValues"]} if "enumValues" in f else {}),
                }
                for f in self.features
            ],
            "models": [
                {
                    "slug": m["slug"],
                    "label": m["label"],
                    "supportsStructuredOutput": m.get(
                        "supportsStructuredOutput", False
                    ),
                }
                for m in self.models
            ],
            "prompts": [{"id": p["id"], "label": p["label"]} for p in self.prompts],
            "parseEngines": self.parse_engines,
            "defaults": {
                "parseEngine": self.default_parse_engine(),
                "promptId": (self.default_prompt() or {}).get("id"),
            },
        }


@dataclass
class Secrets:
    openrouter_api_key: str
    ncbi_api_key: str = ""
    unpaywall_email: str = ""


def load_config(path: str) -> Config:
    cfg = util.read_json(path)
    if cfg is None:
        raise ConfigError(f"Config file not found or empty: {path}")

    errors = sorted(
        Draft202012Validator(CONFIG_META_SCHEMA).iter_errors(cfg),
        key=lambda e: list(e.path),
    )
    if errors:
        msgs = "; ".join(f"{list(e.path)}: {e.message}" for e in errors[:8])
        raise ConfigError(f"Invalid config ({path}): {msgs}")

    # feature-level cross checks
    names = set()
    for f in cfg["features"]:
        if f["name"] in names:
            raise ConfigError(f"Duplicate feature name: {f['name']}")
        names.add(f["name"])
        f.setdefault("nullable", True)
        f.setdefault("description", "")
        if f["type"] == "enum" and not f.get("enumValues"):
            raise ConfigError(f"Feature '{f['name']}' is enum but has no enumValues")

    # normalise models (label fallback per notes.md)
    models = []
    for m in cfg["models"]:
        slug = m["slug"].strip()
        label = (m.get("label") or "").strip() or util.slugify(slug)
        models.append(
            {
                "slug": slug,
                "label": label,
                "supportsStructuredOutput": bool(
                    m.get("supportsStructuredOutput", False)
                ),
            }
        )
    labels = [m["label"] for m in models]
    if len(set(labels)) != len(labels):
        raise ConfigError(f"Duplicate model labels: {labels}")

    # load prompts (text from `file` relative to config dir, or inline `text`)
    cfg_dir = os.path.dirname(os.path.abspath(path))
    prompts = []
    for p in cfg.get("prompts", []):
        if p.get("file"):
            ppath = os.path.join(cfg_dir, p["file"])
            if not os.path.isfile(ppath):
                raise ConfigError(f"Prompt file not found: {ppath}")
            with open(ppath, encoding="utf-8") as fh:
                text = fh.read().strip()
        else:
            text = (p.get("text") or "").strip()
        if not text:
            raise ConfigError(f"Empty prompt: {p.get('label')}")
        prompts.append(
            {"id": util.prompt_id(text), "label": p.get("label") or "prompt", "text": text}
        )
    if not prompts:
        raise ConfigError("Config must declare at least one prompt")

    parse_engines = cfg.get("parseEngines") or ["pdf-text", "mistral-ocr", "native"]

    return Config(
        features=cfg["features"],
        models=models,
        prompts=prompts,
        parse_engines=parse_engines,
        defaults=cfg.get("defaults", {}),
        export=cfg.get("export", {}),
        raw=cfg,
    )


def load_secrets(keys_path: str) -> Secrets:
    util.load_env_file(keys_path)
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key:
        raise ConfigError(
            f"No OPENROUTER_API_KEY found. Set it in the environment or in {keys_path!r}."
        )
    return Secrets(
        openrouter_api_key=key,
        ncbi_api_key=os.environ.get("NCBI_API_KEY", "").strip(),
        unpaywall_email=os.environ.get("UNPAYWALL_EMAIL", "").strip(),
    )
