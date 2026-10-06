"""Build the JSON Schema and the prompt from a project's feature config.

One schema is built and reused for BOTH the OpenRouter response_format and the
local receipt validation (DRY). Per leaf feature the model must return
`{ present: bool, value: <type-schema>, evidence: string|null }`; a group returns
`{ present: bool, items: [ {…that same shape per field…} ] }`.

Deliberately NOT in the wire schema: a group's `maxItems`. Providers' structured-
output dialects are a restricted subset of JSON Schema — OpenAI's drops
minItems/maxItems (and minLength/pattern/format) and rejects schemas carrying
them. The cap is our policy anyway, not the model's contract: it is stated in the
prompt and enforced in validation.py, which truncates and reports it as a repair.
"""
from __future__ import annotations

from . import features as feat


def value_schema(leaf) -> dict:
    """Map a declared feature type to a JSON-Schema fragment for its value."""
    t = leaf.type
    nullable = leaf.nullable

    def nul(base):
        return [base, "null"] if nullable else base

    if t == "string":
        return {"type": nul("string")}
    if t == "number":
        return {"type": nul("number")}
    if t == "boolean":
        return {"type": nul("boolean")}
    if t == "enum":
        vals = list(leaf.enum_values)
        if nullable:
            return {"type": ["string", "null"], "enum": vals + [None]}
        return {"type": "string", "enum": vals}
    if t == "array<string>":
        return {"type": "array", "items": {"type": "string"}}
    if t == "array<number>":
        return {"type": "array", "items": {"type": "number"}}
    raise ValueError(f"Unknown feature type: {t}")


def _leaf_node(leaf) -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["present", "value", "evidence"],
        "properties": {
            "present": {"type": "boolean"},
            "value": value_schema(leaf),
            # evidence is always nullable: inferred/absent values have no quote.
            "evidence": {"type": ["string", "null"]},
        },
    }


def _row_node(group) -> dict:
    """One entry of a repeating group — the same per-field grammar, one level in."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [f.name for f in group.features],
        "properties": {f.name: _leaf_node(f) for f in group.features},
    }


def _group_node(group) -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["present", "items"],
        "properties": {
            # present=false means "this paper reports none" — a real answer,
            # distinct from the extraction having failed.
            "present": {"type": "boolean"},
            "items": {"type": "array", "items": _row_node(group)},
        },
    }


def build_response_schema(features) -> dict:
    """Strict object schema for the whole extraction response."""
    props = {}
    for f in features:
        props[f.name] = _group_node(f) if f.is_group else _leaf_node(f)
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [f.name for f in features],
        "properties": props,
    }


# --------------------------------------------------------------------------- #
# Prompt
# --------------------------------------------------------------------------- #
def _field_line(leaf, indent="- ") -> str:
    return f"{indent}{leaf.name} ({leaf.type_hint()}): {leaf.description}".rstrip()


def _shape_line(leaf, indent) -> str:
    return (
        f'{indent}"{leaf.name}": {{"present": true|false, '
        f'"value": <{leaf.type_hint()}>, "evidence": "<verbatim quote or null>"}}'
    )


def build_prompt(base_text: str, features, context: str | None = None,
                  group_items: dict | None = None) -> str:
    """Append a config-derived Fields section + JSON shape to the base prompt.

    Used for every request; for prompt-only models (no response_format) this is
    the only place the required shape is communicated.

    `context` is the project's own framing — its name and description (see
    projects.Project.prompt_context). It sits between the base rules and the
    field list because it qualifies how the fields should be read: "prefer the
    replication p-value" only makes sense before you see `p_value`.

    `group_items` is {groupName: [label, ...]} — the pass-1 list after curator
    review for this paper (server/extraction.py, store.load_group_items). When a
    group's name is present here (even as an empty list), the prompt states the
    fixed ordered list. Absent entirely falls back to generic preview wording;
    a real group extraction never uses that path.

    NOTE: the whole return value is what util.prompt_id() must hash, not just
    base_text — feature descriptions, group structure and this context are all
    prompt material, so a run's prompt identity has to cover them. See
    extraction.extract_one.
    """
    leaves = feat.leaves(features)
    groups = feat.groups(features)
    group_items = group_items or {}

    lines = [base_text.strip()]
    if context and context.strip():
        lines += ["", context.strip()]

    if leaves:
        lines += ["", "Fields (one value per paper):"]
        lines += [_field_line(f) for f in leaves]

    for g in groups:
        lines += ["", f'Repeating group "{g.name}"' + (f" — {g.description}" if g.description else "")]
        if g.name in group_items:
            declared = group_items[g.name]
            lines.append(
                f"This paper has exactly {len(declared)} entries in this group, "
                "already identified — in this order:"
            )
            lines += [f"  {i + 1}. {label}" for i, label in enumerate(declared)]
            lines.append(
                f"Return EXACTLY {len(declared)} result object{'s' if len(declared) != 1 else ''}, "
                "in the SAME ORDER as listed above, one per entry — do not add, omit, merge, "
                "or reorder them, and do not include an identifier field of your own."
            )
        else:
            lines.append(
                "This is a project preview, so there is no paper-specific list yet. "
                "In live curation, pass 1 identifies the entries; after curator review, "
                "pass 2 receives that fixed ordered list and returns exactly one result "
                "per reviewed entry."
            )
        lines.append("Each entry has these fields:")
        lines += [_field_line(f, indent="  - ") for f in g.features]

    lines += [
        "",
        "Return ONLY valid JSON with exactly this shape (no prose, no code fences):",
        "{",
    ]
    parts = [_shape_line(f, "  ") for f in leaves]
    for g in groups:
        row = ",\n".join(_shape_line(f, "      ") for f in g.features)
        parts.append(
            f'  "{g.name}": {{"present": true|false, "items": [\n'
            f"    {{\n{row}\n    }}\n"
            f"  ]}}"
        )
    lines.append(",\n".join(parts))
    lines.append("}")
    return "\n".join(lines)
