/** Extract the first terminology table from sanitized rendered HTML; remove KaTeX duplicate text. */
import type { GlossaryEntry } from './contracts';
function cellPlainText(cell: Element | null): string {
  if (!cell) return '';
  let txt;
  try {
    let clone = cell.cloneNode(true) as Element;
    let dup = clone.querySelectorAll('.katex-mathml, annotation');
    for (let i = 0; i < dup.length; i++) {
      dup[i].parentNode?.removeChild(dup[i]);
    }
    txt = clone.textContent || '';
  } catch (e) {
    txt = cell.textContent || '';
  }
  return txt.replace(/\s+/g, ' ').trim();
}
export function extractGlossary(article: HTMLElement): GlossaryEntry[] {
  let tables = article.querySelectorAll('table');
  for (let ti = 0; ti < tables.length; ti++) {
    let table = tables[ti];
    let head = table.querySelector('thead th, tr:first-child th');
    let first = (head && head.textContent || '').trim().toLowerCase();
    // The prompt fixes the first column header to "Term" (EN) / "术语" (ZH).
    if (first !== 'term' && first.indexOf('术语') < 0) continue;
    table.classList.add('paper-glossary');
    let rows = table.querySelectorAll('tbody tr');
    let out: GlossaryEntry[] = [];
    for (let ri = 0; ri < rows.length; ri++) {
      let cells = rows[ri].querySelectorAll('td');
      if (cells.length < 2) continue;
      let term = (cells[0].textContent || '').trim();
      // The definition cell is part of the already-rendered report, so it may
      // hold live KaTeX / <strong> / <code> DOM. Capture its HTML so the hover
      // card can render it, and derive a CLEAN plain-text form for aria-label:
      // a naive textContent would garble math because KaTeX duplicates its
      // LaTeX source into a hidden .katex-mathml annotation node.
      let defCell = cells[1];
      let defHtml = (defCell.innerHTML || '').trim();
      let defText = cellPlainText(defCell);
      if (!term || !defText) continue;
      // Skip the prompt's own placeholder rows: "(term)", "（术语）", "...".
      if (/^[(（].*[)）]$/.test(term) || term === '...' || term === '…') continue;
      if (defText.length > 260) defText = defText.slice(0, 257) + '…';
      // Optional 4th column (reading-xp P2): the everyday analogy. Tolerated
      // by every parser when absent (3-column legacy reports render as before).
      let analogy = '';
      if (cells.length >= 4) {
        analogy = cellPlainText(cells[3]);
        if (analogy === '—' || analogy === '-' || analogy === '–') analogy = '';
      }
      out.push({ term: term, def: defText, defHtml: defHtml, analogy: analogy });
    }
    return out;  // only the first matching table
  }
  return [];
}
