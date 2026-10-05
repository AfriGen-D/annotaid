// Curation UI for a repeating group: the rows themselves.
//
// Each row reuses renderFeature for its fields, so a value inside a variant
// behaves exactly like a paper-level value — same widgets, same "AI proposed …
// corrected" audit line, same confirm tick. The only new ideas here are at row
// level: where a row came from, and what happens when the curator rejects one.
//
// A rejected AI row is NOT erased. It was a model claim, so deleting it is a
// false positive worth counting; it collapses into a "removed" shelf and can be
// restored. A row the curator added themselves has no such history and simply
// goes away.
import { el } from "./dom.js";
import { renderFeature } from "./featureEditor.js";

// A row's name is the curator's own declared identity for it (main.js passes
// ctx.groupItemLabels, built from the paper's groupItems — server/project_
// store.load_group_items), looked up by rowId. The identifier-feature scan
// below is only a fallback, for a curator-added row not yet saved (rowId
// still null) or an older config that still marks a feature `identifier`.
function rowLabel(gdef, row, ctx) {
  const declared = ctx && ctx.groupItemLabels && ctx.groupItemLabels[row.rowId];
  if (declared) return declared;
  for (const f of gdef.features) {
    if (!f.identifier) continue;
    const v = row.features[f.name] && row.features[f.name].value;
    if (v !== null && v !== undefined && v !== "" && !(Array.isArray(v) && !v.length)) {
      return String(v);
    }
  }
  return "(unidentified)";
}

function rowProgress(row) {
  const cells = Object.values(row.features || {});
  return `${cells.filter(c => c.confirmed).length}/${cells.length}`;
}

/**
 * gdef   the group definition from the project config
 * gnode  { present, aiPresent, confirmed, rows: [...] } — mutated in place
 * ctx    { openRowId, onOpenRow, onEdit, onStructureChange, onActivateEvidence }
 */
export function renderGroup(gdef, gnode, ctx) {
  const wrap = el("div", "grp");
  wrap.dataset.group = gdef.name;

  const live = (gnode.rows || []).filter(r => !r.deletedAt);
  // `_purge` marks a curator-added entry on its way out. It has to stay in the
  // array long enough to reach the server as deleted:true — the server only
  // acts on rows the payload mentions, so splicing it out locally would leave
  // it alive on the server forever. It is kept off the removed shelf because
  // there is no claim to preserve: it was never the model's.
  const removed = (gnode.rows || []).filter(r => r.deletedAt && !r._purge);

  const head = el("div", "grp-head");
  head.appendChild(el("span", "grp-name", gdef.label || gdef.name));
  head.appendChild(el("span", "grp-count",
    live.length ? `${live.length} ${live.length === 1 ? "entry" : "entries"}` : "none"));
  head.appendChild(el("span", "spacer"));
  const add = el("button", "btn grp-add", "+ Add entry");
  add.disabled = live.length >= (gdef.maxItems || 50);
  add.title = add.disabled
    ? `This group is capped at ${gdef.maxItems} entries`
    : "Add an entry the AI missed";
  // A new entry needs its identity DECLARED (server/project_store.
  // load_group_items), the same as one entered before extraction — it just
  // happens here instead, after the curator noticed the AI's declared list was
  // missing one. ctx.onDeclareItem does the round-trip and hands back the real
  // rowId; skip it (rowId stays null) if this project's group predates the
  // declared-list feature and still uses an AI-extracted identifier field.
  add.onclick = async () => {
    let rowId = null;
    if (ctx.onDeclareItem) {
      const label = window.prompt("Identify this entry (e.g. a variant or haplotype id):", "");
      if (label === null) return;
      const trimmed = label.trim();
      if (!trimmed) return;
      try {
        rowId = await ctx.onDeclareItem(trimmed);
      } catch (err) {
        return;  // ctx.onDeclareItem already surfaced the error (e.g. a toast)
      }
    }
    const blank = { rowId, origin: "curator", deletedAt: null, features: {} };
    for (const f of gdef.features) {
      blank.features[f.name] = {
        type: f.type, aiValue: emptyFor(f), value: emptyFor(f), present: false,
        evidence: null, evidenceMatch: "none", confirmed: false,
        editedAt: null, confirmedAt: null,
      };
    }
    (gnode.rows = gnode.rows || []).push(blank);
    gnode.present = true;
    ctx.onStructureChange();
  };
  head.appendChild(add);
  wrap.appendChild(head);

  if (gdef.description) wrap.appendChild(el("div", "grp-desc", gdef.description));

  if (!live.length) {
    wrap.appendChild(el("div", "grp-none",
      gnode.present ? "No entries yet." : "The AI found none in this paper."));
  }

  for (const row of live) wrap.appendChild(rowCard(gdef, gnode, row, ctx));

  if (removed.length) {
    const shelf = el("details", "grp-removed");
    const sum = el("summary", null,
      `${removed.length} removed — kept as a record of what the AI proposed`);
    shelf.appendChild(sum);
    for (const row of removed) {
      const line = el("div", "grp-removed-row");
      line.appendChild(el("span", "rl-name", rowLabel(gdef, row, ctx)));
      line.appendChild(el("span", "rl-origin", row.origin === "ai" ? "AI" : "you"));
      const undo = el("button", "btn grp-undo", "Restore");
      undo.onclick = () => { row.deletedAt = null; ctx.onStructureChange(); };
      line.appendChild(undo);
      shelf.appendChild(line);
    }
    wrap.appendChild(shelf);
  }

  // Group-level facts, which are curation work in their own right: "I checked,
  // and this paper reports none" is a different claim from "nobody has looked".
  const foot = el("div", "grp-foot");
  const none = el("label", "fe-check");
  const cb = el("input");
  cb.type = "checkbox";
  cb.checked = !gnode.present;
  cb.disabled = live.length > 0;
  cb.title = live.length ? "Remove the entries first" : "";
  cb.onchange = () => { gnode.present = !cb.checked; ctx.onEdit(); };
  none.appendChild(cb);
  none.appendChild(el("span", null, "this paper reports none"));
  foot.appendChild(none);

  const conf = el("label", "confirm" + (gnode.confirmed ? " on" : ""));
  const ccb = el("input");
  ccb.type = "checkbox";
  ccb.checked = !!gnode.confirmed;
  ccb.onchange = () => {
    gnode.confirmed = ccb.checked;
    conf.classList.toggle("on", ccb.checked);
    ctx.onEdit();
  };
  conf.appendChild(ccb);
  conf.appendChild(document.createTextNode(" list is complete"));
  // The AI claiming entries where the curator finds none is itself the finding,
  // so say so rather than quietly overwriting the claim.
  if (gnode.aiPresent && !gnode.present) {
    foot.appendChild(el("span", "grp-diverge", "AI proposed entries here"));
  }
  foot.appendChild(el("span", "spacer"));
  foot.appendChild(conf);
  wrap.appendChild(foot);

  return wrap;
}

