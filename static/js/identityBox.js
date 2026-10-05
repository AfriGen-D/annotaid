// Paper-identity box (brief §5.2, extended for papers with no PubMed ID).
//
// Was pmidBox.js. A PMID prefill comes only from a purely numeric filename stem
// or from an id the curator named on the fetch tab; it is NEVER auto-committed.
//
// The second route exists because most papers have a PMID but not all do. Saying
// "this paper has no PubMed ID" is a real answer to "what is this paper?", not a
// way of skipping the question — so it unblocks curation just as a confirmed PMID
// does. Only the untouched 'pending' state blocks it.
import { el } from "./dom.js";

export function renderIdentityBox(paper, { onConfirm, onNoPmid }) {
  const box = el("div", "pmid-box");
  box.appendChild(el("div", "lab", "Identify this paper"));

  const row = el("div", "row");
  const input = el("input");
  input.type = "text";
  input.value = paper.suggestedPmid || "";   // empty unless numeric stem
  input.placeholder = "enter PMID…";
  input.inputMode = "numeric";
  const btn = el("button", "btn primary", "Confirm");
  row.appendChild(input); row.appendChild(btn);
  box.appendChild(row);

  // The prefill comes either from a numeric filename stem or from a PMID the
  // curator named on the fetch tab before falling back to a manual upload —
  // the wording has to be true for both.
  const prefilled = !!paper.suggestedPmid;
  const note = el("div", "note", prefilled
    ? "Prefilled — verify it is a real PMID for this paper (not a PMC id, DOI, or a number with leading zeros) before confirming."
    : "Nothing could be prefilled. Enter the PMID and confirm.");
  box.appendChild(note);

  const err = el("div", "pmid-err");
  err.hidden = true;
  box.appendChild(err);

  const fail = msg => { err.textContent = msg; err.hidden = false; };
  const clearFail = () => { err.hidden = true; };

  const commit = () => {
    clearFail();
    const pmid = input.value.trim();
    if (!/^\d+$/.test(pmid)) { fail("A PMID is all digits."); input.focus(); return; }
    // source records whether it matched the prefill or was typed manually
    const source = (prefilled && pmid === paper.suggestedPmid) ? "prefill-confirmed" : "manual";
    Promise.resolve(onConfirm(pmid, source)).catch(e => fail(e.message));
  };
  btn.onclick = commit;
  input.onkeydown = e => { if (e.key === "Enter") commit(); };

  // ---- the no-PMID route, collapsed until asked for ----
  const alt = el("div", "pmid-alt");
  const toggle = el("button", "linkish", "This paper has no PubMed ID");
  alt.appendChild(toggle);

  const altBody = el("div", "pmid-alt-body");
  altBody.hidden = true;
  altBody.appendChild(el("div", "lab", "DOI (optional)"));
  const doiRow = el("div", "row");
  const doiInput = el("input");
  doiInput.type = "text";
  doiInput.placeholder = "10.1038/s41588-024-01234";
  const doiBtn = el("button", "btn", "Save without a PMID");
  doiRow.appendChild(doiInput); doiRow.appendChild(doiBtn);
  altBody.appendChild(doiRow);
  altBody.appendChild(el("div", "note",
    "Leave the DOI blank if there isn't one — the paper will be listed by its filename. Either way you can start curating."));
  alt.appendChild(altBody);
  box.appendChild(alt);

  toggle.onclick = () => {
    altBody.hidden = !altBody.hidden;
    toggle.textContent = altBody.hidden
      ? "This paper has no PubMed ID"
      : "Actually, it does have a PubMed ID";
    if (!altBody.hidden) doiInput.focus();
  };

  const commitNoPmid = () => {
    clearFail();
    Promise.resolve(onNoPmid(doiInput.value.trim())).catch(e => fail(e.message));
  };
  doiBtn.onclick = commitNoPmid;
  doiInput.onkeydown = e => { if (e.key === "Enter") commitNoPmid(); };

  return box;
}
