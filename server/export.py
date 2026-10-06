"""Export — available at any point (brief §7 decision).

CSV is config-driven so the manager can paste curated rows into the global
sheet. It uses the curator's final `value`, not the original AI value.
"""
from __future__ import annotations

import csv
import io

from .config_loader import Config
from .project_store import ProjectStore


def _render_value(value, feature_type, csvcfg) -> str:
    if feature_type == "boolean":
        if value is None:
            return csvcfg.get("nullText", "")
        return csvcfg.get("boolTrue", "TRUE") if value else csvcfg.get("boolFalse", "FALSE")
    if feature_type and feature_type.startswith("array"):
        if not value:
            return csvcfg.get("nullText", "")
        return csvcfg.get("arrayDelimiter", "; ").join(str(v) for v in value)
    if value is None or value == "":
        return csvcfg.get("nullText", "")
    return str(value)


def _cell(source, run, paper, row, feature_types, csvcfg) -> str:
    """`row` is the current group row when rowScope names a group, else None.

    A dotted source ("variants.p_value") reads from that row. Dotted paths are
    why nesting beats a flat namespace: a study-level p_value and a per-variant
    p_value are different facts that may share a name.
    """
    # Paper-identity sources. Not every paper has a PMID, so `doi` / `filename` /
    # `uid` are offered too — a project whose papers have no PubMed IDs can still
    # produce a keyed sheet.
    if source == "pmid":
        return run.get("pmid") or ""
    if source == "doi":
        return (paper or {}).get("doi") or ""
    if source == "uid":
        return run.get("uid", "")
    if source == "filename":
        return (paper or {}).get("filename") or ""
    if source == "modelId":
        return run.get("modelLabel") or run.get("modelId", "")

    if "." in source:
        group_name, _, field = source.partition(".")
        if row is None or row.get("_group") != group_name:
            return ""
        if field == "$label":
            # The row's identity is curator-reviewed (server/project_store.
            # load_group_items), not a pass-2 feature — it lives on the paper, joined
            # in here by rowId, never inside run_features.
            items = {it["rowId"]: it["label"] for it in (paper or {}).get("groupItems") or []}
            return items.get(row.get("rowId"), "")
        cell = (row.get("features") or {}).get(field)
        return "" if cell is None else _render_value(
            cell.get("value"), feature_types.get(source), csvcfg
        )

    cell = run.get("features", {}).get(source)
    if cell is not None:
        return _render_value(cell.get("value"), feature_types.get(source), csvcfg)
    # literal / unmapped column -> emit empty to preserve sheet alignment
    return ""


def _default_columns(features) -> list:
    """Default column set when a project has no export.csv.columns of its own:
    every curatable path, groups expanded — plus, for the one group a project
    may have, a leading column for its reviewed row identity ($label),
    since that identity is no longer necessarily one of the group's own
    features (server/project_store.load_group_items)."""
    out = []
    for f in features:
        if f.is_group:
            out.append({"header": f.display, "source": f"{f.name}.$label"})
            out.extend(
                {"header": f"{f.name}.{c.name}", "source": f"{f.name}.{c.name}"}
                for c in f.features
            )
        else:
            out.append({"header": f.name, "source": f.name})
    return out


def export_csv(store: ProjectStore, config: Config) -> str:
    csvcfg = (config.export.get("csv") or {})
    columns = csvcfg.get("columns") or [
        {"header": "pubmed_id", "source": "pmid"},
        {"header": "model", "source": "modelId"},
        *_default_columns(config.features),
    ]
    # Keyed by path, so 'p_value' and 'variants.p_value' get their own types.
    feature_types = {}
    for f in config.features:
        if f.is_group:
            for c in f.features:
                feature_types[f"{f.name}.{c.name}"] = c.type
        else:
            feature_types[f.name] = f.type
    row_scope = csvcfg.get("rowScope", "paper-model")
    papers = {p["uid"]: p for p in store.list_papers()}

    runs = store.list_all_runs()
    if row_scope == "paper":
        # One row per paper: the first run per paper (stable by model order).
        # Deduping on uid, NOT pmid — pmid may be null for several papers, and
        # they would all collapse into a single row.
        seen = {}
        for r in runs:
            seen.setdefault(r["uid"], r)
        runs = list(seen.values())

    buf = io.StringIO()
    writer = csv.writer(buf)
    if csvcfg.get("includeHeaderRow", True):
        writer.writerow([c["header"] for c in columns])
    # rowScope may name a group: one CSV row per entry, with the paper-level
    # columns repeated on each — which is how a variant catalogue actually looks.
    group = config.group(row_scope)

    for run in runs:
        paper = papers.get(run["uid"])
        for row in _scope_rows(run, group):
            writer.writerow(
                [_cell(c["source"], run, paper, row, feature_types, csvcfg)
                 for c in columns]
            )
    return buf.getvalue()


def _scope_rows(run, group):
    """The rows a single run contributes. Without a group scope that is exactly
    one; with one it is one per surviving entry — and still one when there are
    none, so a paper reporting no variants is not silently dropped from the
    sheet."""
    if group is None:
        return [None]
    node = (run.get("groups") or {}).get(group.name) or {}
    rows = [
        {**r, "_group": group.name}
        for r in (node.get("rows") or [])
        # A soft-deleted row is a rejected AI claim. It stays in the database as
        # audit data, but it is NOT part of the curated result.
        if not r.get("deletedAt")
    ]
    return rows or [None]
