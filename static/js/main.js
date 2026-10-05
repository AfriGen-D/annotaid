// annotaid app orchestrator.
import { api, setProject } from "./api.js";
import { $, el, toast, downloadFile } from "./dom.js";
import * as viewer from "./pdfViewer.js";
import * as store from "./store.js";
import { renderPaperList } from "./paperList.js";
import { renderIdentityBox } from "./identityBox.js";
import { renderFeature } from "./featureEditor.js";
import { renderGroup } from "./groupEditor.js";
import { renderGroupItemsList } from "./groupItemsList.js";
import { openAddPapersModal } from "./addPapersModal.js";

const S = {
  config: null,
  project: null,                  // {id, name, description}
  papers: [],
  runsByUid: {},                  // uid -> {modelId: run}
  activeUid: null,
  activeModelByUid: {},           // uid -> modelId
  partialByFeature: {},           // featureName -> bool (transient, this render)
  viewMode: "list",               // "list" | "card" — extraction sidebar layout
  cardIndex: 0,                   // active item index in card mode
  openRowByGroup: {},             // groupName -> rowId currently expanded
  evidenceCells: {},              // matcher key -> the cell it belongs to
};

/* ---------------- boot ---------------- */
// The project id lives in the path (/p/<id>) rather than in server-side "current
// project" state, so two tabs on two projects can't cross over each other.
function projectIdFromPath() {
  const m = location.pathname.match(/^\/p\/([^/]+)\/?$/);
  return m ? decodeURIComponent(m[1]) : null;
}

async function boot() {
  const pid = projectIdFromPath();
  if (!pid) { location.replace("/"); return; }
  setProject(pid);

  viewer.init({
    onActive: name => highlightActiveCard(name),
    onMatches: results => applyMatches(results),
    onSelection: sel => handleTextSelection(sel),
  });
  wireHeader();
  wireLayout();
  $("#pages").addEventListener("scroll", hideSelectionBtn);

  // Flushes buffered edits for EVERY project, not just this one.
  try { await store.flushPending(); } catch (_) {}

  try {
    S.config = await api.config();
  } catch (err) {
    // A stale bookmark, or a project that has been archived: go home with a
    // reason rather than sitting on a half-rendered app.
    if (err.status === 404) {
      location.replace("/?missing=" + encodeURIComponent(pid));
      return;
    }
    throw err;
  }
  S.project = S.config.project;
  applyProjectChrome();

  const state = await api.state();
  S.papers = state.papers || [];
  S.runsByUid = indexRuns(state.runs || []);
  renderLeft();
  const firstReady = S.papers.find(p => p.pmidStatus !== "pending") || S.papers[0];
  if (firstReady) selectPaper(firstReady.uid);
  updateGlobalStat();
}

function indexRuns(arr) {
  const by = {};
  for (const r of arr) (by[r.uid] = by[r.uid] || {})[r.modelId] = r;
  return by;
}

// The header carries the project's name and doubles as the way back to the
// project list.
function applyProjectChrome() {
  const chip = $("#projectChip");
  if (chip && S.project) chip.textContent = S.project.name;
  if (S.project) document.title = `${S.project.name} — annotaid`;
}

function paper(uid) { return S.papers.find(p => p.uid === uid); }
function activePaper() { return paper(S.activeUid); }

/* ---------------- header wiring ---------------- */
function wireHeader() {
  $("#addPdfBtn").addEventListener("click", () => {
    openAddPapersModal({
      onFetchOne: fetchOnePmid,
      onBatchDone: finishPmidBatch,
      onUpload: uploadPdfs,
      onConfirmPmid: confirmPaperPmid,
      onNoPmid: markPaperNoPmid,
      onExtract: runExtraction,
      onFinish: finishAddPapers,
      onSaveGroupItems: async (uid, items) => {
        const result = await api.saveGroupItems(uid, items);
        const p = paper(uid);
        if (p) p.groupItems = result.items;
        return result.items;
      },
      findByPmid: pmid => S.papers.find(p => p.pmid === pmid) || null,
      config: S.config,
      concurrency: (S.config.limits && S.config.limits.pmidFetchConcurrency) || 3,
    });
  });
  $("#importbtn").addEventListener("click", () => $("#importin").click());
  $("#importin").addEventListener("change", async e => {
    const files = [...e.target.files]; e.target.value = "";
    await importJson(files);
  });
  const dbtn = $("#downloadbtn"), menu = $("#downloadmenu");
  dbtn.onclick = () => menu.hidden = !menu.hidden;
  document.addEventListener("click", e => {
    if (!e.target.closest(".menu-wrap")) menu.hidden = true;
  });
  menu.querySelectorAll("button").forEach(b => {
    b.onclick = () => { downloadFile(api.exportUrl(b.dataset.fmt)); menu.hidden = true; };
  });
}

