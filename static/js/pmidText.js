// Parsing for the "Add Paper(s)" PMID box: free text -> a clean list of PMIDs.
// Pure — no DOM, no imports — so it can be exercised on its own.
//
// The same function serves both the textarea and the "load from a file" path:
// the file reader does NOT parse, it merges the file's raw text into the
// textarea, so parsing happens in exactly one place and re-loading the same
// file is deduped for free.

// One class covers every separator the box promises: space, tab, newline, CR,
// comma and semicolon.
const SEP = /[\s,;]+/;

// PMIDs are positive integers and never carry a leading zero. Rejecting those
// here means "0123" or a stray 13-digit ISBN is flagged in the box instead of
// being sent to PubMed to fail slowly, one 15-second round trip at a time.
const PMID = /^[1-9]\d{0,8}$/;

function normalise(token) {
  return token
    .replace(/^[("'\[<]+|[)"'\]>]+$/g, "")   // stray wrapping punctuation
    .replace(/^pmid[:#]?/i, "")              // "PMID:26751406", "pmid 26751406"
    .replace(/\.$/, "")                      // sentence-final full stop
    .trim();
}

/**
 * @returns {{ids: string[], invalid: string[], duplicates: number, total: number}}
 *   ids        valid PMIDs, deduped, first-occurrence order preserved
 *   invalid    tokens that were not PMIDs, deduped (surfaced, never silently dropped)
 *   duplicates count of valid tokens dropped as repeats
 *   total      non-empty tokens seen
 */
export function parsePmidText(text) {
  const tokens = String(text || "").split(SEP).filter(Boolean);
  const ids = [], invalid = [];
  const seen = new Set(), seenBad = new Set();
  let duplicates = 0;

  for (const token of tokens) {
    const t = normalise(token);
    if (!t) continue;
    if (!PMID.test(t)) {
      // A CSV header or a DOI column vanishing without trace is the failure
      // mode that bites — the caller renders these back to the curator.
      if (!seenBad.has(t)) { seenBad.add(t); invalid.push(t); }
      continue;
    }
    if (seen.has(t)) { duplicates++; continue; }
    seen.add(t);
    ids.push(t);
  }

  return { ids, invalid, duplicates, total: tokens.length };
}

/** "12 PMIDs · 2 duplicates removed · 1 entry ignored: "abc"" — the live counter line. */
export function describeParse({ ids, invalid, duplicates }) {
  const bits = [];
  bits.push(ids.length === 1 ? "1 PMID" : `${ids.length} PMIDs`);
  if (duplicates) bits.push(`${duplicates} duplicate${duplicates > 1 ? "s" : ""} removed`);
  if (invalid.length) {
    const shown = invalid.slice(0, 5).map(s => `"${s}"`).join(", ");
    const more = invalid.length > 5 ? ` and ${invalid.length - 5} more` : "";
    bits.push(`${invalid.length} ${invalid.length > 1 ? "entries" : "entry"} ignored: ${shown}${more}`);
  }
  return bits.join(" · ");
}
