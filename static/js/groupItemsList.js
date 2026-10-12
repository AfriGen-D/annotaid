// Shared repeat-group identifier editor. Pass 1 supplies {label, evidence}; the
// curator can correct the label before approving the list for pass 2.
//
// `items` is a live array of {rowId, label, evidence?} — mutated in place, like every
// other editor in this codebase (featureSchemaEditor.js, groupEditor.js).
import { el } from "./dom.js";

function itemKey(label) {
  return String(label || "").trim().replace(/\s+/g, " ").toLocaleLowerCase();
}

export function renderGroupItemsList(items, {
  onChange, onRemove, onDuplicate, onActivateEvidence,
} = {}) {
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
      input.onblur = () => {
        item.label = input.value.trim();
        const key = itemKey(item.label);
        const duplicate = key && items.some((other, at) => at !== i && itemKey(other.label) === key);
        if (duplicate) {
          const label = item.label;
          items.splice(i, 1);
          draw();
          if (onDuplicate) onDuplicate(label);
        }
        if (onChange) onChange();
      };
      input.onkeydown = e => {
        if (e.key === "Enter") { e.preventDefault(); input.blur(); addRow(); }
      };
      row.appendChild(input);

      if (item.evidence) {
        const quote = el("button", "gi-evidence", `“${item.evidence}”`);
        quote.type = "button";
        quote.title = "Locate this evidence in the paper";
        quote.onclick = () => onActivateEvidence && onActivateEvidence(item, i);
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
    if (itemKey(label) && items.some(item => itemKey(item.label) === itemKey(label))) {
      if (onDuplicate) onDuplicate(label);
      return;
    }
    items.push({ rowId: null, label, evidence: null });
    draw();
    const inputs = wrap.querySelectorAll("input");
    if (inputs.length) inputs[inputs.length - 1].focus();
  }

  draw();
  return { el: wrap, addRow, redraw: draw };
}
