/** Paper-mode adapter for the shared instantiated PDF reader.
 * Owns existing Paper actions/metadata only; rendering and cancellation live in
 * pdf-reader.ts. The private registry is the retained UI compatibility boundary.
 */
import { readBrowserStorage } from '../../core/browser-storage';
import { featureRegistry } from '../../feature-registry';
import { t } from '../../i18n';
import { ensurePdfJs } from './pdf-loader';
import { createPdfReader, type PdfReader, type PdfDocument } from './pdf-reader';
import type { SearchResult } from './pdf-reader-content';
export { ensurePdfJs } from './pdf-loader';

interface PaperEntry { pageCount?: number; [key: string]: unknown }
interface PaperPorts {
  Api?: { paper?: { pdfArrayBuffer(url: string, options: { timeout: number; signal?: AbortSignal }): Promise<ArrayBuffer | Uint8Array> } };
  apiUrl?: (url: string) => string;
  debugLog?: (message: string, level?: string) => void;
  _paperPdfDoc?: PdfDocument | null;
  _paperScale?: number;
  _paperTotalPages?: number;
  _paperZoomDebounce?: number | null;
  _getActivePaperEntry?: () => PaperEntry | null;
  _persistPaperEntry?: (entry: PaperEntry) => void;
  _renderPaperLibrary?: () => void;
  _updatePaperTitles?: () => void;
  _setPaperMobileView?: (view: string) => void;
}
const ports = (): PaperPorts => featureRegistry as unknown as PaperPorts;
let reader: PdfReader | null = null;
let currentUrl = '';
let sourceGeneration = 0;

export interface PaperSourceSearch extends SearchResult {
  status: 'ready' | 'unavailable' | 'stale';
  sourceId: string;
  generation: number;
}

/** Search the displayed source, fencing results against reload and teardown. */
export async function searchPaperSource(query: string, signal: AbortSignal): Promise<PaperSourceSearch> {
  const active = reader;
  const generation = sourceGeneration;
  const sourceId = currentUrl;
  const empty = { matches: [], truncated: false, sourceId, generation };
  if (!active?.document || active.state.loading) return { ...empty, status: 'unavailable' };
  const result = await active.search(query, signal);
  if (signal.aborted || active !== reader || generation !== sourceGeneration || sourceId !== currentUrl) {
    return { ...empty, status: 'stale' };
  }
  return { ...result, sourceId, generation, status: 'ready' };
}

/** A match is navigable only while the exact searched document is displayed. */
export function openPaperSourceMatch(result: PaperSourceSearch, page: number): boolean {
  if (result.status !== 'ready' || result.generation !== sourceGeneration || result.sourceId !== currentUrl
      || !reader?.document || reader.state.loading || !result.matches.some(match => match.page === page)) return false;
  const active = reader;
  ports()._setPaperMobileView?.('pdf');
  active.goToPage(page);
  // The native verification dialog closes in this click stack. Focus after
  // that close so the reader's keyboard navigation becomes available.
  queueMicrotask(() => {
    if (reader === active && result.generation === sourceGeneration) active.container.focus({ preventScroll: true });
  });
  return true;
}

