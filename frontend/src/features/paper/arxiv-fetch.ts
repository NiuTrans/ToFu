import { readSSEStream } from '../../core/sse-reader';
import { featureRegistry } from '../../feature-registry';
import { escapeHtml as escape } from '../../html-safety';
type JsonObject = Record<string, unknown>;

interface PaperEntry extends JsonObject { id: string }
interface ArxivFetchApi {
  fetchArxivStream(reference: string, paperId: string): Promise<Response | null>;
}
type ArxivFetchWindow = Window & {
  Api?: { paper?: ArxivFetchApi };
  apiUrl?: (url: string) => string;
  debugLog?: (message: string, level?: string) => void;
  _paperLibrary?: PaperEntry[];
  _activePaperId?: string;
  _paperLoading?: boolean;
  _paperPdfUrl?: string;
  _paperPdfFilename?: string;
  _paperArxivId?: string;
  _paperFileName?: string;
  _paperParsedText?: string;
  _paperTotalPages?: number;
  _paperHash?: string;
  _paperImages?: unknown[];
  _paperQAHistory?: JsonObject[];
  _paperReportCache?: string;
  _paperReviewCache?: string;
  _paperReviewVenue?: string;
  _babelTranslatedPages?: JsonObject;
  _arxivFetchGeneration?: number;
  _renderArxivFetchProgress?: (event: JsonObject) => void;
  _newPaperEntryId?: () => string;
  _createPaperEntry?: (
    title: string,
    pdfUrl: string,
    parsedText: string,
    arxivId: string,
    paperId: string,
  ) => unknown;
  _updatePaperTitles?: () => void;
  _renderPaperLibrary?: () => void;
  _loadPaperPdf?: (url: string) => Promise<unknown>;
  _saveActivePaperState?: () => Promise<unknown> | unknown;
  _setActivePaperId?: (paperId: string) => void;
  _fetchArxivPaper?: typeof fetchArxivPaper;
  _destroyArxivFetch?: typeof destroyArxivFetch;
};

function globals(): ArxivFetchWindow {
  return featureRegistry as unknown as ArxivFetchWindow;
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error ?? '');
}

function stringField(row: JsonObject, field: string): string {
  return typeof row[field] === 'string' ? row[field] as string : '';
}

function isCurrent(generation: number): boolean {
  return globals()._arxivFetchGeneration === generation;
}

function renderFetchProgress(event: JsonObject): void {
  const renderer = globals()._renderArxivFetchProgress;
  if (typeof renderer !== 'function') {
    throw new Error('Paper arXiv progress-renderer port is unavailable');
  }
  renderer(event);
}

