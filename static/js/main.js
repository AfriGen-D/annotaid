// annotaid app orchestrator.
import { api } from "./api.js";
import { $, el, toast } from "./dom.js";
import * as viewer from "./pdfViewer.js";
import * as store from "./store.js";
import { renderPaperList } from "./paperList.js";
import { renderPmidBox } from "./pmidBox.js";
import { renderFeature } from "./featureEditor.js";
import { openAddPapersModal } from "./addPapersModal.js";

const S = {
  config: null,
  papers: [],
  runsByPmid: {},                 // pmid -> {modelId: run}
  activeUid: null,
  activeModelByPmid: {},          // pmid -> modelId
  partialByFeature: {},           // featureName -> bool (transient, this render)
  viewMode: "list",               // "list" | "card" — extraction sidebar layout
  cardIndex: 0,                   // active feature index in card mode
};

/* ---------------- boot ---------------- */
async function boot() {
  viewer.init({
    onActive: name => highlightActiveCard(name),
    onMatches: results => applyMatches(results),
    onSelection: sel => handleTextSelection(sel),
  });
  wireHeader();
  wireLayout();
  $("#pages").addEventListener("scroll", hideSelectionBtn);

  try { await store.flushPending(); } catch (_) {}
  S.config = await api.config();
  const state = await api.state();
  S.papers = state.papers || [];
  S.runsByPmid = indexRuns(state.runs || []);
  renderLeft();
  const firstPaired = S.papers.find(p => p.pmid) || S.papers[0];
  if (firstPaired) selectPaper(firstPaired.uid);
  updateGlobalStat();
}

