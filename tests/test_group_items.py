"""Two-pass row identity for a project's one repeating group.
Run: python -m annotaid.tests.test_group_items

Covers: pass-1 discovery state/evidence, curator review, schema_builder's fixed
reviewed list, and positional pass-2 matching.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from annotaid.server import extraction  # noqa: E402
from annotaid.server import features as feat  # noqa: E402
from annotaid.server import projects as projects_mod  # noqa: E402
from annotaid.server import schema_builder  # noqa: E402
from annotaid.server.config_loader import parse_config  # noqa: E402
from annotaid.server.db import Db  # noqa: E402
from annotaid.server.project_store import ProjectStore  # noqa: E402

CONFIG = {
    "features": [
        {
            "name": "variant_haplotype", "type": "group", "label": "Variant/Haplotype",
            "min": 1, "maxItems": 20,
            "features": [
                {"name": "p_value", "type": "number", "description": "The p-value."},
            ],
        },
    ],
    "models": [{"slug": "anthropic/claude-opus-4.7", "label": "opus"}],
    "prompts": [{"label": "default", "text": "Extract the fields."}],
    "export": {"csv": {"rowScope": "variant_haplotype"}},
}

PASSED = []


def check(label, cond):
    if not cond:
        raise AssertionError(label)
    PASSED.append(label)
    print(f"  ok  {label}")


def main():
    tmp = tempfile.mkdtemp(prefix="annotaid-groupitemstest-")
    try:
        run_all(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{len(PASSED)} checks passed.")


def run_all(tmp):
    db = Db(os.path.join(tmp, "annotaid.db"))
    proj = projects_mod.create_project(db, "GWAS pilot", "", CONFIG)
    store = ProjectStore(db, os.path.join(tmp, "pdfs"), proj["id"])
    cfg = parse_config(CONFIG, label="test")

    from annotaid.server import papers as papers_mod
    paper = papers_mod.store_upload(store, b"%PDF-1.4\n" + b"x" * 40, "33915198.pdf")
    papers_mod.confirm_pmid(store, paper["uid"], "33915198", "manual")

    print("\nstorage: load/save_group_items")
    check("no items declared yet", store.load_group_items(paper["uid"]) == [])
    saved = store.save_group_items(paper["uid"], [
        {"label": "rs1800544", "evidence": "the rs1800544 variant"},
        {"label": "rs1800544"}, {"label": "HLA-B*15:02"},
    ])
    check("three items saved, in order",
          [it["label"] for it in saved] == ["rs1800544", "rs1800544", "HLA-B*15:02"])
    check("each item got a distinct rowId even with a duplicate label",
          len({it["rowId"] for it in saved}) == 3)
    check("load_group_items agrees", store.load_group_items(paper["uid"]) == saved)
    check("pass-1 evidence survives storage", saved[0]["evidence"] == "the rs1800544 variant")
    check("blank labels are dropped, not stored",
          store.save_group_items(paper["uid"], saved + [{"label": "   "}]) == saved)

    print("\nediting preserves rowId for items the client echoes back")
    edited_in = [dict(saved[0]), {"label": "rs9999"}]  # keep #1, drop #2/#3, add a new one
    edited = store.save_group_items(paper["uid"], edited_in)
    check("kept item's rowId survives the edit", edited[0]["rowId"] == saved[0]["rowId"])
    check("new item got a fresh rowId", edited[1]["rowId"] != saved[0]["rowId"])
    check("dropped items are actually gone",
          {it["rowId"] for it in edited} & {saved[1]["rowId"], saved[2]["rowId"]} == set())

    # reset to the three-item state used by the rest of this test
    items = store.save_group_items(paper["uid"], saved)
    check("paper dict surfaces groupItems", store.load_paper(paper["uid"])["groupItems"] == items)

    print("\nschema_builder: the prompt states the fixed declared list")
    prompt_text = schema_builder.build_prompt(
        "Extract.", cfg.features, group_items={"variant_haplotype": [it["label"] for it in items]},
    )
    check("declares the exact count", "exactly 3 entries" in prompt_text)
    check("lists each label in order", "1. rs1800544" in prompt_text and "3. HLA-B*15:02" in prompt_text)
    check("tells the model not to invent an identifier",
          "do not include an identifier field" in prompt_text)

    fallback_prompt = schema_builder.build_prompt("Extract.", cfg.features)
    check("no group_items -> explains the two-pass config-preview path",
          "pass 1 identifies the entries" in fallback_prompt)

    print("\npass 2 requires review, not a non-empty list")
    empty_paper = papers_mod.store_upload(store, b"%PDF-1.4\n" + b"y" * 40, "1.pdf")
    empty_paper = papers_mod.confirm_pmid(store, empty_paper["uid"], "10000001", "manual")
    try:
        extraction.run_extraction(cfg, None, store, empty_paper,
                                   ["anthropic/claude-opus-4.7"], None, None)
        raise AssertionError("expected a ValueError, nothing raised")
    except ValueError as exc:
        check("refuses an unreviewed list, and says so", "review" in str(exc))
    store.save_group_items(empty_paper["uid"], [], reviewed=True, discovered=True)
    reviewed = store.load_paper(empty_paper["uid"])
    check("an approved empty list is valid", reviewed["groupItemsReviewed"] is True
          and reviewed["groupItems"] == [])

    print("\npass 1 discovers mixed identifier formats with review evidence")
    discovery_paper = papers_mod.store_upload(
        store, b"%PDF-1.4\n" + b"z" * 40, "2.pdf"
    )
    discovery_paper = papers_mod.confirm_pmid(
        store, discovery_paper["uid"], "10000002", "manual"
    )
    captured = {}
    original_call = extraction.openrouter.call
    try:
        def fake_call(payload, _key):
            captured["payload"] = payload
            return ('{"items": ['
                    '{"label":"rs1800544","evidence":"variant rs1800544"},'
                    '{"label":"EVA:eva123","evidence":"EVA:eva123 was reported"},'
                    '{"label":"NM_000546.6:c.215C>G","evidence":"NM_000546.6:c.215C>G"}'
                    ']}')
        extraction.openrouter.call = fake_call
        found = extraction.discover_group_items(
            cfg, type("Secrets", (), {"openrouter_api_key": "test"})(), store,
            discovery_paper, cfg.models[0], "pdf-text", "GWAS pilot",
        )
    finally:
        extraction.openrouter.call = original_call
    check("dbSNP, EVA and HGVS forms are retained verbatim",
          [it["label"] for it in found] == [
              "rs1800544", "EVA:eva123", "NM_000546.6:c.215C>G",
          ])
    check("discovery retains a review quote", found[1]["evidence"] == "EVA:eva123 was reported")
    discovery_state = store.load_paper(discovery_paper["uid"])
    check("pass 1 pauses as discovered but unreviewed",
          discovery_state["groupItemsDiscovered"] is True
          and discovery_state["groupItemsReviewed"] is False)
    sent_prompt = captured["payload"]["messages"][0]["content"][0]["text"]
    check("discovery prompt explicitly allows multiple identifier systems",
          all(word in sent_prompt for word in ("dbSNP", "EVA", "HGVS")))

    print("\nextraction._groups_from_canonical: positional matching, never by identifier")
    group = feat.groups(cfg.features)[0]
    # The model returned only 2 value-objects (no identifier of its own) for 3
    # declared rows, and it also can't smuggle extra rows in beyond the count.
    canonical = {
        "variant_haplotype": {"items": [
            {"p_value": {"present": True, "value": 0.03, "evidence": None}},
            {"p_value": {"present": True, "value": 0.09, "evidence": None}},
        ]},
    }
    built = extraction._groups_from_canonical(canonical, cfg.features, items)
    rows = built["variant_haplotype"]["rows"]
    check("one row per DECLARED item, not per AI item", len(rows) == 3)
    check("rows keep the declared rowIds, in order",
          [r["rowId"] for r in rows] == [it["rowId"] for it in items])
    check("row 1 got the AI's first value", rows[0]["features"]["p_value"]["value"] == 0.03)
    check("row 2 got the AI's second value", rows[1]["features"]["p_value"]["value"] == 0.09)
    check("row 3 (AI didn't return one) is blank, not dropped",
          rows[2]["features"]["p_value"]["present"] is False
          and rows[2]["features"]["p_value"]["value"] is None)
    check("every row's origin is 'ai' (values were AI-supplied; existence was curator-declared)",
          all(r["origin"] == "ai" for r in rows))

    blanks = extraction._blank_groups(cfg.features, items)["variant_haplotype"]["rows"]
    check("a failed call still seeds one blank row per declared item",
          len(blanks) == 3 and [r["rowId"] for r in blanks] == [it["rowId"] for it in items])

    print("\nexport: $label resolves from the paper's declared items, not run_features")
    from annotaid.server import export
    run = {
        "uid": paper["uid"], "modelId": "anthropic/claude-opus-4.7", "modelLabel": "opus",
        "promptId": cfg.default_prompt()["id"], "promptHash": "x", "promptText": "x",
        "source": "openrouter", "parseEngine": "pdf-text", "requestedAt": "now", "completedAt": "now",
        "status": "ok", "error": None, "features": {}, "groups": built,
    }
    store.save_run(run)
    csv_text = export.export_csv(store, cfg).replace("\r\n", "\n")
    check("header names the group by its label, plus its one feature",
          csv_text.startswith("pubmed_id,model,Variant/Haplotype,variant_haplotype.p_value\n"))
    check("exported labels match the declared list, PMID duplicated per row",
          "33915198,opus,rs1800544,0.03" in csv_text
          and "33915198,opus,rs1800544,0.09" in csv_text
          and "33915198,opus,HLA-B*15:02," in csv_text)


if __name__ == "__main__":
    main()
