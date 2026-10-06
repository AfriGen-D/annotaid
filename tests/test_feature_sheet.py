"""Spreadsheet -> feature list. Run: python -m annotaid.tests.test_feature_sheet

The template and the parser are two ends of the same pipe: the template must
round-trip clean, and the parser must give a project manager a message they can
act on in Excel (which row, what to fix) for everything wrong they might type.
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from annotaid.server import feature_sheet as sheet  # noqa: E402
from annotaid.server import features as feat  # noqa: E402
from annotaid.server.config_loader import parse_config  # noqa: E402

PASSED = []


def check(label, cond, detail=""):
    if not cond:
        raise AssertionError(f"{label}  {detail}")
    PASSED.append(label)
    print(f"  ok  {label}")


def rejects(label, exc_types, fn, *a, **kw):
    try:
        fn(*a, **kw)
    except exc_types as exc:
        PASSED.append(label)
        print(f"  ok  {label}\n        -> {exc}")
        return
    raise AssertionError(f"{label}: expected a rejection, got none")


HEADER = "Field name,Group,Type,Label,Description,Allowed values,Optional,Identifies entry,Max entries"


def csv(*rows):
    return "\n".join([HEADER] + list(rows))


def main():
    print("\nthe template itself")
    tpl = sheet.build_template_csv()
    check("template is non-empty CSV text", tpl.startswith("Field name,"))
    features = sheet.parse_sheet_csv(tpl)
    check("template round-trips clean", len(features) == 4)
    check("plain features parsed", features[0]["name"] == "p_value"
          and features[0]["type"] == "number")
    check("list-of-text mapped correctly", features[1]["type"] == "array<string>")
    check("enum values split on comma",
          features[2]["enumValues"] == ["European", "African", "Asian", "Mixed"])
    group = features[3]
    check("the group and its fields nested", group["type"] == "group"
          and [f["name"] for f in group["features"]] == ["gene", "odds_ratio"])
    check("row identifiers are not duplicated as pass-2 fields",
          not any(f.get("identifier") for f in group["features"]))
    check("max entries parsed as an int", group["maxItems"] == 50)
    # The whole point of validating through features.parse_features: the
    # template must also survive the SAME rules a hand-built config does.
    cfg = parse_config({"features": features,
                        "models": [{"slug": "m/1"}],
                        "prompts": [{"label": "p", "text": "Extract."}]},
                       label="template")
    check("the template parses as a real project config", len(cfg.groups()) == 1)

    print("\nheader tolerance")
    friendly = csv("p_value,,number,,A p-value.,,TRUE,,")
    check("friendly headers (as shipped) work",
          sheet.parse_sheet_csv(friendly)[0]["name"] == "p_value")
    technical = ("field_name,group,type,label,description,enum_values,optional,identifier,max_items\n"
                "p_value,,number,,A p-value.,,TRUE,,")
    check("technical header spellings also work",
          sheet.parse_sheet_csv(technical)[0]["name"] == "p_value")
    mixed_case = ("  Field Name , Group,TYPE,label,description,allowed values,Optional,identifies entry,max entries\n"
                 "p_value,,number,,A p-value.,,TRUE,,")
    check("header case/spacing is forgiving",
          sheet.parse_sheet_csv(mixed_case)[0]["name"] == "p_value")
    rejects("missing required columns named clearly", sheet.SheetError,
           sheet.parse_sheet_csv, "Group,Description\nx,y")

    print("\nrow-shaped problems (SheetError, not silent corruption)")
    rejects("unknown type keyword names the bad value", sheet.SheetError,
           sheet.parse_sheet_csv, csv("x,,datetime,,d,,TRUE,,"))
    rejects("blank type", sheet.SheetError,
           sheet.parse_sheet_csv, csv("x,,,,d,,TRUE,,"))
    rejects("a field referencing an undeclared group names it and says how to fix it",
           sheet.SheetError, sheet.parse_sheet_csv,
           csv("rsid,variants,string,,d,,TRUE,TRUE,"))
    rejects("a group's own row cannot itself have a Group set (nesting)",
           sheet.SheetError, sheet.parse_sheet_csv,
           csv("outer,,group,,d,,,,50", "inner,outer,group,,d,,,,10"))
    rejects("'identifies entry' outside any group is refused, not ignored",
           sheet.SheetError, sheet.parse_sheet_csv,
           csv("x,,string,,d,,TRUE,TRUE,"))
    rejects("unparseable Optional value", sheet.SheetError,
           sheet.parse_sheet_csv, csv("x,,string,,d,,maybe,,"))
    rejects("unparseable Max entries", sheet.SheetError,
           sheet.parse_sheet_csv, csv("g,,group,,d,,,,many",))
    rejects("a header-only file has nothing to import", sheet.SheetError,
           sheet.parse_sheet_csv, HEADER)

    print("\nboolean spellings a manager might actually type")
    for val in ["TRUE", "true", "Yes", "y", "1"]:
        f = sheet.parse_sheet_csv(csv(f"x,,string,,d,,{val},,"))[0]
        check(f"Optional={val!r} -> nullable True", f["nullable"] is True)
    for val in ["FALSE", "false", "No", "n", "0"]:
        f = sheet.parse_sheet_csv(csv(f"x,,string,,d,,{val},,"))[0]
        check(f"Optional={val!r} -> nullable False", f["nullable"] is False)
    check("blank Optional defaults to nullable True (same default as everywhere else)",
          sheet.parse_sheet_csv(csv("x,,string,,d,,,,"))[0]["nullable"] is True)

    print("\nforgiving of real spreadsheet mess")
    messy = csv("p_value,,number,,A p-value.,,TRUE,,", ",,,,,,,,", "  ,,,,,,,,")
    check("blank trailing rows are skipped silently",
          len(sheet.parse_sheet_csv(messy)) == 1)
    multiline_enum = csv('x,,enum,,d,"A\nB\nC",TRUE,,')
    check("enum values may be newline- as well as comma-separated",
          sheet.parse_sheet_csv(multiline_enum)[0]["enumValues"] == ["A", "B", "C"])

    no_ident = sheet.parse_sheet_csv(csv("g,,group,,d,,,,10", "child,g,string,,d,,TRUE,,"))
    check("a group with no identifier column is fine now — row identity is "
          "curator-declared, not extracted", no_ident[0]["type"] == "group")

    print("\nrows the FILE is fine, but the FEATURES are not — features.py catches it")
    rejects("duplicate names", feat.FeatureError,
           sheet.parse_sheet_csv, csv("x,,string,,d,,TRUE,,", "x,,number,,d,,TRUE,,"))
    rejects("enum with no allowed values", feat.FeatureError,
           sheet.parse_sheet_csv, csv("x,,enum,,d,,TRUE,,"))
    rejects("a dotted field name", feat.FeatureError,
           sheet.parse_sheet_csv, csv("a.b,,string,,d,,TRUE,,"))

    print(f"\n{len(PASSED)} checks passed.")


if __name__ == "__main__":
    main()
