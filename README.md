# annotaid

A local, single-user biocuration gold-standard tool. An LLM proposes structured values for a
configurable set of features from an academic PDF; a human curator verifies each value against the
source (evidence-highlighted), corrects it where needed, and confirms it. The verified output is a
gold-standard dataset — fast to produce, evidence-grounded, and auditable.

Work is organised into **projects**. A project owns its own feature set (plus models, prompts and
CSV export mapping) and its own papers, so several curation efforts can run side by side and the
same paper can be curated independently in more than one of them.

Features come in two shapes. A plain feature is one value per paper. A **repeating group** is a set
of fields recorded once per entry — one per genetic variant, say — so a single paper yields one
study-level record plus N variant records, each with its own rsID, odds ratio and p-value.

Built with a **standard-library-only** Python backend (`http.server` + `urllib`) plus one
dependency, `jsonschema`, for the correctness-critical receipt-validation layer. The frontend is
static HTML/CSS/JS with a vendored copy of pdf.js — no build step, works offline (except the
outbound OpenRouter call).

## Setup

```bash
cd annotaid
pip install -r requirements.txt          # just jsonschema

# API key lives server-side only. Put OPENROUTER_API_KEY in a dotenv-style key file.
# By default the repo-root .keys is used (the same one the scripts/ CLIs read).
#   OPENROUTER_API_KEY=sk-or-v1-...
#   NCBI_API_KEY=...        optional — raises the E-utils rate limit (3 -> 10 req/s)
#   UNPAYWALL_EMAIL=...     optional — enables the Unpaywall finder in "Add Paper(s) -> Fetch by PubMed ID(s)"

python run.py                             # serves http://127.0.0.1:8765
```

Open http://127.0.0.1:8765 in a browser. You land on the **project list**; pick a project or create
one, and curation happens at `/p/<projectId>`.

Flags: `--port`, `--host`, `--config` (the starter template a *new* project is seeded from — each
project owns its config once created), `--keys`, `--data-dir`, `--static-dir`, `--no-secrets`
(start without a key; extraction disabled, useful for reviewing imported data).

## Workflow

0. **Pick or create a project.** Creation is a three-step stepper — Details → Features → Review.

   - **Details** — name, description, and whether papers with no PubMed ID are allowed (off by
     default). The name and description are not decoration: together with the feature descriptions
     they compose the prompt sent to the model, so "Prefer the replication p-value where both are
     reported" belongs in the description.
   - **Features** — a visual editor for the feature set. Start from nothing, from the server's
     starter set, or by importing a configuration document another project manager shared. Every
     keystroke is validated by the server (`POST /api/validate-config`) rather than by a second copy
     of the rules in JavaScript, so the editor can never accept a config the server refuses.

     A fourth route lives inside this step itself: **download a fill-in-the-blanks CSV**
     (`GET /api/feature-template`), fill it in — one row per field — and **upload it back**
     (`POST /api/parse-feature-sheet`). One row per plain feature; to describe a repeating group,
     add a row with `Type=group` first, then put that name in the `Group` column of the rows
     underneath it. An upload only SEEDS the visual editor (exactly like the other three starting
     points) — you review and can still adjust before creating, and a bad cell comes back naming the
     row and what to fix rather than silently mis-parsing.
   - **Review** — the counts, the PubMed-ID policy, and **the exact prompt the model will receive**,
     so the consequence of every description is visible before a single call is paid for.

1. **Add Paper(s)** — a stepper (`static/js/addPapersModal.js`) that carries a paper from intake to
   extraction. The chain branches on the first step, so the steps are deliberately unnumbered:

   ```
   Source ──┬── Upload PDFs ────────────── AI extraction
            └── PubMed IDs ── Fetching ─── AI extraction
   ```

   - **Source** — "I have PubMed ID(s)" or "I have the PDF(s)".
   - **PubMed IDs** — paste one PMID or hundreds (separated by spaces, tabs, commas, semicolons or
     new lines), or load them from a text file (`.txt`/`.csv`/`.tsv`, or no extension). Fetch stays
     greyed out until at least one valid PMID is present.
   - **Fetching** — the backend verifies each id on PubMed, then tries PMC Open Access and other
     open-access sources (`server/ncbi.py`) for the full-text PDF; NCBI's rate limit is enforced
     server-side so several run at once. A determinate progress bar plus a per-id result list show
     what happened: added, already in library, or failed with the specific reason. Failed rows offer
     **Retry**, an in-place **Upload PDF instead** detour, or **Skip and continue**. Since the PMID
     came from PubMed itself, a fetched paper is stored pre-confirmed.
   - **Upload PDFs** — drag-and-drop or file dialog for PDFs you already have. Each gets a
     content-hash id so re-uploads dedupe.
   - **AI extraction** — see step 3. Any paper whose identity is still unresolved is settled
     inline first (confirm a PMID, or record that it has none).
