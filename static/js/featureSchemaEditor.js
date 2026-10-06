// Visual editor for a project's feature schema.
//
// Two levels: paper-level features, and repeating groups whose fields are
// curated once per entry (one per genetic variant, say). A group inside a group
// is rejected by the server, so this editor does not offer it.
//
// Deliberately NOT validating here. The rules — types, identifiers, name shape,
// the nesting cap — live in server/features.py, and a second implementation in
// JavaScript would drift until the editor accepted a config the server refuses.
// The caller posts the draft to /api/validate-config and shows what comes back.
//
// Rerender discipline: structural changes (add / remove / reorder / change type)
// rebuild the list; typing does not. An oninput that rerendered would take the
// caret with it.
import { el } from "./dom.js";

const LEAF_TYPES = [
  ["string", "text"],
  ["number", "number"],
  ["boolean", "true / false"],
  ["enum", "one of a fixed list"],
  ["array<string>", "list of text"],
  ["array<number>", "list of numbers"],
];

// New features arrive already NAMED. An empty name is a real config error, so a
// blank one would flash a red "feature #7 has no name" the instant the manager
// clicks Add — an error they did not make, before they had a chance to type.
// Auto-naming keeps a draft valid at every moment, and the server stays the only
// validator (no client-side notion of "incomplete but not wrong").
function uniqueName(base, siblings) {
  const taken = new Set(siblings.map(f => f.name));
  if (!taken.has(base)) return base;
  for (let i = 2; ; i++) if (!taken.has(`${base}_${i}`)) return `${base}_${i}`;
}

export function blankLeaf(siblings = []) {
  return { name: uniqueName("new_field", siblings), type: "string",
           description: "", nullable: true };
}

export function blankGroup(siblings = []) {
  return {
    name: uniqueName("new_group", siblings),
    type: "group",
    description: "",
    maxItems: 50,
    // Pass 1 discovers row identity separately. These are only the values that
    // pass 2 should curate for each reviewed entry.
    features: [blankLeaf()],
  };
}

/**
 * host      element to render into
 * features  the live array (mutated in place)
 * onChange  called after every edit, structural or textual
 */
