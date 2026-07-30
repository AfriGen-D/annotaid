// "Add PDF" modal: two tabs — fetch full text by PMID (NCBI E-utils / PMC Open
// Access), or upload PDF(s) directly (drag-and-drop or file dialog). The PMID
// tab never fails silently: any fetch error is shown inline with a nudge to the
// Upload tab, since most subscription-only papers simply aren't retrievable
// this way.
import { el } from "./dom.js";

const ROOT_ID = "addPdfModal";

// onFetch(pmid) -> Promise<paper>  (rejects with a user-facing .message)
// onFiles(fileList) -> void        (fire-and-forget; caller handles upload+toast)
export function openAddPdfModal({ onFetch, onFiles }) {
  const root = document.getElementById(ROOT_ID);
  root.innerHTML = "";
  root.hidden = false;

  const modal = el("div", "modal");
  root.appendChild(modal);

  const head = el("div", "modal-head");
  head.appendChild(el("span", null, "Add a paper"));
  const closeBtn = el("button", "modal-close", "×");
  closeBtn.setAttribute("aria-label", "Close");
  head.appendChild(closeBtn);
  modal.appendChild(head);

  const tabs = el("div", "modal-tabs");
  const tabPmid = el("button", "modal-tab on", "Fetch by PubMed ID");
  const tabUpload = el("button", "modal-tab", "Upload PDF");
  tabs.appendChild(tabPmid); tabs.appendChild(tabUpload);
  modal.appendChild(tabs);

  const body = el("div", "modal-body");
  modal.appendChild(body);

  function close() {
    root.hidden = true;
    root.innerHTML = "";
    document.removeEventListener("keydown", onKeydown);
  }
  function onKeydown(e) { if (e.key === "Escape") close(); }
  document.addEventListener("keydown", onKeydown);
  closeBtn.onclick = close;
  root.onclick = e => { if (e.target === root) close(); };

  /* ---------------- PMID pane ---------------- */
  const pmidPane = el("div", "modal-pane");
  pmidPane.appendChild(el("div", "modal-lab", "Enter a PubMed ID (PMID)"));

  const pmidRow = el("div", "modal-row");
  const pmidInput = el("input");
  pmidInput.type = "text";
  pmidInput.inputMode = "numeric";
  pmidInput.placeholder = "e.g. 26751406";
  const fetchBtn = el("button", "btn primary", "Fetch");
  pmidRow.appendChild(pmidInput); pmidRow.appendChild(fetchBtn);
  pmidPane.appendChild(pmidRow);

  const loader = el("div", "modal-loader");
  loader.appendChild(el("span", "spinner-ring"));
  loader.appendChild(el("span", null, "Contacting PubMed / PMC — this can take a minute…"));
  pmidPane.appendChild(loader);

  const status = el("div", "modal-status");
  status.hidden = true;
  pmidPane.appendChild(status);

  pmidPane.appendChild(el("div", "modal-hint",
    "Looks up the paper on PubMed, then tries PubMed Central and other open-access " +
    "sources for the full-text PDF. Not every paper is openly available this way."));

  function setBusy(busy) {
    pmidInput.disabled = busy;
    fetchBtn.disabled = busy;
    loader.classList.toggle("on", busy);
    if (busy) status.hidden = true;
  }

  function showError(message) {
    status.hidden = false;
    status.className = "modal-status err";
    status.innerHTML = "";
    status.appendChild(el("div", null, message));
    const advice = el("div", "advice");
    advice.appendChild(document.createTextNode("Get the PDF yourself and "));
    const link = el("a", null, "upload it instead");
    link.href = "#";
    link.onclick = e => { e.preventDefault(); showTab("upload"); };
    advice.appendChild(link);
    advice.appendChild(document.createTextNode("."));
    status.appendChild(advice);
  }

  async function submitPmid() {
    const pmid = pmidInput.value.trim();
    if (!/^\d+$/.test(pmid)) { pmidInput.focus(); showError("Enter a numeric PMID."); return; }
    setBusy(true);
    try {
      await onFetch(pmid);
      setBusy(false);
      close();
    } catch (err) {
      setBusy(false);
      showError(err && err.message ? err.message : "Could not fetch this paper.");
    }
  }
  fetchBtn.onclick = submitPmid;
  pmidInput.onkeydown = e => { if (e.key === "Enter") submitPmid(); };

  /* ---------------- Upload pane ---------------- */
  const uploadPane = el("div", "modal-pane");
  uploadPane.hidden = true;

  const zone = el("div", "dropzone");
  zone.appendChild(el("div", "dz-big", "Drag & drop PDF(s) here"));
  zone.appendChild(el("div", "dz-or", "or"));
  const chooseBtn = el("button", "btn primary", "Choose file(s)");
  zone.appendChild(chooseBtn);
  uploadPane.appendChild(zone);

  const fileInput = el("input");
  fileInput.type = "file";
  fileInput.multiple = true;
  fileInput.accept = ".pdf,application/pdf";
  fileInput.hidden = true;
  uploadPane.appendChild(fileInput);

  chooseBtn.onclick = () => fileInput.click();
  fileInput.onchange = () => {
    const files = [...fileInput.files];
    fileInput.value = "";
    if (files.length) { onFiles(files); close(); }
  };

  ["dragenter", "dragover"].forEach(evt =>
    zone.addEventListener(evt, e => { e.preventDefault(); zone.classList.add("drag"); }));
  ["dragleave", "drop"].forEach(evt =>
    zone.addEventListener(evt, e => { e.preventDefault(); zone.classList.remove("drag"); }));
  zone.addEventListener("drop", e => {
    const files = [...(e.dataTransfer.files || [])].filter(f =>
      f.type === "application/pdf" || f.name.toLowerCase().endsWith(".pdf"));
    if (files.length) { onFiles(files); close(); }
  });

  body.appendChild(pmidPane);
  body.appendChild(uploadPane);

  /* ---------------- tab switching ---------------- */
  function showTab(name) {
    tabPmid.classList.toggle("on", name === "pmid");
    tabUpload.classList.toggle("on", name === "upload");
    pmidPane.hidden = name !== "pmid";
    uploadPane.hidden = name !== "upload";
    if (name === "pmid") setTimeout(() => pmidInput.focus(), 30);
  }
  tabPmid.onclick = () => showTab("pmid");
  tabUpload.onclick = () => showTab("upload");

  showTab("pmid");
}
