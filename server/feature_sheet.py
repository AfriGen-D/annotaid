"""A feature set as a spreadsheet: download a fill-in-the-blanks CSV, upload it
back as a draft feature list.

Plain CSV, not a real .xlsx workbook — Excel opens, edits and saves CSV natively,
and this way both directions are the stdlib `csv` module with no new dependency
(the backend is otherwise stdlib-only + jsonschema). The cost is no dropdown
validation on the Type column and no locked header row; that gap is covered by
running every upload through features.parse_features(), the SAME validator the
visual editor posts drafts to, so a typo surfaces as a clear message rather than
a corrupted config.

One flat sheet describes the two-level tree: a row with type=group DECLARES a
group (its own Name/Label/Description/Max entries), and every row underneath it
with that name in the Group column is one of its fields — placed AFTER the
group's own row, matching how the template lays it out.
"""
from __future__ import annotations

import csv
import io

from . import features as feat

# canonical column -> every header spelling accepted, normalised (lower, spaces
# and punctuation collapsed to single underscores) before matching.
_HEADER_ALIASES = {
    "name": {"field_name", "name", "field"},
    "group": {"group"},
    "type": {"type"},
    "label": {"label", "display_label", "display_name"},
    "description": {"description"},
    "enum_values": {"allowed_values", "enum_values", "enumvalues"},
    "optional": {"optional", "may_be_absent", "nullable"},
    "identifier": {"identifies_entry", "identifier", "names_the_entry"},
    "max_entries": {"max_entries", "max_items", "maxitems"},
}
_REQUIRED = ("name", "type")

_TYPE_ALIASES = {
    "string": "string", "text": "string",
    "number": "number",
    "boolean": "boolean", "bool": "boolean", "true/false": "boolean", "yes/no": "boolean",
    "enum": "enum", "one_of_a_fixed_list": "enum", "choice": "enum", "list": "enum",
    "array<string>": "array<string>", "list_of_text": "array<string>", "text_list": "array<string>",
    "array<number>": "array<number>", "list_of_numbers": "array<number>", "number_list": "array<number>",
    "group": "group", "repeating_group": "group",
}
_TRUE = {"true", "yes", "y", "1"}
_FALSE = {"false", "no", "n", "0", ""}


class SheetError(Exception):
    """A problem with the FILE — bad header, unreadable type keyword, a field
    referencing a group that was never declared. Distinct from FeatureError,
    which is a problem with the FEATURES those rows describe."""


def _norm(s: str) -> str:
    return "_".join(str(s or "").strip().lower().split())


def _header_map(fieldnames) -> dict:
    by_norm = {_norm(h): h for h in (fieldnames or [])}
    out = {}
    for canon, spellings in _HEADER_ALIASES.items():
        for spelling in spellings:
            if spelling in by_norm:
                out[canon] = by_norm[spelling]
                break
    missing = [c for c in _REQUIRED if c not in out]
    if missing:
        raise SheetError(
            "the spreadsheet is missing a required column: " + ", ".join(missing)
            + " (expected a header row with at least Field name and Type)"
        )
    return out


def _cell(row: dict, cols: dict, key: str) -> str:
    col = cols.get(key)
    return str(row.get(col) or "").strip() if col else ""


def _parse_bool(raw: str, default: bool, where: str, field: str):
    v = _norm(raw)
    if v in _TRUE:
        return True
    if v in _FALSE:
        return default if v == "" else False
    raise SheetError(f"{where}: {field!r} column has {raw!r} — use TRUE or FALSE")


