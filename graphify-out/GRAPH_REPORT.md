# Graph Report - annotaid  (2026-09-10)

## Corpus Check
- 50 files · ~66,404 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 657 nodes · 1491 edges · 26 communities (24 shown, 1 thin omitted)
- Extraction: 94% EXTRACTED · 6% INFERRED · 0% AMBIGUOUS · INFERRED: 93 edges (avg confidence: 0.9)
- Token cost: 66,672 input · 0 output

## Community Hubs (Navigation)
- Project/Feature Setup UI
- Main App Controller (JS)
- Project Store
- HTTP Request Handlers
- Feature Definitions
- Config Loading
- AI Extraction Pipeline
- Project README & Docs
- Add-Papers Modal UI
- Config Validation
- NCBI/PMID Lookup
- PDF Viewer UI
- Database Layer
- Client-side Store/API
- Projects Management
- Server Bootstrap & Routing
- Config Loading (App Entry)
- Group Store Tests
- Projects & Prompt Tests
- Server Utilities
- Store Tests
- Feature Sheet Tests
- Features Tests
- Projects (misc)
- README (misc)

## God Nodes (most connected - your core abstractions)
1. `el()` - 59 edges
2. `ProjectStore` - 45 edges
3. `$` - 30 edges
4. `Config` - 27 edges
5. `register()` - 27 edges
6. `openAddPapersModal()` - 26 edges
7. `Db` - 25 edges
8. `send_json()` - 22 edges
9. `send_error_json()` - 19 edges
10. `openCreateProject()` - 19 edges

## Surprising Connections (you probably didn't know these)
- `main()` --uses--> `Db`  [INFERRED]
  run.py → server/db.py
- `rejects()` --uses--> `ConfigError`  [INFERRED]
  tests/test_features.py → server/config_loader.py
- `main()` --calls--> `parse_config()`  [INFERRED]
  tests/test_feature_sheet.py → server/config_loader.py
- `main()` --calls--> `parse_config()`  [INFERRED]
  tests/test_features.py → server/config_loader.py
- `run_all()` --calls--> `parse_config()`  [INFERRED]
  tests/test_group_store.py → server/config_loader.py

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **End-to-end curation workflow (add paper -> extract -> verify -> confirm -> download)** — readme_add_paper_stepper, readme_extract_step, readme_verify_step, readme_confirm_autosave, readme_download_outputs [EXTRACTED 1.00]
- **Evidence grounding pipeline: prompt rule -> anchoring limitation -> highlight engine -> viewer toolbar** — config_prompt_default_evidence_rule, readme_evidence_anchoring_limitation, readme_pdfviewer_evidence_engine, static_index_viewer_toolbar [INFERRED 0.80]
- **Shared page-shell UI pattern (header/toast/modal-overlay + design system CSS)** — static_home_page, static_index_page, static_css_styles_module [INFERRED 0.75]

## Communities (26 total, 1 thin omitted)

### Community 0 - "Project/Feature Setup UI"
Cohesion: 0.06
Nodes (73): api, el(), escapeHtml(), boolWidget(), buildWidget(), cleanList(), enumWidget(), listWidget() (+65 more)

### Community 1 - "Main App Controller (JS)"
Cohesion: 0.11
Nodes (58): setProject(), $, toast(), renderIdentityBox(), activateCurrentCard(), activeModelId(), activePaper(), applyHighlightToValue() (+50 more)

### Community 2 - "Project Store"
Cohesion: 0.06
Nodes (29): confirm_pmid(), mark_no_pmid(), normalise_doi(), PaperError, Exception, Paper intake: upload, suggested-PMID, and identity resolution (brief §5.2). A…, Record that a paper has no PubMed ID, optionally with a DOI instead. This…, Used by the "fetch by PMID" flow: since the PMID was just verified against… (+21 more)

