# annotaid

A local, single-user biocuration gold-standard tool. An LLM proposes structured values for a
configurable set of features from an academic PDF; a human curator verifies each value against the
source (evidence-highlighted), corrects it where needed, and confirms it. The verified output is a
gold-standard dataset — fast to produce, evidence-grounded, and auditable.

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
#   UNPAYWALL_EMAIL=...     optional — enables the Unpaywall finder in "Add PDF -> Fetch by PubMed ID"

python run.py                             # serves http://127.0.0.1:8765
```

Open http://127.0.0.1:8765 in a browser.

Flags: `--port`, `--host`, `--config`, `--keys`, `--data-dir`, `--static-dir`,
`--no-secrets` (start without a key; extraction disabled, useful for reviewing imported data).

## Workflow

1. **Add PDF** — two ways, both behind the "Add PDF" button:
   - **Fetch by PubMed ID** — enter a PMID; the backend verifies it on PubMed, then tries PMC Open
     Access and other open-access sources (`server/ncbi.py`) for the full-text PDF. Since the PMID
     came from PubMed itself, it's stored pre-confirmed. Most subscription-only papers aren't
     retrievable this way — the modal shows why and points at the Upload tab instead.
   - **Upload** — drag-and-drop or file-dialog upload of PDF(s) you already have. Each gets a
     content-hash id so re-uploads dedupe.
2. **Confirm PMID** — for uploaded (not fetched) PDFs: the filename stem is prefilled *only* if it
   is purely numeric; it is never auto-committed. Confirm (or type) the real PubMed ID. Duplicate
   PMIDs are rejected.
3. **Extract** — pick model(s) and a PDF parse engine (`pdf-text` free, `mistral-ocr` paid,
   `native`) and run. The backend calls OpenRouter, validates/repairs every reply against the
   declared feature types, and stores one run per (paper × model).
4. **Verify** — per feature: the AI value is prefilled and editable; the original `aiValue` is kept
   immutably beside it (unchanged = "human agrees with AI", edited = a correction). Click a value's
   evidence quote to scroll+highlight it in the PDF. A failed match is declared ("not found in PDF")
   — it is never silently shown as no-evidence. In **Card view**, you can also select text directly
   in the PDF and click the floating button to set it as that feature's evidence.
5. **Confirm** each value with the tick. Work autosaves to disk (debounced); a browser refresh never
   loses confirmed work.
6. **Download** at any point:
   - **CSV** — columns/format defined in `config/schema.json` (`export.csv`); paste rows straight
     into the global results sheet.
   - **Audit JSON** — every run with `aiValue` beside the curator's final `value`, for benchmarking.

**Import** existing model-output JSONs (rich `{field:{value,evidence}}` files, or nested
`{pmid:{curator:{...}}}` files) to populate the tool without re-paying for extraction. Import
attaches to a paper whose PMID is already confirmed (upload the PDF first).

## Configuration — `config/schema.json`

Config-driven; no code changes to add/remove features or models. Secrets never live here.

- **features** — each: `name`, `type` (`string | boolean | number | enum | array<string> |
  array<number>`), `enumValues` (if enum), `nullable`, `description` (used in the prompt).
- **models** — OpenRouter `slug`, optional `label`, `supportsStructuredOutput` (send `response_format`
  only for models that honour it; others fall back to prompt-only + repair). **The default slugs are
  placeholders — set them to models you actually have on OpenRouter before extracting.**
- **prompts** — `label` + `file` (or inline `text`). The Fields section + JSON shape are appended
  from the feature list automatically.
- **export.csv** — `columns` (`{header, source}`; source = a feature name / `pmid` / `modelId`),
  `arrayDelimiter`, `boolTrue`/`boolFalse`, `nullText`, `rowScope` (`paper-model` | `paper`),
  `includeHeaderRow`.

## Layout

```
run.py                entrypoint
config/               schema.json + prompt.default.txt
server/               stdlib backend (see module docstrings)
static/               index.html, css/, js/ (ES modules), vendor/pdfjs (pinned 3.11.174)
data/                 runtime store (gitignored): pdfs/, papers/, runs/, index.json, state.json
```

The evidence match/highlight engine in `static/js/pdfViewer.js` and the design system are lifted
from the `visual_curator.html` / `visual_evaluator.html` prototypes.

## Notes & limitations

- **Evidence anchoring is best-effort.** The model's quote comes from OpenRouter's PDF parse/OCR
  text, while highlighting uses pdf.js's embedded text layer, so matching is fuzzy (with
  dehyphenation) and can miss — especially on scanned/two-column papers. A miss is declared, not
  hidden; the quote is always shown.
- **Multi-model reconciliation is deferred.** Each model tab is confirmed independently; collapsing
  N models into one gold value is left to downstream tooling. Single-model papers show no tabs.
- Single-user, localhost-only. No auth, no hosting.