// Collapsible documents pane, resizable extraction pane, restore persisted layout.
function wireLayout() {
  const mainEl = document.querySelector("main");

  const fw = parseInt(localStorage.getItem("annotaid:featw") || "", 10);
  if (fw >= 300 && fw <= 680) mainEl.style.setProperty("--feat-w", fw + "px");
  S.viewMode = localStorage.getItem("annotaid:viewMode") === "card" ? "card" : "list";

  const dt = $("#docsToggle");
  const applyDocs = collapsed => {
    mainEl.classList.toggle("docs-collapsed", collapsed);
    dt.classList.toggle("on", collapsed);
    dt.title = collapsed ? "Show documents" : "Hide documents";
    try { localStorage.setItem("annotaid:docsCollapsed", collapsed ? "1" : ""); } catch (_) {}
  };
  dt.onclick = () => applyDocs(!mainEl.classList.contains("docs-collapsed"));
  applyDocs(localStorage.getItem("annotaid:docsCollapsed") === "1");

  const rez = $("#featResizer");
  let dragging = false;
  rez.addEventListener("mousedown", e => {
    dragging = true; document.body.classList.add("col-resizing"); rez.classList.add("dragging"); e.preventDefault();
  });
  window.addEventListener("mousemove", e => {
    if (!dragging) return;
    const w = Math.max(300, Math.min(680, window.innerWidth - e.clientX));  // min 300px
    mainEl.style.setProperty("--feat-w", w + "px");
  });
  window.addEventListener("mouseup", () => {
    if (!dragging) return;
    dragging = false; document.body.classList.remove("col-resizing"); rez.classList.remove("dragging");
    const w = parseInt(mainEl.style.getPropertyValue("--feat-w"), 10);
    if (w) { try { localStorage.setItem("annotaid:featw", w); } catch (_) {} }
  });
}

// opts.pmid: set when the curator came from a failed "fetch by PMID" row — it
// prefills that paper's confirm box (it is still never auto-confirmed).
async function uploadPdfs(files, opts) {
  const pmid = opts && opts.pmid;
  const added = [];
  for (const f of files) {
    try {
      const p = await api.uploadPdf(f, pmid);
      const i = S.papers.findIndex(x => x.uid === p.uid);
      if (i >= 0) S.papers[i] = p; else S.papers.push(p);
      added.push(p);
    } catch (err) { toast(`Upload failed: ${err.message}`); }
  }
  renderLeft(); updateGlobalStat();
  if (added.length) toast(`${added.length} PDF(s) added`);
  return added;   // the stepper carries these into the confirm/extract step
}

// One PMID of a batch. Deliberately does NOT select or toast — the modal owns
// per-id feedback and finishPmidBatch does the one summary toast at the end.
async function fetchOnePmid(pmid, opts) {
  const p = await api.fetchByPmid(pmid, opts);   // throws with a user-facing .message
  const i = S.papers.findIndex(x => x.uid === p.uid);
  if (i >= 0) S.papers[i] = p; else S.papers.push(p);
  renderLeft(); updateGlobalStat();              // papers show up in the sidebar as they land
  return p;
}

function finishPmidBatch({ added, duplicate, failed, cancelled }) {
  // Aborting the browser fetch does not stop the server thread: it finishes the
  // download and stores the paper regardless, so resync rather than guess.
  if (cancelled) reloadState();
  // Never steal the curator's open paper mid-batch.
  if (added.length && !S.activeUid) selectPaper(added[0].uid);
  const bits = [];
  if (added.length) bits.push(`${added.length} added`);
  if (duplicate) bits.push(`${duplicate} already in library`);
  if (failed) bits.push(`${failed} failed`);
  if (bits.length) toast(bits.join(" · "));
}

