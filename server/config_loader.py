"""Load and meta-validate a curation config, and load secrets from .keys.

The config is config-driven (brief §8): it declares the feature list + types,
prompt(s), and CSV export mapping. AI models and PDF processing are deployment
settings managed by a superadmin; legacy project files may still carry models.
Secrets live only in memory and are never serialised into an /api response.

Since projects landed, a config arrives from one of two places:

  load_config(path)   -- a file on disk; used ONLY for the starter template that
                         seeds a new project
  parse_config(dict)  -- a project's own stored config document

parse_config is the shared validator, so a project config gets exactly the same
meta-schema checks a file always did. A project config must inline its prompt
`text`: a `file` reference is shared mutable state living outside the project,
which breaks both "the project owns its copy" and handing the config to someone
else. Hence prompt_dir=None -- inline text only.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from jsonschema import Draft202012Validator

from . import features as feat, util
from .features import FEATURE_TYPES, FeatureError  # noqa: F401  (re-exported)

# Meta-schema: the shape of the DOCUMENT (fail fast on load).
#
# Deliberately permissive about what is inside `features`: feature semantics —
# types, enum values, identifiers, the two-level nesting cap — are checked by
# features.parse_features, which produces messages a project manager can act on.
# jsonschema's oneOf errors ("is not valid under any of the given schemas") would
# be strictly worse for exactly the input a human is hand-editing.
CONFIG_META_SCHEMA = {
    "type": "object",
    "required": ["features"],
    "additionalProperties": True,
    "properties": {
        "features": {
            "type": "array",
            "minItems": 1,
            "items": {"type": "object", "required": ["name"]},
        },
        "settings": {
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "allowPapersWithoutPmid": {"type": "boolean"},
                "allowExtractionOnAbstract": {"type": "boolean"},
            },
        },
        "models": {
            "type": "array",
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
    settings: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)

    # ---- convenience lookups ----
    # `features` holds features.Leaf / features.Group objects, never raw dicts.
    def feature(self, path: str):
        """By name, or by dotted path for a field inside a group."""
        return feat.find(self.features, path)

    def feature_names(self) -> list:
        """Every curatable path, e.g. ['sample_size', 'variants.rsid', ...]."""
        return feat.paths(self.features)

    def leaves(self) -> list:
        """Paper-level features only."""
        return feat.leaves(self.features)

    def groups(self) -> list:
        return feat.groups(self.features)

    def group(self, name: str):
        for g in self.groups():
            if g.name == name:
                return g
        return None

    @property
    def allow_papers_without_pmid(self) -> bool:
        """Project policy. Absent reads as False: a project must opt IN to
        papers with no PubMed ID, so a curator cannot quietly add unidentified
        papers to a gold standard keyed on PubMed."""
        return bool(self.settings.get("allowPapersWithoutPmid", False))

    @property
    def allow_extraction_on_abstract(self) -> bool:
        """Project policy, same opt-in shape as allow_papers_without_pmid: when
        on, fetch-by-PMID falls back to the PubMed abstract if no open-access
        full-text PDF is found, and extraction runs against that abstract."""
        return bool(self.settings.get("allowExtractionOnAbstract", False))

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

    def portable_dict(self) -> dict:
        """The config half of a project's portable document.

        Prompt text is INLINED (never a `file` reference) so the document is
        self-contained: this is what gets stored in projects.config_json and what
        a project manager hands to a curator.
        """
        return {
            "features": [f.to_dict() for f in self.features],
            "models": self.models,
            "prompts": [{"label": p["label"], "text": p["text"]} for p in self.prompts],
            "parseEngines": self.parse_engines,
            "defaults": self.defaults,
            "export": self.export,
            "settings": dict(self.settings),
        }

    def public_dict(self) -> dict:
        """Config for GET /api/config — NO secrets ever included."""
        return {
            # Nested: a group carries its own `features` list, so the client
            # renders paper-level fields and repeating rows from one payload.
            "features": [f.to_dict() for f in self.features],
            "settings": {
                "allowPapersWithoutPmid": self.allow_papers_without_pmid,
                "allowExtractionOnAbstract": self.allow_extraction_on_abstract,
            },
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
    """Read + validate a config FILE. Only the starter template uses this."""
    cfg = util.read_json(path)
    if cfg is None:
        raise ConfigError(f"Config file not found or empty: {path}")
    return parse_config(cfg, prompt_dir=os.path.dirname(os.path.abspath(path)),
                        label=path)


def parse_config(cfg: dict, prompt_dir: str | None = None, label: str = "config") -> Config:
    """Validate + normalise a config document.

    prompt_dir=None (the default, used for every project config) rejects prompt
    `file` references and requires inline `text`.
    """
    if not isinstance(cfg, dict):
        raise ConfigError(f"Invalid config ({label}): not an object")

    errors = sorted(
        Draft202012Validator(CONFIG_META_SCHEMA).iter_errors(cfg),
        key=lambda e: list(e.path),
    )
    if errors:
        msgs = "; ".join(f"{list(e.path)}: {e.message}" for e in errors[:8])
        raise ConfigError(f"Invalid config ({label}): {msgs}")

    # Feature semantics live in features.py — one place, with messages a project
    # manager can act on. It also applies every default exactly once, which four
    # separate modules used to do independently.
    try:
        parsed_features = feat.parse_features(cfg["features"], label)
    except FeatureError as exc:
        raise ConfigError(str(exc))

    # normalise models (label fallback per notes.md)
    models = []
    for m in cfg.get("models") or []:
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

    # load prompts (inline `text`, or from `file` when a prompt_dir is allowed)
    prompts = []
    for p in cfg.get("prompts", []):
        if p.get("file"):
            if prompt_dir is None:
                raise ConfigError(
                    f"Prompt {p.get('label')!r} uses a file reference; a project "
                    "config must inline its prompt text so it stays portable"
                )
            ppath = os.path.join(prompt_dir, p["file"])
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

    settings = cfg.get("settings") or {}
    if not isinstance(settings, dict):
        raise ConfigError(f"Invalid config ({label}): settings must be an object")

    return Config(
        features=parsed_features,
        models=models,
        prompts=prompts,
        parse_engines=parse_engines,
        defaults=cfg.get("defaults", {}),
        export=cfg.get("export", {}),
        settings=settings,
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
