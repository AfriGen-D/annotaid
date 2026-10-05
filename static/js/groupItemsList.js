// Shared "list of declared ids" editor — the curator's own row identity for a
// project's one repeating group (server/project_store.load_group_items). Used
// both in the curation pane (main.js, one paper at a time) and in the "Add
// Paper(s)" stepper (addPapersModal.js, one per paper being added) — the
// widget itself has no opinion on when its edits get saved, that is entirely
// the caller's job via onChange.
//
// `items` is a live array of {rowId, label} — mutated in place, like every
// other editor in this codebase (featureSchemaEditor.js, groupEditor.js).
import { el } from "./dom.js";

export function renderGroupItemsList(items, { onChange, onRemove } = {}) {
  const wrap = el("div", "gi-list");

  function draw() {
    wrap.innerHTML = "";
    items.forEach((item, i) => {
      const row = el("div", "gi-row");
      const input = el("input");
      input.type = "text";
      input.value = item.label;
      input.placeholder = "e.g. rs1800544";
      input.oninput = () => { item.label = input.value; };
      input.onblur = () => onChange && onChange();
      input.onkeydown = e => {
        if (e.key === "Enter") { e.preventDefault(); input.blur(); addRow(); }
      };
      row.appendChild(input);

      const del = el("button", "fe-icon fe-del", "×");
      del.title = "Remove this entry";
      del.onclick = () => {
        if (onRemove && onRemove(item, i) === false) return;
        items.splice(i, 1);
        draw();
        onChange && onChange();
      };
      row.appendChild(del);
      wrap.appendChild(row);
    });
  }

  function addRow(label = "") {
    items.push({ rowId: null, label });
    draw();
    const inputs = wrap.querySelectorAll("input");
    if (inputs.length) inputs[inputs.length - 1].focus();
  }

  draw();
  return { el: wrap, addRow, redraw: draw };
}
