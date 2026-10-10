/** Decorate one mention per glossary row/section; never traverse code, links or math. */
import type { GlossaryEntry } from './contracts';
import { glossaryAliases } from './aliases';
interface GlossaryMatch { row: number; def: string; defHtml: string; analogy: string }
interface Pick { idx: number; len: number; text: string; def: string; defHtml: string; analogy: string }
function escapeRegExp(s: string): string { return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }
export function decorateGlossaryTerms(article: HTMLElement, glossary: readonly GlossaryEntry[]): void {
  if (!glossary.length) return;
  const document = article.ownerDocument;

  // Build alias → {row, def} map and a combined matcher (longest alias first
  // so "Oracle Pass@K" wins over a bare "Oracle").
  let map = new Map<string, GlossaryMatch>(), aliases: string[] = [];
  for (let r = 0; r < glossary.length; r++) {
    let al = glossaryAliases(glossary[r].term);
    for (let j = 0; j < al.length; j++) {
      let key = al[j].toLowerCase();
      if (map.has(key)) continue;       // first row to claim an alias keeps it
      map.set(key, { row: r, def: glossary[r].def, defHtml: glossary[r].defHtml,
                   analogy: glossary[r].analogy || '' });
      aliases.push(al[j]);
    }
  }
  if (!aliases.length) return;
  aliases.sort(function (a, b) { return b.length - a.length; });
  let re: RegExp;
  try {
    re = new RegExp(aliases.map(escapeRegExp).join('|'), 'gi');
  } catch (e) {
    console.warn('[Paper:Glossary] regex build failed:', e);
    return;
  }

  let SKIP: Record<string, number> = { H1: 1, H2: 1, H3: 1, H4: 1, H5: 1, H6: 1, CODE: 1, PRE: 1,
               A: 1, FIGCAPTION: 1, SCRIPT: 1, STYLE: 1, BUTTON: 1 };
  let seen = new Set<number>();   // row → already decorated in the current section

  function decorateText(node: Text) {
    let text = node.nodeValue;
    if (!text || text.length < 2 || !/\S/.test(text)) return;
    re.lastIndex = 0;
    let picks: Pick[] = [], m: RegExpExecArray | null, pos = 0;
    while ((m = re.exec(text))) {
      let matched = m[0], idx = m.index, key = matched.toLowerCase();
      let entry = map.get(key);
      if (re.lastIndex === idx) re.lastIndex++;   // zero-width safety
      if (!entry || seen.has(entry.row)) continue;
      // Latin word-boundary guard (avoid matching inside a larger word).
      let headLatin = /[A-Za-z0-9]/.test(matched.charAt(0));
      let tailLatin = /[A-Za-z0-9]/.test(matched.charAt(matched.length - 1));
      if (headLatin && idx > 0 && /[A-Za-z0-9]/.test(text.charAt(idx - 1))) continue;
      if (tailLatin && /[A-Za-z0-9]/.test(text.charAt(idx + matched.length))) continue;
      if (idx < pos) continue;       // overlaps a prior pick
      picks.push({ idx: idx, len: matched.length, text: matched, def: entry.def,
                   defHtml: entry.defHtml, analogy: entry.analogy });
      seen.add(entry.row);
      pos = idx + matched.length;
    }
    if (!picks.length) return;
    let frag = document.createDocumentFragment(), cursor = 0;
    for (let p = 0; p < picks.length; p++) {
      let pk = picks[p];
      if (pk.idx > cursor) frag.appendChild(document.createTextNode(text.slice(cursor, pk.idx)));
      let span = document.createElement('span');
      span.className = 'paper-term';
      span.setAttribute('tabindex', '0');
      span.setAttribute('aria-label', pk.text + ': ' + pk.def);
      span.appendChild(document.createTextNode(pk.text));
      // Real DOM hover card so the definition renders its Markdown/KaTeX
      // (a CSS attr(data-def) ::after can only show plain text). The HTML
      // came from the already-sanitized report body, so it's safe to reuse.
      let card = document.createElement('span');
      card.className = 'paper-term-card';
      card.setAttribute('aria-hidden', 'true');   // aria-label on the span already carries the def
      // Analogy first (reading-xp P2): the reader remembers the comparison
      // before the definition — it heads the hover card when present.
      if (pk.analogy) {
        let an = document.createElement('span');
        an.className = 'paper-term-card-analogy';
        an.textContent = '💡 ' + pk.analogy;
        card.appendChild(an);
      }
      let defBody = document.createElement('span');
      defBody.className = 'paper-term-card-def';
      if (pk.defHtml) defBody.innerHTML = pk.defHtml;
      else defBody.textContent = pk.def;
      card.appendChild(defBody);
      span.appendChild(card);
      frag.appendChild(span);
      cursor = pk.idx + pk.len;
    }
    if (cursor < text.length) frag.appendChild(document.createTextNode(text.slice(cursor)));
    node.parentNode?.replaceChild(frag, node);
  }

  function walk(node: Node) {
    let children = [...node.childNodes];
    for (let i = 0; i < children.length; i++) {
      let c = children[i];
      if (c.nodeType === 3) { decorateText(c as Text); continue; }
      if (c.nodeType !== 1) continue;
      const element = c as HTMLElement;
      let tag = element.tagName;
      if (tag === 'H1' || tag === 'H2') seen = new Set<number>();   // new section → re-allow terms
      if (SKIP[tag]) continue;
      if (element.classList.contains('paper-term')) {
        const existing = map.get((element.firstChild?.textContent || '').toLowerCase());
        if (existing) seen.add(existing.row);
        continue;
      }
      if (element.classList && (element.classList.contains('paper-glossary') ||
          element.classList.contains('paper-term') || element.classList.contains('katex'))) continue;
      walk(c);
    }
  }

  try { walk(article); }
  catch (e) { console.warn('[Paper:Glossary] decoration failed:', e); }
}
