// Left pane: the work queue. Papers with status dots (extraction + confirmation
// roll-up), grouped by who holds them:
//
//   My papers — assigned to me (what I should be working on)
//   Pool      — unassigned, anyone on the project can claim one
//   Others    — held by someone else; collapsed by default. A manager opens
//               one of these to reassign it.
//
// A paper is identified by PMID if it has one, else by DOI, else by its filename.
// Only pmidStatus === "pending" is a problem to flag — a paper deliberately
// marked as having no PubMed ID is a normal, curatable paper.
import { el } from "./dom.js";
import { statusOf, statusBadge } from "./paperActions.js";

export function paperLabel(p) {
  return p.pmid || p.doi || p.filename || p.uid;
}

// Status filter values -> label. "" shows everything.
export const STATUS_FILTERS = [
  ["", "All"],
  ["in_progress", "In progress"],
  ["submitted", "Submitted"],
  ["excluded", "Excluded"],
  ["unextractable", "Unextractable"],
  ["unassigned", "Unassigned"],
];

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

function paperRow(p, ctx) {
  const { activeUid, runsByUid, onSelect, me, users } = ctx;
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

  const wf = el("div", "meta wf");
  wf.appendChild(statusBadge(p));
  if (p.assigneeId && (!me || p.assigneeId !== me.id)) {
    wf.appendChild(el("span", "doc-who", (users && users[p.assigneeId]) || "someone"));
  }
  row.appendChild(wf);

  row.onclick = () => onSelect(p.uid);
  return row;
}

/**
 * ctx: {activeUid, runsByUid, onSelect, me, users,
 *       filter         — a STATUS_FILTERS value ("" = all)
 *       othersOpen     — whether the Others section starts expanded
 *       onToggleOthers(open)}
 */
export function renderPaperList(container, papers, ctx) {
  container.innerHTML = "";
  const meId = ctx.me && ctx.me.id;
  const shown = ctx.filter ? papers.filter(p => statusOf(p) === ctx.filter) : papers;

  const mine = [], pool = [], others = [];
  for (const p of shown) {
    if (meId && p.assigneeId === meId) mine.push(p);
    else if (!p.assigneeId) pool.push(p);
    else others.push(p);
  }

  if (!papers.length) {
    container.appendChild(el("div", "doc-empty", "No papers in this project yet."));
    return;
  }
  if (!shown.length) {
    container.appendChild(el("div", "doc-empty", "No papers match this filter."));
    return;
  }

  const section = (title, list, emptyText) => {
    const sec = el("div", "doc-sec");
    const h = el("div", "doc-sec-head");
    h.appendChild(el("span", null, title));
    h.appendChild(el("span", "doc-sec-n", String(list.length)));
    sec.appendChild(h);
    if (!list.length && emptyText) sec.appendChild(el("div", "doc-sec-empty", emptyText));
    for (const p of list) sec.appendChild(paperRow(p, ctx));
    container.appendChild(sec);
  };
  section("My papers", mine, ctx.filter ? "" : "Nothing assigned to you — claim one from the pool.");
  section("Pool", pool, ctx.filter ? "" : "The pool is empty.");

  if (others.length) {
    const det = el("details", "doc-sec doc-others");
    // main.js opens it when the paper being selected lives here.
    det.open = !!ctx.othersOpen;
    const sum = el("summary", "doc-sec-head");
    sum.appendChild(el("span", null, "Others"));
    sum.appendChild(el("span", "doc-sec-n", String(others.length)));
    det.appendChild(sum);
    for (const p of others) det.appendChild(paperRow(p, ctx));
    // Setting .open above fires a (late) toggle too; only report real clicks.
    let last = det.open;
    det.addEventListener("toggle", () => {
      if (det.open === last) return;
      last = det.open;
      if (ctx.onToggleOthers) ctx.onToggleOthers(det.open);
    });
    container.appendChild(det);
  }
}