export function renderFeatureEditor(host, { features, onChange }) {
  const fire = () => onChange && onChange(features);

  function draw() {
    host.innerHTML = "";

    if (!features.length) {
      host.appendChild(el("div", "fe-empty",
        "No features yet. Add the values a curator should record for each paper."));
    }

    host.appendChild(el("div", "fe-sub-lab", "Paper-level features"));
    host.appendChild(el("div", "modal-hint",
      "Each of these values is recorded once for the paper."));
    features.forEach((f, i) => {
      if (f.type !== "group") host.appendChild(leafCard(f, i, null));
    });

    const paperActions = el("div", "fe-actions");
    const addF = el("button", "btn", "+ Feature");
    addF.onclick = () => { features.push(blankLeaf(features)); fire(); draw(); };
    paperActions.appendChild(addF);
    host.appendChild(paperActions);

    host.appendChild(el("div", "fe-sub-lab", "Repeating group (optional)"));
    host.appendChild(el("div", "modal-hint",
      "Use this for a list such as genetic variants. AI discovers the entry IDs first; define only the values to curate for every reviewed entry."));
    features.forEach((f, i) => {
      if (f.type === "group") host.appendChild(groupCard(f, i));
    });

    const groupActions = el("div", "fe-actions");
    const addG = el("button", "btn", "+ Repeating group");
    // At most one per project (server/features.parse_features) — its rows are
    // identified by what the curator declares per paper, not by a feature, so
    // a second group would just be a second unrelated "which rows exist"
    // question with no way to answer it differently.
    const hasGroup = features.some(f => f.type === "group");
    addG.disabled = hasGroup;
    addG.title = hasGroup
      ? "A project may have at most one repeating group"
      : "A set of fields curated once per entry — e.g. one per variant";
    addG.onclick = () => { features.push(blankGroup(features)); fire(); draw(); };
    groupActions.appendChild(addG);
    host.appendChild(groupActions);
  }

  // ---- shared bits --------------------------------------------------------
  function textInput(obj, key, placeholder, cls) {
    const inp = el("input", cls || "fe-input");
    inp.type = "text";
    inp.value = obj[key] || "";
    inp.placeholder = placeholder;
    inp.oninput = () => { obj[key] = inp.value; fire(); };   // no redraw: keep the caret
    return inp;
  }

  function descBox(obj) {
    const ta = el("textarea", "fe-desc");
    ta.rows = 2;
    ta.value = obj.description || "";
    ta.placeholder = "What should the model look for? This text is sent to it verbatim.";
    ta.oninput = () => { obj.description = ta.value; fire(); };
    return ta;
  }

  function moveControls(list, i, redraw) {
    const wrap = el("div", "fe-move");
    const up = el("button", "fe-icon", "↑");
    const down = el("button", "fe-icon", "↓");
    const del = el("button", "fe-icon fe-del", "×");
    up.title = "Move up"; down.title = "Move down"; del.title = "Remove";
    up.disabled = i === 0;
    down.disabled = i === list.length - 1;
    up.onclick = () => { [list[i - 1], list[i]] = [list[i], list[i - 1]]; fire(); redraw(); };
    down.onclick = () => { [list[i + 1], list[i]] = [list[i], list[i + 1]]; fire(); redraw(); };
    del.onclick = () => { list.splice(i, 1); fire(); redraw(); };
    wrap.appendChild(up); wrap.appendChild(down); wrap.appendChild(del);
    return wrap;
  }

  // The top level is displayed in two semantic sections even though it is one
  // config array. Reorder within a section so an imported group cannot make a
  // paper-level arrow appear to do nothing by swapping across that boundary.
  function topLevelControls(i, redraw) {
    const kind = features[i].type === "group" ? "group" : "leaf";
    const peers = features.map((f, idx) => ({ f, idx })).filter(x =>
      (x.f.type === "group" ? "group" : "leaf") === kind);
    const at = peers.findIndex(x => x.idx === i);
    const wrap = el("div", "fe-move");
    const up = el("button", "fe-icon", "↑");
    const down = el("button", "fe-icon", "↓");
    const del = el("button", "fe-icon fe-del", "×");
    up.title = "Move up"; down.title = "Move down"; del.title = "Remove";
    up.disabled = at <= 0;
    down.disabled = at < 0 || at === peers.length - 1;
    up.onclick = () => {
      const j = peers[at - 1].idx;
      [features[j], features[i]] = [features[i], features[j]]; fire(); redraw();
    };
    down.onclick = () => {
      const j = peers[at + 1].idx;
      [features[j], features[i]] = [features[i], features[j]]; fire(); redraw();
    };
    del.onclick = () => { features.splice(i, 1); fire(); redraw(); };
    wrap.append(up, down, del);
    return wrap;
  }

  function typeSelect(f, onTypeChange) {
    const sel = el("select", "fe-type");
    for (const [value, label] of LEAF_TYPES) {
      const o = el("option", null, label);
      o.value = value;
      if (f.type === value) o.selected = true;
      sel.appendChild(o);
    }
    sel.onchange = () => {
      f.type = sel.value;
      // enumValues only means something for an enum — the server rejects it
      // elsewhere, so drop it rather than leave a landmine in the document.
      if (f.type !== "enum") delete f.enumValues;
      else if (!f.enumValues) f.enumValues = [];
      fire();
      onTypeChange();
    };
    return sel;
  }

  function enumRow(f) {
    const row = el("div", "fe-row");
    row.appendChild(el("span", "fe-lab", "Allowed values"));
    const inp = el("input", "fe-input");
    inp.type = "text";
    inp.value = (f.enumValues || []).join(", ");
    inp.placeholder = "e.g. A, C, G, T";
    inp.oninput = () => {
      f.enumValues = inp.value.split(/[,\n]/).map(s => s.trim()).filter(Boolean);
      fire();
    };
    row.appendChild(inp);
    return row;
  }

  function nullableRow(f) {
    const lab = el("label", "fe-check");
    const cb = el("input");
    cb.type = "checkbox";
    cb.checked = f.nullable !== false;
    cb.onchange = () => { f.nullable = cb.checked; fire(); };
    lab.appendChild(cb);
    lab.appendChild(el("span", null, "may be absent from the paper"));
    return lab;
  }

  // ---- a single value ----------------------------------------------------
  function leafCard(f, i, group) {
    const list = group ? group.features : features;
    const redraw = group ? () => drawGroupBody(group) : draw;
    const card = el("div", "fe-card" + (group ? " nested" : ""));

    const head = el("div", "fe-head");
    head.appendChild(textInput(f, "name", "field_name", "fe-input fe-name"));
    head.appendChild(textInput(f, "label", "Display name (optional)", "fe-input fe-label"));
    head.appendChild(typeSelect(f, redraw));
    head.appendChild(group ? moveControls(list, i, redraw) : topLevelControls(i, redraw));
    card.appendChild(head);

    card.appendChild(descBox(f));

    const opts = el("div", "fe-opts");
    opts.appendChild(nullableRow(f));
    card.appendChild(opts);

    if (f.type === "enum") card.appendChild(enumRow(f));
    return card;
  }

  // ---- a repeating group -------------------------------------------------
  function drawGroupBody(g) {
    const body = g._body;
    if (!body) return;
    body.innerHTML = "";
    g.features.forEach((c, i) => body.appendChild(leafCard(c, i, g)));
    const add = el("button", "btn fe-add-field", "+ Field");
    add.onclick = () => { g.features.push(blankLeaf(g.features)); fire(); drawGroupBody(g); };
    body.appendChild(add);
  }

  function groupCard(g, i) {
    const card = el("div", "fe-card fe-group");

    const head = el("div", "fe-head");
    head.appendChild(textInput(g, "name", "group_name", "fe-input fe-name"));
    head.appendChild(textInput(g, "label", "Display name (optional)", "fe-input fe-label"));
    head.appendChild(el("span", "fe-type-fixed", "repeating group"));
    head.appendChild(topLevelControls(i, draw));
    card.appendChild(head);

    card.appendChild(descBox(g));

    const opts = el("div", "fe-opts");
    const cap = el("label", "fe-check");
    cap.appendChild(el("span", null, "at most"));
    const num = el("input", "fe-num");
    num.type = "number";
    num.min = "1";
    num.value = g.maxItems || 50;
    num.oninput = () => { g.maxItems = Math.max(1, parseInt(num.value, 10) || 1); fire(); };
    cap.appendChild(num);
    cap.appendChild(el("span", null, "entries per paper"));
    cap.title = "Caps what one extraction call will accept, so a long supplementary table can't blow the response. You can still add entries by hand.";
    opts.appendChild(cap);
    card.appendChild(opts);

    card.appendChild(el("div", "fe-sub-lab", "Values curated for each reviewed entry"));
    const body = el("div", "fe-sub");
    g._body = body;          // stashed so drawGroupBody can find it
    card.appendChild(body);
    drawGroupBody(g);
    return card;
  }

  draw();
}

// `_body` is a DOM node stashed on the model while editing; it must never reach
// the server (or JSON.stringify, which would throw on the circular node).
export function cleanFeatures(features) {
  return features.map(f => {
    const out = {};
    for (const [k, v] of Object.entries(f)) {
      if (k.startsWith("_")) continue;
      if (k === "label" && !v) continue;
      if (k === "features") { out.features = cleanFeatures(v); continue; }
      out[k] = v;
    }
    return out;
  });
}