async function importJson(files) {
  const items = [];
  for (const f of files) {
    try { items.push({ filename: f.name, data: JSON.parse(await f.text()) }); }
    catch (_) { toast(`${f.name}: invalid JSON`); }
  }
  if (!items.length) return;
  const r = await api.importItems(items);
  await reloadState();
  toast(`Imported ${r.written} run(s)` + (r.skipped ? `, ${r.skipped} skipped` : ""));
  if (r.warnings && r.warnings.length) console.warn("Import warnings:", r.warnings);
}

async function reloadState() {
  const state = await api.state();
  S.papers = state.papers || [];
  S.runsByUid = indexRuns(state.runs || []);
  renderLeft(); updateGlobalStat();
  if (S.activeUid) selectPaper(S.activeUid);
}

/* ---------------- left pane ---------------- */
function renderLeft() {
  renderPaperList($("#doclist"), S.papers, {
    activeUid: S.activeUid, runsByUid: S.runsByUid, onSelect: selectPaper,
  });
  $("#doccount").textContent = S.papers.length;
}

function updateGlobalStat() {
  // "resolved", not "paired": a paper deliberately marked as having no PubMed
  // ID is fully identified and ready to curate.
  const resolved = S.papers.filter(p => p.pmidStatus !== "pending").length;
  $("#globalstat").textContent = S.papers.length ? `${resolved}/${S.papers.length}` : "—";
}

/* ---------------- select + render a paper ---------------- */
async function selectPaper(uid) {
  S.activeUid = uid;
  S.cardIndex = 0;
  renderLeft();
  const p = paper(uid);
  if (!p) return;
  // A DOI or a filename is case-sensitive, and .sub is uppercased by default —
  // showing "10.1016/J.AJHG..." invites the curator to copy out a DOI that
  // won't resolve. .exact turns the transform off for those two.
  const sub = $("#subtitle");
  const exact = p.pmidStatus !== "pending" && !p.pmid;
  sub.textContent = p.pmidStatus === "pending"
    ? "identify this paper"
    : (p.pmid ? `PMID ${p.pmid}` : (p.doi ? `DOI ${p.doi}` : p.filename));
  sub.classList.toggle("exact", exact);
  renderRight();

  // render the PDF — always by uid, which every paper has from the moment its
  // bytes land (a PMID may never arrive). Exception: an allowExtractionOnAbstract
  // paper has no PDF at all (hasPdf === false) — there's nothing to fetch, so
  // show the abstract text itself instead of letting viewer.load() 404.
  const ph = $("#viewerPlaceholder");
  ph.style.display = "none";
  ph.classList.remove("abstract-only");
  if (p.hasPdf === false) {
    viewer.clear();
    ph.style.display = "flex";
    ph.classList.add("abstract-only");
    ph.querySelector(".big").textContent = "Abstract only — no full-text PDF available";
    ph.querySelector(".hint").textContent = p.abstractText || "(no abstract stored)";
    return;
  }
  try {
    await viewer.load(api.pdfUrl(p.uid));
    recomputeMatches();
  } catch (err) {
    viewer.clear();
    ph.style.display = "flex";
    ph.querySelector(".big").textContent = "Failed to render PDF";
    ph.querySelector(".hint").textContent = err.message;
  }
}

/* ---------------- right pane ---------------- */
function activeModelId(p) {
  const runs = S.runsByUid[p.uid] || {};
  const ids = Object.keys(runs);
  let cur = S.activeModelByUid[p.uid];
  if (!cur || !runs[cur]) cur = ids[0] || null;
  S.activeModelByUid[p.uid] = cur;
  return cur;
}

