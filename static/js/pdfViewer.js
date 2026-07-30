// PDF viewer + evidence match/highlight engine.
// Render, text-layer char map, fuzzy matching (with dehyphenation), and highlight
// are lifted largely verbatim from visual_curator.html — do not casually refactor
// the char-map/range math. Adapted only to consume the backend's typed records and
// to report a per-feature evidenceMatch ("matched" | "unmatched" | "none").

/* global pdfjsLib */
pdfjsLib.GlobalWorkerOptions.workerSrc = "/vendor/pdfjs/pdf.worker.min.js";

const pagesEl = () => document.getElementById("pages");

const state = {
  pdf: null,
  pageIndex: [],
  scale: 1.0,
  fitScale: 1.0,
  targets: [],        // {name, evidence, located, rects, pageIdx, partial}
  activeTarget: -1,
  features: [],       // last features passed to computeMatches (for reflow on zoom)
  onActive: null,     // (name) => void
  onNav: null,        // (label) => void
  onMatches: null,    // (results) => void  (fires on initial compute AND zoom reflow)
  onSelection: null,  // ({text, rect} | null) => void  (rect is viewport-relative)
};

export function init({ onActive, onNav, onMatches, onSelection } = {}) {
  state.onActive = onActive;
  state.onNav = onNav;
  state.onMatches = onMatches;
  state.onSelection = onSelection;
  document.getElementById("nextVal").onclick = () => step(1);
  document.getElementById("prevVal").onclick = () => step(-1);
  document.getElementById("zoomIn").onclick = () => rezoom(0.15);
  document.getElementById("zoomOut").onclick = () => rezoom(-0.15);
  document.addEventListener("keydown", e => {
    if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA" || e.target.tagName === "SELECT") return;
    if (e.key === "ArrowRight") { step(1); e.preventDefault(); }
    if (e.key === "ArrowLeft") { step(-1); e.preventDefault(); }
  });
  document.addEventListener("selectionchange", handleSelectionChange);
}

// User can drag-select text in the (invisible, overlaid) text layer — native
// browser selection, styled via ::selection. Reports {text, rect} so the app
// can offer "add to current feature" (card view only; see main.js).
function handleSelectionChange() {
  if (!state.onSelection) return;
  const sel = window.getSelection();
  const text = sel && sel.rangeCount ? sel.toString().trim() : "";
  if (!text) { state.onSelection(null); return; }
  const range = sel.getRangeAt(0);
  const container = pagesEl();
  if (!container.contains(range.commonAncestorContainer)) { state.onSelection(null); return; }
  const rect = range.getBoundingClientRect();
  if (rect.width < 1 && rect.height < 1) { state.onSelection(null); return; }
  state.onSelection({ text, rect });
}

export async function load(url) {
  clear();
  const buf = await (await fetch(url)).arrayBuffer();
  state.pdf = await pdfjsLib.getDocument({ data: buf }).promise;
  await renderAllPages();
}

export function clear() {
  state.pdf = null;
  state.pageIndex = [];
  state.targets = [];
  state.activeTarget = -1;
  state.features = [];
  pagesEl().innerHTML = "";
  updateNavLabel();
}

/* ---------- render ---------- */
async function renderAllPages() {
  const box = pagesEl();
  box.innerHTML = "";
  state.pageIndex = [];

  const first = await state.pdf.getPage(1);
  const natural = first.getViewport({ scale: 1 });
  const avail = box.clientWidth - 44;
  state.fitScale = Math.max(0.6, Math.min(2.2, avail / natural.width));
  if (state.scale === 1.0) state.scale = state.fitScale;
  updateZoomLabel();

  for (let p = 1; p <= state.pdf.numPages; p++) {
    const page = await state.pdf.getPage(p);
    const vp = page.getViewport({ scale: state.scale });
    const wrap = document.createElement("div");
    wrap.className = "page-wrap";
    wrap.style.width = vp.width + "px";
    wrap.style.height = vp.height + "px";
    wrap.dataset.page = p;

    const canvas = document.createElement("canvas");
    const ratio = window.devicePixelRatio || 1;
    canvas.width = vp.width * ratio;
    canvas.height = vp.height * ratio;
    canvas.style.width = vp.width + "px";
    canvas.style.height = vp.height + "px";
    const ctx = canvas.getContext("2d");
    ctx.scale(ratio, ratio);

    const hlLayer = document.createElement("div"); hlLayer.className = "hlLayer";
    const textLayer = document.createElement("div"); textLayer.className = "textLayer";
    textLayer.style.width = vp.width + "px";
    textLayer.style.height = vp.height + "px";

    wrap.appendChild(canvas); wrap.appendChild(hlLayer); wrap.appendChild(textLayer);
    box.appendChild(wrap);

    await page.render({ canvasContext: ctx, viewport: vp }).promise;
    const tc = await page.getTextContent();
    const idx = placeTextLayer(tc, vp, textLayer);
    state.pageIndex.push({ page: p, wrap, hlLayer, textLayer, ...idx });
  }
}

