// The local curator's own identity — name, email, optional OpenRouter key.
//
// One rule governs dismissal in every context (first run or reopened to edit):
// the modal cannot be closed while name or email is blank. Closing is itself a
// save attempt, so a curator who fills the form in and dismisses it (backdrop,
// ×, Escape) never silently loses what they typed.
import { el, toast } from "./dom.js";
import { api } from "./api.js";

export function openUserIdentity({ onSaved } = {}) {
  const root = document.getElementById("userIdentityModal");
  root.innerHTML = "";

  const st = {
    name: "", email: "", hasKey: false, keyInput: "", clearKey: false,
    busy: false, loaded: false,
  };

  const modal = el("div", "modal");
  const head = el("div", "modal-head");
  head.appendChild(el("span", null, "Your info"));
  const closeBtn = el("button", "modal-close", "×");
  head.appendChild(closeBtn);
  modal.appendChild(head);

  const body = el("div", "modal-body");
  modal.appendChild(body);
  root.appendChild(modal);
  root.hidden = false;

  function onKey(e) { if (e.key === "Escape") attemptClose(); }
  document.addEventListener("keydown", onKey);
  closeBtn.onclick = attemptClose;
  root.onclick = e => { if (e.target === root) attemptClose(); };

  function closeNow() {
    root.hidden = true;
    root.innerHTML = "";
    document.removeEventListener("keydown", onKey);
  }

  async function attemptClose() {
    if (st.busy || !st.name.trim() || !st.email.trim()) return;
    await save({ thenClose: true });
  }

  async function save({ thenClose } = {}) {
    const status = document.getElementById("uiStatus");
    if (!st.name.trim() || !st.email.trim()) {
      if (status) {
        status.textContent = "Name and email are both required.";
        status.hidden = false;
      }
      return;
    }
    st.busy = true;
    render();
    try {
      const payload = { name: st.name.trim(), email: st.email.trim() };
      if (st.clearKey) payload.clearOpenrouterKey = true;
      else if (st.keyInput.trim()) payload.openrouterKey = st.keyInput.trim();
      const result = await api.saveIdentity(payload);
      st.busy = false;
      st.hasKey = result.hasOpenRouterKey;
      st.keyInput = "";
      st.clearKey = false;
      if (onSaved) onSaved(result);
      if (thenClose) { closeNow(); return; }
      toast("Saved.");
      render();
    } catch (err) {
      st.busy = false;
      render();
      const s = document.getElementById("uiStatus");
      if (s) { s.textContent = err.message || "Could not save your info."; s.hidden = false; }
    }
  }

  function render() {
    body.innerHTML = "";

    body.appendChild(el("div", "modal-lab", "Name"));
    const nameRow = el("div", "modal-row");
    const nameInput = el("input");
    nameInput.type = "text";
    nameInput.value = st.name;
    nameInput.placeholder = "Jane Doe";
    nameInput.disabled = st.busy;
    nameInput.oninput = () => { st.name = nameInput.value; };
    nameRow.appendChild(nameInput);
    body.appendChild(nameRow);

    body.appendChild(el("div", "modal-lab", "Email"));
    const emailRow = el("div", "modal-row");
    const emailInput = el("input");
    emailInput.type = "email";
    emailInput.value = st.email;
    emailInput.placeholder = "jane@example.com";
    emailInput.disabled = st.busy;
    emailInput.oninput = () => { st.email = emailInput.value; };
    emailRow.appendChild(emailInput);
    body.appendChild(emailRow);

    body.appendChild(el("div", "modal-lab", "OpenRouter API key (optional)"));
    const keyRow = el("div", "modal-row");
    const keyInput = el("input");
    keyInput.type = "password";
    keyInput.autocomplete = "off";
    keyInput.disabled = st.busy;
    keyInput.value = st.keyInput;
    // Masked once set: the real key is never sent back from the server, so the
    // field just shows a placeholder — typing anything here replaces it.
    keyInput.placeholder = st.hasKey
      ? "•••••••••••••••• (set — type to replace)"
      : "sk-or-v1-…";
    keyInput.oninput = () => { st.keyInput = keyInput.value; st.clearKey = false; };
    keyRow.appendChild(keyInput);
    body.appendChild(keyRow);

    if (st.hasKey) {
      const rm = el("button", "linkish", "Remove key");
      rm.type = "button";
      rm.disabled = st.busy;
      rm.onclick = () => {
        st.clearKey = true; st.keyInput = ""; st.hasKey = false;
        render();
      };
      body.appendChild(rm);
    }

    body.appendChild(el("div", "modal-hint",
      "Your name and email are recorded as the creator of any project you start. An OpenRouter key entered here is used instead of the server's own for extraction, and is never shown again once saved."));

    const status = el("div", "modal-status err");
    status.id = "uiStatus";
    status.hidden = true;
    body.appendChild(status);

    const acts = el("div", "step-actions");
    const saveBtn = el("button", "btn primary", st.busy ? "Saving…" : "Save");
    saveBtn.disabled = st.busy || !st.loaded;
    saveBtn.onclick = () => save({});
    acts.appendChild(saveBtn);
    body.appendChild(acts);
  }

  render();
  api.getIdentity().then(ident => {
    st.name = ident.name || "";
    st.email = ident.email || "";
    st.hasKey = ident.hasOpenRouterKey;
    st.loaded = true;
    render();
  }).catch(() => { st.loaded = true; render(); });
}
