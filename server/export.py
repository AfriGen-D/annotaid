"""Export — available at any point (brief §7 decision).

CSV: config-driven columns/format so the curator pastes rows straight into the
     global sheet. Uses the curator's final `value` (not aiValue).
Audit JSON: every run record with aiValue beside value, for benchmarking.
"""
from __future__ import annotations

import csv
import io

from .config_loader import Config
from .store import Store


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


def _cell(source, run, feature_types, csvcfg) -> str:
    if source == "pmid":
        return run.get("pmid", "")
    if source == "modelId":
        return run.get("modelLabel") or run.get("modelId", "")
    feat = run.get("features", {}).get(source)
    if feat is not None:
        return _render_value(feat.get("value"), feature_types.get(source), csvcfg)
    # literal / unmapped column -> emit empty to preserve sheet alignment
    return ""


def export_csv(store: Store, config: Config) -> str:
    csvcfg = (config.export.get("csv") or {})
    columns = csvcfg.get("columns") or [
        {"header": "pubmed_id", "source": "pmid"},
        {"header": "model", "source": "modelId"},
        *[{"header": f["name"], "source": f["name"]} for f in config.features],
    ]
    feature_types = {f["name"]: f["type"] for f in config.features}
    row_scope = csvcfg.get("rowScope", "paper-model")

    runs = store.list_all_runs()
    if row_scope == "paper":
        # one row per paper: pick the first run per pmid (stable by model order)
        seen = {}
        for r in runs:
            seen.setdefault(r["pmid"], r)
        runs = list(seen.values())

    buf = io.StringIO()
    writer = csv.writer(buf)
    if csvcfg.get("includeHeaderRow", True):
        writer.writerow([c["header"] for c in columns])
    for run in runs:
        writer.writerow([_cell(c["source"], run, feature_types, csvcfg) for c in columns])
    return buf.getvalue()


def export_audit_json(store: Store, config: Config) -> dict:
    """Full audit artifact: papers + runs (aiValue beside value)."""
    return {
        "generatedAt": None,  # stamped by caller (Date.now unavailable in some contexts)
        "features": [{"name": f["name"], "type": f["type"]} for f in config.features],
        "papers": store.list_papers(),
        "runs": store.list_all_runs(),
    }