function placeTextLayer(tc, vp, layer) {
  const spans = [];
  const charMap = [];
  let rawText = "";
  tc.items.forEach(item => {
    if (typeof item.str !== "string") return;
    const t = pdfjsLib.Util.transform(vp.transform, item.transform);
    const fontH = Math.hypot(t[2], t[3]);
    const span = document.createElement("span");
    span.textContent = item.str;
    span.style.left = t[4] + "px";
    span.style.top = (t[5] - fontH) + "px";
    span.style.fontSize = fontH + "px";
    span.style.fontFamily = "sans-serif";
    layer.appendChild(span);

    const spanIndex = spans.length;
    const start = rawText.length;
    for (let c = 0; c < item.str.length; c++) charMap.push({ spanIndex, local: c });
    rawText += item.str;
    spans.push({ el: span, start, len: item.str.length });
    rawText += " ";
    charMap.push({ sep: true });
  });
  const { norm, map } = normalizeWithMap(rawText);
  const { norm: normDehyph, map: mapDehyph } = normalizeWithMap(rawText, true);
  return { spans, charMap, rawText, norm, normMap: map, normDehyph, normDehyphMap: mapDehyph };
}

function normalizeWithMap(raw, dehyph = false) {
  let norm = "", map = [], prevSpace = false;
  for (let i = 0; i < raw.length; i++) {
    let ch = raw[i];
    if (ch === "­") continue;
    if (dehyph && ch === "-") {
      let j = i + 1; while (j < raw.length && /\s/.test(raw[j])) j++;
      i = j - 1; continue;
    }
    if (/\s/.test(ch)) {
      if (prevSpace) continue;
      norm += " "; map.push(i); prevSpace = true; continue;
    }
    norm += ch.toLowerCase(); map.push(i); prevSpace = false;
  }
  return { norm, map };
}

/* ---------- matching ---------- */
function normQuery(q, dehyph = false) {
  let out = "", prevSpace = false;
  for (let ch of q) {
    if (ch === "­") continue;
    if (dehyph && ch === "-") continue;
    if (/\s/.test(ch)) { if (!prevSpace) { out += " "; prevSpace = true; } continue; }
    out += ch.toLowerCase(); prevSpace = false;
  }
  return out.trim();
}

function locate(evidence, pageIdx) {
  const attempts = [
    () => find(pageIdx.norm, pageIdx.normMap, normQuery(evidence)),
    () => find(pageIdx.normDehyph, pageIdx.normDehyphMap, normQuery(evidence, true)),
  ];
  for (const a of attempts) { const r = a(); if (r) return r; }
  const words = normQuery(evidence).split(" ").filter(Boolean);
  if (words.length > 6) {
    const short = words.slice(0, 10).join(" ");
    const r = find(pageIdx.norm, pageIdx.normMap, short);
    if (r) return { ...r, partial: true };
  }
  return null;
}

function find(normText, normMap, query) {
  if (!query) return null;
  const at = normText.indexOf(query);
  if (at < 0) return null;
  const rawStart = normMap[at];
  const rawEnd = normMap[Math.min(at + query.length - 1, normMap.length - 1)];
  return { rawStart, rawEnd };
}