export function resolvePaperPdfUrl(url: string): string {
  if (!url || /^(?:blob:|data:)/i.test(url)) return url;
  const index = url.indexOf('/api/');
  const canonical = index >= 0 ? url.slice(index) : url;
  return ports().apiUrl?.(canonical) ?? canonical;
}
export function shouldFetchPdfAsData(): boolean { return readBrowserStorage('tofu_paper_pdf_data') === '1'; }
export async function fetchPdfArrayBuffer(url: string, signal?: AbortSignal): Promise<ArrayBuffer | Uint8Array> {
  const paper = ports().Api?.paper;
  if (!paper) throw new Error('Paper PDF API unavailable');
  return paper.pdfArrayBuffer(url, { timeout: 0, signal });
}
export async function loadPaperPdf(inputUrl: string): Promise<void> {
  const container = document.getElementById('paperPdfViewer');
  if (!container) return;
  const url = resolvePaperPdfUrl(inputUrl);
  if (reader && (reader.container !== container || currentUrl !== url)) destroyPaperPdfViewer();
  currentUrl = url;
  if (!reader) {
    const loading = document.createElement('div');
    loading.className = 'paper-loading';
    loading.textContent = t('common.loading');
    container.replaceChildren(loading);
    reader = createPdfReader(container, { onChange: (state) => {
      const shared = ports();
      shared._paperScale = state.scale;
      shared._paperTotalPages = state.pages;
      shared._paperPdfDoc = reader?.document ?? null;
      container.setAttribute('aria-busy', String(state.loading));
      syncZoomUI();
    }});
  }
  const active = reader;
  sourceGeneration++;
  try {
    const loaded = await active.load({ id: url, url, preferBytes: shouldFetchPdfAsData(),
      loadBytes: (signal) => fetchPdfArrayBuffer(url, signal) });
    if (!loaded || reader !== active) return;
    const shared = ports();
    shared._paperPdfDoc = active.document;
    shared._paperTotalPages = active.state.pages;
    shared._updatePaperTitles?.();
    const entry = shared._getActivePaperEntry?.();
    if (entry) { entry.pageCount = active.state.pages; shared._persistPaperEntry?.(entry); }
    shared._renderPaperLibrary?.();
  } catch (error) {
    if (reader !== active) return;
    ports().debugLog?.(`[Paper] PDF load failed: ${String(error)}`, 'warning');
    if (!active.document) {
      const message = document.createElement('div');
      message.className = 'paper-error';
      message.textContent = t('paper.pdfLoadFailed');
      container.replaceChildren(message);
    }
  }
}
export function paperZoomIn(): void { reader?.setScale((reader?.state.scale ?? 1) + 0.25); }
export function paperZoomOut(): void { reader?.setScale((reader?.state.scale ?? 1) - 0.25); }
export function paperSetScaleFromSlider(value: string | number): void {
  reader?.setScale(Number.parseFloat(String(value)) / 100);
}
export function paperSetScaleFromInput(value: string): void { paperSetScaleFromSlider(value); }
export function paperFitWidth(): void { reader?.setScale('fit'); }
/** Existing wheel controls dispatch a scale change through this adapter. */
export async function renderAllPages(): Promise<false> { reader?.setScale(ports()._paperScale ?? 1); return false; }
export function paperViewerPadX(_container: Element): number { return 32; }
export function syncZoomUI(): void {
  const percentage = Math.round((reader?.state.scale ?? ports()._paperScale ?? 1) * 100);
  const input = document.getElementById('paperZoomLevel') as HTMLInputElement | null;
  const slider = document.getElementById('paperZoomSlider') as HTMLInputElement | null;
  if (input) input.value = `${percentage}%`;
  if (slider) slider.value = String(percentage);
}
export function destroyPaperPdfViewer(): void {
  sourceGeneration++;
  reader?.container.removeAttribute('aria-busy');
  reader?.destroy();
  reader = null;
  currentUrl = '';
  const shared = ports();
  shared._paperPdfDoc = null;
  shared._paperTotalPages = 0;
  if (shared._paperZoomDebounce != null) window.clearTimeout(shared._paperZoomDebounce);
  shared._paperZoomDebounce = null;
}
export function installPaperPdfViewerGlobals(): void {
  Object.assign(featureRegistry, {
    _ensurePdfJs: ensurePdfJs, _resolvePaperPdfUrl: resolvePaperPdfUrl,
    _shouldFetchPdfAsData: shouldFetchPdfAsData, _fetchPdfArrayBuffer: fetchPdfArrayBuffer,
    _loadPaperPdf: loadPaperPdf, _renderAllPages: renderAllPages,
    paperZoomIn, paperZoomOut, paperSetScaleFromSlider, paperSetScaleFromInput, paperFitWidth,
    _paperViewerPadX: paperViewerPadX, _syncZoomUI: syncZoomUI, _updateZoomLabel: syncZoomUI,
    _destroyPaperPdfViewer: destroyPaperPdfViewer,
  });
}
installPaperPdfViewerGlobals();
