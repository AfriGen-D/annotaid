// A minimal, valid one-page PDF with real (selectable) text — enough for pdf.js
// to render, for evidence quotes to be found, and for the server's "%PDF"
// check. Each call can embed a unique marker so two uploads hash differently.

function esc(s) {
  return s.replace(/\\/g, "\\\\").replace(/\(/g, "\\(").replace(/\)/g, "\\)");
}

export function makePdf(lines) {
  const text = lines
    .map((l, i) => `BT /F1 12 Tf 72 ${720 - i * 18} Td (${esc(l)}) Tj ET`)
    .join("\n");
  const objs = [
    "<< /Type /Catalog /Pages 2 0 R >>",
    "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
    "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R " +
      "/Resources << /Font << /F1 5 0 R >> >> >>",
    `<< /Length ${Buffer.byteLength(text)} >>\nstream\n${text}\nendstream`,
    "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
  ];
  let out = "%PDF-1.4\n";
  const offsets = [];
  objs.forEach((o, i) => {
    offsets.push(Buffer.byteLength(out));
    out += `${i + 1} 0 obj\n${o}\nendobj\n`;
  });
  const xref = Buffer.byteLength(out);
  out += `xref\n0 ${objs.length + 1}\n0000000000 65535 f \n`;
  for (const off of offsets) out += `${String(off).padStart(10, "0")} 00000 n \n`;
  out += `trailer\n<< /Size ${objs.length + 1} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`;
  return Buffer.from(out, "latin1");
}

/** A study-like paper whose sentences match the seeded AI values. */
export function samplePaper(marker) {
  return makePdf([
    `Genome-wide association study ${marker}`,
    "We recruited 1250 participants from Kenya and Nigeria.",
    "The cohort was a mixed population of Bantu-speaking adults.",
    "The lead association reached p = 5.2e-8 at rs1801133 (MTHFR).",
    "Population: adults aged 30-65 from rural clinics.",
  ]);
}
