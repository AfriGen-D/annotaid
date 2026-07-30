// annotaid app orchestrator.
import { api } from "./api.js";
import { $, el, toast } from "./dom.js";
import * as viewer from "./pdfViewer.js";
import * as store from "./store.js";
import { renderPaperList } from "./paperList.js";
import { renderPmidBox } from "./pmidBox.js";
import { renderTabs } from "./modelTabs.js";
import { renderFeature } from "./featureEditor.js";

const S = {
  config: null,
  papers: [],
  runsByPmid: {},                 // pmid -> {modelId: run}
  activeUid: null,
  activeModelByPmid: {},          // pmid -> modelId
  partialByFeature: {},           // featureName -> bool (transient, this render)
  extractCollapsed: false,        // "Run AI extraction" panel collapsed?
  viewMode: "list",               // "list" | "card" — extraction sidebar layout
  cardIndex: 0,                   // active feature index in card mode
};

/* ---------------- boot ---------------- */
async function boot() {
  viewer.init({
    onActive: name => highlightActiveCard(name),
    onMatches: results => applyMatches(results),
  });
  wireHeader();
  wireLayout();

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
  $("#filein").addEventListener("change", async e => {
    const files = [...e.target.files]; e.target.value = "";
    await uploadPdfs(files);
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
  S.extractCollapsed = localStorage.getItem("annotaid:extractCollapsed") === "1";
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

async function uploadPdfs(files) {
  let firstNew = null;
  for (const f of files) {
    try {
      const p = await api.uploadPdf(f);
      const i = S.papers.findIndex(x => x.uid === p.uid);
      if (i >= 0) S.papers[i] = p; else S.papers.push(p);
      if (!firstNew) firstNew = p.uid;
    } catch (err) { toast(`Upload failed: ${err.message}`); }
  }
  renderLeft(); updateGlobalStat();
  if (firstNew) selectPaper(firstNew);
  toast(`${files.length} PDF(s) added`);
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
    pane.appendChild(el("div", "no-runs", "No AI runs yet. Pick model(s) above and click Extract, or Import existing outputs."));
    $("#featstat").textContent = "";
    setViewToggle(null);
    return;
  }

  const modelId = activeModelId(p);
  const tabs = renderTabs(runs, modelId, S.config.features.length, id => {
    S.activeModelByPmid[p.pmid] = id; S.cardIndex = 0; renderRight(); recomputeMatches();
  });
  if (tabs) pane.appendChild(tabs);

  const run = runs[modelId];
  pane.appendChild(runMeta(run, p));
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

// The "Run AI extraction" panel sits above the column header (index.html #extractpanel),
// outside the scrolling body. Shown for any confirmed paper, cleared otherwise.
function setExtractPanel(p) {
  const slot = $("#extractpanel");
  if (!slot) return;
  slot.innerHTML = "";
  if (p && p.pmid) slot.appendChild(extractPanel(p));
}

function viewToggle(run) {
  const wrap = el("div", "view-toggle");
  [["list", "List"], ["card", "Card"]].forEach(([mode, label]) => {
    const b = el("button", "vt-btn" + (S.viewMode === mode ? " on" : ""), label);
    b.onclick = () => {
      if (S.viewMode === mode) return;
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

function runMeta(run, p) {
  const m = el("div", "run-meta");
  m.appendChild(el("span", "st-" + run.status, run.status.toUpperCase()));
  m.appendChild(el("span", null, run.source === "import" ? "imported" : `engine: ${run.parseEngine}`));
  if (run.error) { const e = el("span", null, run.error); e.style.color = "var(--bad)"; e.style.maxWidth = "180px"; e.style.overflow = "hidden"; e.style.textOverflow = "ellipsis"; e.style.whiteSpace = "nowrap"; e.title = run.error; m.appendChild(e); }
  const retry = el("button", "retry", "re-run");
  retry.onclick = () => runExtraction(p, [run.modelId], true);
  m.appendChild(retry);
  return m;
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
  // refresh confirm count + tab dots + left list without a full rebuild
  let confirmed = 0;
  for (const f of Object.values(run.features)) if (f.confirmed) confirmed++;
  $("#featstat").textContent = `${confirmed}/${S.config.features.length}✓`;
  refreshTabsAndList();
}

function refreshTabsAndList() {
  const p = activePaper();
  if (!p || !p.pmid) return;
  const runs = S.runsByPmid[p.pmid] || {};
  const bar = document.querySelector(".tabbar");
  const fresh = renderTabs(runs, activeModelId(p), S.config.features.length, id => {
    S.activeModelByPmid[p.pmid] = id; S.cardIndex = 0; renderRight(); recomputeMatches();
  });
  if (bar && fresh) bar.replaceWith(fresh);
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

function extractPanel(p) {
  const panel = el("div", "extract-panel" + (S.extractCollapsed ? " collapsed" : ""));
  const head = el("div", "lab");
  head.appendChild(el("span", null, "Run AI extraction"));
  head.appendChild(el("span", "caret", "▼"));
  head.onclick = () => {
    S.extractCollapsed = !panel.classList.contains("collapsed");
    panel.classList.toggle("collapsed", S.extractCollapsed);
    try { localStorage.setItem("annotaid:extractCollapsed", S.extractCollapsed ? "1" : ""); } catch (_) {}
  };
  panel.appendChild(head);

  const body = el("div", "extract-body");
  const runs = S.runsByPmid[p.pmid] || {};

  const picks = el("div", "model-picks");
  const checks = [];
  for (const m of S.config.models) {
    const lab = el("label");
    const cb = el("input"); cb.type = "checkbox"; cb.value = m.slug;
    cb.checked = !runs[m.slug];                 // pre-check models not yet run
    lab.appendChild(cb);
    lab.appendChild(el("span", null, m.label));
    if (!m.supportsStructuredOutput) lab.appendChild(el("span", "so", "prompt-only"));
    picks.appendChild(lab); checks.push(cb);
  }
  body.appendChild(picks);

  const row = el("div", "extract-row");
  const sel = el("select");
  for (const e of S.config.parseEngines) {
    const o = el("option", null, e + (e === "mistral-ocr" ? " (paid)" : e === "pdf-text" ? " (free)" : ""));
    o.value = e;
    if (e === S.config.defaults.parseEngine) o.selected = true;
    sel.appendChild(o);
  }
  row.appendChild(sel);
  const btn = el("button", "btn primary", "Extract");
  row.appendChild(btn);
  body.appendChild(row);

  const warn = el("div", "engine-warn");
  const syncWarn = () => warn.textContent = sel.value === "mistral-ocr"
    ? "mistral-ocr is paid and forwards at most ~8 images per PDF." : "";
  sel.onchange = syncWarn; syncWarn();
  body.appendChild(warn);

  btn.onclick = () => {
    const models = checks.filter(c => c.checked).map(c => c.value);
    if (!models.length) { toast("Pick at least one model"); return; }
    runExtraction(p, models, false, sel.value, btn);
  };
  panel.appendChild(body);
  return panel;
}

async function runExtraction(p, models, force, parseEngine, btn) {
  const engine = parseEngine || S.config.defaults.parseEngine;
  if (btn) { btn.disabled = true; btn.textContent = "Extracting…"; }
  toast(`Extracting ${models.length} model(s)… this can take a minute`);
  try {
    const { runs } = await api.extract(p.pmid, models, S.config.defaults.promptId, engine, !!force);
    const cur = S.runsByPmid[p.pmid] = S.runsByPmid[p.pmid] || {};
    for (const r of runs) cur[r.modelId] = r;
    S.activeModelByPmid[p.pmid] = runs[0] ? runs[0].modelId : S.activeModelByPmid[p.pmid];
    S.cardIndex = 0;
    renderRight(); recomputeMatches(); renderLeft();
    const ok = runs.filter(r => r.status !== "failed").length;
    toast(`Extraction done: ${ok}/${runs.length} ok`);
  } catch (err) {
    toast(`Extraction failed: ${err.message}`);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "Extract"; }
  }
}

boot();
