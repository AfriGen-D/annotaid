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
};

/* ---------------- boot ---------------- */
async function boot() {
  viewer.init({
    onActive: name => highlightActiveCard(name),
    onMatches: results => applyMatches(results),
  });
  wireHeader();

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
  if (!p) { pane.innerHTML = `<div class="empty-feat">Select a paper.</div>`; return; }

  if (!p.pmid) {
    pane.appendChild(renderPmidBox(p, { onConfirm: (pmid, source) => confirmPmid(p.uid, pmid, source) }));
    pane.appendChild(el("div", "no-runs", "Confirm the PubMed ID to enable AI extraction."));
    $("#featstat").textContent = "";
    return;
  }

  pane.appendChild(extractPanel(p));

  const runs = S.runsByPmid[p.pmid] || {};
  if (!Object.keys(runs).length) {
    pane.appendChild(el("div", "no-runs", "No AI runs yet. Pick model(s) above and click Extract, or Import existing outputs."));
    $("#featstat").textContent = "";
    return;
  }

  const modelId = activeModelId(p);
  const tabs = renderTabs(runs, modelId, S.config.features.length, id => {
    S.activeModelByPmid[p.pmid] = id; renderRight(); recomputeMatches();
  });
  if (tabs) pane.appendChild(tabs);

  const run = runs[modelId];
  pane.appendChild(runMeta(run, p));

  const cards = el("div"); cards.id = "featurecards";
  pane.appendChild(cards);
  renderFeatureCards(run);
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
  let confirmed = 0;
  for (const fdef of S.config.features) {
    const feat = run.features[fdef.name];
    if (!feat) continue;
    if (feat.confirmed) confirmed++;
    const ctx = {
      partial: !!S.partialByFeature[fdef.name],
      onEdit: () => onFeatureEdit(run),
      onActivateEvidence: name => viewer.activateFeature(name),
    };
    cards.appendChild(renderFeature(fdef, feat, ctx));
  }
  $("#featstat").textContent = `${confirmed}/${S.config.features.length}✓`;
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
    S.activeModelByPmid[p.pmid] = id; renderRight(); recomputeMatches();
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
  const panel = el("div", "extract-panel");
  panel.appendChild(el("div", "lab", "Run AI extraction"));
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
  panel.appendChild(picks);

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
  panel.appendChild(row);

  const warn = el("div", "engine-warn");
  const syncWarn = () => warn.textContent = sel.value === "mistral-ocr"
    ? "mistral-ocr is paid and forwards at most ~8 images per PDF." : "";
  sel.onchange = syncWarn; syncWarn();
  panel.appendChild(warn);

  btn.onclick = () => {
    const models = checks.filter(c => c.checked).map(c => c.value);
    if (!models.length) { toast("Pick at least one model"); return; }
    runExtraction(p, models, false, sel.value, btn);
  };
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
