/** Offline citation lineage: supplied cites edges only; never inferred from dates or similarity. */
import { escapeHtml } from '../../../../html-safety';
import type { JsonObject, ResearchStream } from '../state/stream';
import { knownNumber, paperIdentity } from '../state/literature';

type Copy = (key: string) => string;
const esc = (value: unknown) => escapeHtml(String(value ?? ''));
const MAX_EDGES = 40; // At most 80 connected nodes; no layout library or network work.

export function citationLineage(stream: ResearchStream, copy: Copy): string {
  const papers = new Map((stream.papers || []).slice(0, 200).map(p => [paperIdentity(p), p]));
  const seen = new Set<string>();
  const edges = (stream.citationEdges || []).slice(0, 320).filter(edge => {
    const key = JSON.stringify([edge.from, edge.to]);
    if (edge.relation !== 'cites' || edge.from === edge.to || seen.has(key)
        || !papers.has(String(edge.from)) || !papers.has(String(edge.to))) return false;
    seen.add(key);
    return true;
  });
  const shown = edges.slice(0, MAX_EDGES);
  const title = (id: unknown) => esc(papers.get(String(id))?.title || id);
  const diagram = shown.length ? lineageSvg(papers, shown, copy) : '';
  return '<details class="rs-lineage" id="rs-citation-lineage"><summary>' + esc(copy('lineage'))
    + (edges.length ? ' · ' + shown.length + ' / ' + edges.length : '') + '</summary>'
    + '<p>' + esc(copy('lineageHint')) + '</p>'
    + (shown.length ? diagram + '<p>' + esc(copy('lineageBound')) + '</p>'
      + '<details class="rs-lineage-relations"><summary>' + esc(copy('edgeList')) + '</summary><ol>'
      + shown.map(edge => '<li><span>' + title(edge.from) + '</span><b aria-label="'
        + esc(copy('cites')) + '">→</b><span>' + title(edge.to) + '</span></li>').join('')
      + '</ol></details>' : '<p>' + esc(copy('noEdges')) + '</p>')
    + '</details>';
}

function lineageSvg(papers: Map<string, JsonObject>, edges: JsonObject[], copy: Copy): string {
  const ids = [...new Set(edges.flatMap(edge => [String(edge.from), String(edge.to)]))];
  const year = (id: string) => knownNumber(papers.get(id)?.year);
  const years = [...new Set(ids.map(year))].sort((a, b) => a === null ? 1 : b === null ? -1 : a - b);
  const rows = new Map<number | null, number>();
  const positions = new Map(ids.map(id => {
    const value = year(id);
    const row = rows.get(value) || 0;
    rows.set(value, row + 1);
    return [id, { x: 20 + years.indexOf(value) * 280, y: 48 + row * 82 }];
  }));
  const width = Math.max(300, years.length * 280 + 20);
  const height = Math.max(140, Math.max(...rows.values()) * 82 + 55);
  const headings = years.map((value, index) => '<text class="rs-lineage-year" x="'
    + (20 + index * 280) + '" y="26">' + esc(value ?? copy('unknownYear')) + '</text>').join('');
  const paths = edges.map((edge, index) => {
    const from = positions.get(String(edge.from))!;
    const to = positions.get(String(edge.to))!;
    const backwards = from.x > to.x;
    const sameYear = from.x === to.x;
    const x1 = from.x + (backwards ? 0 : 210);
    const x2 = to.x + (backwards || sameYear ? 210 : 0);
    const y1 = from.y + 26;
    const y2 = to.y + 26;
    const bend = sameYear ? x1 + 25 + (index % 4) * 9 : (x1 + x2) / 2;
    return '<path class="rs-lineage-edge" d="M ' + x1 + ' ' + y1 + ' C ' + bend + ' ' + y1
      + ', ' + bend + ' ' + y2 + ', ' + x2 + ' ' + y2 + '" marker-end="url(#rs-citation-arrow)"/>';
  }).join('');
  const nodes = ids.map(id => {
    const point = positions.get(id)!;
    const label = String(papers.get(id)?.title || id);
    const characters = Array.from(label);
    const first = characters.slice(0, 28).join('');
    const second = characters.slice(28, 55).join('') + (characters.length > 55 ? '…' : '');
    return '<g class="rs-lineage-node" transform="translate(' + point.x + ' ' + point.y + ')">'
      + '<title>' + esc(label) + '</title><rect width="210" height="54" rx="4"/>'
      + '<text x="10" y="21">' + esc(first) + '</text><text x="10" y="40">' + esc(second) + '</text></g>';
  }).join('');
  return '<div class="rs-lineage-viewport" tabindex="0" role="region" aria-label="' + esc(copy('lineage')) + '">'
    + '<svg class="rs-lineage-diagram" role="img" aria-labelledby="rs-lineage-title rs-lineage-description" width="'
    + width + '" height="' + height + '" viewBox="0 0 ' + width + ' ' + height + '">'
    + '<title id="rs-lineage-title">' + esc(copy('lineage')) + '</title>'
    + '<desc id="rs-lineage-description">' + esc(copy('lineageHint')) + '</desc>'
    + '<defs><marker id="rs-citation-arrow" viewBox="0 0 10 10" refX="10" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z"/></marker></defs>'
    + headings + paths + nodes + '</svg></div>';
}
