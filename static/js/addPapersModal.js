// "Add Paper(s)" — a horizontal stepper that carries a paper from intake all the
// way to AI extraction.
//
// The chain branches on the first step, so the steps are deliberately NOT
// numbered: "I have the PDFs" is three steps, "I have PubMed IDs" is four, and
// a number would change under the curator mid-flow.
//
//   Source ──┬── Upload PDFs ─────────────────┬── AI extraction
//            └── PubMed IDs ── Fetching ──────┘
//
// The PMID path never fails silently: every id gets its own row with its own
// verdict, because most subscription-only papers aren't retrievable and the
// curator needs to know *which* ones to go and fetch by hand.
//
// Fetching and extraction are server-side background jobs (jobs.js): the
// modal starts ONE job for the whole list and renders its items as it polls.
// Closing the modal stops nothing — main.js follows every job it starts and
// folds the results in, and the Jobs panel shows where it got to.
//
// The same modal, opened with `bulk`, is just the extraction step: run the
// chosen models over the open paper or every paper still lacking a run.
import { el } from "./dom.js";
import { parsePmidText, describeParse } from "./pmidText.js";
import { parsePmidVariantText, describeVariantParse } from "./variantPmidText.js";
import { renderGroupItemsList } from "./groupItemsList.js";
import { followJob, isActive, importItemView, extractItemView, jobProgress } from "./jobs.js";

const ROOT_ID = "addPdfModal";
const SOFT_CAP = 100;      // warn above this, never block
const MAX_JOB = 500;       // the server's per-job item limit (server/jobs.py MAX_ITEMS)
const MAX_ROWS = 500;      // rows rendered up front; the rest appear if they need attention

// .txt/.csv/.tsv, plus extensionless files — plain ID lists are routinely saved
// with no extension at all (the repo's own `10ids` / `ids` corpora are), and
// rejecting those would turn away exactly the files this feature is for.
function isIdFile(name) {
  return /\.(txt|csv|tsv)$/i.test(name) || !/\./.test(name);
}

const STEP_LABEL = {
  source: "Source",
  upload: "Upload PDFs",
  ids: "PubMed IDs",
  fetching: "Fetching",
  variants: "Row IDs",
  extract: "AI extraction",
};

function plural(n, word) { return `${n} ${word}${n === 1 ? "" : "s"}`; }

/**
 * onStartImport(pmids) -> Promise<job>     creates ONE import job (main.js also follows it)
 * onRetryJob(jid) -> Promise<job>          a new job of only the failed items
 * onCancelJob(jid) -> Promise<job>
 * resolvePaper(uid) -> Promise<paper|null> a paper a job added, once main.js has it
 * onUpload(files, {pmid}) -> Promise<paper[]>
 * onConfirmPmid(uid, pmid) -> Promise<paper>
 * onNoPmid(uid, doi) -> Promise<paper>            doi may be ""
 * onStartExtract({uids, models, parseEngine}) -> Promise<job>   one extract job
 * onSaveGroupItems(uid, items) -> Promise<items>  the curator's declared row
 *   identity for the project's one repeating group — see server/extraction.py.
 *   Only called if the project's config actually has a group.
 * onFinish(papers) -> void      called once when the stepper closes
 * findByPmid(pmid) -> paper|null
 * config
 * canExtract                    false: the extraction step explains why instead
 * bulk  (optional) {papers() -> paper[], hasRun(uid, model) -> bool, currentUid}
 *   opens straight on the extraction step for papers already in the project.
 */
