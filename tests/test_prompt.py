"""Prompt composition + prompt identity — run: python -m annotaid.tests.test_prompt

The point of this test: a run's prompt identity must cover everything that
actually shaped the prompt. Feature descriptions and the project's own
description are prompt material, so two projects sharing one base template do NOT
share a prompt, and editing a description must change the recorded hash.
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from annotaid.server import features as feat  # noqa: E402
from annotaid.server import schema_builder, util  # noqa: E402
from annotaid.server.projects import Project  # noqa: E402

PASSED = []


def check(label, cond):
    if not cond:
        raise AssertionError(label)
    PASSED.append(label)
    print(f"  ok  {label}")


RAW = [
    {"name": "p_value", "type": "number",
     "description": "The reported p-value for the primary result."},
    {"name": "ancestry", "type": "enum", "enumValues": ["European", "African"],
     "description": "Participant ancestry as the authors describe it."},
]
RAW_WITH_GROUP = RAW + [
    {"name": "variants", "type": "group",
     "description": "One entry per genetic variant reported.",
     "maxItems": 12,
     "features": [
         {"name": "rsid", "type": "string", "identifier": True,
          "description": "The dbSNP rsID, e.g. rs1801133."},
         {"name": "variant_id", "type": "string", "identifier": True,
          "description": "A positional id used when no rsID is given."},
         {"name": "p_value", "type": "number",
          "description": "The p-value for THIS variant's association."},
     ]},
]
FEATURES = feat.parse_features(RAW)
FEATURES_WITH_GROUP = feat.parse_features(RAW_WITH_GROUP)
BASE = "You are a biomedical curation assistant."


def project(name, description):
    return Project(name.lower(), name, description, None, None, "now")


def main():
    print("\ncomposition")
    gwas = project("GWAS pilot", "Prefer the replication p-value where both are given.")
    prompt = schema_builder.build_prompt(BASE, FEATURES, gwas.prompt_context())

    check("base rules present", BASE in prompt)
    check("project name present", "Curation project: GWAS pilot" in prompt)
    check("project description present",
          "Prefer the replication p-value" in prompt)
    for f in FEATURES:
        check(f"feature description present: {f.name}", f.description in prompt)
    check("enum values spelled out for the model",
          "one of: European, African" in prompt)
    check("context sits before the field list — it qualifies how fields are read",
          prompt.index("Curation project") < prompt.index("Fields ("))
    check("json shape still communicated",
          '"p_value": {"present": true|false' in prompt)

    print("\nrepeating group")
    gp = schema_builder.build_prompt(BASE, FEATURES_WITH_GROUP, gwas.prompt_context())
    check("the group is announced by name", 'Repeating group "variants"' in gp)
    check("the group description is included",
          "One entry per genetic variant reported." in gp)
    check("preview explains pass 1", "pass 1 identifies the entries" in gp)
    check("preview explains review before pass 2", "after curator review" in gp)
    check("nested field descriptions reach the pass-2 prompt preview",
          "The p-value for THIS variant's association." in gp)
    check("the nested JSON shape is shown",
          '"variants": {"present": true|false, "items": [' in gp)
    check("paper-level fields are separated from the group",
          gp.index("Fields (one value per paper)") < gp.index("Repeating group"))

    print("\nresponse schema")
    sch = schema_builder.build_response_schema(FEATURES_WITH_GROUP)
    check("every top-level feature is required",
          set(sch["required"]) == {"p_value", "ancestry", "variants"})
    gnode = sch["properties"]["variants"]
    check("group node is {present, items}",
          set(gnode["required"]) == {"present", "items"})
    row = gnode["properties"]["items"]["items"]
    check("a row requires every declared field",
          set(row["required"]) == {"rsid", "variant_id", "p_value"})
    check("a row field keeps the {present,value,evidence} grammar",
          set(row["properties"]["rsid"]["required"]) == {"present", "value", "evidence"})
    check("strict objects throughout (providers demand it)",
          sch["additionalProperties"] is False
          and gnode["additionalProperties"] is False
          and row["additionalProperties"] is False)
    # OpenAI-family structured outputs reject minItems/maxItems; the cap is our
    # policy and lives in validation.py instead.
    check("maxItems is NOT in the wire schema",
          "maxItems" not in gnode["properties"]["items"])
    check("a nested p_value does not collide with the study-level one",
          sch["properties"]["p_value"]["properties"]["value"]["type"] == ["number", "null"]
          and row["properties"]["p_value"]["properties"]["value"]["type"] == ["number", "null"])

    print("\nno context")
    bare = schema_builder.build_prompt(BASE, FEATURES)
    check("omitting context is still a valid prompt",
          "Fields (" in bare and "Curation project" not in bare)
    check("blank context adds no empty block",
          schema_builder.build_prompt(BASE, FEATURES, "   ") == bare)

    print("\nprompt identity")
    other = project("Paediatric cohorts", "Under-18 participants only.")
    p2 = schema_builder.build_prompt(BASE, FEATURES, other.prompt_context())
    check("same template, different project -> different prompt", prompt != p2)
    check("...and therefore a different promptHash",
          util.prompt_id(prompt) != util.prompt_id(p2))
    check("the TEMPLATE hash is identical for both — which is exactly why a "
          "template id alone cannot identify a run's prompt",
          util.prompt_id(BASE) == util.prompt_id(BASE))

    edited = feat.parse_features(
        [dict(RAW[0], description="The p-value of the REPLICATION cohort."), RAW[1]]
    )
    p3 = schema_builder.build_prompt(BASE, edited, gwas.prompt_context())
    check("editing a feature description changes the promptHash",
          util.prompt_id(prompt) != util.prompt_id(p3))

    renamed = project("GWAS pilot", "A completely different instruction.")
    p4 = schema_builder.build_prompt(BASE, FEATURES, renamed.prompt_context())
    check("editing the project description changes the promptHash",
          util.prompt_id(prompt) != util.prompt_id(p4))

    check("adding a group changes the promptHash",
          util.prompt_id(prompt) != util.prompt_id(gp))
    check("hashing is stable across calls",
          util.prompt_id(prompt) == util.prompt_id(
              schema_builder.build_prompt(BASE, FEATURES, gwas.prompt_context())))

    print(f"\n{len(PASSED)} checks passed.")


if __name__ == "__main__":
    main()