2. **Identify the paper** — uploaded (not fetched) PDFs need this: a PMID prefilled from the
   filename stem if it is purely numeric, or from the PMID you named on the fetch step; it is never
   auto-committed. Duplicate PMIDs are rejected *within a project* — another project may hold the
   same one.

   Most papers have a PubMed ID; some genuinely do not. "This paper has no PubMed ID" (optionally
   with a DOI) is a real answer rather than a way of skipping the question, and it unblocks curation
   exactly as a confirmed PMID does. A paper is listed by its PMID, else its DOI, else its filename.
   Only a paper nobody has decided about yet is flagged and blocked.
3. **Extract** — the last step of the stepper. Pick model(s) and a PDF parse engine (`pdf-text`
   free, `mistral-ocr` paid, `native`); the settings apply to every paper this run added, and each
   paper reports its own success/failure row. The backend calls OpenRouter, validates/repairs every
   reply against the declared feature types, and stores one run per (paper × model). Extraction is
   only reachable here — the Extraction pane keeps just the switcher for which model's output you
   are reading.

   One call returns the whole nested document, groups included. Entries beyond a group's `maxItems`
   are dropped and the run is recorded as *repaired* rather than clean.
4. **Verify** — per feature: the AI value is prefilled and editable; the original `aiValue` is kept
   immutably beside it (unchanged = "human agrees with AI", edited = a correction). Click a value's
   evidence quote to scroll+highlight it in the PDF. A failed match is declared ("not found in PDF")
   — it is never silently shown as no-evidence. In **Card view**, you can also select text directly
   in the PDF and click the floating button to set it as that feature's evidence.

   A **repeating group** shows one collapsed line per entry, titled by its identifier. Open one and
   its fields behave exactly like paper-level values. You can add an entry the AI missed, and reject
   one it invented. The audit pair works at this level too: an AI entry you reject is kept as a
   record (it is a false positive) and can be restored; an entry you added yourself is marked as
   yours (it is a miss) and is simply removed if you delete it. "This paper reports none" and "the
   list is complete" are recorded separately, because a checked-empty list is not the same fact as
   an unexamined one.
5. **Confirm** each value with the tick. Work autosaves to disk (debounced); a browser refresh never
   loses confirmed work.
6. **Download** at any point:
   - **CSV** — columns/format defined by the project's config (`export.csv`); paste rows straight
     into the global results sheet.
   - **Audit JSON** — every run with `aiValue` beside the curator's final `value`, plus the fully
     composed prompt that produced it, for benchmarking.

   Both are scoped to the open project.

**Import** existing model-output JSONs (rich `{field:{value,evidence}}` files, or nested
`{pmid:{curator:{...}}}` files) to populate the tool without re-paying for extraction. Import
attaches to a paper whose PMID is already confirmed (upload the PDF first).

## Configuration

Config-driven; no code changes to add/remove features or models. Secrets never live here.

Each project stores its own copy of this document, and `GET /api/projects/<id>` returns it as a
self-contained, shareable file (`annotaidProject: 1`, plus `name` and `description`). Prompt text is
always inlined there — a `file` reference would point outside the project and break both ownership
and portability. `config/schema.json` is now only the **starter template** new projects are seeded
from.

- **feature spreadsheet** — `GET /api/feature-template` / `POST /api/parse-feature-sheet`
  (`server/feature_sheet.py`) round-trip a config's features through plain CSV, since Excel opens,
  edits and saves CSV natively and this way both directions are the stdlib `csv` module with no new
  dependency. Columns: `Field name, Group, Type, Label, Description, Allowed values, Optional,
  Identifies entry, Max entries`. An uploaded sheet is run through the exact same
  `features.parse_features()` the JSON path uses, so a spreadsheet gets the same guarantees a
  hand-built config does — never a second, looser set of rules.
- **features** — each: `name` (letters, digits, `_`, `-`; no dots, which separate a group from its
  field), `type` (`string | boolean | number | enum | array<string> | array<number> | group`),
  `enumValues` (if enum), `nullable`, optional `label`, `description` (used in the prompt).