### Community 3 - "HTTP Request Handlers"
Cohesion: 0.12
Nodes (49): h_app_shell(), h_config(), h_export(), h_extract(), h_feature_template(), h_fetch_pmid(), h_home_shell(), h_identity() (+41 more)

### Community 4 - "Feature Definitions"
Cohesion: 0.07
Nodes (32): build_template_csv(), _cell(), _header_map(), _norm(), _parse_bool(), parse_sheet_csv(), Exception, A feature set as a spreadsheet: download a fill-in-the-blanks CSV, upload it… (+24 more)

### Community 5 - "Config Loading"
Cohesion: 0.06
Nodes (29): Config, Project policy. Absent reads as False: a project must opt IN to papers with no…, The config half of a project's portable document. Prompt text is INLINED (never…, Config for GET /api/config — NO secrets ever included., By name, or by dotted path for a field inside a group., Every curatable path, e.g. ['sample_size', 'variants.rsid', ...]., Paper-level features only., _cell() (+21 more)

### Community 6 - "AI Extraction Pipeline"
Cohesion: 0.08
Nodes (39): _blank_features(), _blank_groups(), _cell(), extract_one(), _features_from_canonical(), _groups_from_canonical(), Run extraction for (paper × model): call OpenRouter, validate, build records.…, One curated cell. aiValue is written once here and never again — the pair (what… (+31 more)

### Community 7 - "Project README & Docs"
Cohesion: 0.06
Nodes (36): Evidence-quote / absent-field grounding rule, Default extraction prompt template, Add Paper(s) stepper workflow, AnnotAid (biocuration gold-standard tool), Config-driven design: no code changes to add/remove features or models, Confirm + autosave, Download outputs: CSV / Audit JSON, Evidence anchoring is best-effort (fuzzy match) (+28 more)

### Community 8 - "Add-Papers Modal UI"
Cohesion: 0.12
Nodes (30): isIdFile(), LABEL, openAddPapersModal(), acceptDetour(), acceptUploads(), buildRows(), chain(), choiceCard() (+22 more)

### Community 9 - "Config Validation"
Cohesion: 0.14
Nodes (24): _clean_of_extras(), coerce_feature(), coerce_group(), coerce_payload(), coerce_value(), extract_json(), _first(), _is_empty_value() (+16 more)

### Community 10 - "NCBI/PMID Lookup"
Cohesion: 0.16
Nodes (19): _download_pdf_url(), _eutils_params(), _fetch_from_tgz(), fetch_fulltext_pdf(), _find_pdf(), _get_article_ids(), _is_pdf(), _MinIntervalGate (+11 more)

### Community 11 - "PDF Viewer UI"
Cohesion: 0.20
Nodes (21): activateFeature(), clear(), clearHighlights(), computeMatches(), find(), handleSelectionChange(), init(), load() (+13 more)

### Community 12 - "Database Layer"
Cohesion: 0.16
Nodes (8): Connection, Db, DbError, RuntimeError, SQLite connection management + schema (replaces the JSON file store). Why…, Serialised, explicit transaction. Rolls back on any exception., ProjectRegistry, Resolves a project id to a Project, caching the parsed Config. The cache is…

### Community 13 - "Client-side Store/API"
Cohesion: 0.21
Nodes (15): jget(), jpost(), projectId(), safeErr(), bufKey(), clearBuffer(), editableCells(), editableRun() (+7 more)

### Community 14 - "Projects Management"
Cohesion: 0.26
Nodes (15): archive_project(), _clean_description(), _clean_name(), create_project(), get_project_row(), new_id(), portable_document(), ProjectError (+7 more)

### Community 15 - "Server Bootstrap & Routing"
Cohesion: 0.19
Nodes (9): build_server(), Context, make_handler(), ThreadingHTTPServer + request handler. Stdlib only., There is no global `config` or `store` any more — a feature set and a paper…, annotaid — local biocuration gold-standard tool (stdlib backend)., Tiny regex router: (method, path) -> (handler, path-params)., Router (+1 more)

### Community 16 - "Config Loading (App Entry)"
Cohesion: 0.26
Nodes (11): main(), annotaid entrypoint. python run.py # serve on http://127.0.0.1:8765 python…, ConfigError, load_config(), load_secrets(), parse_config(), Exception, Load and meta-validate a curation config, and load secrets from .keys. The… (+3 more)

### Community 17 - "Group Store Tests"
Cohesion: 0.35
Nodes (11): ai_row(), by_rsid(), cell(), check(), main(), make_run(), num_cell(), raises() (+3 more)

### Community 18 - "Projects & Prompt Tests"
Cohesion: 0.22
Nodes (7): Project, One resolved project: metadata + parsed Config + its own store., The name and description are semantic input to the model, not just UI chrome —…, check(), main(), project(), Prompt composition + prompt identity — run: python -m…

### Community 19 - "Server Utilities"
Cohesion: 0.20
Nodes (10): atomic_write_bytes(), atomic_write_json(), load_env_file(), prompt_id(), Small stdlib-only helpers shared across the backend. Several of these are…, Load simple KEY=value lines into os.environ (does not overwrite existing).…, Stable id identifying the exact prompt used (brief §6 promptId)., Make a string safe to use as a single path segment (no separators). (+2 more)

### Community 20 - "Store Tests"
Cohesion: 0.48
Nodes (6): check(), main(), make_run(), raises(), Smoke test for the SQLite store — run: python -m annotaid.tests.test_store No…, run_all()

### Community 21 - "Feature Sheet Tests"
Cohesion: 0.53
Nodes (5): check(), csv(), main(), Spreadsheet -> feature list. Run: python -m annotaid.tests.test_feature_sheet…, rejects()

### Community 22 - "Features Tests"
Cohesion: 0.60
Nodes (4): check(), main(), Feature schema parsing + group coercion. Run: python -m…, rejects()

### Community 23 - "Projects (misc)"
Cohesion: 0.50
Nodes (4): _leaf_count(), list_projects(), Total curatable values a config declares: paper-level features PLUS every field…, Home-screen payload. One query — cheap because run_features is relational…

## Knowledge Gaps
- **19 isolated node(s):** `STEP_LABEL`, `LABEL`, `LEAF_TYPES`, `S`, `S` (+14 more)
  These have ≤1 connection - possible missing edges or undocumented components. (Counts symbols only; 175 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **1 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `ProjectStore` connect `Project Store` to `Config Loading`, `AI Extraction Pipeline`, `Database Layer`, `Projects Management`, `Group Store Tests`, `Store Tests`?**
  _High betweenness centrality (0.094) - this node is a cross-community bridge._
- **Why does `Config` connect `Config Loading` to `Config Loading (App Entry)`, `Projects Management`, `AI Extraction Pipeline`, `Server Bootstrap & Routing`?**
  _High betweenness centrality (0.045) - this node is a cross-community bridge._
- **Why does `el()` connect `Project/Feature Setup UI` to `Add-Papers Modal UI`, `Main App Controller (JS)`?**
  _High betweenness centrality (0.040) - this node is a cross-community bridge._
- **Are the 12 inferred relationships involving `ProjectStore` (e.g. with `export_audit_json()` and `export_csv()`) actually correct?**
  _`ProjectStore` has 12 INFERRED edges - model-reasoned connections that need verification._
- **Are the 7 inferred relationships involving `Config` (e.g. with `Context` and `export_audit_json()`) actually correct?**
  _`Config` has 7 INFERRED edges - model-reasoned connections that need verification._
- **Are the 25 inferred relationships involving `register()` (e.g. with `h_app_shell()` and `h_config()`) actually correct?**
  _`register()` has 25 INFERRED edges - model-reasoned connections that need verification._
- **What connects `STEP_LABEL`, `LABEL`, `LEAF_TYPES` to the rest of the system?**
  _19 weakly-connected nodes found - possible documentation gaps or missing edges._