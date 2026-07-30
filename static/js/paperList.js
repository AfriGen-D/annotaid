// Left pane: papers by PMID with status dots (extraction + confirmation roll-up).
import { el, escapeHtml } from "./dom.js";

function confirmStats(runs) {
  let confirmed = 0, total = 0, anyFailed = false;
  for (const run of Object.values(runs || {})) {
    if (run.status === "failed") anyFailed = true;
    for (const f of Object.values(run.features || {})) {
      total++; if (f.confirmed) confirmed++;
    }
  }
  return { confirmed, total, anyFailed };
}

export function renderPaperList(container, papers, { activeUid, runsByPmid, onSelect }) {
  container.innerHTML = "";
  for (const p of papers) {
    const runs = p.pmid ? (runsByPmid[p.pmid] || {}) : {};
    const { confirmed, total, anyFailed } = confirmStats(runs);

    let dot = "none";
    if (!p.pmid) dot = "bad";
    else if (total > 0 && confirmed >= total) dot = "ok";
    else if (confirmed > 0 || total > 0) dot = "part";
    if (anyFailed && dot !== "ok") dot = "bad";

    const row = el("div", "doc" + (p.uid === activeUid ? " active" : "") + (p.pmid ? "" : " needspmid"));
    const name = el("div", "name", p.pmid || "needs PMID");
    row.appendChild(name);
    row.appendChild(el("div", "fn", p.filename));
    const meta = el("div", "meta");
    meta.appendChild(el("span", "dot " + dot));
    meta.appendChild(el("span", null, p.pmid ? (total ? `${Object.keys(runs).length} model(s)` : "no runs") : "unconfirmed"));
    if (p.pmid && total) meta.appendChild(el("span", "prog", `${confirmed}/${total}✓`));
    row.appendChild(meta);
    row.onclick = () => onSelect(p.uid);
    container.appendChild(row);
  }
}
