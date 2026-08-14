// Type-adaptive feature editor. Renders one feature card:
//   editable widget (by type) + immutable "AI proposed" line + evidence + confirm.
// Mutates the passed run-feature object in place and calls ctx.onEdit(name).
// Editor widgets adapted from visual_evaluator.html (listEditor / bool-toggle / setWas).
import { el, escapeHtml } from "./dom.js";
import { helpIcon } from "./tooltip.js";

/* ---------- value helpers ---------- */
function normList(arr) {
  if (!Array.isArray(arr)) return [];
  return arr.map(x => String(x).toLowerCase().replace(/\s+/g, " ").trim()).filter(Boolean).sort();
}
function setsEqual(a, b) {
  const na = normList(a), nb = normList(b);
  return na.length === nb.length && na.every((v, i) => v === nb[i]);
}
function valueEqual(a, b, type) {
  if (type.startsWith("array")) return setsEqual(a, b);
  return JSON.stringify(a) === JSON.stringify(b);
}
function renderValue(v, type) {
  if (type === "boolean") return v == null ? "not reported" : String(v);
  if (type.startsWith("array")) return (Array.isArray(v) && v.length) ? v.join(", ") : "—";
  return (v == null || v === "") ? "—" : String(v);
}
function cleanList(arr) { return (arr || []).map(s => String(s).trim()).filter(Boolean); }

/* ---------- main render ---------- */
export function renderFeature(fdef, feat, ctx) {
  const group = el("div", "feat-group");
  group.dataset.feature = fdef.name;

  const head = el("div", "feat-head");
  head.appendChild(el("span", "feat-name", fdef.name));
  // schema description, on demand — same head is used by both list and card view
  if (fdef.description) head.appendChild(helpIcon(fdef.description, `What is “${fdef.name}”?`));
  head.appendChild(el("span", "feat-type", fdef.type));
  const pit = el("span", "pit " + (feat.present ? "t" : "f"), feat.present ? "present" : "not reported");
  head.appendChild(pit);
  group.appendChild(head);

  const markEdited = () => {
    pit.className = "pit " + (feat.present ? "t" : "f");
    pit.textContent = feat.present ? "present" : "not reported";
    refreshAudit();
    ctx.onEdit(fdef.name);
  };

  group.appendChild(buildWidget(fdef, feat, markEdited));

  // immutable AI-proposed line + change indicator (the audit signal)
  const audit = el("div", "ai-line");
  group.appendChild(audit);
  function refreshAudit() {
    const changed = !valueEqual(feat.value, feat.aiValue, fdef.type);
    audit.innerHTML =
      `AI proposed: <b>${escapeHtml(renderValue(feat.aiValue, fdef.type))}</b>` +
      (changed ? ` · <span style="color:var(--accent)">corrected</span>` : ` · unchanged`);
    const conf = group.querySelector(".confirm");
    if (conf) conf.classList.toggle("corrected", changed);
  }

  // evidence block (clickable) — the quote is always shown, match state declared
  if (feat.evidence) {
    const ev = el("div", "vev", "“" + feat.evidence + "”");
    ev.title = "Jump to evidence in the PDF";
    ev.onclick = () => ctx.onActivateEvidence(fdef.name);
    group.appendChild(ev);
  }

  // status row: match tag + confirm tick
  const status = el("div", "vstatus");
  status.appendChild(matchTag(feat.evidenceMatch, ctx.partial));
  const conf = el("label", "confirm" + (feat.confirmed ? " on" : ""));
  const cb = el("input"); cb.type = "checkbox"; cb.checked = !!feat.confirmed;
  conf.appendChild(cb);
  conf.appendChild(document.createTextNode(" confirmed"));
  cb.onchange = () => {
    feat.confirmed = cb.checked;
    conf.classList.toggle("on", cb.checked);
    refreshAudit();
    ctx.onEdit(fdef.name);
  };
  status.appendChild(conf);
  group.appendChild(status);

  refreshAudit();
  return group;
}

