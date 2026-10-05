"""The local curator's own identity (name, email, optional OpenRouter key), and
its stamp onto a project's portable document. Run: python -m annotaid.tests.test_identity
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from annotaid.server import identity  # noqa: E402
from annotaid.server import projects as projects_mod  # noqa: E402
from annotaid.server.db import Db  # noqa: E402

CONFIG = {
    "features": [{"name": "p_value", "type": "number", "description": "The reported p-value."}],
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


def main():
    tmp = tempfile.mkdtemp(prefix="annotaid-identitytest-")
    try:
        run_all(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{len(PASSED)} checks passed.")


def run_all(tmp):
    db = Db(os.path.join(tmp, "annotaid.db"))

    print("\nfirst run")
    ident = identity.get_identity(db)
    check("not set before anything is saved", ident == {
        "set": False, "name": "", "email": "", "hasOpenRouterKey": False,
    })
    check("no key before anything is saved", identity.get_openrouter_key(db) == "")

    print("\nvalidation")
    raises("blank name rejected", identity.IdentityError,
           identity.save_identity, db, "  ", "jane@example.com")
    raises("blank email rejected", identity.IdentityError,
           identity.save_identity, db, "Jane Doe", "  ")
    raises("email shape rejected", identity.IdentityError,
           identity.save_identity, db, "Jane Doe", "not-an-email")

    print("\nsave + read back")
    saved = identity.save_identity(db, "Jane Doe", "jane@example.com")
    check("save reports set", saved["set"] is True)
    check("save reports the name", saved["name"] == "Jane Doe")
    check("save reports the email", saved["email"] == "jane@example.com")
    check("no key yet", saved["hasOpenRouterKey"] is False)
    check("get_identity agrees", identity.get_identity(db) == saved)

    print("\nOpenRouter key — masked, never round-tripped")
    with_key = identity.save_identity(db, "Jane Doe", "jane@example.com",
                                       openrouter_key="sk-or-v1-abc123")
    check("hasOpenRouterKey flips on", with_key["hasOpenRouterKey"] is True)
    check("the raw key is not in the identity dict",
          "openrouterKey" not in with_key and "openrouter_key" not in with_key)
    check("get_openrouter_key returns the raw value (server-side only)",
          identity.get_openrouter_key(db) == "sk-or-v1-abc123")

    print("\nediting without touching the key leaves it alone")
    edited = identity.save_identity(db, "Jane D.", "jane@example.com")
    check("name update took", edited["name"] == "Jane D.")
    check("key survives an edit that doesn't mention it",
          edited["hasOpenRouterKey"] is True
          and identity.get_openrouter_key(db) == "sk-or-v1-abc123")

    print("\nremoving the key")
    cleared = identity.save_identity(db, "Jane D.", "jane@example.com", clear_key=True)
    check("hasOpenRouterKey flips off", cleared["hasOpenRouterKey"] is False)
    check("get_openrouter_key is empty again", identity.get_openrouter_key(db) == "")

    print("\nstamped onto a project's portable document")
    proj_no_ident = projects_mod.create_project(db, "No creator", "", CONFIG)
    doc_no_ident = projects_mod.portable_document(db, proj_no_ident["id"])
    check("createdBy is None when nothing was passed", doc_no_ident["createdBy"] is None)

    proj = projects_mod.create_project(
        db, "GWAS pilot", "", CONFIG,
        created_by={"name": "Jane D.", "email": "jane@example.com"},
    )
    doc = projects_mod.portable_document(db, proj["id"])
    check("createdBy carries the name", doc["createdBy"]["name"] == "Jane D.")
    check("createdBy carries the email", doc["createdBy"]["email"] == "jane@example.com")
    check("createdBy is provenance, not curation settings",
          "createdBy" not in projects_mod.CONFIG_KEYS)


if __name__ == "__main__":
    main()