export function openAddPapersModal(opts) {
  const {
    onStartImport, onRetryJob, onCancelJob, resolvePaper, onUpload, onConfirmPmid,
    onNoPmid, onStartExtract, onSaveGroupItems, onFinish, findByPmid, config,
  } = opts;
  const bulk = opts.bulk || null;
  const canExtract = opts.canExtract !== false;
  const groupDef = (config.features || []).find(f => f.type === "group") || null;

  const st = {
    path: bulk ? "extract" : null,   // null | "pdf" | "pmid" | "extract" (bulk)
    step: "source",
    rows: new Map(),   // pmid -> row record (fetch step)
    order: [],
    job: null,         // the import job the fetching step is showing
    unfollow: null,    // stops THIS modal listening; never stops the job
    localDone: 0,      // ids settled without the server (already in the project)
    settledBefore: 0,  // ids an earlier job settled for good, before a retry
    resolving: [],     // promises: papers the job added, being looked up
    advanced: false,   // auto-moved on after a clean finish (only once per job)
    running: false,
    closed: false,
    papers: new Map(), // uid -> paper, everything this run produced
    uploadHint: null,  // pmid the manual-upload detour is for
    idsMode: "plain",  // "plain" | "variant" — which paste format the ids step is reading
    groupItemsByUid: new Map(),   // uid -> [{rowId, label}], drafted here, saved in "variants"
  };

  // The step after intake (upload / fetching) is "variants" only when there is
  // a group to declare rows for at all — otherwise it would be an empty step
  // nobody can do anything with.
  function gotoAfterIntake() { goto(groupDef ? "variants" : "extract"); }

  const root = document.getElementById(ROOT_ID);
  root.innerHTML = "";
  root.hidden = false;

  const modal = el("div", "modal stepper-modal");
  root.appendChild(modal);

  const head = el("div", "modal-head");
  head.appendChild(el("span", null, bulk ? "Run AI extraction" : "Add paper(s)"));
  const closeBtn = el("button", "modal-close", "×");
  closeBtn.setAttribute("aria-label", "Close");
  head.appendChild(closeBtn);
  modal.appendChild(head);

  /* ---------------- stepper chrome ---------------- */
  const stepper = el("div", "stepper");
  const stepRow = el("div", "stepper-steps");
  stepper.appendChild(stepRow);
  modal.appendChild(stepper);
  stepper.hidden = !!bulk;          // a one-step chain is not a stepper

  function chain() {
    const variants = groupDef ? ["variants"] : [];
    if (st.path === "extract") return ["extract"];
    if (st.path === "pdf") return ["source", "upload", ...variants, "extract"];
    if (st.path === "pmid") return ["source", "ids", "fetching", ...variants, "extract"];
    return ["source", "extract"];
  }

  // Progress is carried by the connector lines going green behind you, so there
  // is no separate progress bar to keep in sync.
  function renderStepper() {
    const steps = chain();
    const at = Math.max(0, steps.indexOf(st.step));
    stepRow.innerHTML = "";
    steps.forEach((key, i) => {
      const s = el("div", "st-step" + (i < at ? " done" : i === at ? " on" : ""));
      const node = el("div", "st-node");
      if (i < at) node.appendChild(el("span", "st-tick", "✓"));
      s.appendChild(node);
      s.appendChild(el("div", "st-label", STEP_LABEL[key]));
      stepRow.appendChild(s);
      // Until a path is chosen the middle steps are unknown — hold their place
      // with a ghost so the row doesn't jump when they appear.
      if (i === 0 && !st.path) {
        const ghost = el("div", "st-step ghost");
        ghost.appendChild(el("div", "st-node"));
        ghost.appendChild(el("div", "st-label", "…"));
        stepRow.appendChild(ghost);
      }
    });
  }

  const body = el("div", "modal-body");
  modal.appendChild(body);

  let reported = false;
  function close() {
    if (st.closed) return;
    st.closed = true;
    // Only this modal stops listening. The jobs it started keep running on the
    // server, and main.js — which follows them too — still folds in the papers
    // and runs they produce.
    if (st.unfollow) st.unfollow();
    if (exState.unfollow) exState.unfollow();
    root.hidden = true;
    root.innerHTML = "";
    document.removeEventListener("keydown", onKeydown);
    if (!reported && onFinish) { reported = true; onFinish([...st.papers.values()]); }
  }
  function onKeydown(e) { if (e.key === "Escape") close(); }
  document.addEventListener("keydown", onKeydown);
  closeBtn.onclick = close;
  root.onclick = e => { if (e.target === root) close(); };

  /* ================= STEP: source ================= */
  const sourcePane = el("div", "modal-pane");
  sourcePane.appendChild(el("div", "modal-lab", "What are you starting from?"));
  const choices = el("div", "source-choices");

  function choiceCard(title, sub, path) {
    const c = el("button", "source-card");
    c.appendChild(el("span", "sc-title", title));
    c.appendChild(el("span", "sc-sub", sub));
    c.onclick = () => { st.path = path; goto(path === "pdf" ? "upload" : "ids"); };
    return c;
  }
  choices.appendChild(choiceCard(
    "I have PubMed ID(s)",
    "Paste or load a list of PMIDs. Each is looked up on PubMed and its open-access full text fetched for you.",
    "pmid"));
  choices.appendChild(choiceCard(
    "I have the PDF(s)",
    "Upload PDFs you already have. You confirm each paper's PubMed ID before extraction.",
    "pdf"));
  sourcePane.appendChild(choices);
  body.appendChild(sourcePane);

  /* ================= STEP: ids ================= */
  const idsPane = el("div", "modal-pane");
  idsPane.hidden = true;

  const PLAIN_PLACEHOLDER =
    "26751406, 31452104\n29875302 12491487\n\nSeparate with spaces, tabs, commas, semicolons or new lines.";
  const VARIANT_PLACEHOLDER =
    "33915198 - rs1800544, rs553668\n33622083 rs35599367\n34175889 - HLA-B*15:02, HLA-B*56:02"
    + "\n\nOne paper per line: PMID first, then its variant/haplotype ids"
    + " (dash or space after the PMID, ids separated by commas or spaces).";

  // Only worth offering if this project actually has somewhere to put the ids
  // — a project with no repeating group has no "Row IDs" step to feed.
  let modeToggle = null;
  if (groupDef) {
    modeToggle = el("div", "sub-tabs ids-mode");
    const modePlain = el("button", "sub-tab on", "Plain PMIDs");
    const modeVariant = el("button", "sub-tab",
      `PMID + ${groupDef.label || groupDef.name} IDs`);
    modeToggle.appendChild(modePlain);
    modeToggle.appendChild(modeVariant);
    idsPane.appendChild(modeToggle);

    const setMode = mode => {
      if (st.idsMode === mode) return;
      if (ta.value.trim() && !window.confirm(
        "Switching formats clears what you've typed here. Continue?"
      )) return;
      st.idsMode = mode;
      modePlain.classList.toggle("on", mode === "plain");
      modeVariant.classList.toggle("on", mode === "variant");
      ta.value = "";
      ta.placeholder = mode === "variant" ? VARIANT_PLACEHOLDER : PLAIN_PLACEHOLDER;
      fileOk.hidden = true; fileErr.hidden = true;
      recount();
      if (!st.closed) ta.focus();
    };
    modePlain.onclick = () => setMode("plain");
    modeVariant.onclick = () => setMode("variant");
  }

  const idTabs = el("div", "sub-tabs");
  const tabPaste = el("button", "sub-tab on", "Paste IDs");
  const tabFile = el("button", "sub-tab", "From a file");
  idTabs.appendChild(tabPaste); idTabs.appendChild(tabFile);
  idsPane.appendChild(idTabs);

  const pastePane = el("div", "sub-pane");
  const ta = el("textarea", "pmid-ta");
  ta.placeholder = PLAIN_PLACEHOLDER;
  pastePane.appendChild(ta);
  idsPane.appendChild(pastePane);

  const filePane = el("div", "sub-pane");
  filePane.hidden = true;
  const drop = el("div", "pmid-drop");
  drop.appendChild(el("div", "dz-big", "Drop a text file of PMIDs here"));
  drop.appendChild(el("div", "dz-or", "or"));
  const chooseIds = el("button", "btn", "Choose a file");
  drop.appendChild(chooseIds);
  filePane.appendChild(drop);
  const idFile = el("input");
  idFile.type = "file";
  idFile.multiple = true;
  // No `accept` filter: extensionless ID lists are common and a picker filter
  // would hide them. isIdFile() validates the choice instead, with an inline error.
  idFile.className = "pmid-id-file";
  idFile.hidden = true;
  filePane.appendChild(idFile);
  const fileErr = el("div", "pmid-file-err");
  fileErr.hidden = true;
  filePane.appendChild(fileErr);
  const fileOk = el("div", "pmid-file-ok");
  fileOk.hidden = true;
  filePane.appendChild(fileOk);
  idsPane.appendChild(filePane);

  const count = el("div", "pmid-count");
  idsPane.appendChild(count);
  const warn = el("div", "pmid-warn");
  warn.hidden = true;
  idsPane.appendChild(warn);

  const idsActions = el("div", "step-actions");
  const idsBack = el("button", "btn", "Back");
  const fetchBtn = el("button", "btn primary", "Fetch");
  idsActions.appendChild(idsBack); idsActions.appendChild(fetchBtn);
  idsPane.appendChild(idsActions);

  idsPane.appendChild(el("div", "modal-hint",
    "Looks each paper up on PubMed, then tries PubMed Central and other open-access " +
    "sources for the full-text PDF. Not every paper is openly available this way."));
  body.appendChild(idsPane);

  function showIdTab(which) {
    tabPaste.classList.toggle("on", which === "paste");
    tabFile.classList.toggle("on", which === "file");
    pastePane.hidden = which !== "paste";
    filePane.hidden = which !== "file";
    if (which === "paste") setTimeout(() => { if (!st.closed) ta.focus(); }, 30);
  }
  tabPaste.onclick = () => showIdTab("paste");
  tabFile.onclick = () => showIdTab("file");

  // In "variant" mode `parsed` is a parsePmidVariantText() result instead —
  // pmids()/describeCurrent() below are what the rest of this step reads, so
  // everywhere else stays mode-agnostic.
  let parsed = { ids: [], invalid: [], duplicates: 0, total: 0 };
  function parsedPmids() {
    return st.idsMode === "variant" ? parsed.entries.map(e => e.pmid) : parsed.ids;
  }
  function variantIdsFor(pmid) {
    if (st.idsMode !== "variant") return [];
    const e = parsed.entries.find(x => x.pmid === pmid);
    return e ? e.variantIds : [];
  }
  function recount() {
    parsed = st.idsMode === "variant" ? parsePmidVariantText(ta.value) : parsePmidText(ta.value);
    const n = parsedPmids().length;
    // Nothing typed yet is not a state worth narrating — the disabled Fetch
    // button already says everything.
    count.textContent = parsed.total
      ? (st.idsMode === "variant" ? describeVariantParse(parsed) : describeParse(parsed))
      : "";
    fetchBtn.disabled = n === 0 || n > MAX_JOB;
    fetchBtn.textContent = n === 0 ? "Fetch"
      : `Fetch ${plural(n, "paper")}${n > SOFT_CAP && n <= MAX_JOB ? " anyway" : ""}`;

    if (n > MAX_JOB) {
      warn.textContent = `${n} PMIDs. One import can take at most ${MAX_JOB} — split the list and add it in parts.`;
      warn.hidden = false;
    } else if (n > SOFT_CAP) {
      warn.textContent =
        `${n} PMIDs. This runs on the server a few at a time and can take a while. ` +
        `You can close this at any point — the import keeps going, and the Jobs panel shows where it got to.`;
      warn.hidden = false;
    } else warn.hidden = true;
  }
  ta.addEventListener("input", recount);

  // The file is NOT parsed here: its text is merged into the textarea so that
  // parsing (and de-duplication) happens in exactly one place.
  async function mergeIdFiles(files) {
    const good = [...files].filter(f => isIdFile(f.name));
    const bad = [...files].filter(f => !isIdFile(f.name));
    fileErr.hidden = !bad.length;
    if (bad.length) {
      fileErr.textContent =
        `Ignored ${bad.map(f => f.name).join(", ")} — a PMID list must be a plain text file (.txt, .csv, .tsv or no extension).`;
    }
    for (const f of good) {
      const text = await f.text();
      ta.value = ta.value.trim() ? `${ta.value.trim()}\n${text}` : text;
    }
    if (good.length) {
      recount();
      fileOk.hidden = false;
      fileOk.textContent = `Loaded ${good.map(f => f.name).join(", ")} — ${describeParse(parsed)}`;
    }
  }
  chooseIds.onclick = () => idFile.click();
  idFile.onchange = () => {
    const files = [...idFile.files];
    idFile.value = "";
    if (files.length) mergeIdFiles(files);
  };
  ["dragenter", "dragover"].forEach(evt =>
    drop.addEventListener(evt, e => { e.preventDefault(); drop.classList.add("drag"); }));
  ["dragleave", "drop"].forEach(evt =>
    drop.addEventListener(evt, e => { e.preventDefault(); drop.classList.remove("drag"); }));
  drop.addEventListener("drop", e => {
    const files = [...(e.dataTransfer.files || [])];
    if (files.length) mergeIdFiles(files);
  });

  idsBack.onclick = () => { st.path = null; goto("source"); };
  fetchBtn.onclick = startFetch;
  ta.addEventListener("keydown", e => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); startFetch(); }
  });

  /* ================= STEP: fetching ================= */
  const fetchPane = el("div", "modal-pane");
  fetchPane.hidden = true;

  const progress = el("div", "batch-progress");
  const track = el("div", "batch-progress-track");
  const fill = el("div", "batch-progress-fill");
  track.appendChild(fill);
  progress.appendChild(track);
  const progressLabel = el("div", "batch-progress-label");
  progress.appendChild(progressLabel);
  fetchPane.appendChild(progress);

  const summary = el("div", "batch-summary");
  summary.hidden = true;
  fetchPane.appendChild(summary);

  const list = el("div", "batch-list");
  fetchPane.appendChild(list);
  const moreNote = el("div", "batch-more");
  moreNote.hidden = true;

  const fetchActions = el("div", "step-actions");
  const stopBtn = el("button", "btn", "Cancel import");
  stopBtn.title = "Stop the server picking up more PMIDs (closing this window does NOT stop it)";
  const retryBtn = el("button", "btn", "Retry failed");
  const skipBtn = el("button", "btn primary", "Continue");
  // renderSummary owns these; start hidden so none flashes before the first run
  stopBtn.hidden = retryBtn.hidden = skipBtn.hidden = true;
  fetchActions.appendChild(stopBtn);
  fetchActions.appendChild(retryBtn);
  fetchActions.appendChild(skipBtn);
  fetchPane.appendChild(fetchActions);
  body.appendChild(fetchPane);

  /* ---- manual-upload detour, shown inside the fetching step ---- */
  const detour = el("div", "detour");
  detour.hidden = true;
  const detourLab = el("div", "detour-lab");
  detour.appendChild(detourLab);
  const detourZone = el("div", "dropzone");
  detourZone.appendChild(el("div", "dz-big", "Drop the PDF here"));
  detourZone.appendChild(el("div", "dz-or", "or"));
  const detourChoose = el("button", "btn primary", "Choose file");
  detourZone.appendChild(detourChoose);
  detour.appendChild(detourZone);
  const detourCancel = el("button", "btn detour-cancel", "Cancel");
  detour.appendChild(detourCancel);
  fetchPane.appendChild(detour);

  const detourInput = el("input");
  detourInput.type = "file";
  detourInput.accept = ".pdf,application/pdf";
  detourInput.hidden = true;
  fetchPane.appendChild(detourInput);

  function openDetour(pmid) {
    st.uploadHint = pmid;
    detourLab.textContent = `Upload the PDF for PMID ${pmid}. You'll confirm the PMID in the next step.`;
    detour.hidden = false;
    list.hidden = true;
    fetchActions.hidden = true;
  }
  function closeDetour() {
    st.uploadHint = null;
    detour.hidden = true;
    list.hidden = false;
    fetchActions.hidden = false;
  }
  detourCancel.onclick = closeDetour;
  detourChoose.onclick = () => detourInput.click();
  detourInput.onchange = async () => {
    const files = [...detourInput.files];
    detourInput.value = "";
    if (files.length) await acceptDetour(files);
  };
  ["dragenter", "dragover"].forEach(evt =>
    detourZone.addEventListener(evt, e => { e.preventDefault(); detourZone.classList.add("drag"); }));
  ["dragleave", "drop"].forEach(evt =>
    detourZone.addEventListener(evt, e => { e.preventDefault(); detourZone.classList.remove("drag"); }));
  detourZone.addEventListener("drop", async e => {
    const files = [...(e.dataTransfer.files || [])].filter(f =>
      f.type === "application/pdf" || f.name.toLowerCase().endsWith(".pdf"));
    if (files.length) await acceptDetour(files);
  });

  async function acceptDetour(files) {
    const pmid = st.uploadHint;
    const papers = await onUpload(files, { pmid });
    papers.forEach(p => { st.papers.set(p.uid, p); if (pmid) stashVariantIds(p.uid, pmid); });
    if (pmid && papers.length) {
      const r = st.rows.get(pmid);
      if (r) r.uploaded = true;   // the job's verdict no longer applies to this row
      setRow(pmid, { label: "uploaded", tone: "uploaded", msg: "PDF uploaded — confirm the PMID in the next step" });
    }
    closeDetour();
    renderSummary();
  }

  /* ---------------- result rows ---------------- */
  function makeRow(pmid) {
    const node = el("div", "batch-row");
    node.appendChild(el("span", "bp-id", pmid));
    const stateNode = el("span", "bp-state pending", "waiting");
    node.appendChild(stateNode);
    const msgNode = el("span", "bp-msg");
    node.appendChild(msgNode);
    const altNode = el("button", "bp-alt", "Upload PDF for this PMID");
    altNode.hidden = true;
    altNode.onclick = () => openDetour(pmid);
    node.appendChild(altNode);
    return { pmid, tone: "pending", outcome: null, uploaded: false, node, stateNode, msgNode, altNode };
  }

  function buildRows(pmids) {
    st.rows.clear();
    st.order = pmids.slice();
    list.innerHTML = "";
    const frag = document.createDocumentFragment();
    pmids.forEach((pmid, i) => {
      const r = makeRow(pmid);
      st.rows.set(pmid, r);
      // Above MAX_ROWS the node is held back: hundreds of rows is a lot of DOM
      // for a list nobody reads when everything succeeds. Rows that end up
      // needing attention are inserted on their state change.
      if (i < MAX_ROWS) frag.appendChild(r.node);
    });
    frag.appendChild(moreNote);
    list.appendChild(frag);
    const hidden = Math.max(0, pmids.length - MAX_ROWS);
    moreNote.hidden = hidden === 0;
    moreNote.textContent = `+${hidden} more — rows appear here if they need your attention.`;
  }

  // view: {label, tone, msg} — see jobs.importItemView. `outcome` decides
  // whether the row offers the manual-upload detour.
  function setRow(pmid, view, outcome) {
    const r = st.rows.get(pmid);
    if (!r) return;
    r.tone = view.tone;
    r.outcome = outcome || null;
    if (st.closed) return;
    if (!r.node.parentNode && view.tone !== "pending" && view.tone !== "ok") list.insertBefore(r.node, moreNote);
    r.stateNode.className = "bp-state " + view.tone;
    r.stateNode.innerHTML = "";
    if (view.tone === "running") r.stateNode.appendChild(el("span", "spinner-ring"));
    else r.stateNode.appendChild(document.createTextNode(view.label));
    r.msgNode.textContent = view.msg || "";
    r.altNode.hidden = r.uploaded || outcome !== "no_pdf";
  }

  function tally() {
    const t = { ok: 0, dup: 0, failed: 0, uploaded: 0, waiting: 0 };
    for (const r of st.rows.values()) {
      if (r.tone === "pending" || r.tone === "running") t.waiting++;
      else if (r.tone in t) t[r.tone]++;
    }
    return t;
  }

  function renderSummary() {
    if (st.closed) return;
    const t = tally();
    const bits = [];
    if (t.ok) bits.push(`${t.ok} added`);
    if (t.dup) bits.push(`${t.dup} already in project`);
    if (t.uploaded) bits.push(`${t.uploaded} uploaded by hand`);
    if (t.failed) bits.push(`${t.failed} need attention`);
    summary.textContent = bits.join(" · ");
    summary.hidden = !bits.length;

    // What can be retried is what the JOB says failed — a row uploaded by
    // hand since then is settled, whatever the job thought of it.
    const retryable = st.job && !st.running
      ? (st.job.items || []).filter(it => (it.state === "failed" || it.state === "cancelled")
          && !(st.rows.get(it.ref) || {}).uploaded).length
      : 0;
    stopBtn.hidden = !st.running || !st.job;
    stopBtn.disabled = false;
    retryBtn.hidden = st.running || !retryable;
    retryBtn.disabled = false;
    retryBtn.textContent = `Retry failed (${retryable})`;
    skipBtn.hidden = st.running;
    // Skip leaves every unresolved id behind and moves on with what worked.
    skipBtn.textContent = t.failed ? `Skip ${t.failed} and continue` : "Continue";
  }

  function renderProgress() {
    if (st.closed) return;
    const total = st.order.length;
    const done = st.localDone + (st.job ? countFinishedRows() : 0);
    fill.style.width = total ? Math.round(done / total * 100) + "%" : "0%";
    progressLabel.textContent = total ? `${done} of ${total} complete` : "";
    progress.classList.toggle("on", total > 0);
  }
  function countFinishedRows() {
    let n = 0;
    for (const it of (st.job && st.job.items) || []) {
      if (it.state !== "waiting" && it.state !== "running") n++;
    }
    // A retry job only holds the retried ids; the ones the first job settled
    // are still settled.
    return n + st.settledBefore;
  }

  // Drafts this pmid's declared ids (from the "PMID + variant IDs" paste
  // format) against the paper's uid — not saved yet, just carried forward to
  // the "variants" step, which is where onSaveGroupItems actually persists it.
  function stashVariantIds(uid, pmid) {
    const ids = variantIdsFor(pmid);
    if (ids.length) st.groupItemsByUid.set(uid, ids.map(label => ({ rowId: null, label })));
  }

  // A job item that produced (or found) a paper: carry it into the later steps.
  function adopt(pmid, uid) {
    if (!uid) {
      const existing = findByPmid && findByPmid(pmid);
      if (existing) { st.papers.set(existing.uid, existing); stashVariantIds(existing.uid, pmid); }
      return;
    }
    st.resolving.push(resolvePaper(uid).then(p => {
      if (p) { st.papers.set(p.uid, p); stashVariantIds(p.uid, pmid); }
    }).catch(() => {}));
  }

  function onImportTick(job) {
    if (st.closed || !st.job || job.id !== st.job.id) return;
    st.job = job;
    for (const it of job.items || []) {
      const r = st.rows.get(it.ref);
      if (!r || r.uploaded) continue;
      const settled = it.state !== "waiting" && it.state !== "running";
      if (settled && !r.adopted &&
          (it.outcome === "added" || it.outcome === "added_abstract" || it.outcome === "duplicate")) {
        r.adopted = true;
        adopt(it.ref, it.result && it.result.uid);
      }
      setRow(it.ref, importItemView(it), it.outcome);
    }
    st.running = isActive(job);
    renderProgress(); renderSummary();
    if (!st.running && !st.advanced) {
      st.advanced = true;
      if (job.state === "failed" && job.error) {
        summary.textContent = `The import stopped: ${job.error}`;
        summary.hidden = false;
      }
      // Straight through to the next step when nothing needs the curator.
      if (job.state === "done" && !tally().failed) continueAfterFetch();
    }
  }

  function attachJob(job) {
    if (st.unfollow) st.unfollow();
    st.job = job;
    st.advanced = false;
    st.running = isActive(job);
    st.unfollow = followJob(job.id, onImportTick, job);
    renderProgress(); renderSummary();
  }

  // Waits for the papers the job added to be looked up first — the next steps
  // need the paper objects, not just their ids.
  async function continueAfterFetch() {
    skipBtn.disabled = true;
    await Promise.all(st.resolving);
    skipBtn.disabled = false;
    if (!st.closed && st.step === "fetching") gotoAfterIntake();
  }

  async function startFetch() {
    if (st.running || !parsedPmids().length) return;
    const ids = parsedPmids();
    if (ids.length > MAX_JOB) return;
    goto("fetching");
    buildRows(ids);
    st.job = null; st.localDone = 0; st.settledBefore = 0; st.resolving = [];
    // Pre-flight: anything already in the project is settled without asking
    // the server. On a re-pasted list this turns a long no-op into an instant one.
    const todo = [];
    for (const pmid of ids) {
      // Already-held papers still belong to this run — the curator asked for
      // them by id, so they carry through to the extraction step.
      const existing = findByPmid && findByPmid(pmid);
      if (existing) {
        setRow(pmid, { label: "in project", tone: "dup", msg: "already in this project" });
        st.papers.set(existing.uid, existing);
        stashVariantIds(existing.uid, pmid);
        st.localDone++;
      } else todo.push(pmid);
    }
    if (!todo.length) {
      renderProgress(); renderSummary();
      gotoAfterIntake();
      return;
    }
    st.running = true;
    renderProgress(); renderSummary();
    try {
      attachJob(await onStartImport(todo));
    } catch (err) {
      st.running = false;
      todo.forEach(p => setRow(p, { label: "failed", tone: "failed",
        msg: (err && err.message) || "could not start the import" }));
      renderProgress(); renderSummary();
    }
  }

  // Cancelling is explicit: the job stops picking up new ids; ones already
  // being fetched finish. Closing the modal does NOT do this.
  stopBtn.onclick = async () => {
    if (!st.job) return;
    stopBtn.disabled = true;
    try { onImportTick(await onCancelJob(st.job.id)); }
    catch (err) { summary.textContent = err.message; summary.hidden = false; stopBtn.disabled = false; }
  };
  retryBtn.onclick = async () => {
    if (!st.job) return;
    retryBtn.disabled = true;
    try {
      const next = await onRetryJob(st.job.id);
      // Everything the old job settled for good stays settled.
      const retried = new Set((next.items || []).map(it => it.ref));
      st.settledBefore = st.order.filter(p => !retried.has(p)).length - st.localDone;
      for (const ref of retried) {
        const r = st.rows.get(ref);
        if (r && !r.uploaded) { r.adopted = false; setRow(ref, { label: "waiting", tone: "pending", msg: "" }); }
      }
      attachJob(next);
    } catch (err) {
      retryBtn.disabled = false;
      summary.textContent = err.message; summary.hidden = false;
    }
  };
  skipBtn.onclick = () => continueAfterFetch();

  /* ================= STEP: upload ================= */
  const uploadPane = el("div", "modal-pane");
  uploadPane.hidden = true;

  const zone = el("div", "dropzone");
  zone.appendChild(el("div", "dz-big", "Drag & drop PDF(s) here"));
  zone.appendChild(el("div", "dz-or", "or"));
  const chooseBtn = el("button", "btn primary", "Choose file(s)");
  zone.appendChild(chooseBtn);
  uploadPane.appendChild(zone);

  const fileInput = el("input");
  fileInput.type = "file";
  fileInput.multiple = true;
  fileInput.accept = ".pdf,application/pdf";
  fileInput.hidden = true;
  uploadPane.appendChild(fileInput);

  const uploadList = el("div", "batch-list");
  uploadList.hidden = true;
  uploadPane.appendChild(uploadList);

  const upActions = el("div", "step-actions");
  const upBack = el("button", "btn", "Back");
  const upNext = el("button", "btn primary", "Continue");
  upNext.disabled = true;
  upActions.appendChild(upBack); upActions.appendChild(upNext);
  uploadPane.appendChild(upActions);
  body.appendChild(uploadPane);

  async function acceptUploads(files) {
    const papers = await onUpload(files, {});
    papers.forEach(p => st.papers.set(p.uid, p));
    uploadList.innerHTML = "";
    for (const p of st.papers.values()) {
      const r = el("div", "batch-row");
      r.appendChild(el("span", "bp-id", p.pmid || p.filename));
      r.appendChild(el("span", "bp-state ok", "added"));
      r.appendChild(el("span", "bp-msg", p.pmid ? "" : "PubMed ID needed for extraction"));
      uploadList.appendChild(r);
    }
    uploadList.hidden = st.papers.size === 0;
    upNext.disabled = st.papers.size === 0;
  }

  chooseBtn.onclick = () => fileInput.click();
  fileInput.onchange = () => {
    const files = [...fileInput.files];
    fileInput.value = "";
    if (files.length) acceptUploads(files);
  };
  ["dragenter", "dragover"].forEach(evt =>
    zone.addEventListener(evt, e => { e.preventDefault(); zone.classList.add("drag"); }));
  ["dragleave", "drop"].forEach(evt =>
    zone.addEventListener(evt, e => { e.preventDefault(); zone.classList.remove("drag"); }));
  zone.addEventListener("drop", e => {
    const files = [...(e.dataTransfer.files || [])].filter(f =>
      f.type === "application/pdf" || f.name.toLowerCase().endsWith(".pdf"));
    if (files.length) acceptUploads(files);
  });
  upBack.onclick = () => { st.path = null; goto("source"); };
  upNext.onclick = () => gotoAfterIntake();

  /* ================= STEP: variants (curator-declared group row ids) ====== */
  // Only reachable when groupDef is set (see chain()) — the AI fills in values
  // for whatever rows are declared here, it never invents or names one itself
  // (server/extraction.py). Pre-filled from the "PMID + variant IDs" paste
  // format when that was used; blank and editable either way.
  const variantsPane = el("div", "modal-pane");
  variantsPane.hidden = true;
  body.appendChild(variantsPane);

  function renderVariantsStep() {
    variantsPane.innerHTML = "";
    const papers = [...st.papers.values()];

    variantsPane.appendChild(el("div", "modal-lab",
      `Declare ${groupDef.label || groupDef.name} IDs`));
    variantsPane.appendChild(el("div", "modal-hint",
      "One list per paper — the AI fills in values for each id you declare here, "
      + "it does not invent or name the rows itself. You can also add or edit these "
      + "later, from the paper's own curation view."));

    if (!papers.length) {
      variantsPane.appendChild(el("div", "modal-hint", "No papers were added yet."));
    }

    const cards = el("div", "variant-cards");
    for (const p of papers) {
      const items = st.groupItemsByUid.get(p.uid) || [];
      st.groupItemsByUid.set(p.uid, items);   // same array reused across re-renders

      const card = el("div", "variant-card");
      const head = el("div", "variant-card-head");
      head.appendChild(el("span", "bp-id", p.pmid || p.filename));
      card.appendChild(head);

      const list = renderGroupItemsList(items, {});
      card.appendChild(list.el);

      const addBtn = el("button", "btn gi-add", "+ Add entry");
      addBtn.onclick = () => list.addRow();
      card.appendChild(addBtn);

      cards.appendChild(card);
    }
    variantsPane.appendChild(cards);

    const status = el("div", "modal-status err");
    status.hidden = true;
    variantsPane.appendChild(status);

    const acts = el("div", "step-actions");
    const back = el("button", "btn", "Back");
    back.onclick = () => goto(st.path === "pdf" ? "upload" : "fetching");
    const cont = el("button", "btn primary", "Continue");
    cont.onclick = async () => {
      if (!onSaveGroupItems) { goto("extract"); return; }
      cont.disabled = true; back.disabled = true; status.hidden = true;
      try {
        await Promise.all(papers.map(async p => {
          const saved = await onSaveGroupItems(p.uid, st.groupItemsByUid.get(p.uid) || []);
          st.groupItemsByUid.set(p.uid, saved);
        }));
        goto("extract");
      } catch (err) {
        status.textContent = (err && err.message) || "Could not save one or more entries.";
        status.hidden = false;
        cont.disabled = false; back.disabled = false;
      }
    };
    acts.appendChild(back); acts.appendChild(cont);
    variantsPane.appendChild(acts);
  }

  /* ================= STEP: extract ================= */
  const extractPane = el("div", "modal-pane");
  extractPane.hidden = true;
  body.appendChild(extractPane);

  // scope: bulk mode only — "current" (the open paper) | "lacking" (every
  // paper without a run for at least one of the chosen models).
  const exState = { checks: [], job: null, unfollow: null,
                    scope: bulk && bulk.currentUid ? "current" : "lacking" };

  function chosenModels() { return exState.checks.filter(c => c.checked).map(c => c.value); }

  // The papers the Extract button will send, given the models ticked now.
  function extractTargets() {
    if (!bulk) return [...st.papers.values()].filter(p => p.pmidStatus !== "pending");
    const all = bulk.papers().filter(p => p.pmidStatus !== "pending");
    if (exState.scope === "current") return all.filter(p => p.uid === bulk.currentUid);
    const models = chosenModels();
    return all.filter(p => models.some(m => !bulk.hasRun(p.uid, m)));
  }

  function renderExtractStep() {
    extractPane.innerHTML = "";
    exState.checks = [];

    const papers = bulk ? [] : [...st.papers.values()];
    if (!bulk && !papers.length) {
      extractPane.appendChild(el("div", "modal-hint", "No papers were added, so there is nothing to extract."));
      const acts = el("div", "step-actions");
      const b = el("button", "btn primary", "Close");
      b.onclick = close;
      acts.appendChild(b);
      extractPane.appendChild(acts);
      return;
    }

    // A fetched paper is pre-confirmed (the PMID came from PubMed itself), but an
    // uploaded one is not — and extraction needs a paper's identity RESOLVED.
    // Resolved does not mean "has a PMID": most papers have one, some genuinely
    // don't, and "this one has no PubMed ID" is a real answer rather than a way
    // of skipping the question. Without that second route an uploaded PDF with
    // no PMID is a dead end in this flow.
    const unconfirmed = papers.filter(p => p.pmidStatus === "pending");
    if (unconfirmed.length) {
      extractPane.appendChild(el("div", "modal-lab", "Identify these papers"));
      extractPane.appendChild(el("div", "modal-hint",
        "These PDFs need an identity before they can be extracted. Nothing is committed until you confirm it."));
      const box = el("div", "confirm-list");
      for (const p of unconfirmed) {
        const r = el("div", "confirm-row");
        r.appendChild(el("span", "bp-id", p.filename));
        const inp = el("input", "confirm-pmid");
        inp.type = "text";
        inp.inputMode = "numeric";
        inp.placeholder = "PMID…";
        inp.value = p.suggestedPmid || "";
        const b = el("button", "btn", "Confirm");
        const alt = el("button", "btn ghost-btn", "No PMID");
        const err = el("span", "bp-msg");

        // The row has two modes rather than two rows: PMID (default) or DOI.
        let doiMode = false;
        const setMode = next => {
          doiMode = next;
          inp.value = doiMode ? "" : (p.suggestedPmid || "");
          inp.placeholder = doiMode ? "DOI (optional)…" : "PMID…";
          inp.inputMode = doiMode ? "text" : "numeric";
          b.textContent = doiMode ? "Save" : "Confirm";
          alt.textContent = doiMode ? "Cancel" : "No PMID";
          err.textContent = "";
          inp.focus();
        };
        alt.onclick = () => setMode(!doiMode);

        const commit = async () => {
          const raw = inp.value.trim();
          if (!doiMode && !/^\d+$/.test(raw)) {
            err.textContent = "Enter a numeric PMID."; inp.focus(); return;
          }
          b.disabled = true; alt.disabled = true; err.textContent = "";
          try {
            const updated = doiMode
              ? await onNoPmid(p.uid, raw)
              : await onConfirmPmid(p.uid, raw);
            st.papers.set(updated.uid, updated);
            renderExtractStep();
          } catch (e2) {
            b.disabled = false; alt.disabled = false;
            err.textContent = (e2 && e2.message) || "could not save";
          }
        };
        b.onclick = commit;
        inp.onkeydown = e => { if (e.key === "Enter") commit(); };
        r.appendChild(inp); r.appendChild(b); r.appendChild(alt); r.appendChild(err);
        box.appendChild(r);
      }
      extractPane.appendChild(box);
    }

    // Managers only, and only where the server has an OpenRouter key. The
    // papers are in the project either way; someone with access can run it later.
    if (!canExtract) {
      extractPane.appendChild(el("div", "modal-lab", "AI extraction"));
      extractPane.appendChild(el("div", "modal-hint",
        "AI extraction isn't available here — it needs a project manager, and the server must have an extraction key configured."));
      const acts = el("div", "step-actions");
      const b = el("button", "btn primary", "Finish");
      b.onclick = close;
      acts.appendChild(b);
      extractPane.appendChild(acts);
      return;
    }

    const heading = el("div", "modal-lab");
    extractPane.appendChild(heading);

    const picks = el("div", "model-picks");
    for (const m of config.models) {
      const lab = el("label");
      const cb = el("input"); cb.type = "checkbox"; cb.value = m.slug; cb.checked = true;
      lab.appendChild(cb);
      lab.appendChild(el("span", null, m.label));
      if (!m.supportsStructuredOutput) lab.appendChild(el("span", "so", "prompt-only"));
      picks.appendChild(lab); exState.checks.push(cb);
    }
    extractPane.appendChild(picks);

    // Bulk: which papers. Counted live, since "lacking a run" depends on the
    // models ticked above.
    let scopeBox = null;
    if (bulk) {
      scopeBox = el("div", "ex-scope");
      const opt = (value, text) => {
        const lab = el("label");
        const rb = el("input"); rb.type = "radio"; rb.name = "exScope"; rb.value = value;
        rb.checked = exState.scope === value;
        rb.onchange = () => { exState.scope = value; refresh(); };
        const span = el("span", null, text);
        lab.appendChild(rb); lab.appendChild(span);
        scopeBox.appendChild(lab);
        return span;
      };
      if (bulk.currentUid) opt("current", "The open paper");
      exState.lackingLabel = opt("lacking", "Every paper without a run for the chosen models");
      extractPane.appendChild(scopeBox);
    }

    const row = el("div", "extract-row");
    const sel = el("select");
    for (const e of config.parseEngines) {
      const o = el("option", null, e + (e === "mistral-ocr" ? " (paid)" : e === "pdf-text" ? " (free)" : ""));
      o.value = e;
      if (e === config.defaults.parseEngine) o.selected = true;
      sel.appendChild(o);
    }
    row.appendChild(sel);
    extractPane.appendChild(row);

    const engineWarn = el("div", "engine-warn");
    const syncWarn = () => engineWarn.textContent = sel.value === "mistral-ocr"
      ? "mistral-ocr is paid and forwards at most ~8 images per PDF." : "";
    sel.onchange = syncWarn; syncWarn();
    extractPane.appendChild(engineWarn);

    const exProgress = el("div", "batch-progress");
    const exTrack = el("div", "batch-progress-track");
    const exFill = el("div", "batch-progress-fill");
    exTrack.appendChild(exFill);
    exProgress.appendChild(exTrack);
    const exLabel = el("div", "batch-progress-label");
    exProgress.appendChild(exLabel);
    extractPane.appendChild(exProgress);

    const exList = el("div", "batch-list");
    exList.hidden = true;
    extractPane.appendChild(exList);

    const acts = el("div", "step-actions");
    const runBtn = el("button", "btn primary");
    const doneBtn = el("button", "btn", bulk ? "Close" : "Finish");
    acts.appendChild(runBtn); acts.appendChild(doneBtn);
    extractPane.appendChild(acts);
    doneBtn.onclick = close;
    extractPane.appendChild(el("div", "modal-hint",
      "Extraction runs on the server. You can close this window — it keeps going, and the results appear on each paper as they finish."));

    if (unconfirmed.length) {
      extractPane.appendChild(el("div", "modal-hint",
        "Resolve every paper above to enable extraction."));
    }

    function refresh() {
      if (exState.job && isActive(exState.job)) return;   // mid-run: leave the button alone
      const n = extractTargets().length;
      if (bulk) {
        const lacking = bulk.papers().filter(p => p.pmidStatus !== "pending")
          .filter(p => chosenModels().some(m => !bulk.hasRun(p.uid, m))).length;
        exState.lackingLabel.textContent =
          `Every paper without a run for the chosen models (${lacking})`;
        heading.textContent = "Run AI extraction";
      } else {
        heading.textContent = `Run AI extraction — ${plural(n, "paper")} ready`;
      }
      runBtn.textContent = n ? `Extract ${plural(n, "paper")}` : "Nothing to extract";
      runBtn.disabled = !n || !!unconfirmed.length || !chosenModels().length;
    }
    exState.checks.forEach(cb => { cb.onchange = refresh; });
    refresh();

    const nodes = new Map();   // uid -> {s, m}
    function onExtractTick(job) {
      if (st.closed || !exState.job || job.id !== exState.job.id) return;
      exState.job = job;
      for (const it of job.items || []) {
        const n = nodes.get(it.ref);
        if (!n) continue;
        const v = extractItemView(it);
        n.s.className = "bp-state " + v.tone;
        n.s.innerHTML = "";
        if (v.tone === "running") n.s.appendChild(el("span", "spinner-ring"));
        else n.s.textContent = v.label;
        n.m.textContent = v.msg;
      }
      const { finished, total } = jobProgress(job);
      exFill.style.width = total ? Math.round(finished / total * 100) + "%" : "0%";
      exLabel.textContent = `${finished} of ${total} complete`;
      if (isActive(job)) return;

      // Finish becomes the CTA once the run is done; re-running is the side door.
      runBtn.classList.remove("extracting");
      runBtn.className = "btn";
      runBtn.textContent = "Extract again";
      runBtn.disabled = false;
      doneBtn.className = "btn primary";
      if (job.state === "failed" && job.error) {
        engineWarn.textContent = `Extraction stopped: ${job.error}`;
      }
    }

    runBtn.onclick = async () => {
      const models = chosenModels();
      if (!models.length) { engineWarn.textContent = "Pick at least one model."; return; }
      const targets = extractTargets();
      if (!targets.length) { refresh(); return; }
      runBtn.disabled = true;
      runBtn.innerHTML = "";
      runBtn.appendChild(el("span", "btn-spinner"));
      runBtn.appendChild(document.createTextNode("Extracting…"));
      runBtn.classList.add("extracting");
      exList.innerHTML = ""; exList.hidden = false;
      exProgress.classList.add("on");
      exFill.style.width = "0%";
      exLabel.textContent = `0 of ${targets.length} complete`;

      nodes.clear();
      for (const p of targets) {
        const r = el("div", "batch-row");
        r.appendChild(el("span", "bp-id", p.pmid || p.doi || p.filename));
        const s = el("span", "bp-state pending", "waiting");
        const m = el("span", "bp-msg");
        r.appendChild(s); r.appendChild(m);
        exList.appendChild(r);
        nodes.set(p.uid, { s, m });
      }

      try {
        // ONE job for every paper: the server runs a few at a time, and keeps
        // going if this window closes.
        const job = await onStartExtract({ uids: targets.map(p => p.uid), models, parseEngine: sel.value });
        if (exState.unfollow) exState.unfollow();
        exState.job = job;
        exState.unfollow = followJob(job.id, onExtractTick, job);
      } catch (err) {
        runBtn.classList.remove("extracting");
        runBtn.className = "btn primary";
        runBtn.textContent = "Try again";
        runBtn.disabled = false;
        engineWarn.textContent = (err && err.message) || "could not start extraction";
        for (const n of nodes.values()) {
          n.s.className = "bp-state failed"; n.s.textContent = "failed";
        }
      }
    };
  }

  /* ---------------- navigation ---------------- */
  const PANES = {
    source: sourcePane, ids: idsPane, fetching: fetchPane,
    upload: uploadPane, variants: variantsPane, extract: extractPane,
  };

  function goto(step) {
    if (st.closed) return;
    st.step = step;
    for (const [key, pane] of Object.entries(PANES)) pane.hidden = key !== step;
    if (step === "variants") renderVariantsStep();
    if (step === "extract") renderExtractStep();
    if (step === "ids") setTimeout(() => { if (!st.closed) ta.focus(); }, 30);
    renderStepper();
  }

  recount();
  showIdTab("paste");
  goto(bulk ? "extract" : "source");
}
