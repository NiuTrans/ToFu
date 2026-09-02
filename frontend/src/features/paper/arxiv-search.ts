import { featureRegistry } from '../../feature-registry';
import type { I18nKey } from '../../i18n';
type JsonObject = Record<string, unknown>;

interface ArxivCard extends JsonObject {
  arxiv_id?: string;
  title?: string;
  authors?: unknown[];
  summary?: string;
  published?: string;
  primary_category?: string;
}

interface ArxivSearchApi {
  searchArxiv(query: string, limit: number): Promise<JsonObject | null>;
}

interface KatexApi {
  renderToString(tex: string, options: JsonObject): string;
}

type ArxivSearchWindow = Window & {
  Api?: { paper?: ArxivSearchApi };
  t?: (key: string) => string;
  escapeHtml?: (value: unknown) => string;
  debugLog?: (message: string, level?: string) => void;
  katex?: KatexApi;
  _ensureKatex?: () => unknown;
  _paperSearchResults?: ArxivCard[];
  _lastArxivSearchQuery?: string;
  _fetchArxivPaper?: (reference: string) => unknown;
  _showPaperLanding?: () => void;
  _looksLikeArxivRef?: typeof looksLikeArxivRef;
  _submitArxivQuery?: typeof submitArxivQuery;
  _searchArxivPapers?: typeof searchArxivPapers;
  _escWithInlineMath?: typeof escapeWithInlineMath;
  _renderArxivSearchResults?: typeof renderArxivSearchResults;
  _openArxivResult?: typeof openArxivResult;
};

function globals(): ArxivSearchWindow {
  return featureRegistry as unknown as ArxivSearchWindow;
}

function escape(value: unknown): string {
  const helper = globals().escapeHtml;
  if (helper) return helper(value);
  const node = document.createElement('span');
  node.textContent = value == null ? '' : String(value);
  return node.innerHTML;
}

function translate(key: I18nKey): string {
  return globals().t?.(key) ?? key;
}

function errorMessage(error: unknown): string {
  if (error && typeof error === 'object') {
    const row = error as JsonObject;
    const code = typeof row.code === 'string' ? row.code : '';
    if (code && !['network', 'timeout', 'parse'].includes(code)) return code;
    if (typeof row.message === 'string') return row.message;
  }
  return error instanceof Error ? error.message : String(error ?? '');
}

export function looksLikeArxivRef(value: string): boolean {
  const reference = (value || '').trim();
  return /arxiv\.org\//i.test(reference)
    || /^\d{4}\.\d{4,5}(v\d+)?$/.test(reference)
    || /^[a-z-]+\/\d{7}(v\d+)?$/i.test(reference);
}

export function submitArxivQuery(): void {
  const input = document.getElementById('paperArxivUrl') as HTMLInputElement | null;
  const query = input?.value?.trim() ?? '';
  if (!query) {
    globals().debugLog?.(
      'Please enter a title to search, or an arXiv URL / ID', 'warning',
    );
    return;
  }
  if (looksLikeArxivRef(query)) void globals()._fetchArxivPaper?.(query);
  else void searchArxivPapers(query);
}

export async function searchArxivPapers(query: string): Promise<void> {
  const viewer = document.getElementById('paperPdfViewer');
  if (viewer) {
    viewer.innerHTML = '<div class="paper-loading paper-search-loading">'
      + '<div class="paper-loading-spinner"></div><div>'
      + escape(translate('paper.searching')) + '</div></div>';
  }
  try {
    const api = globals().Api?.paper;
    if (!api) throw new Error('Paper search API unavailable');
    const data = await api.searchArxiv(query, 12);
    if (!data || data.ok !== true) {
      const envelope = data?.error;
      const detail = typeof envelope === 'string'
        ? envelope
        : envelope && typeof envelope === 'object'
          ? String((envelope as JsonObject).message || '')
          : '';
      throw new Error(detail || 'arXiv search failed');
    }
    const results = Array.isArray(data.results)
      ? data.results.filter((row): row is ArxivCard => Boolean(
        row && typeof row === 'object'))
      : [];
    globals()._paperSearchResults = results;
    renderArxivSearchResults(query, results);
  } catch (error: unknown) {
    console.error('[Paper] arXiv search failed:', error);
    if (!viewer) return;
    const detail = errorMessage(error);
    viewer.innerHTML = '<div class="paper-error">'
      + escape(translate('paper.searchFailed'))
      + (detail
        ? '<div class="paper-error-detail">' + escape(detail) + '</div>'
        : '')
      + '<br><button data-tofu-action="_showPaperLanding()" class="paper-retry-btn">'
      + escape(translate('paper.searchBack')) + '</button></div>';
  }
}