function renderRight() {
  hideSelectionBtn();
  const pane = $("#rightpane");
  pane.innerHTML = "";
  const p = activePaper();
  if (!p) { pane.innerHTML = `<div class="empty-feat">Select a paper.</div>`; setViewToggle(null); setExtractPanel(null); return; }

  // Only 'pending' blocks curation. A paper with no PubMed ID has still been
  // identified, so it falls through to the normal extraction view.
  if (p.pmidStatus === "pending") {
    pane.appendChild(renderIdentityBox(p, {
      onConfirm: (pmid, source) => confirmPmid(p.uid, pmid, source),
      onNoPmid: doi => markNoPmid(p.uid, doi),
    }));
    pane.appendChild(el("div", "no-runs",
      "Confirm a PubMed ID — or record that this paper has none — to enable AI extraction."));
    $("#featstat").textContent = "";
    setViewToggle(null);
    setExtractPanel(null);
    return;
  }

  // extract panel lives above the column header (index.html #extractpanel), not in the scrolling body
  setExtractPanel(p);

  // The project's one repeating group (if any) needs its row identity DECLARED
  // by the curator before extraction can use it at all (server/extraction.py)
  // — so this has to be visible whether or not a run exists yet.
  const groupDef = (S.config.features || []).find(f => f.type === "group");
  if (groupDef) pane.appendChild(renderGroupItemsPanel(p, groupDef));

  const runs = S.runsByUid[p.uid] || {};
  if (!Object.keys(runs).length) {
    pane.appendChild(el("div", "no-runs", "No AI runs yet. Run extraction from “Add Paper(s)”, or Import existing outputs."));
    $("#featstat").textContent = "";
    setViewToggle(null);
    return;
  }

  const modelId = activeModelId(p);
  const run = runs[modelId];
  setViewToggle(run);

  const cards = el("div"); cards.id = "featurecards";
  pane.appendChild(cards);
  renderFeatureCards(run);
}

// ---------------- group row identity (curator-declared, not AI-extracted) ---
// A project's one repeating group needs to know WHICH rows exist before
// extraction can fill in values for them (server/extraction.py refuses to run
// otherwise) — this is the "either when uploading the pmid or after the pdf is
// loaded" entry point for the latter; the former lives in addPapersModal.js.
function groupItemLabelsFor(p) {
  const out = {};
  for (const it of (p && p.groupItems) || []) out[it.rowId] = it.label;
  return out;
}

// Used by groupEditor.js's "+ Add entry" (adding a row directly to an
// existing run, after the fact) so that row gets the SAME kind of declared
// identity as one entered up front — otherwise it would show as
// "(unidentified)" forever and never get a label in a CSV export.
async function declareGroupItem(p, label) {
  try {
    const result = await api.saveGroupItems(p.uid, [...(p.groupItems || []), { label }]);
    p.groupItems = result.items;
    renderRight();  // refreshes the group-items panel too, if this paper is active
    return result.items[result.items.length - 1].rowId;
  } catch (err) {
    toast(`Could not save "${label}": ${err.message}`);
    throw err;
  }
}

function renderGroupItemsPanel(p, groupDef) {
  const wrap = el("div", "gi-panel");
  wrap.appendChild(el("div", "gi-head", groupDef.label || groupDef.name));
  if (groupDef.description) wrap.appendChild(el("div", "gi-desc", groupDef.description));
  wrap.appendChild(el("div", "gi-hint",
    "Declare the rows this paper reports before running extraction — the AI fills in values for each one, it does not invent them."));

  const runCount = Object.keys(S.runsByUid[p.uid] || {}).length;
  p.groupItems = p.groupItems || [];

  async function commit() {
    try {
      const result = await api.saveGroupItems(p.uid, p.groupItems);
      p.groupItems = result.items;
      list.redraw();
    } catch (err) {
      toast(`Could not save: ${err.message}`);
    }
  }

  const list = renderGroupItemsList(p.groupItems, {
    onChange: commit,
    onRemove: item => {
      if (!runCount) return true;
      return window.confirm(
        `Remove "${item.label || "this entry"}"? Any extracted values already saved for `
        + "it will no longer show a label.");
    },
  });

  const addBtn = el("button", "btn gi-add", "+ Add entry");
  addBtn.onclick = () => list.addRow();

  wrap.appendChild(list.el);
  wrap.appendChild(addBtn);
  return wrap;
}

// The List/Card toggle lives in the Extraction column header (index.html #viewtoggle),
// so it survives #rightpane rebuilds. Clear it when no run is shown.
function setViewToggle(run) {
  const slot = $("#viewtoggle");
  if (!slot) return;
  slot.innerHTML = "";
  if (run) slot.appendChild(viewToggle(run));
}

// The model switcher sits above the column header (index.html #extractpanel),
// outside the scrolling body, so it survives #rightpane rebuilds.
function setExtractPanel(p) {
  const slot = $("#extractpanel");
  if (!slot) return;
  slot.innerHTML = "";
  const bar = p && p.pmidStatus !== "pending" ? modelBar(p) : null;
  if (bar) slot.appendChild(bar);
}