function rowCard(gdef, gnode, row, ctx) {
  const open = ctx.openRowId === row.rowId ||
               (row.rowId === null && ctx.openRowId === "__new__");
  const card = el("div", "grp-row" + (open ? " open" : ""));

  const head = el("div", "grp-row-head");
  head.appendChild(el("span", "rl-caret", open ? "▾" : "▸"));
  const nameEl = el("span", "rl-name", rowLabel(gdef, row, ctx));
  head.appendChild(nameEl);
  if (row.origin === "curator") {
    const b = el("span", "rl-origin you", "added by you");
    b.title = "The AI did not propose this entry";
    head.appendChild(b);
  }
  head.appendChild(el("span", "spacer"));
  const prog = el("span", "rl-prog", rowProgress(row));
  head.appendChild(prog);
  const del = el("button", "fe-icon fe-del", "×");
  del.title = row.origin === "ai"
    ? "Reject this entry (kept as a record that the AI proposed it)"
    : "Remove this entry";
  del.onclick = e => {
    e.stopPropagation();
    row.deletedAt = new Date().toISOString();
    // The server soft-deletes an AI row (a rejected claim is a false positive
    // worth counting) and hard-deletes a curator one. Either way it must be
    // SENT, not just dropped from the local array.
    if (row.origin !== "ai") row._purge = true;
    ctx.onStructureChange();
  };
  head.appendChild(del);
  head.onclick = () => ctx.onOpenRow(open ? null : (row.rowId || "__new__"));
  card.appendChild(head);

  if (open) {
    const body = el("div", "grp-row-body");
    for (const fdef of gdef.features) {
      const cell = row.features[fdef.name];
      if (!cell) continue;
      body.appendChild(renderFeature(fdef, cell, {
        partial: false,
        onEdit: () => {
          // Refresh just this row's header rather than redrawing the group: a
          // redraw mid-edit would collapse the entry and take the caret with it.
          // The name matters as much as the count — editing an identifier
          // renames the entry, and a stale title is how you edit the wrong one.
          nameEl.textContent = rowLabel(gdef, row, ctx);
          prog.textContent = rowProgress(row);
          ctx.onEdit();
        },
        onActivateEvidence: () =>
          ctx.onActivateEvidence(`${gdef.name}.${fdef.name}@${row.rowId || "new"}`),
      }));
    }
    card.appendChild(body);
  }
  return card;
}

function emptyFor(fdef) {
  if (String(fdef.type).startsWith("array")) return [];
  if (fdef.type === "boolean") return fdef.nullable === false ? false : null;
  return null;
}
