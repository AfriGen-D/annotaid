"""Build a JSON Schema from the feature config.

One schema is built and reused for BOTH the OpenRouter response_format and the
local receipt validation (DRY). Per feature the model must return
`{ present: bool, value: <type-schema>, evidence: string|null }`.
"""
from __future__ import annotations


def value_schema(feature: dict) -> dict:
    """Map a declared feature type to a JSON-Schema fragment for its value."""
    t = feature["type"]
    nullable = feature.get("nullable", True)

    def nul(base):
        return [base, "null"] if nullable else base

    if t == "string":
        return {"type": nul("string")}
    if t == "number":
        return {"type": nul("number")}
    if t == "boolean":
        return {"type": nul("boolean")}
    if t == "enum":
        vals = list(feature.get("enumValues", []))
        if nullable:
            return {"type": ["string", "null"], "enum": vals + [None]}
        return {"type": "string", "enum": vals}
    if t == "array<string>":
        return {"type": "array", "items": {"type": "string"}}
    if t == "array<number>":
        return {"type": "array", "items": {"type": "number"}}
    raise ValueError(f"Unknown feature type: {t}")


def build_response_schema(features: list) -> dict:
    """Strict object schema for the whole extraction response."""
    props = {}
    for f in features:
        props[f["name"]] = {
            "type": "object",
            "additionalProperties": False,
            "required": ["present", "value", "evidence"],
            "properties": {
                "present": {"type": "boolean"},
                "value": value_schema(f),
                # evidence is always nullable: inferred/absent values have no quote.
                "evidence": {"type": ["string", "null"]},
            },
        }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [f["name"] for f in features],
        "properties": props,
    }


def empty_value(feature: dict):
    """Type-appropriate empty value used when repairing a missing feature."""
    t = feature["type"]
    if t in ("array<string>", "array<number>"):
        return []
    if t == "boolean":
        return False if not feature.get("nullable", True) else None
    return None


def feature_type_hint(feature: dict) -> str:
    """Human-readable type hint for the prompt-only baseline."""
    t = feature["type"]
    if t == "enum":
        return "one of: " + ", ".join(feature.get("enumValues", []))
    return t


def build_prompt(base_text: str, features: list) -> str:
    """Append a config-derived Fields section + JSON shape to the base prompt.

    Used for every request; for prompt-only models (no response_format) this is
    the only place the required shape is communicated.
    """
    lines = [base_text.strip(), "", "Fields:"]
    for f in features:
        lines.append(
            f"- {f['name']} ({feature_type_hint(f)}): {f.get('description', '').strip()}"
        )
    lines += [
        "",
        "Return ONLY valid JSON with exactly this shape (no prose, no code fences):",
        "{",
    ]
    parts = []
    for f in features:
        parts.append(
            f'  "{f["name"]}": {{"present": true|false, '
            f'"value": <{feature_type_hint(f)}>, "evidence": "<verbatim quote or null>"}}'
        )
    lines.append(",\n".join(parts))
    lines.append("}")
    return "\n".join(lines)
