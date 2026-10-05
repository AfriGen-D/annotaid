"""Smoke test for the SQLite store — run: python -m annotaid.tests.test_store

No test dependency on purpose (the project ships stdlib + jsonschema only). This
covers the parts a browser click-through cannot see: identity uniqueness scoping,
aiValue immutability, value round-tripping, and the pmid-is-joined-not-stored
property.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from annotaid.server import papers as papers_mod  # noqa: E402
from annotaid.server import projects as projects_mod  # noqa: E402
from annotaid.server.db import Db  # noqa: E402
from annotaid.server.project_store import ProjectStore  # noqa: E402

PDF_A = b"%PDF-1.4\n" + b"alpha" * 40
PDF_B = b"%PDF-1.4\n" + b"beta" * 40

CONFIG = {
    "features": [
        {"name": "p_value", "type": "number",
         "description": "The reported p-value."},
        {"name": "countries", "type": "array<string>",
         "description": "Countries the participants are from."},
        {"name": "mixed_population", "type": "boolean",
         "description": "Whether the sample mixes populations."},
    ],
    "models": [{"slug": "anthropic/claude-opus-4.7", "label": "opus"}],
    "prompts": [{"label": "default", "text": "Extract the fields."}],
}

PASSED = []


def check(label, cond):
    if not cond:
        raise AssertionError(label)
    PASSED.append(label)
    print(f"  ok  {label}")


def raises(label, exc_type, fn, *a, **kw):
    try:
        fn(*a, **kw)
    except exc_type as exc:
        PASSED.append(label)
        print(f"  ok  {label}  ({type(exc).__name__}: {str(exc)[:60]})")
        return
    raise AssertionError(f"{label}: expected {exc_type.__name__}, nothing raised")


def make_run(uid, model_id="anthropic/claude-opus-4.7"):
    return {
        "uid": uid, "modelId": model_id, "modelLabel": "opus",
        "promptId": "sha256:template", "promptHash": "sha256:composed",
        "promptText": "Extract the fields.\n\nCuration project: T\n\nFields:\n- p_value",
        "source": "openrouter", "parseEngine": "pdf-text",
        "requestedAt": "2026-09-08T00:00:00+00:00",
        "completedAt": "2026-09-08T00:00:09+00:00",
        "status": "ok", "error": None,
        "features": {
            # 6.7e-08 is the real value from the corpus — a float that must
            # survive the JSON round-trip exactly, because aiValue vs value IS
            # the audit signal.
            "p_value": {"type": "number", "aiValue": 6.7e-08, "value": 6.7e-08,
                        "present": True, "evidence": "p=6.7 x 10-8",
                        "evidenceMatch": "none", "confirmed": False,
                        "editedAt": None, "confirmedAt": None},
            "countries": {"type": "array<string>", "aiValue": [], "value": [],
                          "present": False, "evidence": None,
                          "evidenceMatch": "none", "confirmed": False,
                          "editedAt": None, "confirmedAt": None},
            "mixed_population": {"type": "boolean", "aiValue": True,
                                 "value": True, "present": True,
                                 "evidence": "mixed cohort",
                                 "evidenceMatch": "none", "confirmed": False,
                                 "editedAt": None, "confirmedAt": None},
        },
    }


def main():
    tmp = tempfile.mkdtemp(prefix="annotaid-storetest-")
    try:
        run_all(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{len(PASSED)} checks passed.")


def run_all(tmp):
    db = Db(os.path.join(tmp, "annotaid.db"))
    blobs = os.path.join(tmp, "pdfs")

    print("\nprojects")
    a = projects_mod.create_project(db, "GWAS pilot", "Non-European cohorts.", CONFIG)
    b = projects_mod.create_project(db, "Second project", "", CONFIG)
    check("two projects created", a["id"] != b["id"])
    raises("duplicate project name rejected", projects_mod.ProjectError,
           projects_mod.create_project, db, "GWAS pilot", "", CONFIG)
    raises("unnamed project rejected", projects_mod.ProjectError,
           projects_mod.create_project, db, "  ", "", CONFIG)
    raises("config with a prompt file reference rejected (not portable)",
           projects_mod.ProjectError, projects_mod.create_project, db, "Bad", "",
           dict(CONFIG, prompts=[{"label": "x", "file": "prompt.txt"}]))

    sa = ProjectStore(db, blobs, a["id"])
    sb = ProjectStore(db, blobs, b["id"])

    print("\nshared PDF pool + per-project papers")
    pa = papers_mod.store_upload(sa, PDF_A, "27882227.pdf")
    pb = papers_mod.store_upload(sb, PDF_A, "27882227.pdf")
    check("same bytes -> same uid in both projects", pa["uid"] == pb["uid"])
    check("PDF stored once in the shared pool",
          len([f for f in os.listdir(blobs) if f.endswith(".pdf")]) == 1)
    check("filename prefill suggests the numeric stem",
          pa["suggestedPmid"] == "27882227")
    check("a new paper starts identity-pending", pa["pmidStatus"] == "pending")

    print("\nidentity")
    ca = papers_mod.confirm_pmid(sa, pa["uid"], "27882227", "manual")
    cb = papers_mod.confirm_pmid(sb, pb["uid"], "27882227", "manual")
    check("the SAME pmid confirmed independently in two projects",
          ca["pmid"] == cb["pmid"] == "27882227")
    check("confirming sets status", ca["pmidStatus"] == "confirmed")

    other = papers_mod.store_upload(sa, PDF_B, "other.pdf")
    raises("pmid collision WITHIN a project rejected", papers_mod.PaperError,
           papers_mod.confirm_pmid, sa, other["uid"], "27882227", "manual")

    # Two identity-pending papers must coexist: this is what the PARTIAL unique
    # index buys us, and it is the whole point of supporting PMID-less papers.
    third = papers_mod.store_upload(sa, b"%PDF-1.4\n" + b"gamma" * 40, "scan.pdf")
    check("two papers with no pmid coexist",
          third["pmid"] is None and other["pmid"] is None)

    nod = papers_mod.mark_no_pmid(sa, other["uid"], "https://doi.org/10.1038/s41588-024-01234")
    check("doi url wrapper stripped", nod["doi"] == "10.1038/s41588-024-01234")
    check("no-pmid paper is status 'none'", nod["pmidStatus"] == "none")
    check("no-pmid paper still has no pmid", nod["pmid"] is None)
    raises("malformed doi rejected", papers_mod.PaperError,
           papers_mod.mark_no_pmid, sa, third["uid"], "not-a-doi")
    raises("doi collision within a project rejected", papers_mod.PaperError,
           papers_mod.mark_no_pmid, sa, third["uid"], "10.1038/s41588-024-01234")

    print("\nruns keyed on uid")
    sa.save_run(make_run(other["uid"]))          # a paper with NO pmid
    got = sa.load_run(other["uid"], "anthropic/claude-opus-4.7")
    check("run stored against a paper with no pmid", got is not None)
    check("that run reads back with pmid=None", got["pmid"] is None)
    check("float round-trips exactly", got["features"]["p_value"]["aiValue"] == 6.7e-08)
    check("empty array round-trips", got["features"]["countries"]["value"] == [])
    check("bool round-trips as a bool",
          got["features"]["mixed_population"]["value"] is True)
    check("promptText omitted from list responses",
          "promptText" not in sa.list_all_runs()[0])
    check("promptText present when asked for",
          sa.list_all_runs(with_prompt_text=True)[0]["promptText"].startswith("Extract"))

    # pmid is joined from papers, never stored on the run — so resolving a
    # paper's identity later cannot leave stale run records behind.
    sa.save_run(make_run(third["uid"]))
    papers_mod.confirm_pmid(sa, third["uid"], "39792745", "manual")
    late = sa.load_run(third["uid"], "anthropic/claude-opus-4.7")
    check("pmid confirmed AFTER extraction shows up on the existing run",
          late["pmid"] == "39792745")

    print("\nderived models + isolation")
    check("paper.models derived from runs",
          sa.load_paper(other["uid"])["models"] == ["anthropic/claude-opus-4.7"])
    check("project B sees none of project A's runs", sb.list_all_runs() == [])
    check("project B's copy of the shared paper has no models",
          sb.load_paper(pb["uid"])["models"] == [])

    print("\nabstract-only papers (allowExtractionOnAbstract fallback)")
    normal = sb.load_paper(pb["uid"])
    check("a normal upload has hasPdf True", normal["hasPdf"] is True)
    check("a normal upload has no abstract text", normal["abstractText"] is None)

    ab = papers_mod.store_and_confirm_from_abstract(sb, "31234567", "Background: ...")
    check("abstract-only paper has no PDF", ab["hasPdf"] is False)
    check("abstract-only paper carries the abstract text",
          ab["abstractText"] == "Background: ...")
    check("abstract-only paper is pre-confirmed like the PDF fetch path",
          ab["pmidStatus"] == "confirmed" and ab["pmid"] == "31234567")
    check("no PDF blob was written for it", sb.resolve_pdf(ab["uid"]) is None)

    again_ab = papers_mod.store_and_confirm_from_abstract(sb, "31234567", "Background: ...")
    check("re-fetching the same PMID's abstract dedupes to the same paper",
          again_ab["uid"] == ab["uid"])
    raises("abstract fallback still enforces the per-project pmid uniqueness",
           papers_mod.PaperError, papers_mod.store_and_confirm_from_abstract,
           sb, "27882227", "Some other abstract.")

    from annotaid.server import extraction  # noqa: E402 (kept local to this block)
    from annotaid.server.config_loader import parse_config as _parse_config
    cfg_off = _parse_config(CONFIG, label="abs-off")
    raises("run_extraction refuses an abstract-only paper when the project "
           "hasn't opted in", ValueError, extraction.run_extraction,
           cfg_off, None, sb, ab, ["anthropic/claude-opus-4.7"], None, None)

    print("\nmerge_run_edits (aiValue immutability)")
    sa.merge_run_edits(other["uid"], "anthropic/claude-opus-4.7", {
        "p_value": {"value": 0.001, "present": True, "confirmed": True},
    })
    m = sa.load_run(other["uid"], "anthropic/claude-opus-4.7")["features"]["p_value"]
    check("aiValue preserved through an edit", m["aiValue"] == 6.7e-08)
    check("value updated", m["value"] == 0.001)
    check("type preserved", m["type"] == "number")
    check("editedAt stamped", m["editedAt"] is not None)
    check("confirmedAt stamped", m["confirmedAt"] is not None)
    check("confirmed flag set", m["confirmed"] is True)

    # aiValue must be ignored even if a client tries to send one.
    sa.merge_run_edits(other["uid"], "anthropic/claude-opus-4.7", {
        "p_value": {"value": 0.001, "aiValue": 999, "confirmed": False},
    })
    m2 = sa.load_run(other["uid"], "anthropic/claude-opus-4.7")["features"]["p_value"]
    check("a client-sent aiValue is ignored", m2["aiValue"] == 6.7e-08)
    check("un-confirming clears confirmedAt", m2["confirmedAt"] is None)
    check("editedAt survives an unchanged value", m2["editedAt"] == m["editedAt"])

    sa.merge_run_edits(other["uid"], "anthropic/claude-opus-4.7", {
        "not_a_feature": {"value": "x"},
    })
    m3 = sa.load_run(other["uid"], "anthropic/claude-opus-4.7")["features"]
    check("features the AI never proposed are not invented",
          "not_a_feature" not in m3)
    raises("merging into a missing run raises", KeyError,
           sa.merge_run_edits, "deadbeef0000", "anthropic/claude-opus-4.7", {})

    print("\nui state")
    sa.save_state({"zoom": 1.4})
    check("ui state round-trips", sa.load_state() == {"zoom": 1.4})
    check("ui state is per project", sb.load_state() == {})

    print("\nhome-screen stats")
    # A paper nobody has resolved yet, so `pending` is actually exercised:
    # every other paper in project A has had its identity settled by now.
    papers_mod.store_upload(sa, b"%PDF-1.4\n" + b"delta" * 40, "untouched.pdf")
    stats = {p["name"]: p for p in projects_mod.list_projects(db)}
    ga = stats["GWAS pilot"]["stats"]
    check("paper count", ga["papers"] == 4)
    check("identity-pending count", ga["pending"] == 1)
    check("run count", ga["runs"] == 2)
    check("confirmed feature count", ga["confirmed"] == 0)
    check("total feature rows", ga["featuresTotal"] == 6)
    check("feature count read from the stored config",
          stats["GWAS pilot"]["featureCount"] == 3)

    print("\nportable document")
    doc = projects_mod.portable_document(db, a["id"])
    check("document is versioned", doc["annotaidProject"] == 1)
    check("document carries name + description",
          doc["name"] == "GWAS pilot" and doc["description"].startswith("Non-Euro"))
    check("prompt text is inlined, not a file reference",
          doc["prompts"][0]["text"] == "Extract the fields."
          and "file" not in doc["prompts"][0])
    check("document survives a JSON round-trip", json.loads(json.dumps(doc)) == doc)

    print("\narchive cascades")
    projects_mod.archive_project(db, a["id"])
    check("archived project leaves the list",
          "GWAS pilot" not in {p["name"] for p in projects_mod.list_projects(db)})
    check("archive is not a delete — its runs are still there",
          db.scalar("SELECT COUNT(*) FROM runs WHERE project_id=?", (a["id"],)) == 2)

    # Deleting a project (as opposed to archiving) must take its curation with it.
    with db.write() as c:
        c.execute("DELETE FROM projects WHERE id=?", (a["id"],))
    check("deleting a project cascades papers away",
          db.scalar("SELECT COUNT(*) FROM papers WHERE project_id=?", (a["id"],)) == 0)
    check("deleting a project cascades runs away",
          db.scalar("SELECT COUNT(*) FROM runs WHERE project_id=?", (a["id"],)) == 0)
    check("deleting a project cascades run_features away",
          db.scalar("SELECT COUNT(*) FROM run_features WHERE project_id=?",
                    (a["id"],)) == 0)
    check("the other project is untouched",
          # project B: the shared-pool upload (pb) + the abstract-only paper
          # (ab) added in the section above — 2, not the pre-abstract-test 1.
          db.scalar("SELECT COUNT(*) FROM papers WHERE project_id=?", (b["id"],)) == 2)


if __name__ == "__main__":
    main()