export function escapeWithInlineMath(value: unknown): string {
  const raw = value == null ? '' : String(value);
  if (!raw.includes('$') && !raw.includes('\\(')) return escape(raw);
  const katex = globals().katex;
  if (!katex) {
    try { globals()._ensureKatex?.(); } catch { /* lazy loader is best effort */ }
  }
  const expression = /\$(?!\$)((?:\\.|[^$\\])+?)\$(?!\$)|\\\(([\s\S]*?)\\\)/g;
  let html = '';
  let last = 0;
  for (let match = expression.exec(raw); match; match = expression.exec(raw)) {
    html += escape(raw.slice(last, match.index));
    const tex = String(match[1] ?? match[2] ?? '').trim();
    if (katex) {
      try {
        html += katex.renderToString(tex, {
          throwOnError: false,
          displayMode: false,
          strict: false,
          trust: true,
        });
      } catch {
        html += '<code class="math-error">' + escape(tex) + '</code>';
      }
    } else {
      html += '<code class="math-pending">' + escape(tex) + '</code>';
    }
    last = expression.lastIndex;
  }
  return html + escape(raw.slice(last));
}

function searchHeader(query: string): string {
  return '<div class="paper-search-head">'
    + '<button class="paper-search-back" data-tofu-action="_showPaperLanding()" title="'
    + escape(translate('paper.searchBack')) + '">'
    + '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/></svg>'
    + '</button><div class="paper-search-head-text">'
    + '<div class="paper-search-head-title">'
    + escape(translate('paper.searchResultsTitle')) + '</div>'
    + '<div class="paper-search-head-q">“' + escape(query) + '”</div>'
    + '</div></div>';
}

export function renderArxivSearchResults(
  query: string,
  results: ArxivCard[],
): void {
  const viewer = document.getElementById('paperPdfViewer');
  if (!viewer) return;
  globals()._lastArxivSearchQuery = query;
  const header = searchHeader(query);
  if (!results.length) {
    viewer.innerHTML = '<div class="paper-search">' + header
      + '<div class="paper-search-empty">'
      + escape(translate('paper.searchNoResults')) + '</div></div>';
    return;
  }
  const cards = results.map((card, index) => {
    const authors = Array.isArray(card.authors) ? card.authors.map(String) : [];
    const authorText = authors.slice(0, 4).join(', ')
      + (authors.length > 4 ? ' et al.' : '');
    const meta: string[] = [];
    if (card.primary_category) {
      meta.push('<span class="paper-card-cat">'
        + escape(card.primary_category) + '</span>');
    }
    if (card.published) {
      meta.push('<span class="paper-card-date">' + escape(card.published) + '</span>');
    }
    meta.push('<span class="paper-card-id">arXiv:' + escape(card.arxiv_id) + '</span>');
    return '<div class="paper-result-card" role="button" tabindex="0" data-idx="'
      + index + '" data-tofu-action="_openArxivResult(' + index + ')"'
      + ' data-tofu-action-keydown="if(event.key===\'Enter\'||event.key===\' \'){event.preventDefault();_openArxivResult('
      + index + ')}"><div class="paper-result-num">' + (index + 1) + '</div>'
      + '<div class="paper-result-body"><div class="paper-result-title">'
      + escapeWithInlineMath(card.title || card.arxiv_id) + '</div>'
      + (authorText
        ? '<div class="paper-result-authors">' + escape(authorText) + '</div>'
        : '')
      + (card.summary
        ? '<div class="paper-result-summary">'
          + escapeWithInlineMath(card.summary) + '</div>'
        : '')
      + '<div class="paper-result-meta">' + meta.join('') + '</div></div>'
      + '<div class="paper-result-arrow"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"/></svg></div></div>';
  }).join('');
  viewer.innerHTML = '<div class="paper-search">' + header
    + '<div class="paper-search-hint">'
    + escape(translate('paper.searchResultsHint')) + '</div>'
    + '<div class="paper-result-list">' + cards + '</div></div>';
}

export function openArxivResult(index: number): void {
  const card = globals()._paperSearchResults?.[index];
  if (card?.arxiv_id) void globals()._fetchArxivPaper?.(card.arxiv_id);
}

export function installArxivSearchGlobals(): void {
  const target = globals();
  target._looksLikeArxivRef = looksLikeArxivRef;
  target._submitArxivQuery = submitArxivQuery;
  target._searchArxivPapers = searchArxivPapers;
  target._escWithInlineMath = escapeWithInlineMath;
  target._renderArxivSearchResults = renderArxivSearchResults;
  target._openArxivResult = openArxivResult;
}

installArxivSearchGlobals();
