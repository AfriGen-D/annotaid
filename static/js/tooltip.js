// Singleton hover tooltip. Rendered at <body> level with position:fixed —
// a pane-local absolutely-positioned tooltip would be clipped by
// .col-body{overflow:auto} (the extraction pane scrolls).
import { el } from "./dom.js";

let tipEl = null;
let anchorEl = null;
let showT = null;

function ensureTip() {
  if (tipEl) return tipEl;
  tipEl = el("div", "tooltip");
  tipEl.setAttribute("role", "tooltip");
  tipEl.hidden = true;
  document.body.appendChild(tipEl);
  // Any scroll/resize invalidates the anchor rect we positioned against.
  document.addEventListener("scroll", () => hide(), true);
  window.addEventListener("resize", () => hide());
  document.addEventListener("keydown", e => { if (e.key === "Escape") hide(); });
  return tipEl;
}

function place(anchor) {
  const t = tipEl;
  const M = 8;                       // viewport margin
  const GAP = 8;                     // gap between icon and bubble
  t.style.left = "0px";
  t.style.top = "0px";
  const a = anchor.getBoundingClientRect();
  const r = t.getBoundingClientRect();

  let left = a.left + a.width / 2 - r.width / 2;
  left = Math.max(M, Math.min(left, window.innerWidth - r.width - M));

  let top = a.top - r.height - GAP;
  const below = top < M;
  if (below) top = a.bottom + GAP;
  t.classList.toggle("below", below);

  // Arrow tracks the icon even when the bubble was clamped to the viewport.
  const arrow = Math.max(10, Math.min(a.left + a.width / 2 - left, r.width - 10));
  t.style.setProperty("--tip-arrow", arrow + "px");
  t.style.left = Math.round(left) + "px";
  t.style.top = Math.round(top) + "px";
}

export function show(anchor, text) {
  const t = ensureTip();
  anchorEl = anchor;
  t.textContent = text;
  t.hidden = false;
  place(anchor);
  t.classList.add("on");
}

export function hide() {
  clearTimeout(showT);
  if (!tipEl || tipEl.hidden) return;
  tipEl.classList.remove("on");
  tipEl.hidden = true;
  anchorEl = null;
}

/** Wire hover/focus (and tap) on `anchor` to a tooltip showing `text`. */
export function attachTooltip(anchor, text, { delay = 110 } = {}) {
  const open = immediate => {
    clearTimeout(showT);
    if (immediate) show(anchor, text);
    else showT = setTimeout(() => show(anchor, text), delay);
  };
  const close = () => { clearTimeout(showT); if (anchorEl === anchor) hide(); };

  anchor.addEventListener("mouseenter", () => open(false));
  anchor.addEventListener("mouseleave", close);
  anchor.addEventListener("focus", () => open(true));
  anchor.addEventListener("blur", close);
  // Touch/click: toggle, so the description is reachable without a hover.
  anchor.addEventListener("click", e => {
    e.preventDefault();
    e.stopPropagation();
    if (anchorEl === anchor) hide(); else open(true);
  });
}

/** A "?" affordance that shows `text` on hover/focus. */
export function helpIcon(text, label) {
  const b = el("button", "help-icon", "?");
  b.type = "button";
  b.tabIndex = 0;
  b.setAttribute("aria-label", label || "Description");
  b.title = "";                       // suppress any inherited native tooltip
  attachTooltip(b, text);
  return b;
}
