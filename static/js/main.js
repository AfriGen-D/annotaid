// annotaid app orchestrator.
//
// Everyone is signed in, and what they may do in this project comes from the
// server (`state.me`, `state.permissions`). Two rules shape the whole screen:
//   - managers add papers, run extraction, export, assign and staff the team;
//   - only a paper's assignee, while it is in progress, edits its curated
//     values. Every other paper renders read only (paperActions.canEdit).
// The server enforces both; the UI just doesn't offer what would only error.
import { api, setProject } from "./api.js";
import { $, el, toast, downloadFile } from "./dom.js";
import { renderUserMenu } from "./session.js";
import * as viewer from "./pdfViewer.js";
import * as store from "./store.js";
import { renderPaperList, paperLabel, STATUS_FILTERS } from "./paperList.js";
import { renderIdentityBox } from "./identityBox.js";
import { renderFeature, lockControls } from "./featureEditor.js";
import { renderGroup } from "./groupEditor.js";
import { renderGroupItemsList } from "./groupItemsList.js";
import { openAddPapersModal } from "./addPapersModal.js";
import { renderPaperBar, canEdit } from "./paperActions.js";
import { followJob, isActive } from "./jobs.js";
import { openJobsPanel } from "./jobsPanel.js";
import { openTeamPanel } from "./teamPanel.js";

const S = {
  config: null,
  project: null,                  // {id, name, description}
  account: null,                  // getMe(): the signed-in account (role: superadmin | manager | user)
  me: null,                       // state.me: {id, name, role} — role in THIS project
  permissions: {},                // {manage, addPapers, extract, export, assign}
  users: {},                      // user id -> name, for assignees and "confirmed by"
  members: [],                    // this project's members (assign menu, team)
  statusFilter: "",               // paper list filter (paperList.STATUS_FILTERS)
  othersOpen: false,              // the paper list's "Others" section
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

  // The user menu doubles as the sign-in gate: no session -> /login, a forced
  // password change -> /account, and either way this never resolves.
  S.account = await renderUserMenu($("#userMenu"));

  // Autosave never fires for a paper this user can't edit, and a 403 that
  // slips through anyway (reassigned under us) resyncs instead of retrying.
  store.setEditGuard(uid => canEdit(paper(uid), S.me));
  store.setForbiddenHandler(() => reloadState());

  // Flushes buffered edits for EVERY project, not just this one.
  try { await store.flushPending(); } catch (_) {}

  try {
    S.config = await api.config();
  } catch (err) {
    // A stale bookmark, a project that has been archived, or one this account
    // isn't a member of (the server answers 404 for all three): go home with
    // a reason rather than sitting on a half-rendered app.
    if (err.status === 404) {
      location.replace("/?missing=" + encodeURIComponent(pid));
      return;
    }
    throw err;
  }
  S.project = S.config.project;
  S.permissions = S.config.permissions || {};
  applyProjectChrome();

  applyState(await api.state());
  renderLeft();
  // Land on my own work first, then anything identified.
  const first = S.papers.find(p => canEdit(p, S.me))
    || S.papers.find(p => p.pmidStatus !== "pending") || S.papers[0];
  if (first) selectPaper(first.uid);
  updateGlobalStat();
  resumeJobs();
}

// Everything /state carries that isn't per-paper UI.
function applyState(state) {
  S.papers = state.papers || [];
  S.runsByUid = indexRuns(state.runs || []);
  S.me = state.me || S.me;
  S.permissions = state.permissions || S.permissions;
  S.users = state.users || {};
  S.members = state.members || [];
  applyPermissions();
}

