// Shared repeat-group identifier editor. Pass 1 supplies {label, evidence}; the
// curator can correct the label before approving the list for pass 2.
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

      if (item.evidence) {
        const quote = el("div", "gi-evidence", `“${item.evidence}”`);
        quote.title = "Evidence found during identifier discovery";
        row.appendChild(quote);
      }

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
    items.push({ rowId: null, label, evidence: null });
    draw();
    const inputs = wrap.querySelectorAll("input");
    if (inputs.length) inputs[inputs.length - 1].focus();
  }

  draw();
  return { el: wrap, addRow, redraw: draw };
}
