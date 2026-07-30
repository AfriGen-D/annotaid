// Right pane per-model tab bar (hidden when a single model). Adapted from
// visual_evaluator.html tabBar. Dot = per-model confirmation/status roll-up.
import { el } from "./dom.js";

function tabStat(run, featureCount) {
  if (!run) return { cls: "none", frac: "" };
  if (run.status === "failed") return { cls: "failed", frac: "failed" };
  const feats = Object.values(run.features || {});
  const confirmed = feats.filter(f => f.confirmed).length;
  const total = feats.length || featureCount;
  const cls = confirmed === 0 ? "none" : (confirmed >= total ? "done" : "partial");
  return { cls, frac: `${confirmed}/${total}` };
}

// runs: {modelId: run}. Returns null if only one model (caller hides the bar).
export function renderTabs(runs, activeModelId, featureCount, onSelect) {
  const ids = Object.keys(runs);
  if (ids.length <= 1) return null;
  const bar = el("div", "tabbar");
  ids.forEach(id => {
    const run = runs[id];
    const t = el("button", "tab" + (id === activeModelId ? " active" : ""));
    const st = tabStat(run, featureCount);
    t.appendChild(el("span", "tab-dot " + st.cls));
    t.appendChild(el("span", "tab-name", run.modelLabel || id));
    if (st.frac) t.appendChild(el("span", "tab-frac", st.frac));
    t.onclick = () => onSelect(id);
    bar.appendChild(t);
  });
  return bar;
}
