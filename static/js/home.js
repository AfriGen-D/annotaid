// Project picker — the first thing a curator sees.
//
// A separate document from the curation app rather than a screen inside it:
// index.html/main.js boot straight into the three-pane UI and cannot do that
// before they know which project they are in. Keeping them apart also means the
// back button and deep links to /p/<id> work with no routing machinery.
import { $, el, toast, downloadFile } from "./dom.js";
import { api } from "./api.js";
import { openCreateProject } from "./projectCreate.js";
import { openUserIdentity } from "./userIdentity.js";

const S = { projects: [], global: null, template: null, identity: null };

async function boot() {
  wireHeader();
  showReturnNotice();
  try {
    const [g, list, identity] = await Promise.all(
      [api.globalConfig(), api.listProjects(), api.getIdentity()]
    );
    S.global = g;
    S.projects = list.projects || [];
    S.identity = identity;
    // Optional: a project can be defined from scratch, so a missing template is
    // not an error. Fetched up front so the creation stepper opens instantly.
    if (g.hasTemplate) {
      try { S.template = await api.template(); } catch (_) { S.template = null; }
    }
  } catch (err) {
    return fatal(`Could not reach the server: ${err.message}`);
  }
  render();
  // First run: nothing saved yet. openUserIdentity's own dismissal rule (can't
  // close while name/email are blank) is what actually blocks the homescreen —
  // this just decides whether to open it unprompted.
  if (!S.identity.set) {
    openUserIdentity({ onSaved: ident => { S.identity = ident; } });
  }
}

function wireHeader() {
  $("#newProjectBtn").onclick = openNewProjectModal;
  $("#yourInfoBtn").onclick = () => {
    openUserIdentity({ onSaved: ident => { S.identity = ident; } });
  };
}

// main.js sends the curator here with ?missing=<id> when a bookmarked project has
// gone. Saying so beats silently landing them on a list.
function showReturnNotice() {
  const missing = new URLSearchParams(location.search).get("missing");
  if (!missing) return;
  const n = $("#notice");
  n.textContent = "That project no longer exists — it may have been archived.";
  n.hidden = false;
  history.replaceState(null, "", "/");
}

function fatal(msg) {
  const grid = $("#projectGrid");
  grid.innerHTML = "";
  grid.appendChild(el("div", "proj-empty", msg));
}

/* ---------------- render ---------------- */
function render() {
  const grid = $("#projectGrid");
  grid.innerHTML = "";

  if (!S.projects.length) {
    const empty = el("div", "proj-empty");
    empty.appendChild(el("div", "big", "No projects yet"));
    empty.appendChild(el("div", "hint",
      "A curation project defines the features to extract and holds the papers to extract them from. Create one to begin."));
    const b = el("button", "btn primary", "Create your first project");
    b.onclick = openNewProjectModal;
    empty.appendChild(b);
    grid.appendChild(empty);
    return;
  }

  for (const p of S.projects) grid.appendChild(card(p));
}

function closeAllCardMenus() {
  document.querySelectorAll(".pc-menu-wrap .menu").forEach(m => { m.hidden = true; });
}
document.addEventListener("click", e => {
  if (!e.target.closest(".pc-menu-wrap")) closeAllCardMenus();
});

function card(p) {
  // The whole card is the link, so there is no small target to aim at. The
  // ellipsis menu sits on top of it — every handler inside it must stop the
  // click reaching the <a> or it would navigate into the project instead.
  const a = el("a", "proj-card");
  a.href = `/p/${encodeURIComponent(p.id)}`;

  a.appendChild(cardMenu(p));
  a.appendChild(el("div", "pc-name", p.name));
  a.appendChild(el("div", "pc-desc", p.description || "No description."));

  // Group count and model count are still in `p` (and in the portable
  // document) — just not surfaced on the card. Feature count alone answers
  // "what does this project curate", which is what the card is for.
  const chips = el("div", "pc-chips");
  chips.appendChild(el("span", "chip", `${p.featureCount} feature${p.featureCount === 1 ? "" : "s"}`));
  a.appendChild(chips);

  const s = p.stats;
  const stats = el("div", "pc-stats");
  stats.appendChild(stat(s.papers, "papers"));
  // Only worth showing when there is something to act on.
  if (s.pending) stats.appendChild(stat(s.pending, "need identity", "warn"));
  stats.appendChild(stat(s.runs, "runs"));
  stats.appendChild(stat(
    s.featuresTotal ? `${Math.round((s.confirmed / s.featuresTotal) * 100)}%` : "—",
    "confirmed"));
  a.appendChild(stats);

  a.appendChild(el("div", "pc-foot", `updated ${relTime(p.updatedAt)}`));
  return a;
}

function cardMenu(p) {
  const wrap = el("div", "menu-wrap pc-menu-wrap");
  const btn = el("button", "pc-menu-btn", "⋮");
  btn.type = "button";
  btn.title = "Project actions";
  const menu = el("div", "menu");
  menu.hidden = true;

  const noCuration = !(p.stats && p.stats.confirmed > 0);
  const dlValues = el("button", null, "Download curated values");
  dlValues.type = "button";
  dlValues.disabled = noCuration;
  if (noCuration) dlValues.title = "No curated values yet";
  dlValues.onclick = e => {
    e.preventDefault(); e.stopPropagation();
    downloadFile(api.exportUrlFor(p.id, "csv"));
    menu.hidden = true;
  };
  menu.appendChild(dlValues);

  const dlConfig = el("button", null, "Download config file");
  dlConfig.type = "button";
  dlConfig.onclick = e => {
    e.preventDefault(); e.stopPropagation();
    downloadFile(api.configFileUrl(p.id));
    menu.hidden = true;
  };
  menu.appendChild(dlConfig);

  btn.onclick = e => {
    e.preventDefault(); e.stopPropagation();
    const wasHidden = menu.hidden;
    closeAllCardMenus();
    menu.hidden = !wasHidden;
  };

  // Catch-all: a click anywhere in the wrap that isn't one of the specific
  // button handlers above (e.g. the menu's own padding) must still not reach
  // the enclosing <a> — those handlers already stopped propagation for
  // themselves, so this only ever fires for the gaps between them.
  wrap.onclick = e => { e.preventDefault(); e.stopPropagation(); };

  wrap.appendChild(btn);
  wrap.appendChild(menu);
  return wrap;
}

function stat(value, label, cls) {
  const w = el("div", "pc-stat" + (cls ? " " + cls : ""));
  w.appendChild(el("span", "v", String(value)));
  w.appendChild(el("span", "l", label));
  return w;
}

function relTime(iso) {
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "recently";
  const secs = Math.max(0, (Date.now() - then) / 1000);
  const steps = [[60, "s"], [60, "m"], [24, "h"], [7, "d"], [4.35, "w"], [12, "mo"]];
  let v = secs, unit = "s";
  for (const [div, next] of steps) {
    if (v < div) break;
    v /= div; unit = next;
  }
  if (unit === "s" && v < 45) return "just now";
  return `${Math.round(v)}${unit} ago`;
}

/* ---------------- new project ---------------- */
function openNewProjectModal() {
  openCreateProject({
    template: S.template,
    onCreated: p => { location.href = `/p/${encodeURIComponent(p.id)}`; },
  });
}

boot();
