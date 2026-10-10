/** Glossary matching names; preserves abbreviations, CJK and prompt annotation rules. */
export function glossaryAliases(term: string): string[] {
  let raw = [];
  raw.push(term);
  let base = term.replace(/[(（][^)）]*[)）]/g, '').trim();   // strip parentheticals
  raw.push(base);
  let paren = term.match(/[(（]([^)）]+)[)）]/);
  if (paren) {
    let inner = paren[1].trim();
    // Drop meta annotations the prompt may add; keep real abbreviations.
    if (!/本文首创|首创|借鉴|新增|高\/低|效用|introduced|borrowed|coined/i.test(inner)) raw.push(inner);
  }
  base.split(/\s*[\/、，]\s*/).forEach(function (p) { raw.push(p); });

  let seen = new Set<string>(), out: string[] = [];
  for (let i = 0; i < raw.length; i++) {
    let a = (raw[i] || '').trim();
    if (!a) continue;
    let key = a.toLowerCase();
    if (seen.has(key)) continue;
    let hasCjk = /[\u3400-\u4dbf\u4e00-\u9fff]/.test(a);
    let isAbbrev = /^[A-Z0-9][A-Z0-9@+\-]{1,}$/.test(a);   // e.g. TTS, RL, Best@K
    // Length gate: CJK ≥2 chars, Latin ≥3 chars (abbreviations ≥2).
    if (hasCjk) { if (a.length < 2) continue; }
    else if (!isAbbrev && a.length < 3) continue;
    seen.add(key);
    out.push(a);
  }
  return out;
}
