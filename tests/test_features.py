"""Feature schema parsing + group coercion.
Run: python -m annotaid.tests.test_features

These are the rules a project manager will hit in the feature editor, so the
messages matter as much as the verdicts.
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from annotaid.server import features as feat  # noqa: E402
from annotaid.server import schema_builder, validation  # noqa: E402
from annotaid.server.config_loader import ConfigError, parse_config  # noqa: E402

PASSED = []


def check(label, cond, detail=""):
    if not cond:
        raise AssertionError(f"{label}  {detail}")
    PASSED.append(label)
    print(f"  ok  {label}")


def rejects(label, fn, *a, **kw):
    try:
        fn(*a, **kw)
    except (feat.FeatureError, ConfigError) as exc:
        PASSED.append(label)
        print(f"  ok  {label}\n        -> {exc}")
        return
    raise AssertionError(f"{label}: expected a rejection, got none")


GROUP = {
    "name": "variants", "type": "group",
    "description": "One per variant.",
    "maxItems": 3,
    "features": [
        {"name": "rsid", "type": "string", "identifier": True, "description": "rsID."},
        {"name": "variant_id", "type": "string", "identifier": True, "description": "Positional id."},
        {"name": "odds_ratio", "type": "number", "description": "OR."},
        {"name": "risk_allele", "type": "enum", "enumValues": ["A", "C", "G", "T"],
         "description": "Effect allele."},
    ],
}
LEAF = {"name": "sample_size", "type": "number", "description": "Total N."}


def main():
    print("\nparsing")
    fs = feat.parse_features([LEAF, GROUP])
    check("a leaf and a group parse", len(fs) == 2)
    check("the leaf is not a group", fs[0].is_group is False)
    check("the group is a group", fs[1].is_group is True)
    check("defaults applied exactly once", fs[0].nullable is True)
    check("group children parsed", len(fs[1].features) == 4)
    check("identifiers picked up",
          [f.name for f in fs[1].identifiers()] == ["rsid", "variant_id"])
    check("dotted paths enumerated",
          feat.paths(fs) == ["sample_size", "variants.rsid", "variants.variant_id",
                             "variants.odds_ratio", "variants.risk_allele"])
    check("find by dotted path", feat.find(fs, "variants.odds_ratio").type == "number")
    check("find by plain name", feat.find(fs, "sample_size").type == "number")
    check("find returns None for a bogus path", feat.find(fs, "variants.nope") is None)
    check("leaves/groups split", len(feat.leaves(fs)) == 1 and len(feat.groups(fs)) == 1)

    print("\nrow naming")
    g = fs[1]
    check("named by the first identifier with a value",
          g.row_label({"rsid": "rs1801133", "variant_id": None}) == "rs1801133")
    check("falls back to the next identifier",
          g.row_label({"rsid": None, "variant_id": "chr1:123:A:G"}) == "chr1:123:A:G")
    # A row with no identifier is KEPT and flagged, not rejected — same policy as
    # an evidence quote that doesn't match the PDF.
    check("an unidentified row still gets a label",
          g.row_label({}) == "(unidentified)")

    print("\nrules the editor has to enforce")
    rejects("a group inside a group (the two-level cap)", feat.parse_features,
            [{"name": "a", "type": "group", "features": [dict(GROUP)]}])
    rejects("a second repeating group", feat.parse_features,
            [dict(GROUP), dict(GROUP, name="haplotypes")])
    no_ident = feat.parse_features(
        [{"name": "v", "type": "group",
          "features": [{"name": "x", "type": "string"}]}]
    )
    check("a group with no identifier is fine now — row identity is "
          "curator-declared, not extracted (server/project_store.load_group_items)",
          not no_ident[0].identifiers())
    rejects("a group with no features", feat.parse_features,
            [{"name": "v", "type": "group", "features": []}])
    rejects("an identifier outside a group", feat.parse_features,
            [{"name": "x", "type": "string", "identifier": True}])
    rejects("duplicate names at the same level", feat.parse_features, [LEAF, LEAF])
    rejects("duplicate names inside a group", feat.parse_features,
            [{"name": "v", "type": "group", "features": [
                {"name": "x", "type": "string", "identifier": True},
                {"name": "x", "type": "number"}]}])
    rejects("an unknown type", feat.parse_features,
            [{"name": "x", "type": "datetime"}])
    rejects("enum with no values", feat.parse_features,
            [{"name": "x", "type": "enum"}])
    rejects("enumValues on a non-enum", feat.parse_features,
            [{"name": "x", "type": "number", "enumValues": ["a"]}])
    rejects("min above maxItems", feat.parse_features,
            [dict(GROUP, min=9, maxItems=2)])
    rejects("a nameless feature", feat.parse_features, [{"type": "string"}])

    # The SAME name at two levels is legal and is the reason for nesting.
    both = feat.parse_features([
        {"name": "p_value", "type": "number", "description": "Study-level."},
        dict(GROUP, features=GROUP["features"] + [
            {"name": "p_value", "type": "number", "description": "Per variant."}]),
    ])
    check("the same name at two levels is allowed",
          feat.find(both, "p_value").description == "Study-level."
          and feat.find(both, "variants.p_value").description == "Per variant.")

    print("\nround-trip through the document")
    doc = {"features": [LEAF, GROUP],
           "models": [{"slug": "m/one", "label": "one"}],
           "prompts": [{"label": "p", "text": "Extract."}],
           "settings": {"allowPapersWithoutPmid": True}}
    cfg = parse_config(doc, label="test")
    check("config exposes the policy", cfg.allow_papers_without_pmid is True)
    check("missing policy reads as False (opt-in, not opt-out)",
          parse_config({k: v for k, v in doc.items() if k != "settings"},
                       label="t").allow_papers_without_pmid is False)
    again = parse_config(cfg.portable_dict(), label="round2")
    check("portable_dict re-parses identically",
          feat.paths(again.features) == feat.paths(cfg.features))
    check("the policy survives the round-trip", again.allow_papers_without_pmid is True)

    doc_abs = {**doc, "settings": {"allowExtractionOnAbstract": True}}
    cfg_abs = parse_config(doc_abs, label="test-abs")
    check("abstract-extraction policy exposed", cfg_abs.allow_extraction_on_abstract is True)
    check("missing abstract policy reads as False (opt-in, not opt-out)",
          parse_config({k: v for k, v in doc.items() if k != "settings"},
                       label="t").allow_extraction_on_abstract is False)
    check("the abstract policy survives the round-trip",
          parse_config(cfg_abs.portable_dict(), label="round2-abs")
          .allow_extraction_on_abstract is True)
    check("group cell templates carry type + empty",
          feat.group_cell_templates(cfg.features)["variants"]["odds_ratio"]
          == {"type": "number", "empty": None})

    print("\ncoercing a model's group reply")
    schema = schema_builder.build_response_schema(cfg.features)
    clean = (
        '{"sample_size": {"present": true, "value": 4571, "evidence": "4571 adults"},'
        ' "variants": {"present": true, "items": ['
        '   {"rsid": {"present": true, "value": "rs1801133", "evidence": "rs1801133"},'
        '    "variant_id": {"present": false, "value": null, "evidence": null},'
        '    "odds_ratio": {"present": true, "value": 1.42, "evidence": "OR=1.42"},'
        '    "risk_allele": {"present": true, "value": "T", "evidence": "T allele"}}'
        ' ]}}'
    )
    canon, status, errs = validation.process(clean, cfg.features, schema)
    check("a well-formed nested reply is clean", status == "ok", f"{status} {errs}")
    check("the row came through", len(canon["variants"]["items"]) == 1)
    check("a nested value is typed",
          canon["variants"]["items"][0]["odds_ratio"]["value"] == 1.42)

    # Models very often return a bare array instead of {present, items}.
    bare = ('{"sample_size": 4571, "variants": ['
            '{"rsid": "rs999", "odds_ratio": "2.5", "risk_allele": "g"}]}')
    canon2, status2, _ = validation.process(bare, cfg.features, schema)
    check("a bare array is accepted as the item list", status2 == "repaired", status2)
    row = canon2["variants"]["items"][0]
    check("bare field values are wrapped and coerced", row["odds_ratio"]["value"] == 2.5)
    check("a missing nested field is filled, not dropped",
          row["variant_id"]["present"] is False)
    check("group present is inferred from the rows",
          canon2["variants"]["present"] is True)
    # 'g' is not one of A/C/G/T — case matters for an allele, so it is NOT
    # silently upper-cased into a match.
    check("an out-of-range enum becomes null rather than a guess",
          row["risk_allele"]["value"] is None)

    empty = '{"sample_size": null, "variants": {"present": false, "items": []}}'
    canon3, status3, _ = validation.process(empty, cfg.features, schema)
    check('"this paper reports none" survives as an explicit answer',
          canon3["variants"]["present"] is False
          and canon3["variants"]["items"] == [])

    print("\nthe row cap")
    many = ('{"sample_size": 1, "variants": [' + ",".join(
        '{"rsid": "rs%d"}' % i for i in range(10)) + ']}')
    canon4, status4, errs4 = validation.process(many, cfg.features, schema)
    check("rows over maxItems are dropped", len(canon4["variants"]["items"]) == 3)
    check("...and dropping them is reported as a repair, never as clean",
          status4 == "repaired", status4)
    check("...with a message naming the group",
          any("variants" in e for e in errs4), errs4)
    check("the bookkeeping key never leaks into the record",
          "truncated" not in canon4["variants"])

    missing = '{"sample_size": 5}'
    canon5, status5, _ = validation.process(missing, cfg.features, schema)
    check("an omitted group is repaired to absent-and-empty",
          canon5["variants"] == {"present": False, "items": []})
    check("...and that is a repair", status5 == "repaired")

    print(f"\n{len(PASSED)} checks passed.")


if __name__ == "__main__":
    main()