- **a group** (`"type": "group"`) additionally carries its own `features` list, an optional `min`
  and `maxItems` (default 50), and needs **at least one child marked `"identifier": true`** — that
  is what names an entry in the curation list, and without it a curator cannot tell two entries
  apart. Several identifiers means "any one of these", which is how a variant is keyed by rsID *or*
  by a positional id. Nesting stops at two levels: a group inside a group is a config error.
  The same name may appear at both levels — a study-level `p_value` and a per-variant `p_value` are
  different facts — and dotted paths (`variants.p_value`) tell them apart.
- **settings** — `allowPapersWithoutPmid` (default `false`).
- **models** — OpenRouter `slug`, optional `label`, `supportsStructuredOutput` (send `response_format`
  only for models that honour it; others fall back to prompt-only + repair). **The default slugs are
  placeholders — set them to models you actually have on OpenRouter before extracting.**
- **prompts** — `label` + `file` (or inline `text`; a stored project config must inline it). The
  project's name and description, the Fields section and the JSON shape are all appended
  automatically. A run records both `promptId` (the template) and `promptHash` (the prompt actually
  sent) — they differ, because feature descriptions and the project description are prompt material.
- **export.csv** — `columns` (`{header, source}`; source = a feature name, a dotted group path like
  `variants.rsid`, or `pmid` / `doi` / `filename` / `uid` / `modelId`),
  `arrayDelimiter`, `boolTrue`/`boolFalse`, `nullText`, `includeHeaderRow`, and `rowScope`:
  `paper-model` (default), `paper`, or **a group's name** — one row per entry with the paper-level
  columns repeated on each, which is how a variant catalogue is actually shaped. A paper with no
  entries still emits one row rather than vanishing from the sheet.

## Layout

```
run.py                entrypoint
config/               schema.json + prompt.default.txt  (starter template only)
server/               stdlib backend (see module docstrings)
  features.py         typed feature/group definitions — one place for every default
  db.py               SQLite connections + schema
  project_store.py    one project's papers / runs / group entries / curation state
  projects.py         project CRUD, home-screen stats, the registry handlers resolve through
static/               home.html (project list), index.html (curation app), css/, js/ (ES modules),
                      vendor/pdfjs (pinned 3.11.174)
  js/featureSchemaEditor.js  the visual feature editor
  js/projectCreate.js        the three-step creation stepper
  js/groupEditor.js          curating a repeating group's entries
tests/                python -m annotaid.tests.test_store          (storage integrity)
                      python -m annotaid.tests.test_group_store    (group entry lifecycle)
                      python -m annotaid.tests.test_features       (schema rules + coercion)
                      python -m annotaid.tests.test_prompt         (prompt composition + identity)
                      python -m annotaid.tests.test_feature_sheet  (spreadsheet round-trip)
                      python -m annotaid.tests.test_identity       (curator identity + project createdBy)
                      python -m annotaid.tests.test_group_items    (curator-declared group row identity)
data/                 runtime store (gitignored):
  annotaid.db         projects, papers, runs, group entries, per-feature curation state,
                      UI state
  pdfs/<uid>.pdf      shared content-addressed pool — uid IS sha256(bytes)[:12], so the same
                      PDF in two projects is one file
```

Routes are `/api/projects/<id>/…`: the project id travels in the path rather than as server-side
"current project" state, so two tabs on two projects cannot cross over each other.

The evidence match/highlight engine in `static/js/pdfViewer.js` and the design system are lifted
from the `visual_curator.html` / `visual_evaluator.html` prototypes.

## Notes & limitations

- **Evidence anchoring is best-effort.** The model's quote comes from OpenRouter's PDF parse/OCR
  text, while highlighting uses pdf.js's embedded text layer, so matching is fuzzy (with
  dehyphenation) and can miss — especially on scanned/two-column papers. A miss is declared, not
  hidden; the quote is always shown.
- **Multi-model reconciliation is deferred.** Each model tab is confirmed independently; collapsing
  N models into one gold value is left to downstream tooling. Single-model papers show no tabs.
- **Imports match on PMID**, so a paper recorded as having no PubMed ID cannot be an import target.
  The import formats are flat, so an imported run has no group entries.
- **A model that returns nothing looks the same as a paper that contains nothing.** An all-absent
  reply is schema-valid, so the repair retry cannot catch it — and a long nested prompt makes it
  more likely. If a run comes back suspiciously empty, re-run it with `force`.
- Single-user, localhost-only. No auth, no hosting — so the project-manager / curator split is a
  convention about who authors a config document, not an enforced permission boundary.
- Archiving a project hides it and is not a delete: its papers, runs and PDFs stay on disk.