function viewToggle(run) {
  const wrap = el("div", "view-toggle");
  [["list", "List"], ["card", "Card"]].forEach(([mode, label]) => {
    const b = el("button", "vt-btn" + (S.viewMode === mode ? " on" : ""), label);
    b.onclick = () => {
      if (S.viewMode === mode) return;
      hideSelectionBtn();
      S.viewMode = mode;
      try { localStorage.setItem("annotaid:viewMode", mode); } catch (_) {}
      wrap.querySelectorAll(".vt-btn").forEach(x => x.classList.remove("on"));
      b.classList.add("on");
      renderFeatureCards(run);
      if (mode === "card") activateCurrentCard(run);
    };
    wrap.appendChild(b);
  });
  return wrap;
}

// What Card view steps through: every paper-level value, then each repeating
// group as a single card. A group is a table, not a value, so it gets its own
// stop on the walk rather than being flattened into one.
function cardItems(run) {
  return S.config.features.filter(
    f => f.type === "group" ? !!(run.groups || {})[f.name] : !!run.features[f.name]
  );
}

function renderFeatureCards(run) {
  const cards = $("#featurecards");
  if (!cards) return;
  cards.innerHTML = "";
  const items = cardItems(run);

  if (S.viewMode === "card") {
    S.cardIndex = Math.max(0, Math.min(items.length - 1, S.cardIndex));
    cards.appendChild(cardNav(run, items));
    const fdef = items[S.cardIndex];
    if (fdef) cards.appendChild(makeCard(run, fdef, true));
  } else {
    for (const fdef of items) cards.appendChild(makeCard(run, fdef, false));
  }
  updateFeatStat(run);
}

// Counts every confirmable thing: paper-level values, every cell of every live
// group entry, and each group's own "list is complete". A rejected entry is not
// counted — it is a record of what the AI claimed, not work to be done.
function updateFeatStat(run) {
  let confirmed = 0, total = 0;
  for (const f of Object.values(run.features || {})) {
    total++; if (f.confirmed) confirmed++;
  }
  for (const g of Object.values(run.groups || {})) {
    total++; if (g.confirmed) confirmed++;
    for (const row of (g.rows || [])) {
      if (row.deletedAt) continue;
      for (const c of Object.values(row.features || {})) {
        total++; if (c.confirmed) confirmed++;
      }
    }
  }
  $("#featstat").textContent = `${confirmed}/${total}✓`;
}

function makeCard(run, fdef, isCard) {
  let card;
  if (fdef.type === "group") {
    card = renderGroup(fdef, run.groups[fdef.name], {
      openRowId: S.openRowByGroup[fdef.name] || null,
      onOpenRow: rowId => {
        S.openRowByGroup[fdef.name] = rowId;
        renderFeatureCards(run);
      },
      onEdit: () => onFeatureEdit(run),
      onStructureChange: () => onGroupStructureChange(run),
      onActivateEvidence: key => viewer.activateFeature(key),
      groupItemLabels: groupItemLabelsFor(paper(run.uid)),
      onDeclareItem: label => declareGroupItem(paper(run.uid), label),
    });
  } else {
    card = renderFeature(fdef, run.features[fdef.name], {
      partial: !!S.partialByFeature[fdef.name],
      onEdit: () => onFeatureEdit(run),
      onActivateEvidence: name => viewer.activateFeature(name),
    });
  }
  if (isCard) card.classList.add("card");
  return card;
}

function cardNav(run, feats) {
  const nav = el("div", "card-nav");
  const prev = el("button", "card-arrow", "← prev");
  const cur = feats[S.cardIndex];
  const pos = el("span", "card-pos",
    `${S.cardIndex + 1} / ${feats.length}  ·  ${cur ? (cur.label || cur.name) : ""}`);
  const next = el("button", "card-arrow", "next →");
  prev.disabled = S.cardIndex <= 0;
  next.disabled = S.cardIndex >= feats.length - 1;
  prev.onclick = () => gotoCard(run, S.cardIndex - 1);
  next.onclick = () => gotoCard(run, S.cardIndex + 1);
  nav.appendChild(prev); nav.appendChild(pos); nav.appendChild(next);
  return nav;
}

function gotoCard(run, idx) {
  hideSelectionBtn();
  const items = cardItems(run);
  S.cardIndex = Math.max(0, Math.min(items.length - 1, idx));
  renderFeatureCards(run);
  activateCurrentCard(run);
}

