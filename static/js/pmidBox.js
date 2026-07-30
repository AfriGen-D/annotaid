// PMID-resolution box (brief §5.2): prefill only if the filename stem is purely
// numeric; NEVER auto-commit — the curator must confirm.
import { el } from "./dom.js";

export function renderPmidBox(paper, { onConfirm }) {
  const box = el("div", "pmid-box");
  box.appendChild(el("div", "lab", "Confirm PubMed ID"));

  const row = el("div", "row");
  const input = el("input");
  input.type = "text";
  input.value = paper.suggestedPmid || "";   // empty unless numeric stem
  input.placeholder = "enter PMID…";
  input.inputMode = "numeric";
  const btn = el("button", "btn primary", "Confirm");
  row.appendChild(input); row.appendChild(btn);
  box.appendChild(row);

  const prefilled = !!paper.suggestedPmid;
  box.appendChild(el("div", "note", prefilled
    ? "Prefilled from the filename — verify it is a real PMID (not a PMC id, DOI, or a number with leading zeros) before confirming."
    : "The filename isn't numeric, so nothing was prefilled. Enter the PMID and confirm."));

  const commit = () => {
    const pmid = input.value.trim();
    if (!/^\d+$/.test(pmid)) { input.focus(); return; }
    // source records whether it matched the prefill or was typed manually
    const source = (prefilled && pmid === paper.suggestedPmid) ? "prefill-confirmed" : "manual";
    onConfirm(pmid, source);
  };
  btn.onclick = commit;
  input.onkeydown = e => { if (e.key === "Enter") commit(); };
  return box;
}