function indexRuns(arr) {
  const by = {};
  for (const r of arr) (by[r.pmid] = by[r.pmid] || {})[r.modelId] = r;
  return by;
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
      onExtract: runExtraction,
      onFinish: finishAddPapers,
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
    b.onclick = () => { downloadFile(`/api/export?format=${b.dataset.fmt}`); menu.hidden = true; };
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

function downloadFile(url) {
  const a = el("a"); a.href = url; a.download = ""; document.body.appendChild(a); a.click(); a.remove();
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
  S.runsByPmid = indexRuns(state.runs || []);
  renderLeft(); updateGlobalStat();
  if (S.activeUid) selectPaper(S.activeUid);
}

/* ---------------- left pane ---------------- */
function renderLeft() {
  renderPaperList($("#doclist"), S.papers, {
    activeUid: S.activeUid, runsByPmid: S.runsByPmid, onSelect: selectPaper,
  });
  $("#doccount").textContent = S.papers.length;
}

function updateGlobalStat() {
  const paired = S.papers.filter(p => p.pmid).length;
  $("#globalstat").textContent = S.papers.length ? `${paired}/${S.papers.length}` : "—";
}

/* ---------------- select + render a paper ---------------- */
async function selectPaper(uid) {
  S.activeUid = uid;
  S.cardIndex = 0;
  renderLeft();
  const p = paper(uid);
  if (!p) return;
  $("#subtitle").textContent = p.pmid ? `PMID ${p.pmid}` : "confirm PMID";
  renderRight();

  // render the PDF (by pmid if confirmed, else by uid so the curator can read it)
  const ph = $("#viewerPlaceholder");
  ph.style.display = "none";
  try {
    await viewer.load(api.pdfUrl(p.pmid || p.uid));
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
  const runs = S.runsByPmid[p.pmid] || {};
  const ids = Object.keys(runs);
  let cur = S.activeModelByPmid[p.pmid];
  if (!cur || !runs[cur]) cur = ids[0] || null;
  S.activeModelByPmid[p.pmid] = cur;
  return cur;
}

function renderRight() {
  hideSelectionBtn();
  const pane = $("#rightpane");
  pane.innerHTML = "";
  const p = activePaper();
  if (!p) { pane.innerHTML = `<div class="empty-feat">Select a paper.</div>`; setViewToggle(null); setExtractPanel(null); return; }

  if (!p.pmid) {
    pane.appendChild(renderPmidBox(p, { onConfirm: (pmid, source) => confirmPmid(p.uid, pmid, source) }));
    pane.appendChild(el("div", "no-runs", "Confirm the PubMed ID to enable AI extraction."));
    $("#featstat").textContent = "";
    setViewToggle(null);
    setExtractPanel(null);
    return;
  }

  // extract panel lives above the column header (index.html #extractpanel), not in the scrolling body
  setExtractPanel(p);

  const runs = S.runsByPmid[p.pmid] || {};
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
  const bar = p && p.pmid ? modelBar(p) : null;
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

function renderFeatureCards(run) {
  const cards = $("#featurecards");
  if (!cards) return;
  cards.innerHTML = "";
  const feats = S.config.features.filter(f => run.features[f.name]);

  if (S.viewMode === "card") {
    S.cardIndex = Math.max(0, Math.min(feats.length - 1, S.cardIndex));
    cards.appendChild(cardNav(run, feats));
    const fdef = feats[S.cardIndex];
    if (fdef) cards.appendChild(makeCard(run, fdef, true));
  } else {
    for (const fdef of feats) cards.appendChild(makeCard(run, fdef, false));
  }

  let confirmed = 0;
  for (const f of Object.values(run.features)) if (f.confirmed) confirmed++;
  $("#featstat").textContent = `${confirmed}/${S.config.features.length}✓`;
}

function makeCard(run, fdef, isCard) {
  const ctx = {
    partial: !!S.partialByFeature[fdef.name],
    onEdit: () => onFeatureEdit(run),
    onActivateEvidence: name => viewer.activateFeature(name),
  };
  const card = renderFeature(fdef, run.features[fdef.name], ctx);
  if (isCard) card.classList.add("card");
  return card;
}

function cardNav(run, feats) {
  const nav = el("div", "card-nav");
  const prev = el("button", "card-arrow", "← prev");
  const cur = feats[S.cardIndex];
  const pos = el("span", "card-pos", `${S.cardIndex + 1} / ${feats.length}  ·  ${cur ? cur.name : ""}`);
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
  const feats = S.config.features.filter(f => run.features[f.name]);
  S.cardIndex = Math.max(0, Math.min(feats.length - 1, idx));
  renderFeatureCards(run);
  activateCurrentCard(run);
}

function activateCurrentCard(run) {
  const feats = S.config.features.filter(f => run.features[f.name]);
  const fdef = feats[S.cardIndex];
  if (fdef) viewer.activateFeature(fdef.name);  // highlight evidence (no-op if none)
}

function onFeatureEdit(run) {
  store.scheduleSave(run.pmid, run.modelId, run.features);
  // refresh confirm count + left list without a full rebuild — nothing in the
  // extract panel depends on a single feature edit (it only reflects which
  // models have a run, not confirm state within one).
  let confirmed = 0;
  for (const f of Object.values(run.features)) if (f.confirmed) confirmed++;
  $("#featstat").textContent = `${confirmed}/${S.config.features.length}✓`;
  renderLeft();
}

/* ---------------- evidence matching bridge ---------------- */
function recomputeMatches() {
  const p = activePaper();
  if (!p || !p.pmid) return;
  const runs = S.runsByPmid[p.pmid] || {};
  const run = runs[activeModelId(p)];
  if (!run) return;
  const feats = S.config.features
    .filter(f => run.features[f.name])
    .map(f => ({ name: f.name, evidence: run.features[f.name].evidence }));
  viewer.computeMatches(feats);   // fires onMatches -> applyMatches
}

function applyMatches(results) {
  const p = activePaper();
  if (!p || !p.pmid) return;
  const run = (S.runsByPmid[p.pmid] || {})[activeModelId(p)];
  if (!run) return;
  let changed = false;
  S.partialByFeature = {};
  for (const r of results) {
    S.partialByFeature[r.name] = r.partial;
    const feat = run.features[r.name];
    if (feat && feat.evidenceMatch !== r.evidenceMatch) { feat.evidenceMatch = r.evidenceMatch; changed = true; }
  }
  renderFeatureCards(run);
  if (changed) store.scheduleSave(run.pmid, run.modelId, run.features);
}

/* ---------------- PDF selection -> evidence (card view only) ---------------- */
let selBtn = null;

function currentCardFeature(run) {
  const feats = S.config.features.filter(f => run.features[f.name]);
  return feats[S.cardIndex] || null;
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
  if (!p || !p.pmid) { hideSelectionBtn(); return; }
  const run = (S.runsByPmid[p.pmid] || {})[activeModelId(p)];
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
  store.scheduleSave(run.pmid, run.modelId, run.features);
  toast(`Added to "${fdef.name}"`);
}

function hideSelectionBtn() {
  if (selBtn) selBtn.hidden = true;
}

function highlightActiveCard(name) {
  // card mode: PDF evidence nav should move the visible card to that feature
  if (S.viewMode === "card") {
    const p = activePaper();
    if (!p || !p.pmid) return;
    const run = (S.runsByPmid[p.pmid] || {})[activeModelId(p)];
    if (!run) return;
    const feats = S.config.features.filter(f => run.features[f.name]);
    const idx = feats.findIndex(f => f.name === name);
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
    toast(err.status === 409 ? `PMID ${pmid} already used by another paper` : `Could not confirm: ${err.message}`);
  }
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
    S.activeModelByPmid[p.pmid] = sel.value; S.cardIndex = 0; renderRight(); recomputeMatches();
  };
  return sel;
}

// Extraction is driven from the "Add Paper(s)" stepper now, so this pane keeps
// only the per-model result switcher — which model's output you are reading.
function modelBar(p) {
  const runs = S.runsByPmid[p.pmid] || {};
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
  const { runs } = await api.extract(p.pmid, models, S.config.defaults.promptId, engine, false);
  const cur = S.runsByPmid[p.pmid] = S.runsByPmid[p.pmid] || {};
  for (const r of runs) cur[r.modelId] = r;
  S.activeModelByPmid[p.pmid] = runs[0] ? runs[0].modelId : S.activeModelByPmid[p.pmid];
  S.cardIndex = 0;
  renderRight(); recomputeMatches(); renderLeft();
  return runs;
}

// The stepper closed: land on whatever it produced, so the curator isn't
// dropped on an empty pane after adding and extracting a paper.
function finishAddPapers(papers) {
  renderLeft(); updateGlobalStat();
  const landing = (papers || []).find(p => p.pmid) || (papers || [])[0];
  if (landing && !S.activeUid) { selectPaper(landing.uid); return; }
  renderRight(); recomputeMatches();
}

boot();
