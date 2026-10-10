/** Compact literature workbench; metadata is server evidence, sorting is ephemeral. */
import { escapeHtml } from '../../../../html-safety';
import { featureRegistry } from '../../../../feature-registry';
import type { ResearchStream, JsonObject } from '../state/stream';
import { knownNumber, paperIdentity, sortedPapers, type LiteratureSort } from '../state/literature';
import { citationLineage } from './citation-lineage';
import { LiteratureGraph } from './literature-graph';
export { citationLineage } from './citation-lineage';

type Translator = (key: string) => string;
const esc = (value: unknown) => escapeHtml(String(value ?? ''));
const SORTS: LiteratureSort[] = ['year-desc', 'year-asc', 'citations-desc', 'citations-asc'];
const STATUS_KEYS: Record<string, string> = {
  discovered: 'statusDiscovered', parsed: 'statusParsed', cached: 'statusCached',
  cache_hit: 'statusCached', metadata_only: 'statusMetadata', error: 'statusFailed', failed: 'statusFailed',
};

function titleLink(paper: JsonObject): string {
  const title = esc(paper.title || paperIdentity(paper));
  if (paper.arxivId) {
    return '<button type="button" class="rs-paper-title" data-research-paper="'
      + esc(paper.arxivId) + '">' + title + ' ↗</button>';
  }
  const url = paper.doi ? 'https://doi.org/' + encodeURIComponent(String(paper.doi))
    : paper.semanticScholarId ? 'https://www.semanticscholar.org/paper/' + encodeURIComponent(String(paper.semanticScholarId)) : '';
  return url ? '<a class="rs-paper-title" href="' + esc(url)
    + '" target="_blank" rel="noopener noreferrer">' + title + ' ↗</a>' : title;
}

function viewToggle(stream: ResearchStream, copy: (key: string) => string): string {
  const view = stream.literatureView || 'graph';
  return '<span class="rs-view-toggle" role="group" aria-label="' + esc(copy('viewToggle')) + '">'
    + (['graph', 'list'] as const).map(mode => '<button type="button" class="rs-view-btn'
      + (view === mode ? ' is-active' : '') + '" aria-pressed="' + String(view === mode)
      + '" data-literature-view="' + mode + '">' + esc(copy(mode === 'graph' ? 'viewGraph' : 'viewList'))
      + '</button>').join('') + '</span>';
}

export function paperCards(stream: ResearchStream, t?: Translator): string {
  const copy = (key: string) => t ? t('evolution.research.library.' + key) : key;
  const sort = stream.literatureSort || 'year-desc';
  const view = stream.literatureView || 'graph';
  const papers = sortedPapers((stream.papers || []).slice(0, 200), sort);
  const number = (value: unknown) => knownNumber(value) ?? '—';
  if (view === 'graph') {
    return '<section class="rs-library"><header><h2>' + esc(copy('title')) + ' <small>' + papers.length + '</small></h2>'
      + viewToggle(stream, copy) + '</header>'
      + '<div id="researchLiteratureGraph" class="rs-literature-graph" role="region" aria-label="' + esc(copy('viewGraph')) + '"></div>'
      + '<p class="rs-library-coverage">' + esc(copy('coverage')) + '</p>' + citationLineage(stream, copy) + '</section>';
  }
  const rows = papers.map(paper => '<tr id="rs-paper-' + esc(encodeURIComponent(paperIdentity(paper))) + '">'
    + '<td>' + titleLink(paper) + '<small>' + esc(paperIdentity(paper)) + '</small></td>'
    + '<td>' + esc(paper.venue || copy('unknownVenue')) + '</td>'
    + '<td>' + number(paper.year) + '</td>'
    + '<td title="' + esc(paper.citationCountSource || copy('unknownSource')) + '">' + number(paper.citationCount) + '</td>'
    + '<td>' + esc(STATUS_KEYS[String(paper.status)] ? copy(STATUS_KEYS[String(paper.status)]) : paper.status || '—')
    + (paper.error ? '<small>' + esc(paper.error) + '</small>' : '') + '</td></tr>').join('');
  return '<section class="rs-library"><header><h2>' + esc(copy('title')) + ' <small>' + papers.length + '</small></h2>'
    + viewToggle(stream, copy)
    + '<label for="rs-literature-sort">' + esc(copy('sort')) + '</label><select id="rs-literature-sort">'
    + SORTS.map(value => '<option value="' + value + '"' + (sort === value ? ' selected' : '') + '>'
      + esc(copy(value)) + '</option>').join('') + '</select></header>'
    + (papers.length ? '<div class="rs-paper-table-scroll" tabindex="0" role="region" aria-label="' + esc(copy('title')) + '">'
      + '<table class="rs-paper-table"><thead><tr>'
      + ['paper', 'venue', 'year', 'citations', 'status'].map(key => '<th scope="col">' + esc(copy(key)) + '</th>').join('')
      + '</tr></thead><tbody>' + rows + '</tbody></table></div>'
      : '<p class="rs-library-empty">' + esc(copy('empty')) + '</p>')
    + '<p class="rs-library-coverage">' + esc(copy('coverage')) + '</p>' + citationLineage(stream, copy) + '</section>';
}

function openPaperReader(paper: JsonObject): void {
  const registry = featureRegistry as unknown as { _researchReader?: (id: string) => Promise<void> };
  const id = String(paper.arxivId || '');
  if (id) void registry._researchReader?.(id);
}

// One graph instance per stream; patchResearchDom skips the host's subtree
// (stable-dom.ts) so the canvas, camera and layout survive live repaints.
let graphSession: { stream: ResearchStream; graph: LiteratureGraph; host: HTMLElement } | null = null;

export function destroyLiteratureGraph(): void {
  graphSession?.graph.destroy();
  graphSession = null;
}

export function bindPaperCards(host: HTMLElement, stream?: ResearchStream, repaint?: () => void, t?: Translator): void {
  host.querySelectorAll<HTMLButtonElement>('[data-research-paper]').forEach(button => {
    button.onclick = () => {
      const registry = featureRegistry as unknown as { _researchReader?: (id: string) => Promise<void> };
      void registry._researchReader?.(button.dataset.researchPaper || '');
    };
  });
  const select = host.querySelector<HTMLSelectElement>('#rs-literature-sort');
  if (select && stream) select.onchange = () => {
    if (SORTS.includes(select.value as LiteratureSort)) {
      stream.literatureSort = select.value as LiteratureSort;
      repaint?.();
    }
  };
  host.querySelectorAll<HTMLButtonElement>('[data-literature-view]').forEach(button => {
    button.onclick = () => {
      if (!stream) return;
      const view = button.dataset.literatureView === 'list' ? 'list' : 'graph';
      if (stream.literatureView !== view) {
        stream.literatureView = view;
        repaint?.();
      }
    };
  });
  const graphHost = host.querySelector<HTMLElement>('#researchLiteratureGraph');
  if (!stream || !graphHost) {
    if (graphSession && !document.contains(graphSession.host)) destroyLiteratureGraph();
    return;
  }
  const copy = (key: string) => t ? t('evolution.research.library.' + key) : key;
  if (!graphSession || graphSession.stream !== stream || graphSession.host !== graphHost) {
    destroyLiteratureGraph();
    graphSession = { stream, graph: new LiteratureGraph(graphHost), host: graphHost };
  }
  graphSession.graph.update(stream, copy, {
    onOpen: openPaperReader,
    onViewList: () => {
      stream.literatureView = 'list';
      repaint?.();
    },
  });
}
