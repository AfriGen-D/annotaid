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
  onChange, onRemove, onBulkRemove, onDuplicate, onActivateEvidence,
} = {}) {
  const wrap = el("div", "gi-list");
  const selected = new Set();

  function draw() {
    wrap.innerHTML = "";
    for (const item of selected) if (!items.includes(item)) selected.delete(item);

    if (items.length) {
      const tools = el("div", "gi-bulk-tools");
      const selectLabel = el("label", "gi-select-all");
      const selectAll = el("input");
      selectAll.type = "checkbox";
      selectAll.checked = selected.size === items.length;
      selectAll.indeterminate = selected.size > 0 && selected.size < items.length;
      selectAll.onchange = () => {
        selected.clear();
        if (selectAll.checked) items.forEach(item => selected.add(item));
        draw();
      };
      selectLabel.appendChild(selectAll);
      selectLabel.appendChild(el("span", null, "Select all"));
      tools.appendChild(selectLabel);

      const remove = el("button", "btn gi-remove-selected",
        `Remove selected (${selected.size})`);
      remove.type = "button";
      remove.disabled = selected.size === 0;
      remove.onclick = async () => {
        const chosen = items.filter(item => selected.has(item));
        if (!chosen.length || (onBulkRemove && await onBulkRemove(chosen) === false)) return;
        for (let i = items.length - 1; i >= 0; i--) {
          if (selected.has(items[i])) items.splice(i, 1);
        }
        selected.clear();
        draw();
        onChange && onChange();
      };
      tools.appendChild(remove);
      wrap.appendChild(tools);
    }

    items.forEach((item, i) => {
      const row = el("div", "gi-row");
      const choose = el("input", "gi-select");
      choose.type = "checkbox";
      choose.checked = selected.has(item);
      choose.setAttribute("aria-label", `Select ${item.label || `entry ${i + 1}`} for removal`);
      choose.onchange = () => {
        if (choose.checked) selected.add(item);
        else selected.delete(item);
        draw();
      };
      row.appendChild(choose);
      const input = el("input");
      input.type = "text";
      input.className = "gi-label-input";
      input.value = item.label;
      input.placeholder = "e.g. rs1800544";
      input.oninput = () => { item.label = input.value; };
      input.onblur = () => {
        item.label = input.value.trim();
        const key = itemKey(item.label);
        const duplicate = key && items.some((other, at) => at !== i && itemKey(other.label) === key);
        if (duplicate) {
          const label = item.label;
          selected.delete(item);
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
      del.onclick = async () => {
        if (onRemove && await onRemove(item, i) === false) return;
        selected.delete(item);
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
    const inputs = wrap.querySelectorAll(".gi-label-input");
    if (inputs.length) inputs[inputs.length - 1].focus();
  }

  draw();
  return { el: wrap, addRow, redraw: draw };
}
