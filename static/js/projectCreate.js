// "New project" — a three-step stepper: Details → Schema → Review.
//
// Replaces the placeholder name-and-description dialog. A project manager either
// defines a feature set here, or imports a configuration document another
// manager authored — the two creation routes the workflow actually has.
//
// Linear, not branching: how you start (blank / server template / imported file)
// only decides what step 2 is PRE-POPULATED with, so the step count never
// changes under the manager mid-flow.
import { el } from "./dom.js";
import { api } from "./api.js";
import { renderFeatureEditor, cleanFeatures, blankLeaf } from "./featureSchemaEditor.js";

const STEPS = ["details", "features", "review"];
const STEP_LABEL = { details: "Details", features: "Schema", review: "Review" };

/**
 * onCreated(project) -> void
 * template  the server's starter config document, or null
 */
export function openCreateProject({ onCreated, template }) {
  const root = document.getElementById("newProjectModal");
  root.innerHTML = "";

  const st = {
    step: "details",
    name: "",
    description: "",
    allowNoPmid: false,
    allowAbstractExtraction: false,
    features: [],
    // Project-owned prompt/export details ride along from the template or an
    // imported file. Models and PDF processing stay server-wide.
    carry: baseCarry(template),
    validation: null,
    busy: false,
  };

  const modal = el("div", "modal stepper-modal fe-modal");
  const head = el("div", "modal-head");
  head.appendChild(el("span", null, "New curation project"));
  const closeBtn = el("button", "modal-close", "×");
  head.appendChild(closeBtn);
  modal.appendChild(head);

  const stepper = el("div", "stepper");
  const stepRow = el("div", "stepper-steps");
  stepper.appendChild(stepRow);
  modal.appendChild(stepper);

  const body = el("div", "modal-body");
  modal.appendChild(body);
  root.appendChild(modal);
  root.hidden = false;

  function close() {
    root.hidden = true;
    root.innerHTML = "";
    document.removeEventListener("keydown", onKey);
  }
  function onKey(e) { if (e.key === "Escape" && !st.busy) close(); }
  document.addEventListener("keydown", onKey);
  closeBtn.onclick = close;
  root.onclick = e => { if (e.target === root && !st.busy) close(); };

  function drawStepper() {
    const at = STEPS.indexOf(st.step);
    stepRow.innerHTML = "";
    STEPS.forEach((key, i) => {
      const s = el("div", "st-step" + (i < at ? " done" : i === at ? " on" : ""));
      const node = el("div", "st-node");
      if (i < at) node.appendChild(el("span", "st-tick", "✓"));
      s.appendChild(node);
      s.appendChild(el("div", "st-label", STEP_LABEL[key]));
      stepRow.appendChild(s);
    });
  }

  function goto(step) { st.step = step; render(); }

  function render() {
    drawStepper();
    body.innerHTML = "";
    if (st.step === "details") drawDetails();
    else if (st.step === "features") drawFeatures();
    else drawReview();
  }

  /* ------------------------------ details ------------------------------ */
  function drawDetails() {
    body.appendChild(el("div", "modal-lab", "Name"));
    const nameRow = el("div", "modal-row");
    const name = el("input");
    name.type = "text";
    name.maxLength = 120;
    name.placeholder = "e.g. GWAS pilot";
    name.value = st.name;
    name.oninput = () => { st.name = name.value; };
    nameRow.appendChild(name);
    body.appendChild(nameRow);

    body.appendChild(el("div", "modal-lab", "Description"));
    const descRow = el("div", "modal-row");
    const desc = el("textarea");
    desc.rows = 3;
    desc.placeholder = 'What is being curated, plus any standing instruction — e.g. "Prefer the replication p-value where both are reported."';
    desc.value = st.description;
    desc.oninput = () => { st.description = desc.value; };
    descRow.appendChild(desc);
    body.appendChild(descRow);
    // Not a subtitle: this text is sent to the model alongside every feature
    // description, so the manager needs to know it is doing work.
    body.appendChild(el("div", "modal-hint",
      "The name and description are sent to the model as part of the prompt, alongside each feature's own description."));

    body.appendChild(el("div", "modal-lab", "Papers"));
    const pol = el("label", "fe-check");
    const cb = el("input");
    cb.type = "checkbox";
    cb.checked = st.allowNoPmid;
    cb.onchange = () => { st.allowNoPmid = cb.checked; };
    pol.appendChild(cb);
    pol.appendChild(el("span", null, "allow papers with no PubMed ID"));
    body.appendChild(pol);
    body.appendChild(el("div", "modal-hint",
      "Off by default: every paper needs a confirmed PMID. Turn it on and a curator may instead record a DOI, or that a paper has no external identifier at all."));

    const absPol = el("label", "fe-check");
    const absCb = el("input");
    absCb.type = "checkbox";
    absCb.checked = st.allowAbstractExtraction;
    absCb.onchange = () => { st.allowAbstractExtraction = absCb.checked; };
    absPol.appendChild(absCb);
    absPol.appendChild(el("span", null, "allow extraction on abstract"));
    body.appendChild(absPol);
    body.appendChild(el("div", "modal-hint",
      "Off by default: \"fetch by PMID\" fails if no open-access full-text PDF can be found. Turn it on and it falls back to the paper's abstract instead, so a curator still gets a lower-fidelity extraction rather than nothing."));

    body.appendChild(el("div", "modal-lab", "Start the feature set from"));
    const seedRow = el("div", "fe-seed");
    const status = el("div", "modal-status err");
    status.hidden = true;

    const blank = el("button", "btn", "Nothing — I'll define it");
    blank.onclick = () => { st.features = [blankLeaf([])]; st.carry = baseCarry(template); next(status); };
    seedRow.appendChild(blank);

    if (template) {
      const tpl = el("button", "btn", "The server's starter set");
      tpl.onclick = () => {
        st.features = JSON.parse(JSON.stringify(template.features || []));
        st.carry = baseCarry(template);
        next(status);
      };
      seedRow.appendChild(tpl);
    }

    const imp = el("label", "btn", "A shared configuration file");
    const file = el("input");
    file.type = "file";
    file.accept = ".json,application/json";
    file.hidden = true;
    imp.appendChild(file);
    imp.onclick = () => file.click();
    file.onchange = async () => {
      const f = file.files[0];
      file.value = "";
      if (!f) return;
      status.hidden = true;
      try {
        const doc = JSON.parse(await f.text());
        if (!Array.isArray(doc.features) || !doc.features.length) {
          throw new Error("that file has no features array");
        }
        // A shared document carries its author's name and description too.
        // Prefill them, but do not overwrite anything already typed.
        if (!st.name && doc.name) st.name = doc.name;
        if (!st.description && doc.description) st.description = doc.description;
        if (doc.settings && typeof doc.settings.allowPapersWithoutPmid === "boolean") {
          st.allowNoPmid = doc.settings.allowPapersWithoutPmid;
        }
        if (doc.settings && typeof doc.settings.allowExtractionOnAbstract === "boolean") {
          st.allowAbstractExtraction = doc.settings.allowExtractionOnAbstract;
        }
        st.features = doc.features;
        st.carry = baseCarry(doc);
        next(status);
      } catch (err) {
        status.textContent = `Could not read that file: ${err.message}`;
        status.hidden = false;
      }
    };
    seedRow.appendChild(imp);

    body.appendChild(seedRow);
    body.appendChild(status);
  }

  function next(status) {
    if (!st.name.trim()) {
      status.textContent = "A project needs a name.";
      status.hidden = false;
      return;
    }
    goto("features");
  }

  /* ------------------------------ features ----------------------------- */
  function drawFeatures() {
    body.appendChild(el("div", "modal-hint",
      "First define values that apply once to the whole paper. Then, if needed, add one repeating group for entries such as variants. After PDFs are loaded, AI identifies those entries in pass 1; the assigned curator reviews them before pass 2 extracts these fields."));

    body.appendChild(sheetToolbar());

    const host = el("div", "fe-list");
    body.appendChild(host);
    renderFeatureEditor(host, {
      features: st.features,
      onChange: () => { st.validation = null; scheduleValidate(); },
    });

    const live = el("div", "fe-live");
    live.id = "feLive";
    body.appendChild(live);

    const acts = el("div", "step-actions");
    const back = el("button", "btn", "← Back");
    back.onclick = () => goto("details");
    const fwd = el("button", "btn primary", "Review →");
    fwd.onclick = async () => {
      const v = await validate();
      if (v.ok) goto("review");
    };
    acts.appendChild(back);
    acts.appendChild(fwd);
    body.appendChild(acts);

    scheduleValidate();
  }

  // A spreadsheet is a THIRD way to seed the feature list, alongside blank /
  // template / imported config — it just arrives one step later, since a
  // manager who already started editing by hand may still want to switch.
  // The parsed result is the exact shape the editor already renders, so
  // swapping it in is a full re-render, nothing bespoke.
  function sheetToolbar() {
    const bar = el("div", "fe-sheet-bar");

    const dl = el("a", "btn", "Download spreadsheet template");
    dl.href = api.featureTemplateUrl();
    dl.download = "annotaid_feature_template.csv";
    bar.appendChild(dl);

    const up = el("label", "btn", "Upload filled-in spreadsheet");
    const file = el("input");
    file.type = "file";
    file.accept = ".csv,text/csv";
    file.hidden = true;
    up.appendChild(file);
    bar.appendChild(up);

    bar.appendChild(el("div", "fe-sheet-hint",
      'One row per field. To describe a repeating group (e.g. one row per genetic variant), add a row with Type "group" first, then put its name in the Group column of the fields underneath it.'));

    const err = el("div", "modal-status err fe-sheet-err");
    err.hidden = true;
    bar.appendChild(err);

    file.onchange = async () => {
      const f = file.files[0];
      file.value = "";
      if (!f) return;
      err.hidden = true;

      // A meaningful head start (more than the single auto-named placeholder
      // row a blank project starts with) is worth protecting.
      const hasWork = st.features.length > 1
        || (st.features[0] && st.features[0].name !== "new_field");
      if (hasWork && !window.confirm(
        `Replace the current ${st.features.length} feature(s) with the uploaded spreadsheet?`
      )) return;

      let res;
      try {
        res = await api.parseFeatureSheet(await f.text());
      } catch (e) {
        err.textContent = `Could not read that file: ${e.message}`;
        err.hidden = false;
        return;
      }
      if (!res.ok) {
        // The row number and the fix are already in the message (SheetError /
        // FeatureError were written to be read by whoever is filling in a
        // spreadsheet, not a developer).
        err.textContent = res.error;
        err.hidden = false;
        return;
      }
      st.features = res.features;
      st.validation = null;
      render();   // full re-render: the editor, the live validator, all of it
    };

    return bar;
  }

  let vTimer;
  function scheduleValidate() {
    clearTimeout(vTimer);
    vTimer = setTimeout(() => validate().catch(() => {}), 400);
  }

  async function validate() {
    const live = document.getElementById("feLive");
    let res;
    try {
      res = await api.validateConfig(st.name || "Untitled project", st.description,
                                     draftConfig());
    } catch (err) {
      res = { ok: false, error: err.message };
    }
    st.validation = res;
    if (live) {
      live.className = "fe-live " + (res.ok ? "ok" : "bad");
      live.textContent = res.ok
        ? `${res.paths.length} value(s) per paper` +
          (res.groupCount ? `, across ${res.groupCount} repeating group(s)` : "")
        : res.error;
    }
    return res;
  }

  /* ------------------------------- review ------------------------------ */
  function drawReview() {
    const v = st.validation;
    body.appendChild(el("div", "modal-lab", st.name));
    if (st.description) body.appendChild(el("div", "fe-review-desc", st.description));

    const facts = el("div", "fe-facts");
    facts.appendChild(fact(v && v.paths ? v.paths.length : 0, "values per paper"));
    facts.appendChild(fact(v ? v.groupCount : 0, "repeating groups"));
    facts.appendChild(fact(st.allowNoPmid ? "allowed" : "required",
                           "PubMed ID"));
    facts.appendChild(fact(st.allowAbstractExtraction ? "allowed" : "off",
                           "abstract extraction"));
    body.appendChild(facts);

    // The prompt is the thing the project actually does. Showing it here means
    // the manager sees the consequence of their descriptions before any money
    // is spent on a call.
    body.appendChild(el("div", "modal-lab", "Prompt the model will receive"));
    const pre = el("pre", "fe-prompt");
    pre.textContent = (v && v.promptPreview) || "(unavailable)";
    body.appendChild(pre);

    const status = el("div", "modal-status err");
    status.hidden = true;
    body.appendChild(status);

    const acts = el("div", "step-actions");
    const back = el("button", "btn", "← Back");
    back.onclick = () => goto("features");
    const create = el("button", "btn primary", "Create project");
    create.onclick = async () => {
      status.hidden = true;
      st.busy = true;
      create.disabled = back.disabled = true;
      create.textContent = "Creating…";
      try {
        const p = await api.createProject(st.name.trim(), st.description.trim(),
                                          draftConfig());
        close();
        onCreated(p);
      } catch (err) {
        st.busy = false;
        create.disabled = back.disabled = false;
        create.textContent = "Create project";
        status.textContent = err.message || "Could not create the project.";
        status.hidden = false;
      }
    };
    acts.appendChild(back);
    acts.appendChild(create);
    body.appendChild(acts);
  }

  function fact(value, label) {
    const w = el("div", "pc-stat");
    w.appendChild(el("span", "v", String(value)));
    w.appendChild(el("span", "l", label));
    return w;
  }

  function draftConfig() {
    return {
      ...st.carry,
      features: cleanFeatures(st.features),
      settings: { ...(st.carry.settings || {}),
                  allowPapersWithoutPmid: st.allowNoPmid,
                  allowExtractionOnAbstract: st.allowAbstractExtraction },
    };
  }

  render();
}

// Everything a project config needs that this editor does not edit. AI models
// and PDF processing are intentionally absent from the project workflow; the
// superadmin manages those once for the server.
function baseCarry(doc) {
  const d = doc || {};
  return {
    prompts: d.prompts ? JSON.parse(JSON.stringify(d.prompts)) : [],
    export: d.export ? JSON.parse(JSON.stringify(d.export)) : undefined,
    settings: d.settings ? { ...d.settings } : {},
  };
}
