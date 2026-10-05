// Parsing for the "PMID + Variant IDs" paste box: one paper per line, its
// PubMed ID first, then the variant/haplotype ids that paper's row(s) in the
// project's one repeating group should be declared for. Pure — no DOM — same
// reasoning as pmidText.js: one file loaded into the box, one file dropped in,
// one parse.
//
// Line shape: "<pmid><sep><id>[, <id> ...]" where <sep> is a dash (with
// optional surrounding spaces) or just whitespace — "33915198 - rs1800544,
// rs553668" and "33915198 rs1800544 rs553668" both work. Only the FIRST
// separator after the PMID is treated specially, so a dash INSIDE an id
// (HLA-B*15:02) is never mistaken for the pmid/id boundary.
import { PMID } from "./pmidText.js";

const LINE = /^(\d+)\s*-?\s*(.*)$/;
const ID_SEP = /[\s,]+/;

/**
 * @returns {{
 *   entries: {pmid: string, variantIds: string[]}[],  one per PMID, in first-
 *     occurrence order; a PMID repeated across lines is merged, not dropped
 *   invalidLines: string[],   lines with no leading numeric PMID
 *   noIds: string[],          PMIDs whose line had no id after it (kept, not
 *     dropped — the curator may still fill them in on the next step)
 *   duplicateLines: number,
 *   total: number,            non-blank lines seen
 * }}
 */
export function parsePmidVariantText(text) {
  const lines = String(text || "").split(/\r\n|\r|\n/);
  const entries = [];
  const byPmid = new Map();  // pmid -> index into entries
  const invalidLines = [];
  const noIds = [];
  let duplicateLines = 0;
  let total = 0;

  for (const raw of lines) {
    const line = raw.trim();
    if (!line) continue;
    total++;

    const m = line.match(LINE);
    if (!m || !PMID.test(m[1])) {
      invalidLines.push(line);
      continue;
    }
    const pmid = m[1];
    const rest = m[2].trim();
    const variantIds = rest ? rest.split(ID_SEP).filter(Boolean) : [];

    if (byPmid.has(pmid)) {
      entries[byPmid.get(pmid)].variantIds.push(...variantIds);
      duplicateLines++;
    } else {
      byPmid.set(pmid, entries.length);
      entries.push({ pmid, variantIds });
    }
  }

  for (const e of entries) if (!e.variantIds.length) noIds.push(e.pmid);
  return { entries, invalidLines, noIds, duplicateLines, total };
}

/** "12 papers · 34 variant ids · 1 paper with none yet · 2 duplicate lines merged
 * · 1 line ignored: "not a pmid"" — the live counter line. */
export function describeVariantParse({ entries, invalidLines, noIds, duplicateLines }) {
  const bits = [];
  bits.push(entries.length === 1 ? "1 paper" : `${entries.length} papers`);
  const idCount = entries.reduce((n, e) => n + e.variantIds.length, 0);
  bits.push(idCount === 1 ? "1 variant id" : `${idCount} variant ids`);
  if (noIds.length) {
    bits.push(`${noIds.length} paper${noIds.length > 1 ? "s" : ""} with none yet`);
  }
  if (duplicateLines) {
    bits.push(`${duplicateLines} duplicate line${duplicateLines > 1 ? "s" : ""} merged`);
  }
  if (invalidLines.length) {
    const shown = invalidLines.slice(0, 3).map(s => `"${s}"`).join(", ");
    const more = invalidLines.length > 3 ? ` and ${invalidLines.length - 3} more` : "";
    bits.push(`${invalidLines.length} line${invalidLines.length > 1 ? "s" : ""} ignored: ${shown}${more}`);
  }
  return bits.join(" · ");
}