// Hide what this role can't do. `hidden` (not disabled) for the header: a
// curator has no use for a greyed-out "Add Paper(s)".
function applyPermissions() {
  const P = S.permissions || {};
  $("#addPdfBtn").hidden = !P.addPapers;
  $("#importbtn").hidden = !P.manage;          // model-output import is manager-only too
  $("#extractBtn").hidden = !P.extract;
  $("#downloadwrap").hidden = !P.export;
  $("#jobsBtn").hidden = !P.manage;
  $("#teamBtn").hidden = !P.manage;
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
// What the "Add Paper(s)" stepper and its extraction-only form share.
function modalCallbacks() {
  return {
    onStartImport: startImportJob,
    onRetryJob: async jid => trackJob(await api.retryJob(jid)),
    onCancelJob: jid => api.cancelJob(jid),
    resolvePaper,
    onUpload: uploadPdfs,
    onConfirmPmid: confirmPaperPmid,
    onNoPmid: markPaperNoPmid,
    onStartExtract: startExtractJob,
    onFinish: finishAddPapers,
    onSaveGroupItems: async (uid, items) => {
      const result = await api.saveGroupItems(uid, items);
      const p = paper(uid);
      if (p) p.groupItems = result.items;
      return result.items;
    },
    findByPmid: pmid => S.papers.find(p => p.pmid === pmid) || null,
    config: S.config,
    canExtract: !!S.permissions.extract,
  };
}

// The extraction-only form: the open paper, or every paper lacking a run.
function openExtractModal() {
  openAddPapersModal({
    ...modalCallbacks(),
    bulk: {
      papers: () => S.papers,
      hasRun: (uid, model) => !!(S.runsByUid[uid] || {})[model],
      currentUid: activePaper() && activePaper().pmidStatus !== "pending" ? S.activeUid : null,
    },
  });
}

function wireHeader() {
  $("#addPdfBtn").addEventListener("click", () => openAddPapersModal(modalCallbacks()));
  $("#extractBtn").addEventListener("click", openExtractModal);
  $("#jobsBtn").addEventListener("click", () => openJobsPanel({
    users: S.users,
    labelFor: uid => { const p = paper(uid); return p ? paperLabel(p) : uid; },
    onJobStarted: trackJob,
  }));
  $("#teamBtn").addEventListener("click", () => openTeamPanel({
    account: S.account,
    me: S.me,
    onMembers: members => { S.members = members; renderBar(); },
    onReleased: n => { toast(`${n} paper(s) went back to the pool`); reloadState(); },
  }));

  // Status filter for the paper list; remembered per browser.
  const filter = $("#statusFilter");
  for (const [value, label] of STATUS_FILTERS) {
    const o = el("option", null, label);
    o.value = value;
    filter.appendChild(o);
  }
  try { S.statusFilter = localStorage.getItem("annotaid:statusFilter") || ""; } catch (_) {}
  if (!STATUS_FILTERS.some(([v]) => v === S.statusFilter)) S.statusFilter = "";
  filter.value = S.statusFilter;
  filter.onchange = () => {
    S.statusFilter = filter.value;
    try { localStorage.setItem("annotaid:statusFilter", S.statusFilter); } catch (_) {}
    renderLeft();
  };

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

// opts.pmid: set when the curator came from an import row with no open-access
// PDF — it prefills that paper's confirm box (it is still never auto-confirmed).
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

/* ---------------- background jobs ---------------- */
// Every job this tab starts (or finds still running on load) is followed here,
// independently of whichever modal or panel started it: that is what puts new
// papers in the list and new runs on screen after the modal has been closed.
const tracked = new Set();   // job ids already followed by main.js

function trackJob(job, { quietFirst = false } = {}) {
  if (!job || tracked.has(job.id)) return job;
  tracked.add(job.id);
  const handled = new Set();   // item seqs already folded in
  let first = quietFirst;
  const unfollow = followJob(job.id, j => {
    const items = j.items;
    if (!items) return;          // a seed from the job list: wait for the first poll
    const fresh = items.filter(it =>
      it.state !== "waiting" && it.state !== "running" && !handled.has(it.seq));
    fresh.forEach(it => handled.add(it.seq));
    // Resumed on page load: what finished before this tab was open is already
    // in the state we just loaded.
    if (first) { first = false; }
    else if (j.kind === "import") {
      if (fresh.some(it => it.outcome === "added" || it.outcome === "added_abstract")) refreshPapers();
    } else if (j.kind === "extract") {
      const uids = new Set(fresh.map(it => (it.result && it.result.uid) || it.ref));
      uids.forEach(uid => reloadRuns(uid));
    }
    if (!isActive(j)) {
      unfollow();
      tracked.delete(j.id);
      toast(jobSummary(j));
    }
  }, job);
  return job;
}

function jobSummary(j) {
  const c = j.counts || {};
  const what = j.kind === "import" ? "Import" : "Extraction";
  if (j.state === "cancelled") return `${what} cancelled`;
  if (j.state === "failed") return `${what} stopped${j.error ? `: ${j.error}` : ""}`;
  const bits = [];
  if (c.done) bits.push(`${c.done} done`);
  if (c.skipped) bits.push(`${c.skipped} skipped`);
  if (c.failed) bits.push(`${c.failed} failed`);
  return `${what} finished` + (bits.length ? ` — ${bits.join(" · ")}` : "");
}

// A manager reloading mid-import should still see the papers land.
async function resumeJobs() {
  if (!S.permissions.manage) return;
  try {
    const { jobs } = await api.listJobs();
    (jobs || []).filter(isActive).forEach(j => trackJob(j, { quietFirst: true }));
  } catch (_) { /* the Jobs panel will show it */ }
}

async function startImportJob(pmids) {
  return trackJob(await api.createJob({ kind: "import", refs: pmids }));
}

async function startExtractJob({ uids, models, parseEngine }) {
  return trackJob(await api.createJob({
    kind: "extract", refs: uids, models,
    promptId: S.config.defaults.promptId,
    parseEngine: parseEngine || S.config.defaults.parseEngine,
    force: false,
  }));
}

// Re-reads the paper list. Calls made while one is in flight coalesce into ONE
// follow-up read, so a burst of "added" items costs two requests, not twenty —
// and nobody awaiting it sees a list from before their paper landed.
let papersReq = null, papersAgain = false;
function refreshPapers() {
  if (papersReq) { papersAgain = true; return papersReq; }
  papersReq = (async () => {
    do {
      papersAgain = false;
      const { papers } = await api.papers();
      mergePapers(papers || []);
    } while (papersAgain);
  })().catch(() => {}).finally(() => { papersReq = null; });
  return papersReq;
}

function mergePapers(papers) {
  const before = activePaper();
  S.papers = papers;
  renderLeft(); updateGlobalStat();
  const after = activePaper();
  // The open paper changed hands or state (someone else's action): redraw it
  // so its read-only state is right. Otherwise leave the editor alone.
  if (before && after && (before.assigneeId !== after.assigneeId
      || before.curationStatus !== after.curationStatus || before.pmidStatus !== after.pmidStatus)) {
    renderBar(); renderRight(); recomputeMatches();
  }
}

// A paper a job just added, once the list has it.
async function resolvePaper(uid) {
  if (!paper(uid)) await refreshPapers();
  return paper(uid) || null;
}

async function reloadRuns(uid) {
  let runs;
  try { ({ runs } = await api.runs(uid)); } catch (_) { return; }
  const by = {};
  for (const r of runs || []) by[r.modelId] = r;
  S.runsByUid[uid] = by;
  renderLeft();
  if (uid === S.activeUid) {
    if (!S.activeModelByUid[uid] && runs && runs[0]) S.activeModelByUid[uid] = runs[0].modelId;
    renderRight(); recomputeMatches();
  }
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
  let state;
  try { state = await api.state(); } catch (err) { toast(`Could not reload: ${err.message}`); return; }
  applyState(state);
  renderLeft(); updateGlobalStat();
  if (S.activeUid) selectPaper(S.activeUid);
}

/* ---------------- left pane ---------------- */
function renderLeft() {
  renderPaperList($("#doclist"), S.papers, {
    activeUid: S.activeUid, runsByUid: S.runsByUid, onSelect: selectPaper,
    me: S.me, users: S.users, filter: S.statusFilter,
    othersOpen: S.othersOpen, onToggleOthers: open => { S.othersOpen = open; },
  });
  $("#doccount").textContent = S.papers.length;
}

/* ---------------- workflow bar (above the PDF) ---------------- */
function renderBar() {
  renderPaperBar($("#paperbar"), activePaper(), {
    me: S.me,
    isManager: !!S.permissions.assign,
    members: S.members,
    users: S.users,
    onPaper: updatePaper,
    onError: err => {
      toast(err.message || "That didn't work");
      // Someone else got there first (409) or it's no longer ours (403):
      // what is on screen is stale.
      if (err.status === 409 || err.status === 403) reloadState();
    },
  });
}

// A workflow action came back with the updated paper.
function updatePaper(p) {
  const i = S.papers.findIndex(x => x.uid === p.uid);
  if (i >= 0) S.papers[i] = p; else S.papers.push(p);
  renderLeft();
  if (p.uid === S.activeUid) { renderBar(); renderRight(); recomputeMatches(); }
  // In-progress counts on the team list moved; cheap to refresh.
  api.members().then(r => { S.members = r.members || S.members; }).catch(() => {});
}

// May this user change the paper's SET-UP — its identity and declared group
// rows? Managers always (extraction needs both), otherwise only the assignee
// while it's in progress (server/workflow.setup_block_reason).
function canSetup(p) {
  return !!S.permissions.manage || canEdit(p, S.me);
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
  const p = paper(uid);
  // Held by someone else: make sure the (collapsible) Others section is open,
  // so the paper on screen is visible in the list. The curator can collapse it.
  if (p && p.assigneeId && S.me && p.assigneeId !== S.me.id) S.othersOpen = true;
  renderLeft();
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
  renderBar();
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

  const setup = canSetup(p);

  // Only 'pending' blocks curation. A paper with no PubMed ID has still been
  // identified, so it falls through to the normal extraction view.
  if (p.pmidStatus === "pending") {
    const box = renderIdentityBox(p, {
      onConfirm: (pmid, source) => confirmPmid(p.uid, pmid, source),
      onNoPmid: doi => markNoPmid(p.uid, doi),
    });
    if (!setup) lockControls(box);
    pane.appendChild(box);
    pane.appendChild(el("div", "no-runs", setup
      ? "Confirm a PubMed ID — or record that this paper has none — to enable AI extraction."
      : "This paper's identity hasn't been confirmed yet. Its assignee or a project manager can confirm it."));
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
  if (groupDef) {
    const gi = renderGroupItemsPanel(p, groupDef);
    if (!setup) lockControls(gi);
    pane.appendChild(gi);
  }

  const runs = S.runsByUid[p.uid] || {};
  if (!Object.keys(runs).length) {
    const box = el("div", "no-runs");
    if (S.permissions.extract) {
      box.appendChild(el("div", null, "No AI runs yet."));
      const b = el("button", "btn primary no-runs-btn", "Run extraction…");
      b.onclick = openExtractModal;
      box.appendChild(b);
    } else {
      box.textContent = "No AI runs yet. A project manager runs AI extraction on papers.";
    }
    pane.appendChild(box);
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
  const readOnly = !canEdit(paper(run.uid), S.me);
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
      readOnly,
      users: S.users,
    });
  } else {
    card = renderFeature(fdef, run.features[fdef.name], {
      readOnly,
      users: S.users,
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
  if (!canEdit(paper(run.uid), S.me)) return;   // controls are disabled; belt and braces
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
  // Re-matching evidence happens on every load. It is only worth saving — and
  // only allowed — on a paper this user is curating.
  if (changed && canEdit(p, S.me)) store.scheduleSave(run.uid, run.modelId, run);
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
  // Selecting text to copy it is fine on any paper; turning it into a value
  // is curation, so not on a read-only one.
  if (!canEdit(activePaper(), S.me)) { hideSelectionBtn(); return; }
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
  if (!canEdit(paper(run.uid), S.me)) return;

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
  // The whole run, like every other save: editableRun() reads run.features.
  store.scheduleSave(run.uid, run.modelId, run);
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

// Extraction is an extract job started from the "Add Paper(s)" stepper or the
// header's "Run extraction…" (managers), so this pane keeps only the per-model
// result switcher — which model's output you are reading. A finished job's
// runs arrive through trackJob -> reloadRuns.
function modelBar(p) {
  const runs = S.runsByUid[p.uid] || {};
  if (!Object.keys(runs).length) return null;
  const bar = el("div", "model-bar");
  bar.appendChild(el("span", "lab", "Model"));
  bar.appendChild(modelSelect(p, runs));
  return bar;
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