function activateCurrentCard(run) {
  const fdef = cardItems(run)[S.cardIndex];
  // A group has no single evidence quote of its own — its rows do.
  if (fdef && fdef.type !== "group") viewer.activateFeature(fdef.name);
}

function onFeatureEdit(run) {
  store.scheduleSave(run.uid, run.modelId, run);
  // refresh confirm count + left list without a full rebuild — nothing in the
  // extract panel depends on a single feature edit (it only reflects which
  // models have a run, not confirm state within one).
  updateFeatStat(run);
  renderLeft();
}

// Adding, rejecting or restoring an entry cannot be debounced-and-forgotten:
// the SERVER mints row ids and stamps origins, so we save at once and adopt the
// run it returns. Without that a newly added row (sent with rowId:null) would be
// sent as new again on the next save and duplicated.
async function onGroupStructureChange(run) {
  renderFeatureCards(run);            // optimistic, so the click feels immediate
  try {
    const saved = await store.flushNow(run.uid, run.modelId, run);
    const bucket = S.runsByUid[run.uid] || (S.runsByUid[run.uid] = {});
    bucket[run.modelId] = saved;
    renderRight();
    recomputeMatches();
    renderLeft();
  } catch (err) {
    toast(`Could not save that change: ${err.message}`);
    reloadState();
  }
}

/* ---------------- evidence matching bridge ---------------- */
// Every quote in the run, paper-level and inside every live group entry.
// The matcher treats `name` as an opaque key, so a cell inside a group gets a
// composite one — two entries can quote different spans for the same field.
function evidenceTargets(run) {
  const out = [];
  S.evidenceCells = {};
  for (const f of S.config.features) {
    if (f.type === "group") {
      const g = (run.groups || {})[f.name];
      if (!g) continue;
      for (const row of (g.rows || [])) {
        if (row.deletedAt) continue;
        const label = rowTitle(f, row);
        for (const c of f.features) {
          const cell = row.features[c.name];
          if (!cell) continue;
          const key = `${f.name}.${c.name}@${row.rowId || "new"}`;
          S.evidenceCells[key] = cell;
          out.push({ name: key, label: `${label} · ${c.name}`, evidence: cell.evidence });
        }
      }
    } else if (run.features[f.name]) {
      S.evidenceCells[f.name] = run.features[f.name];
      out.push({ name: f.name, label: f.label || f.name,
                 evidence: run.features[f.name].evidence });
    }
  }
  return out;
}

function rowTitle(gdef, row) {
  for (const c of gdef.features) {
    if (!c.identifier) continue;
    const v = row.features[c.name] && row.features[c.name].value;
    if (v !== null && v !== undefined && v !== "") return String(v);
  }
  return "(unidentified)";
}

function recomputeMatches() {
  const p = activePaper();
  if (!p) return;
  const runs = S.runsByUid[p.uid] || {};
  const run = runs[activeModelId(p)];
  if (!run) return;
  viewer.computeMatches(evidenceTargets(run));   // fires onMatches -> applyMatches
}

function applyMatches(results) {
  const p = activePaper();
  if (!p) return;
  const run = (S.runsByUid[p.uid] || {})[activeModelId(p)];
  if (!run) return;
  let changed = false;
  S.partialByFeature = {};
  for (const r of results) {
    S.partialByFeature[r.name] = r.partial;
    // Resolved through the map built by evidenceTargets, so a composite key
    // lands on the right cell of the right entry.
    const cell = S.evidenceCells[r.name];
    if (cell && cell.evidenceMatch !== r.evidenceMatch) {
      cell.evidenceMatch = r.evidenceMatch;
      changed = true;
    }
  }
  renderFeatureCards(run);
  if (changed) store.scheduleSave(run.uid, run.modelId, run);
}

/* ---------------- PDF selection -> evidence (card view only) ---------------- */
let selBtn = null;

function currentCardFeature(run) {
  const fdef = cardItems(run)[S.cardIndex] || null;
  // A group card is a table of entries, not one value, so there is nothing
  // unambiguous to drop a PDF selection into.
  return fdef && fdef.type !== "group" ? fdef : null;
}

let listViewSelectHintShown = false;