export async function fetchArxivPaper(
  directReference?: string | null,
  reuseId?: string,
): Promise<void> {
  const state = globals();
  const input = document.getElementById('paperArxivUrl') as HTMLInputElement | null;
  const reference = (directReference ?? input?.value ?? '').trim();
  if (!reference) {
    state.debugLog?.('Please enter an arXiv URL or ID', 'warning');
    return;
  }
  const api = state.Api?.paper;
  const newId = state._newPaperEntryId;
  if (!api || !newId) {
    throw new Error('arXiv ingest dependencies unavailable');
  }

  const generation = (state._arxivFetchGeneration ?? 0) + 1;
  state._arxivFetchGeneration = generation;
  state._paperLoading = true;
  renderFetchProgress({ stage: 'resolve' });
  const library = state._paperLibrary ?? [];
  const reusing = Boolean(reuseId && library.some((paper) => paper.id === reuseId));
  const paperId = reuseId || newId();

  try {
    const response = await api.fetchArxivStream(reference, paperId);
    if (!response?.ok || !response.body) {
      let detail = '';
      if (response) {
        try {
          const envelope = await response.json() as JsonObject;
          detail = stringField(envelope, 'error');
        } catch { /* HTTP status remains the fallback */ }
      }
      throw new Error(detail || `HTTP ${response?.status ?? '?'}`);
    }
    let done: JsonObject | null = null;
    let streamError = '';
    let currentArxivId = '';
    await readSSEStream(response, {
      flushTail: false,
      onLine(line) {
        if (!isCurrent(generation)) return true;
        if (!line.startsWith('data: ')) return false;
        const payload = line.slice(6).trim();
        if (!payload) return false;
        let event: JsonObject;
        try { event = JSON.parse(payload) as JsonObject; } catch (error: unknown) {
          console.warn('[Paper:arXiv] Bad SSE payload:', error, payload);
          return false;
        }
        const eventArxivId = stringField(event, 'arxiv_id');
        if (eventArxivId) currentArxivId = eventArxivId;
        event.arxiv_id = eventArxivId || currentArxivId;
        if (event.stage === 'error') {
          streamError = stringField(event, 'error') || 'Fetch failed';
          return true;
        }
        renderFetchProgress(event);
        if (event.stage === 'done') done = event;
        return false;
      },
    });
    if (!isCurrent(generation)) return;
    if (streamError) throw new Error(streamError);
    if (!done) throw new Error('Fetch ended without completion');
    const result = done as JsonObject;
    const rawPdfUrl = stringField(result, 'pdf_url');
    state._paperPdfUrl = state.apiUrl?.(rawPdfUrl) ?? rawPdfUrl;
    const match = /\/api\/paper\/pdf\/([^?#]+)/.exec(rawPdfUrl);
    state._paperPdfFilename = match ? decodeURIComponent(match[1]) : '';
    state._paperArxivId = stringField(result, 'arxiv_id') || currentArxivId;
    state._paperFileName = stringField(result, 'title').trim()
      || `arXiv:${state._paperArxivId}`;
    state._paperParsedText = stringField(result, 'parsed_text');
    state._paperTotalPages = Number(result.total_pages) || 0;
    state._paperHash = stringField(result, 'paper_hash');
    state._paperImages = Array.isArray(result.images) ? result.images : [];
    state._createPaperEntry?.(
      state._paperFileName,
      state._paperPdfUrl,
      state._paperParsedText,
      state._paperArxivId,
      paperId,
    );
    state._paperQAHistory = [];
    state._paperReportCache = '';
    state._paperReviewCache = '';
    state._paperReviewVenue = '';
    state._babelTranslatedPages = {};
    state._updatePaperTitles?.();
    state._renderPaperLibrary?.();

    const parseError = stringField(result, 'parse_error');
    if (parseError) {
      state.debugLog?.(`[Paper] PDF text extraction failed: ${parseError}`, 'warning');
    } else if (state._paperParsedText) {
      const length = Number(result.text_length) || state._paperParsedText.length;
      const figures = state._paperImages.length
        ? ` (${state._paperImages.length} figures)` : '';
      state.debugLog?.(
        `arXiv parsed: ${state._paperTotalPages} pages, ${length} chars${figures}`,
        'success',
      );
    } else {
      state.debugLog?.(
        '[Paper] arXiv PDF loaded but no text extracted — Q&A and Report unavailable',
        'warning',
      );
    }
    await state._loadPaperPdf?.(state._paperPdfUrl);
    if (!isCurrent(generation)) return;
    await state._saveActivePaperState?.();
    state.debugLog?.(
      `Fetched arXiv:${state._paperArxivId}${result.cached ? ' (cached)' : ''}`,
      'success',
    );
  } catch (error: unknown) {
    if (!isCurrent(generation)) return;
    console.error('[Paper] arXiv fetch failed:', error);
    if (!reusing) {
      state._paperLibrary = (state._paperLibrary ?? []).filter(
        (paper) => paper.id !== paperId,
      );
      if (state._activePaperId === paperId) state._setActivePaperId?.('');
    }
    state._renderPaperLibrary?.();
    const viewer = document.getElementById('paperPdfViewer');
    if (viewer) {
      viewer.innerHTML = '<div class="paper-error">Failed: ' + escape(message(error))
        + '<br><button data-tofu-action="_showPaperLanding()" class="paper-retry-btn">'
        + 'Try Again</button></div>';
    }
  } finally {
    if (isCurrent(generation)) state._paperLoading = false;
  }
}

export function destroyArxivFetch(): void {
  const state = globals();
  state._arxivFetchGeneration = (state._arxivFetchGeneration ?? 0) + 1;
  state._paperLoading = false;
}

export function installArxivFetchGlobals(): void {
  const target = globals();
  target._fetchArxivPaper = fetchArxivPaper;
  target._destroyArxivFetch = destroyArxivFetch;
}

installArxivFetchGlobals();