function rectsFor(pageIdx, rawStart, rawEnd) {
  const { charMap, spans, wrap } = pageIdx;
  let s = rawStart; while (s <= rawEnd && charMap[s] && charMap[s].sep) s++;
  let e = rawEnd; while (e >= s && charMap[e] && charMap[e].sep) e--;
  if (!charMap[s] || !charMap[e] || charMap[s].sep || charMap[e].sep) return null;
  const startSpan = spans[charMap[s].spanIndex], startOff = charMap[s].local;
  const endSpan = spans[charMap[e].spanIndex], endOff = charMap[e].local + 1;
  if (!startSpan || !endSpan || !startSpan.el.firstChild || !endSpan.el.firstChild) return null;
  const range = document.createRange();
  try {
    range.setStart(startSpan.el.firstChild, Math.min(startOff, startSpan.el.firstChild.length));
    range.setEnd(endSpan.el.firstChild, Math.min(endOff, endSpan.el.firstChild.length));
  } catch (_) { return null; }
  const wrapRect = wrap.getBoundingClientRect();
  const rects = [...range.getClientRects()].map(r => ({
    left: r.left - wrapRect.left, top: r.top - wrapRect.top, width: r.width, height: r.height,
  })).filter(r => r.width > 0.5 && r.height > 0.5);
  return rects.length ? rects : null;
}

/* ---------- targets / evidenceMatch ---------- */
// features: [{name, evidence}]. Returns [{name, evidenceMatch, partial}].
export function computeMatches(features) {
  state.features = features || [];
  state.targets = [];
  state.activeTarget = -1;
  const results = [];
  for (const f of state.features) {
    const t = { name: f.name, evidence: f.evidence || null, located: false, rects: null, pageIdx: null, partial: false };
    if (t.evidence && state.pageIndex.length) {
      for (const pi of state.pageIndex) {
        const hit = locate(t.evidence, pi);
        if (hit) {
          const rects = rectsFor(pi, hit.rawStart, hit.rawEnd);
          if (rects) { t.located = true; t.rects = rects; t.pageIdx = pi; t.partial = !!hit.partial; break; }
        }
      }
      state.targets.push(t);
      results.push({ name: f.name, evidenceMatch: t.located ? "matched" : "unmatched", partial: t.partial });
    } else {
      results.push({ name: f.name, evidenceMatch: "none", partial: false });
    }
  }
  updateNavLabel();
  if (state.onMatches) state.onMatches(results);
  return results;
}

export function activateFeature(name) {
  const idx = state.targets.findIndex(t => t.name === name);
  if (idx >= 0) setActiveTarget(idx);
}

function setActiveTarget(idx) {
  clearHighlights();
  state.activeTarget = idx;
  const t = state.targets[idx];
  if (!t) { updateNavLabel(); return; }
  if (state.onActive) state.onActive(t.name);
  if (t.located && t.rects) {
    for (const r of t.rects) {
      const div = document.createElement("div");
      div.className = "hl pulse";
      div.style.left = r.left + "px"; div.style.top = r.top + "px";
      div.style.width = r.width + "px"; div.style.height = r.height + "px";
      t.pageIdx.hlLayer.appendChild(div);
      setTimeout(() => div.classList.remove("pulse"), 650);
    }
    const first = t.rects[0];
    const box = pagesEl();
    const wrapTop = t.pageIdx.wrap.offsetTop;
    box.scrollTo({ top: wrapTop + first.top - 120, behavior: "smooth" });
  }
  updateNavLabel();
}

function step(dir) {
  if (!state.targets.length) return;
  let i = state.activeTarget;
  i = (i + dir + state.targets.length) % state.targets.length;
  setActiveTarget(i);
}

function clearHighlights() {
  state.pageIndex.forEach(pi => (pi.hlLayer.innerHTML = ""));
}

function updateNavLabel() {
  const elm = document.getElementById("navlabel");
  const n = state.targets.length;
  if (!n) { elm.textContent = "—"; if (state.onNav) state.onNav("—"); return; }
  const i = state.activeTarget;
  if (i < 0) { elm.textContent = `${n} evidence span(s)`; return; }
  const t = state.targets[i];
  const st = t.located ? (t.partial ? "partial" : "found") : "not found in PDF";
  const label = `${i + 1}/${n} · ${t.name} · ${st}`;
  elm.textContent = label;
  if (state.onNav) state.onNav(label);
}

async function rezoom(delta) {
  if (!state.pdf) return;
  state.scale = Math.max(0.5, Math.min(3, state.scale + delta));
  updateZoomLabel();
  await renderAllPages();
  // recompute matches against the freshly rendered layer (fires onMatches)
  computeMatches(state.features);
}

function updateZoomLabel() {
  document.getElementById("zoomlabel").textContent = Math.round(state.scale * 100) + "%";
}