function handleTextSelection(sel) {
  if (!sel) { listViewSelectHintShown = false; hideSelectionBtn(); return; }
  if (S.viewMode !== "card") {
    // Selecting text here used to be a silent no-op outside Card view — easy to
    // mistake for "this feature doesn't work" and fall back to typing the quote
    // into a value field by hand instead.
    if (!listViewSelectHintShown) {
      toast("Switch to Card view to add a PDF selection as evidence");
      listViewSelectHintShown = true;
    }
    hideSelectionBtn();
    return;
  }
  const p = activePaper();
  if (!p) { hideSelectionBtn(); return; }
  const run = (S.runsByUid[p.uid] || {})[activeModelId(p)];
  if (!run) { hideSelectionBtn(); return; }
  const fdef = currentCardFeature(run);
  if (!fdef) { hideSelectionBtn(); return; }
  showSelectionBtn(sel, fdef, run);
}

function showSelectionBtn(sel, fdef, run) {
  if (!selBtn) {
    selBtn = el("button", "sel-add-btn");
    document.body.appendChild(selBtn);
  }
  selBtn.textContent = `+ Add to "${fdef.name}"`;
  const top = Math.max(6, sel.rect.top - 38);
  selBtn.style.top = top + "px";
  selBtn.style.left = Math.max(6, sel.rect.left) + "px";
  selBtn.hidden = false;
  selBtn.onclick = () => {
    hideSelectionBtn();
    window.getSelection().removeAllRanges();
    applyHighlightToValue(sel.text, fdef, run);
  };
}

// Sets the curated value from a PDF highlight (or appends to it, for list
// features) — this is the actual answer, not the evidence quote (that stays
// whatever the AI extracted, the immutable aiValue counterpart).
function applyHighlightToValue(rawText, fdef, run) {
  const feat = run.features[fdef.name];
  const text = rawText.trim();
  if (!feat || !text) return;

  if (fdef.type === "boolean") {
    toast(`"${fdef.name}" is a true/false feature — use the toggle instead of a highlight`);
    return;
  }

  if (fdef.type === "enum") {
    const match = (fdef.enumValues || []).find(v => v.toLowerCase() === text.toLowerCase());
    if (!match) { toast(`"${text.slice(0, 40)}" doesn't match any allowed value for "${fdef.name}"`); return; }
    feat.value = match;
  } else if (fdef.type.startsWith("array")) {
    if (!Array.isArray(feat.value)) feat.value = [];
    if (fdef.type === "array<number>") {
      const n = Number(text);
      if (Number.isNaN(n)) { toast(`"${text.slice(0, 40)}" isn't a valid number for "${fdef.name}"`); return; }
      feat.value.push(n);
    } else {
      feat.value.push(text);
    }
  } else if (fdef.type === "number") {
    const n = Number(text);
    if (Number.isNaN(n)) { toast(`"${text.slice(0, 40)}" isn't a valid number for "${fdef.name}"`); return; }
    feat.value = n;
  } else {
    feat.value = text;
  }
  feat.present = true;

  renderFeatureCards(run);
  store.scheduleSave(run.uid, run.modelId, run.features);
  toast(`Added to "${fdef.name}"`);
}

function hideSelectionBtn() {
  if (selBtn) selBtn.hidden = true;
}

