// Left pane: papers with status dots (extraction + confirmation roll-up).
//
// A paper is identified by PMID if it has one, else by DOI, else by its filename.
// Only pmidStatus === "pending" is a problem to flag — a paper deliberately
// marked as having no PubMed ID is a normal, curatable paper.
import { el } from "./dom.js";

export function paperLabel(p) {
  return p.pmid || p.doi || p.filename || p.uid;
}

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

export function renderPaperList(container, papers, { activeUid, runsByUid, onSelect }) {
  container.innerHTML = "";
  for (const p of papers) {
    const pending = p.pmidStatus === "pending";
    const runs = pending ? {} : (runsByUid[p.uid] || {});
    const { confirmed, total, anyFailed } = confirmStats(runs);

    let dot = "none";
    if (pending) dot = "bad";
    else if (total > 0 && confirmed >= total) dot = "ok";
    else if (confirmed > 0 || total > 0) dot = "part";
    if (anyFailed && dot !== "ok") dot = "bad";

    const row = el("div", "doc" + (p.uid === activeUid ? " active" : "") + (pending ? " needspmid" : ""));
    const name = el("div", "name", pending ? "needs identity" : paperLabel(p));
    row.appendChild(name);
    // The filename is the label itself for a paper with neither PMID nor DOI —
    // don't print it twice.
    if (paperLabel(p) !== p.filename || pending) {
      row.appendChild(el("div", "fn", p.filename));
    }
    const meta = el("div", "meta");
    meta.appendChild(el("span", "dot " + dot));
    meta.appendChild(el("span", null,
      pending ? "unresolved"
              : (total ? `${Object.keys(runs).length} model(s)` : "no runs")));
    if (!pending && total) meta.appendChild(el("span", "prog", `${confirmed}/${total}✓`));
    row.appendChild(meta);
    row.onclick = () => onSelect(p.uid);
    container.appendChild(row);
  }
}
