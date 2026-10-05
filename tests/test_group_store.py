"""Group-row storage: the lifecycle that carries the row-level audit signal.
Run: python -m annotaid.tests.test_group_store

The field-level signal is aiValue beside value. One level up it is origin plus
deleted_at: an AI row the curator rejects is a false positive, one they add is a
miss, and erasing either destroys the count. These tests pin that down.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from annotaid.server import features as feat  # noqa: E402
from annotaid.server import papers as papers_mod  # noqa: E402
from annotaid.server import projects as projects_mod  # noqa: E402
from annotaid.server.config_loader import parse_config  # noqa: E402
from annotaid.server.db import Db  # noqa: E402
from annotaid.server.project_store import ProjectStore  # noqa: E402

PDF = b"%PDF-1.4\n" + b"paper" * 40

CONFIG = {
    "features": [
        {"name": "sample_size", "type": "number", "description": "Total N."},
        {"name": "variants", "type": "group", "description": "One per variant.",
         "features": [
             {"name": "rsid", "type": "string", "identifier": True, "description": "rsID."},
             {"name": "variant_id", "type": "string", "identifier": True, "description": "Positional id."},
             {"name": "odds_ratio", "type": "number", "description": "OR."},
         ]},
    ],
    "models": [{"slug": "m/one", "label": "one"}],
    "prompts": [{"label": "p", "text": "Extract."}],
}
MODEL = "m/one"
PASSED = []


def check(label, cond, detail=""):
    if not cond:
        raise AssertionError(f"{label}  {detail}")
    PASSED.append(label)
    print(f"  ok  {label}")


def raises(label, exc_type, fn, *a, **kw):
    try:
        fn(*a, **kw)
    except exc_type as exc:
        PASSED.append(label)
        print(f"  ok  {label}  ({type(exc).__name__})")
        return
    raise AssertionError(f"{label}: expected {exc_type.__name__}")


def cell(value, ai=None):
    return {"type": "string", "aiValue": ai if ai is not None else value,
            "value": value, "present": value is not None, "evidence": None,
            "evidenceMatch": "none", "confirmed": False,
            "editedAt": None, "confirmedAt": None}


def num_cell(value):
    return dict(cell(value), type="number")


def ai_row(rsid, orr):
    return {"rowId": None, "origin": "ai", "deletedAt": None,
            "features": {"rsid": cell(rsid), "variant_id": cell(None),
                         "odds_ratio": num_cell(orr)}}


def make_run(uid):
    return {
        "uid": uid, "modelId": MODEL, "modelLabel": "one",
        "promptId": "sha256:t", "promptHash": "sha256:c", "promptText": "…",
        "source": "openrouter", "parseEngine": "pdf-text",
        "requestedAt": "2026-09-09T00:00:00+00:00",
        "completedAt": "2026-09-09T00:00:05+00:00",
        "status": "ok", "error": None,
        "features": {"sample_size": num_cell(4571)},
        "groups": {"variants": {"present": True, "rows": [
            ai_row("rs111", 1.4), ai_row("rs222", 2.1), ai_row("rs333", 0.8),
        ]}},
    }


def rows_of(run, include_deleted=False):
    rows = run["groups"]["variants"]["rows"]
    return rows if include_deleted else [r for r in rows if not r["deletedAt"]]


def by_rsid(run):
    return {r["features"]["rsid"]["value"]: r for r in rows_of(run, True)}


def main():
    tmp = tempfile.mkdtemp(prefix="annotaid-grouptest-")
    try:
        run_all(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{len(PASSED)} checks passed.")


def run_all(tmp):
    db = Db(os.path.join(tmp, "annotaid.db"))
    proj = projects_mod.create_project(db, "Variants", "Per-variant curation.", CONFIG)
    store = ProjectStore(db, os.path.join(tmp, "pdfs"), proj["id"])
    cfg = parse_config(CONFIG, label="test")
    defs = feat.group_cell_templates(cfg.features)

    paper = papers_mod.store_upload(store, PDF, "27882227.pdf")
    papers_mod.confirm_pmid(store, paper["uid"], "27882227", "manual")
    uid = paper["uid"]

    print("\nsaving a run with rows")
    store.save_run(make_run(uid))
    r = store.load_run(uid, MODEL)
    check("paper-level cells still land", r["features"]["sample_size"]["value"] == 4571)
    check("three rows stored", len(rows_of(r)) == 3)
    check("rows keep declaration order",
          [x["features"]["rsid"]["value"] for x in rows_of(r)]
          == ["rs111", "rs222", "rs333"])
    check("every row got a generated id",
          all(len(x["rowId"]) == 8 for x in rows_of(r)))
    check("row ids are distinct", len({x["rowId"] for x in rows_of(r)}) == 3)
    check("rows are marked as the AI's",
          all(x["origin"] == "ai" for x in rows_of(r)))
    check("nested numbers keep their type",
          rows_of(r)[0]["features"]["odds_ratio"]["value"] == 1.4)
    check("group present flag stored", r["groups"]["variants"]["present"] is True)
    check("aiPresent recorded alongside it",
          r["groups"]["variants"]["aiPresent"] is True)
    check("paper-level cells are not mixed into rows",
          "sample_size" not in rows_of(r)[0]["features"])

    print("\nediting a cell inside a row")
    target = rows_of(r)[1]["rowId"]
    store.merge_run_edits(uid, MODEL, {}, {"variants": {"rows": [
        {"rowId": x["rowId"],
         "features": ({"odds_ratio": {"value": 9.9, "confirmed": True}}
                      if x["rowId"] == target else {})}
        for x in rows_of(r)
    ]}}, defs)
    r2 = store.load_run(uid, MODEL)
    edited = next(x for x in rows_of(r2) if x["rowId"] == target)
    check("the edited row's value changed", edited["features"]["odds_ratio"]["value"] == 9.9)
    check("its aiValue is preserved", edited["features"]["odds_ratio"]["aiValue"] == 2.1)
    check("editedAt stamped", edited["features"]["odds_ratio"]["editedAt"])
    check("confirmedAt stamped", edited["features"]["odds_ratio"]["confirmedAt"])
    others = [x for x in rows_of(r2) if x["rowId"] != target]
    check("sibling rows untouched",
          [x["features"]["odds_ratio"]["value"] for x in others] == [1.4, 0.8])
    check("editing a row does not touch paper-level cells",
          r2["features"]["sample_size"]["value"] == 4571)

    print("\ndeleting an AI row is a soft delete (it is a FALSE POSITIVE)")
    doomed = by_rsid(r2)["rs333"]["rowId"]
    store.merge_run_edits(uid, MODEL, {}, {"variants": {"rows": [
        {"rowId": x["rowId"], "deleted": x["rowId"] == doomed} for x in rows_of(r2)
    ]}}, defs)
    r3 = store.load_run(uid, MODEL)
    check("it leaves the curated set", len(rows_of(r3)) == 2)
    check("but survives in the record", len(rows_of(r3, True)) == 3)
    gone = by_rsid(r3)["rs333"]
    check("stamped with a deletion time", bool(gone["deletedAt"]))
    check("still attributed to the AI", gone["origin"] == "ai")
    check("its cells survive too, so the claim is still readable",
          gone["features"]["odds_ratio"]["aiValue"] == 0.8)

    print("\nundeleting restores it")
    store.merge_run_edits(uid, MODEL, {}, {"variants": {"rows": [
        {"rowId": x["rowId"], "deleted": False} for x in rows_of(r3, True)
    ]}}, defs)
    r4 = store.load_run(uid, MODEL)
    check("back in the curated set", len(rows_of(r4)) == 3)
    check("deletedAt cleared", by_rsid(r4)["rs333"]["deletedAt"] is None)

    print("\nadding a row the AI missed")
    store.merge_run_edits(uid, MODEL, {}, {"variants": {"rows": [
        {"rowId": x["rowId"]} for x in rows_of(r4)
    ] + [
        {"rowId": None, "features": {"rsid": {"value": "rs444"},
                                     "odds_ratio": {"value": 3.3}}}
    ]}}, defs)
    r5 = store.load_run(uid, MODEL)
    check("the row exists", len(rows_of(r5)) == 4)
    added = by_rsid(r5)["rs444"]
    check("marked as the curator's, so it reads as an AI MISS",
          added["origin"] == "curator")
    check("its value is what the curator typed", added["features"]["rsid"]["value"] == "rs444")
    # The AI proposed nothing here, so "AI said nothing, human supplied this" is
    # the truthful pair — not aiValue == the human's own answer.
    check("its aiValue is the type's empty value, not the human's answer",
          added["features"]["rsid"]["aiValue"] is None)
    check("editedAt stamped on the supplied cell", added["features"]["rsid"]["editedAt"])
    check("fields the curator left blank still exist",
          added["features"]["variant_id"]["value"] is None)
    check("every declared field is present on a new row",
          set(added["features"]) == {"rsid", "variant_id", "odds_ratio"})
    check("declared types are applied to a new row",
          added["features"]["odds_ratio"]["type"] == "number")

    print("\ndeleting a curator row removes it outright (no signal to keep)")
    store.merge_run_edits(uid, MODEL, {}, {"variants": {"rows": [
        {"rowId": x["rowId"], "deleted": x["origin"] == "curator"}
        for x in rows_of(r5)
    ]}}, defs)
    r6 = store.load_run(uid, MODEL)
    check("gone from the curated set", len(rows_of(r6)) == 3)
    check("and gone from the record entirely", len(rows_of(r6, True)) == 3)
    check("its cells were cleaned up, not orphaned",
          db.scalar(
              "SELECT COUNT(*) FROM run_features WHERE project_id=? AND row_id=?",
              (proj["id"], added["rowId"])) == 0)

    print("\nreordering")
    reversed_ids = [x["rowId"] for x in rows_of(r6)][::-1]
    store.merge_run_edits(uid, MODEL, {}, {"variants": {"rows": [
        {"rowId": rid} for rid in reversed_ids
    ]}}, defs)
    r7 = store.load_run(uid, MODEL)
    check("order follows the payload", [x["rowId"] for x in rows_of(r7)] == reversed_ids)
    check("reordering did not disturb the values",
          {x["features"]["rsid"]["value"] for x in rows_of(r7)}
          == {"rs111", "rs222", "rs333"})

    print("\ngroup-level absence, confirmed")
    store.merge_run_edits(uid, MODEL, {},
                          {"variants": {"present": False, "confirmed": True}}, defs)
    r8 = store.load_run(uid, MODEL)
    check("present cleared", r8["groups"]["variants"]["present"] is False)
    check("confirmed recorded", r8["groups"]["variants"]["confirmed"] is True)
    check("confirmedAt stamped", bool(r8["groups"]["variants"]["confirmedAt"]))
    # aiPresent is immutable, exactly like aiValue on a cell: the model DID claim
    # variants, and the curator overruling that is the finding.
    check("aiPresent still says what the AI claimed",
          r8["groups"]["variants"]["aiPresent"] is True)
    store.merge_run_edits(uid, MODEL, {}, {"variants": {"confirmed": False}}, defs)
    check("un-confirming clears confirmedAt",
          store.load_run(uid, MODEL)["groups"]["variants"]["confirmedAt"] is None)

    print("\nre-running replaces its own output")
    store.save_run(make_run(uid))
    r9 = store.load_run(uid, MODEL)
    check("rows replaced wholesale, no accumulation", len(rows_of(r9, True)) == 3)
    check("no orphaned cells left behind",
          db.scalar("SELECT COUNT(*) FROM run_features WHERE project_id=? "
                    "AND row_id NOT IN (SELECT row_id FROM run_rows "
                    "WHERE project_id=?) AND row_id != ''",
                    (proj["id"], proj["id"])) == 0)

    print("\nrobustness")
    raises("merging into a missing run raises", KeyError,
           store.merge_run_edits, "deadbeef0000", MODEL, {}, {"variants": {}}, defs)
    store.merge_run_edits(uid, MODEL, {}, {"nope": {"rows": [{"rowId": None}]}}, defs)
    check("edits to an unknown group are ignored, not crashed",
          "nope" not in store.load_run(uid, MODEL)["groups"])
    store.merge_run_edits(uid, MODEL, {}, {"variants": {"rows": [
        {"rowId": "ffffffff", "features": {"rsid": {"value": "x"}}}]}}, defs)
    check("an unknown rowId does not resurrect or duplicate a row",
          len(rows_of(store.load_run(uid, MODEL), True)) == 4)

    print("\nlist and delete paths")
    check("list_runs carries groups too",
          "variants" in store.list_runs(uid)[0]["groups"])
    check("list_all_runs carries groups",
          "variants" in store.list_all_runs()[0]["groups"])
    with db.write() as c:
        c.execute("DELETE FROM projects WHERE id=?", (proj["id"],))
    for table in ("run_rows", "run_groups", "run_features"):
        check(f"deleting the project cascades {table} away",
              db.scalar(f"SELECT COUNT(*) FROM {table} WHERE project_id=?",
                        (proj["id"],)) == 0)


if __name__ == "__main__":
    main()