function highlightActiveCard(name) {
  // card mode: PDF evidence nav should move the visible card to that feature
  if (S.viewMode === "card") {
    const p = activePaper();
    if (!p) return;
    const run = (S.runsByUid[p.uid] || {})[activeModelId(p)];
    if (!run) return;
    // A composite key ("variants.rsid@ab12") belongs to a group card.
    const head = String(name).split(".")[0];
    const idx = cardItems(run).findIndex(f => f.name === head);
    if (idx >= 0 && idx !== S.cardIndex) { S.cardIndex = idx; renderFeatureCards(run); }
    return;
  }
  document.querySelectorAll(".feat-group.active-feat").forEach(e => e.classList.remove("active-feat"));
  const card = document.querySelector(`.feat-group[data-feature="${cssEsc(name)}"]`);
  if (card) { card.classList.add("active-feat"); card.scrollIntoView({ block: "nearest" }); }
}
function cssEsc(s) { return String(s).replace(/["\\]/g, "\\$&"); }

/* ---------------- PMID + extraction ---------------- */
async function confirmPmid(uid, pmid, source) {
  try {
    const updated = await api.confirmPmid(uid, pmid, source);
    const i = S.papers.findIndex(x => x.uid === uid);
    if (i >= 0) S.papers[i] = updated;
    renderLeft(); updateGlobalStat();
    selectPaper(uid);
    toast(`PMID ${pmid} confirmed`);
  } catch (err) {
    toast(err.status === 409 ? `PMID ${pmid} already used by another paper in this project` : `Could not confirm: ${err.message}`);
  }
}

// The other way to resolve a paper's identity: it genuinely has no PubMed ID.
// The identity box renders its own inline error, so this rethrows.
async function markNoPmid(uid, doi) {
  const updated = await api.markNoPmid(uid, doi);
  const i = S.papers.findIndex(x => x.uid === uid);
  if (i >= 0) S.papers[i] = updated;
  renderLeft(); updateGlobalStat();
  selectPaper(uid);
  toast(doi ? `Saved with DOI ${doi}` : "Saved without a PubMed ID");
  return updated;
}

// Same call, but for the stepper: it renders its own inline error next to the
// row, so this one rethrows instead of toasting and never steals the selection.
async function confirmPaperPmid(uid, pmid) {
  const updated = await api.confirmPmid(uid, pmid, "manual");
  const i = S.papers.findIndex(x => x.uid === uid);
  if (i >= 0) S.papers[i] = updated; else S.papers.push(updated);
  renderLeft(); updateGlobalStat();
  return updated;
}

// Same again for the stepper's "No PMID" route: rethrows so the row can show
// its own inline error, and never steals the curator's selection.
async function markPaperNoPmid(uid, doi) {
  const updated = await api.markNoPmid(uid, doi);
  const i = S.papers.findIndex(x => x.uid === uid);
  if (i >= 0) S.papers[i] = updated; else S.papers.push(updated);
  renderLeft(); updateGlobalStat();
  return updated;
}

// Dropdown in the Extraction header: pick which already-run model's curation to
// view (models with no run yet are listed but disabled).
// Lives inside .lab, CSS-hidden while expanded (see styles.css).
function modelSelect(p, runs) {
  const modelId = activeModelId(p);
  const sel = el("select", "model-select");
  for (const m of S.config.models) {
    const has = !!runs[m.slug];
    const o = el("option", null, m.label + (has ? "" : " — not run yet"));
    o.value = m.slug;
    o.disabled = !has;
    if (m.slug === modelId) o.selected = true;
    sel.appendChild(o);
  }
  // .lab's own onclick toggles collapse — interacting with the dropdown must not.
  sel.addEventListener("mousedown", e => e.stopPropagation());
  sel.addEventListener("click", e => e.stopPropagation());
  sel.onchange = () => {
    S.activeModelByUid[p.uid] = sel.value; S.cardIndex = 0; renderRight(); recomputeMatches();
  };
  return sel;
}

// Extraction is driven from the "Add Paper(s)" stepper now, so this pane keeps
// only the per-model result switcher — which model's output you are reading.
function modelBar(p) {
  const runs = S.runsByUid[p.uid] || {};
  if (!Object.keys(runs).length) return null;
  const bar = el("div", "model-bar");
  bar.appendChild(el("span", "lab", "Model"));
  bar.appendChild(modelSelect(p, runs));
  return bar;
}

// Runs one paper through the extraction endpoint and folds the result into
// state. Returns the runs so the caller can report per-model success; it throws
// on failure rather than toasting, because the stepper shows its own per-paper row.
async function runExtraction(p, models, parseEngine) {
  const engine = parseEngine || S.config.defaults.parseEngine;
  const { runs } = await api.extract(p.uid, models, S.config.defaults.promptId, engine, false);
  const cur = S.runsByUid[p.uid] = S.runsByUid[p.uid] || {};
  for (const r of runs) cur[r.modelId] = r;
  S.activeModelByUid[p.uid] = runs[0] ? runs[0].modelId : S.activeModelByUid[p.uid];
  S.cardIndex = 0;
  renderRight(); recomputeMatches(); renderLeft();
  return runs;
}

// The stepper closed: land on whatever it produced, so the curator isn't
// dropped on an empty pane after adding and extracting a paper.
function finishAddPapers(papers) {
  renderLeft(); updateGlobalStat();
  const landing = (papers || []).find(p => p.pmidStatus !== "pending") || (papers || [])[0];
  if (landing && !S.activeUid) { selectPaper(landing.uid); return; }
  renderRight(); recomputeMatches();
}

boot();
