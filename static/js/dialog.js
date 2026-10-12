// Small modal dialogs for the curation app: the paper history, the reason a
// paper is excluded, the jobs and team panels. Same .modal look as the "Add
// Paper(s)" stepper, but each dialog brings its own overlay so one can sit on
// top of another without the stepper's single #addPdfModal slot.
import { el } from "./dom.js";

const open = [];   // stack, so Escape closes only the topmost

document.addEventListener("keydown", e => {
  if (e.key === "Escape" && open.length) { e.stopImmediatePropagation(); open[open.length - 1].close(); }
});

/** -> {body, foot, close, setTitle}. `cls` adds a class to .modal (e.g. "wide"). */
export function openDialog({ title, cls, onClose } = {}) {
  const previousFocus = document.activeElement;
  const overlay = el("div", "modal-overlay dlg-overlay");
  const modal = el("div", "modal dlg" + (cls ? " " + cls : ""));
  modal.setAttribute("role", "dialog");
  modal.setAttribute("aria-modal", "true");
  const head = el("div", "modal-head");
  const titleEl = el("span", null, title || "");
  const x = el("button", "modal-close", "×");
  x.setAttribute("aria-label", "Close");
  head.appendChild(titleEl); head.appendChild(x);
  const body = el("div", "modal-body");
  modal.appendChild(head); modal.appendChild(body);
  overlay.appendChild(modal);
  document.body.appendChild(overlay);

  let closed = false;
  const d = {
    body,
    setTitle: t => { titleEl.textContent = t; },
    close() {
      if (closed) return;
      closed = true;
      overlay.remove();
      const i = open.indexOf(d);
      if (i >= 0) open.splice(i, 1);
      if (onClose) onClose();
      if (previousFocus && previousFocus.isConnected) previousFocus.focus();
    },
  };
  x.onclick = d.close;
  overlay.addEventListener("mousedown", e => { if (e.target === overlay) d.close(); });
  open.push(d);
  return d;
}

/** A styled replacement for window.confirm. Resolves false on Cancel, ×, backdrop or Escape. */
export function confirmAction({ title = "Confirm action", message, confirmLabel = "Confirm", destructive = false }) {
  return new Promise(resolve => {
    let accepted = false;
    const d = openDialog({ title, onClose: () => resolve(accepted) });
    d.body.appendChild(el("div", "dlg-text", message));
    const acts = el("div", "step-actions");
    const cancel = el("button", "btn", "Cancel");
    const ok = el("button", "btn " + (destructive ? "danger" : "primary"), confirmLabel);
    cancel.type = ok.type = "button";
    cancel.onclick = d.close;
    ok.onclick = () => { accepted = true; d.close(); };
    acts.append(cancel, ok);
    d.body.appendChild(acts);
    setTimeout(() => cancel.focus(), 0);
  });
}

/** A styled replacement for window.prompt. Resolves null on cancellation. */
export function askText({ title, prompt, placeholder = "", confirmLabel = "Add", required = true }) {
  return new Promise(resolve => {
    let answer = null;
    const d = openDialog({ title, onClose: () => resolve(answer) });
    if (prompt) d.body.appendChild(el("div", "dlg-text", prompt));
    const input = el("input", "dlg-input");
    input.type = "text";
    input.placeholder = placeholder;
    d.body.appendChild(input);
    const err = el("div", "dlg-err");
    err.hidden = true;
    d.body.appendChild(err);
    const acts = el("div", "step-actions");
    const cancel = el("button", "btn", "Cancel");
    const ok = el("button", "btn primary", confirmLabel);
    cancel.type = ok.type = "button";
    cancel.onclick = d.close;
    const commit = () => {
      const value = input.value.trim();
      if (required && !value) {
        err.textContent = "Enter a value.";
        err.hidden = false;
        input.focus();
        return;
      }
      answer = value;
      d.close();
    };
    ok.onclick = commit;
    input.onkeydown = e => { if (e.key === "Enter") { e.preventDefault(); commit(); } };
    acts.append(cancel, ok);
    d.body.appendChild(acts);
    setTimeout(() => input.focus(), 0);
  });
}

/**
 * Asks for a required reason (excluding a paper, reopening one). Resolves to the
 * trimmed text, or null if cancelled. `required: false` allows an empty answer.
 */
export function askReason({ title, prompt, confirmLabel = "Save", placeholder = "", required = true }) {
  return new Promise(resolve => {
    let answer = null;
    const d = openDialog({ title, onClose: () => resolve(answer) });
    if (prompt) d.body.appendChild(el("div", "dlg-text", prompt));
    const ta = el("textarea", "dlg-reason");
    ta.rows = 4;
    ta.maxLength = 2000;
    ta.placeholder = placeholder;
    d.body.appendChild(ta);
    const err = el("div", "dlg-err");
    err.hidden = true;
    d.body.appendChild(err);
    const acts = el("div", "step-actions");
    const cancel = el("button", "btn", "Cancel");
    const ok = el("button", "btn primary", confirmLabel);
    acts.appendChild(cancel); acts.appendChild(ok);
    d.body.appendChild(acts);
    cancel.onclick = d.close;
    const commit = () => {
      const v = ta.value.trim();
      if (required && !v) { err.textContent = "A reason is required."; err.hidden = false; ta.focus(); return; }
      answer = v;
      d.close();
    };
    ok.onclick = commit;
    ta.onkeydown = e => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); commit(); } };
    setTimeout(() => ta.focus(), 20);
  });
}

// "14:02" for today, "3 Oct 14:02" this year, else "3 Oct 2025". Local time.
export function shortTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const now = new Date();
  const hm = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  if (d.toDateString() === now.toDateString()) return hm;
  const dm = d.toLocaleDateString([], { day: "numeric", month: "short" });
  if (d.getFullYear() === now.getFullYear()) return `${dm} ${hm}`;
  return d.toLocaleDateString([], { day: "numeric", month: "short", year: "numeric" });
}
