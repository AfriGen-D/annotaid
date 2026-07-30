"""Receipt-validation layer (brief §7).

Every model reply is validated and type-coerced against the declared per-feature
type on receipt, with structural repair — REGARDLESS of whether the model claims
json_schema support. Structure enforcement guarantees shape, never correctness; the
dangerous class (well-formed but wrong) is for the human to catch.
"""
from __future__ import annotations

import json
import re

from jsonschema import Draft202012Validator

from . import schema_builder

# Tolerant key aliasing (borrowed from the prototype's parseFeatures).
VALUE_KEYS = ["value", "values", "val", "label", "name", "term"]
EVIDENCE_KEYS = [
    "evidence",
    "quote",
    "searchable_text",
    "search_text",
    "text",
    "span",
    "snippet",
    "support",
    "context",
]
PRESENT_KEYS = ["present", "present_in_text", "present_in_paper", "found"]
TRUE_SET = {"yes", "y", "true", "t", "1"}
FALSE_SET = {"no", "n", "false", "f", "0"}


# --------------------------------------------------------------------------- #
# JSON extraction (ported from pipeline.py)
# --------------------------------------------------------------------------- #
def extract_json(text: str):
    """Pull a JSON object out of the model's reply (handles ``` fences / prose)."""
    text = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return json.loads(text[start : end + 1])
    raise ValueError("no JSON object found in response")


# --------------------------------------------------------------------------- #
# Coercion helpers
# --------------------------------------------------------------------------- #
def _first(node: dict, keys):
    for k in keys:
        if k in node:
            return node[k], True
    return None, False


def to_list_str(val) -> list:
    if val is None or val == "" or val == "null":
        return []
    if isinstance(val, list):
        return [str(v).strip() for v in val if str(v).strip()]
    return [s.strip() for s in str(val).split(";") if s.strip()] if ";" in str(val) else [str(val).strip()]


def to_list_num(val) -> list:
    out = []
    items = val if isinstance(val, list) else ([val] if val not in (None, "", "null") else [])
    for v in items:
        try:
            out.append(_num(v))
        except (TypeError, ValueError):
            continue
    return out


def _num(v):
    if isinstance(v, bool):
        raise ValueError("bool is not a number")
    if isinstance(v, (int, float)):
        return v
    s = str(v).strip()
    f = float(s)
    return int(f) if f.is_integer() else f


def to_bool(val):
    if isinstance(val, bool):
        return val
    s = str(val).strip().lower()
    if s in TRUE_SET:
        return True
    if s in FALSE_SET:
        return False
    return None


def coerce_value(raw, feature: dict):
    """Force a raw value to the feature's declared type. Never raises."""
    t = feature["type"]
    nullable = feature.get("nullable", True)
    if t == "array<string>":
        return to_list_str(raw)
    if t == "array<number>":
        return to_list_num(raw)
    if t == "boolean":
        b = to_bool(raw)
        return b if b is not None else (None if nullable else False)
    if t == "number":
        if raw in (None, "", "null"):
            return None
        try:
            return _num(raw)
        except (TypeError, ValueError):
            return None
    if t == "enum":
        if raw is None:
            return None
        s = str(raw).strip()
        return s if s in feature.get("enumValues", []) else None
    # string
    if raw in (None, "", "null"):
        return None
    return str(raw).strip()


def _is_empty_value(val, feature) -> bool:
    if feature["type"].startswith("array"):
        return not val
    return val in (None, "")


def coerce_feature(node, feature: dict) -> dict:
    """Normalise one feature's raw node -> canonical {present, value, evidence}."""
    if isinstance(node, dict):
        raw_val, has_val = _first(node, VALUE_KEYS)
        if not has_val:
            # the object might itself be the value (rare) — leave as-is for coercion
            raw_val = node
        evidence, _ = _first(node, EVIDENCE_KEYS)
        present_raw, has_present = _first(node, PRESENT_KEYS)
    else:
        raw_val, evidence, has_present, present_raw = node, None, False, None

    value = coerce_value(raw_val, feature)

    if has_present:
        present = to_bool(present_raw)
        if present is None:
            present = not _is_empty_value(value, feature)
    else:
        present = not _is_empty_value(value, feature)

    ev = evidence
    if not isinstance(ev, str) or not ev.strip():
        ev = None  # never fabricate a quote (brief §7)
    else:
        ev = ev.strip()

    return {"present": present, "value": value, "evidence": ev}


def coerce_payload(parsed: dict, features: list) -> dict:
    """Coerce every feature. Missing features are filled by the repair step."""
    parsed = parsed if isinstance(parsed, dict) else {}
    # allow a nested {"features": {...}} envelope
    if "features" in parsed and isinstance(parsed["features"], dict):
        parsed = parsed["features"]
    canonical = {}
    for f in features:
        if f["name"] in parsed:
            canonical[f["name"]] = coerce_feature(parsed[f["name"]], f)
    return canonical


def _repair(canonical: dict, features: list) -> dict:
    """Fill missing features with an absent placeholder; drop unknown keys."""
    out = {}
    for f in features:
        if f["name"] in canonical:
            out[f["name"]] = canonical[f["name"]]
        else:
            out[f["name"]] = {
                "present": False,
                "value": schema_builder.empty_value(f),
                "evidence": None,
            }
    return out


def validate(canonical: dict, response_schema: dict) -> list:
    errs = sorted(
        Draft202012Validator(response_schema).iter_errors(canonical),
        key=lambda e: list(e.path),
    )
    return [f"{list(e.path)}: {e.message}" for e in errs]


def process(raw_text: str, features: list, response_schema: dict):
    """Return (canonical_features, status, errors).

    status: 'ok' (clean, no repair needed) | 'repaired' (coercion/fill applied) |
    'failed' (unparseable or still invalid after repair — caller may retry).
    """
    try:
        parsed = extract_json(raw_text)
    except (ValueError, json.JSONDecodeError) as exc:
        return None, "failed", [f"unparseable JSON: {exc}"]

    canonical = coerce_payload(parsed, features)
    had_all = len(canonical) == len(features)
    errs = validate(canonical, response_schema)

    if not errs and had_all and _clean_of_extras(parsed, features):
        return canonical, "ok", []

    repaired = _repair(canonical, features)
    errs2 = validate(repaired, response_schema)
    if not errs2:
        return repaired, "repaired", []
    return repaired, "failed", errs2


def _clean_of_extras(parsed, features) -> bool:
    """True if the parsed object had no extra top-level keys and every node was a
    proper {present, value, evidence} dict (i.e. genuinely 'ok', not just coercible)."""
    if not isinstance(parsed, dict):
        return False
    obj = parsed.get("features") if isinstance(parsed.get("features"), dict) else parsed
    names = {f["name"] for f in features}
    if set(obj.keys()) - names:
        return False
    for f in features:
        node = obj.get(f["name"])
        if not isinstance(node, dict):
            return False
        if set(node.keys()) - {"present", "value", "evidence"}:
            return False
    return True