function matchTag(match, partial) {
  const label = match === "matched" ? (partial ? "partial match" : "located")
    : match === "unmatched" ? "not found in PDF"
    : "no quote";
  return el("span", "tag " + match, label);
}

/* ---------- widgets ---------- */
function buildWidget(fdef, feat, onChange) {
  const t = fdef.type;
  if (t === "boolean") return boolWidget(feat, onChange);
  if (t === "enum") return enumWidget(fdef, feat, onChange);
  if (t.startsWith("array")) return listWidget(fdef, feat, onChange);
  return scalarWidget(fdef, feat, onChange);  // string | number
}

function boolWidget(feat, onChange) {
  const wrap = el("div", "bool-toggle");
  const defs = [["true", true], ["false", false], ["n/a", null]];
  const btns = [];
  defs.forEach(([txt, val]) => {
    const b = el("button", null, txt);
    if (feat.value === val) b.classList.add("on");
    b.onclick = () => {
      feat.value = val;
      feat.present = val !== null;
      btns.forEach(x => x.classList.remove("on"));
      b.classList.add("on");
      onChange();
    };
    btns.push(b); wrap.appendChild(b);
  });
  return wrap;
}

function enumWidget(fdef, feat, onChange) {
  const sel = el("select", "enum-input");
  const na = el("option", null, "— not reported —"); na.value = "";
  sel.appendChild(na);
  (fdef.enumValues || []).forEach(v => {
    const o = el("option", null, v); o.value = v;
    if (feat.value === v) o.selected = true;
    sel.appendChild(o);
  });
  if (feat.value == null || feat.value === "") na.selected = true;
  sel.onchange = () => {
    feat.value = sel.value || null;
    feat.present = !!sel.value;
    onChange();
  };
  return sel;
}

function scalarWidget(fdef, feat, onChange) {
  const inp = el("input", "scalar-input");
  inp.type = fdef.type === "number" ? "number" : "text";
  inp.value = feat.value == null ? "" : feat.value;
  inp.placeholder = "— not reported —";
  inp.oninput = () => {
    const raw = inp.value.trim();
    if (raw === "") { feat.value = null; feat.present = false; }
    else if (fdef.type === "number") {
      const n = Number(raw); feat.value = Number.isNaN(n) ? feat.value : n; feat.present = true;
    } else { feat.value = raw; feat.present = true; }
    onChange();
  };
  return inp;
}

function listWidget(fdef, feat, onChange) {
  const isNum = fdef.type === "array<number>";
  if (!Array.isArray(feat.value)) feat.value = [];
  const box = el("div", "list-edit");

  function rebuild() {
    box.innerHTML = "";
    if (feat.value.length === 0) box.appendChild(el("div", "empty-hint", "— no values —"));
    feat.value.forEach((val, i) => {
      const row = el("div", "val-row");
      const inp = el("input", "val-input");
      inp.type = isNum ? "number" : "text";
      inp.value = val;
      inp.oninput = () => {
        feat.value[i] = isNum ? (inp.value === "" ? "" : Number(inp.value)) : inp.value;
        feat.present = cleanList(feat.value).length > 0;
        onChange();
      };
      inp.onkeydown = e => { if (e.key === "Enter") { e.preventDefault(); addAt(i + 1); } };
      const rm = el("button", "val-rm", "×"); rm.title = "Remove";
      rm.onclick = () => { feat.value.splice(i, 1); feat.present = cleanList(feat.value).length > 0; rebuild(); onChange(); };
      row.appendChild(inp); row.appendChild(rm); box.appendChild(row);
    });
    const add = el("button", "add-val", "+ Add value");
    add.onclick = () => addAt(feat.value.length);
    box.appendChild(add);
  }
  function addAt(index) {
    feat.value.splice(index, 0, isNum ? 0 : "");
    feat.present = true;
    rebuild();
    const inputs = box.querySelectorAll(".val-input");
    if (inputs[index]) inputs[index].focus();
    onChange();
  }
  rebuild();
  return box;
}