def rows_to_features(rows: list) -> list:
    """[{canonical_column: cell, ...}, ...] -> a raw features list, the same
    shape the visual editor edits. Raises SheetError for anything that can't be
    expressed as that shape at all; everything else (duplicate names, an enum
    with no values, an empty repeating group, name characters) is left to
    features.parse_features so there is exactly one place those rules live.
    """
    top_level, groups_by_name = [], {}

    for i, row in enumerate(rows, start=2):  # row 1 is the header
        name = row.get("name", "").strip()
        if not name and not row.get("type", "").strip():
            continue  # a blank trailing row — spreadsheets are full of these
        where = f"row {i}"
        if not name:
            raise SheetError(f"{where}: has a type but no field name")

        group_name = row.get("group", "").strip()
        raw_type = _norm(row.get("type", ""))
        if not raw_type:
            raise SheetError(f"{where} ({name}): the Type column is empty")
        ftype = _TYPE_ALIASES.get(raw_type)
        if ftype is None:
            raise SheetError(
                f"{where} ({name}): {row.get('type')!r} isn't a type this app knows — "
                "use text, number, boolean, enum, list of text, list of numbers, or group"
            )

        if ftype == "group":
            if group_name:
                raise SheetError(
                    f"{where} ({name}): a group's own row must have an EMPTY Group "
                    "column — a group cannot be nested inside another group"
                )
            g = {"name": name, "type": "group", "features": []}
            if row.get("label", "").strip():
                g["label"] = row["label"].strip()
            if row.get("description", "").strip():
                g["description"] = row["description"].strip()
            max_raw = row.get("max_entries", "").strip()
            if max_raw:
                try:
                    g["maxItems"] = int(max_raw)
                except ValueError:
                    raise SheetError(f"{where} ({name}): Max entries must be a whole number")
            groups_by_name[name] = g
            top_level.append(g)
            continue

        leaf = {"name": name, "type": ftype}
        if row.get("label", "").strip():
            leaf["label"] = row["label"].strip()
        if row.get("description", "").strip():
            leaf["description"] = row["description"].strip()
        leaf["nullable"] = _parse_bool(row.get("optional", ""), True, where, "Optional")
        if ftype == "enum":
            leaf["enumValues"] = [
                v.strip() for v in row.get("enum_values", "").replace("\n", ",").split(",")
                if v.strip()
            ]

        if group_name:
            leaf["identifier"] = _parse_bool(
                row.get("identifier", ""), False, where, "Identifies entry"
            )
            target = groups_by_name.get(group_name)
            if target is None:
                raise SheetError(
                    f"{where} ({name}): Group is {group_name!r}, but no row above it "
                    f"declares that group — add a row with Type=group and "
                    f"Field name={group_name!r} before this one"
                )
            target["features"].append(leaf)
        else:
            if row.get("identifier", "").strip():
                raise SheetError(
                    f"{where} ({name}): 'Identifies entry' only means something for a "
                    "field inside a group — this row has no Group set"
                )
            top_level.append(leaf)

    if not top_level:
        raise SheetError("the spreadsheet has no rows to import")
    return top_level


def parse_sheet_csv(text: str) -> list:
    """Full pipeline: CSV text -> validated raw features list.

    Raises SheetError (a file-shaped problem) or features.FeatureError (a
    feature-shaped problem, e.g. an empty repeating group) — the caller
    presents either the same way, since a project manager filling in a
    spreadsheet doesn't need to know which layer caught it.
    """
    try:
        reader = csv.DictReader(io.StringIO(text))
    except csv.Error as exc:
        raise SheetError(f"could not read that as CSV: {exc}")
    cols = _header_map(reader.fieldnames)
    canon_rows = [{k: _cell(row, cols, k) for k in _HEADER_ALIASES} for row in reader]
    top_level = rows_to_features(canon_rows)
    feat.parse_features(top_level, where="uploaded spreadsheet")  # raises FeatureError
    return top_level


# --------------------------------------------------------------------------- #
# Template
# --------------------------------------------------------------------------- #
_TEMPLATE_HEADER = [
    "Field name", "Group", "Type", "Label", "Description",
    "Allowed values", "Optional", "Identifies entry", "Max entries",
]
_TEMPLATE_ROWS = [
    ["p_value", "", "number", "", "The reported p-value for the study's primary result.",
     "", "TRUE", "", ""],
    ["countries", "", "list of text", "",
     "Country or countries the study participants are from.", "", "TRUE", "", ""],
    ["ancestry", "", "enum", "", "Participant ancestry as the authors describe it.",
     "European, African, Asian, Mixed", "TRUE", "", ""],
    ["variants", "", "group", "Variants",
     "One entry per genetic variant the paper reports an association for.",
     "", "", "", "50"],
    ["gene", "variants", "string", "", "Gene mapped to this variant.", "", "TRUE", "", ""],
    ["odds_ratio", "variants", "number", "", "Odds ratio reported for this variant.",
     "", "TRUE", "", ""],
]


def build_template_csv() -> str:
    """A fill-in-the-blanks starting point with paper-level fields and one
    repeating group. Pass 1 discovers the group's row identifiers, so the
    nested example contains only values pass 2 should curate."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(_TEMPLATE_HEADER)
    w.writerows(_TEMPLATE_ROWS)
    return buf.getvalue()
