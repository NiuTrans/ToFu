import { featureRegistry } from '../../feature-registry';
type JsonObject = Record<string, unknown>;
type PdfJsModule = typeof import('pdfjs-dist/legacy/build/pdf.mjs');

interface PdfViewport {
  width: number;
  height: number;
}

interface PdfPage {
  getViewport(options: { scale: number }): PdfViewport;
  render(options: JsonObject): { promise: Promise<unknown> };
  getTextContent(): Promise<unknown>;
}

interface PdfDocument {
  numPages: number;
  getPage(page: number): Promise<PdfPage>;
  destroy(): unknown;
}

interface PdfJs {
  getDocument(source: string | { data: ArrayBuffer | Uint8Array }): {
    promise: Promise<PdfDocument>;
  };
  renderTextLayer?(options: JsonObject): unknown;
}

interface PaperApi {
  pdfArrayBuffer(
    url: string,
    options: { timeout: number },
  ): Promise<ArrayBuffer | Uint8Array>;
}

interface PaperEntry extends JsonObject {
  pageCount?: number;
}

type PaperPdfWindow = Window & {
  Api?: { paper?: PaperApi };
  pdfjsLib?: PdfJs;
  apiUrl?: (url: string) => string;
  debugLog?: (message: string, level?: string) => void;
  escapeHtml?: (value: unknown) => string;
  _ensurePdfJs?: () => Promise<unknown>;
  _paperNow?: () => number;
  _updatePaperTitles?: () => void;
  _getActivePaperEntry?: () => PaperEntry | null;
  _persistPaperEntry?: (entry: PaperEntry) => void;
  _renderPaperLibrary?: () => void;
  _paperPdfDoc?: PdfDocument | null;
  _paperScale?: number;
  _paperTotalPages?: number;
  _paperCurrentUrl?: string;
  _paperViaData?: boolean;
  _paperRenderToken?: number;
  _paperLoadGen?: number;
  _paperReopenInFlight?: boolean;
  _paperIntersectionObserver?: IntersectionObserver | null;
  _paperResizeObserver?: ResizeObserver | null;
  _paperZoomDebounce?: number | null;
  _resolvePaperPdfUrl?: typeof resolvePaperPdfUrl;
  _shouldFetchPdfAsData?: typeof shouldFetchPdfAsData;
  _fetchPdfArrayBuffer?: typeof fetchPdfArrayBuffer;
  _openPaperPdfDoc?: typeof openPaperPdfDoc;
  _loadPaperPdf?: typeof loadPaperPdf;
  _renderAllPages?: typeof renderAllPages;
  _rasterizePage?: typeof rasterizePage;
  _releasePage?: typeof releasePage;
  _maybeReopenViaData?: typeof maybeReopenViaData;
  _observePageWrappers?: typeof observePageWrappers;
  paperZoomIn?: typeof paperZoomIn;
  paperZoomOut?: typeof paperZoomOut;
  paperSetScaleFromSlider?: typeof paperSetScaleFromSlider;
  paperSetScaleFromInput?: typeof paperSetScaleFromInput;
  paperFitWidth?: typeof paperFitWidth;
  _paperViewerPadX?: typeof paperViewerPadX;
  _syncZoomUI?: typeof syncZoomUI;
  _updateZoomLabel?: typeof syncZoomUI;
  _destroyPaperPdfViewer?: typeof destroyPaperPdfViewer;
};

interface OpenedPdf {
  doc: PdfDocument;
  viaData: boolean;
}

let loadedPdfJs: PdfJs | undefined;
let pdfJsLoading: Promise<PdfJs> | undefined;

function globals(): PaperPdfWindow {
  return featureRegistry as unknown as PaperPdfWindow;
}

function state(): PaperPdfWindow {
  const target = globals();
  target._paperScale ??= 1.5;
  target._paperTotalPages ??= 0;
  target._paperCurrentUrl ??= '';
  target._paperViaData ??= false;
  target._paperRenderToken ??= 0;
  target._paperLoadGen ??= 0;
  target._paperReopenInFlight ??= false;
  target._paperIntersectionObserver ??= null;
  target._paperResizeObserver ??= null;
  target._paperZoomDebounce ??= null;
  return target;
}

function currentPdfJs(): PdfJs | undefined {
  return loadedPdfJs ?? globals().pdfjsLib;
}

function pdfjs(): PdfJs {
  const library = currentPdfJs();
  if (!library) throw new Error('PDF.js is unavailable');
  return library;
}

/** Load PDF.js and its matching worker as one retryable, Paper-owned unit. */
export function ensurePdfJs(): Promise<PdfJs> {
  const installed = currentPdfJs();
  if (installed) return Promise.resolve(installed);
  if (pdfJsLoading) return pdfJsLoading;

  pdfJsLoading = Promise.all([
    import('pdfjs-dist/legacy/build/pdf.mjs'),
    import('pdfjs-dist/legacy/build/pdf.worker.min.mjs?url'),
  ]).then(([module, worker]) => {
    module.GlobalWorkerOptions.workerSrc = worker.default;
    const library = module as unknown as PdfJs;
    loadedPdfJs = library;
    // Keep the private feature service in sync for retained Paper islands and
    // existing injected test doubles. This does not expose a window global.
    globals().pdfjsLib = library;
    return library;
  }).catch((error: unknown) => {
    // A transient chunk/proxy failure must not poison every later retry in the
    // same tab. Vite's preload handler may repair the URL or reload the graph.
    pdfJsLoading = undefined;
    throw error;
  });
  return pdfJsLoading;
}

function log(message: string, level = 'info'): void {
  globals().debugLog?.(message, level);
}

function now(): number {
  const clock = globals()._paperNow;
  if (typeof clock === 'function') return clock();
  return typeof performance !== 'undefined' && performance.now
    ? performance.now() : Date.now();
}

function errorMessage(error: unknown): string {
  if (error instanceof Error) return error.message;
  return String(error ?? 'unknown error');
}

function escape(value: unknown): string {
  const helper = globals().escapeHtml;
  if (typeof helper === 'function') return helper(value);
  const span = document.createElement('span');
  span.textContent = value == null ? '' : String(value);
  return span.innerHTML;
}

/** Re-base a persisted API URL onto the live proxy base path. */
export function resolvePaperPdfUrl(url: string): string {
  if (!url) return url;
  const index = url.indexOf('/api/');
  if (index < 0) return url;
  const canonical = url.slice(index);
  return globals().apiUrl?.(canonical) ?? canonical;
}

/** Optional operator gate for bypassing broken HTTP Range proxies. */
export function shouldFetchPdfAsData(): boolean {
  try {
    return localStorage.getItem('tofu_paper_pdf_data') === '1';
  } catch {
    return false;
  }
}

/** Download bytes through the unified API client, with no transfer timeout. */
export async function fetchPdfArrayBuffer(
  url: string,
): Promise<ArrayBuffer | Uint8Array> {
  const index = (url || '').indexOf('/api/');
  const canonical = index >= 0 ? url.slice(index) : url;
  const paper = globals().Api?.paper;
  if (!paper) throw new Error('Paper PDF API unavailable');
  return paper.pdfArrayBuffer(canonical, { timeout: 0 });
}

/** Open through pdf.js, retrying exactly once with client-owned bytes. */
export async function openPaperPdfDoc(
  url: string,
  forceData = false,
): Promise<OpenedPdf> {
  if (forceData || shouldFetchPdfAsData()) {
    log('[Paper] Loading PDF via client ArrayBuffer (range-bypass)…');
    const bytes = await fetchPdfArrayBuffer(url);
    return { doc: await pdfjs().getDocument({ data: bytes }).promise, viaData: true };
  }
  let documentHandle: PdfDocument | null = null;
  try {
    documentHandle = await pdfjs().getDocument(url).promise;
    await documentHandle.getPage(1);
    return { doc: documentHandle, viaData: false };
  } catch (error: unknown) {
    try { documentHandle?.destroy(); } catch { /* best-effort stale doc cleanup */ }
    log(
      `[Paper] URL load failed (${errorMessage(error)}) — auto-retrying via `
      + 'client ArrayBuffer (range-bypass)…',
      'warning',
    );
    const bytes = await fetchPdfArrayBuffer(url);
    return { doc: await pdfjs().getDocument({ data: bytes }).promise, viaData: true };
  }
}

/** Load one selected paper; generation fencing prevents stale selection writes. */
export async function loadPaperPdf(inputUrl: string): Promise<void> {
  const shared = state();
  const generation = (shared._paperLoadGen ?? 0) + 1;
  shared._paperLoadGen = generation;
  const stale = (): boolean => generation !== state()._paperLoadGen;
  const url = resolvePaperPdfUrl(inputUrl);
  shared._paperCurrentUrl = url;
  const viewer = document.getElementById('paperPdfViewer');
  if (!viewer) return;
  viewer.innerHTML = '<div class="paper-loading"><div class="paper-loading-spinner">'
    + '</div><div>Loading PDF…</div></div>';

  try {
    try {
      await ensurePdfJs();
    } catch (error: unknown) {
      console.error('[Paper] PDF.js failed to load:', error);
      if (!stale()) {
        viewer.innerHTML = '<div class="paper-error">PDF.js failed to load: '
          + `${escape(errorMessage(error))}</div>`;
      }
      return;
    }
    if (stale()) return;

    try { shared._paperPdfDoc?.destroy(); } catch { /* best-effort old doc cleanup */ }
    shared._paperPdfDoc = null;
    const started = now();
    const opened = await openPaperPdfDoc(url);
    if (stale()) {
      try { opened.doc.destroy(); } catch { /* best-effort stale doc cleanup */ }
      return;
    }
    shared._paperPdfDoc = opened.doc;
    shared._paperViaData = opened.viaData;
    shared._paperTotalPages = opened.doc.numPages;
    log(
      `[Paper] doc opened in ${Math.round(now() - started)}ms `
      + `(viaData=${opened.viaData}, pages=${opened.doc.numPages})`,
    );
    globals()._updatePaperTitles?.();
    try {
      const firstPage = await opened.doc.getPage(1);
      const viewport = firstPage.getViewport({ scale: 1 });
      const container = document.getElementById('paperPdfViewer');
      const width = container
        ? container.clientWidth - paperViewerPadX(container) : 0;
      if (width > 0) shared._paperScale = Math.max(0.25, Math.min(4, width / viewport.width));
    } catch (error: unknown) {
      console.warn('[Paper] Initial fit-width failed:', error);
    }
    syncZoomUI();
    await renderAllPages();

    const entry = globals()._getActivePaperEntry?.();
    if (entry) {
      entry.pageCount = shared._paperTotalPages;
      globals()._persistPaperEntry?.(entry);
    }
    globals()._renderPaperLibrary?.();
  } catch (error: unknown) {
    console.error('[Paper] Failed to load PDF:', error);
    if (!stale()) {
      viewer.innerHTML = `<div class="paper-error">Failed to load PDF: `
        + `${escape(errorMessage(error))}</div>`;
    }
  }
}

/** Build stable page shells, raster page one, then virtualize the remainder. */
export async function renderAllPages(): Promise<false> {
  const shared = state();
  const documentHandle = shared._paperPdfDoc;
  const viewer = document.getElementById('paperPdfViewer');
  if (!documentHandle || !viewer) return false;
  const token = (shared._paperRenderToken ?? 0) + 1;
  shared._paperRenderToken = token;
  viewer.innerHTML = '';
  shared._paperIntersectionObserver?.disconnect();
  shared._paperIntersectionObserver = null;
  const started = now();
  const scale = shared._paperScale ?? 1.5;

  for (let pageNumber = 1; pageNumber <= (shared._paperTotalPages ?? 0); pageNumber += 1) {
    if (token !== state()._paperRenderToken) return false;
    let width: number;
    let height: number;
    try {
      const page = await documentHandle.getPage(pageNumber);
      const viewport = page.getViewport({ scale });
      width = viewport.width;
      height = viewport.height;
    } catch (error: unknown) {
      console.warn('[Paper] Failed to size page', pageNumber, ':', error);
      width = 612 * scale;
      height = 792 * scale;
    }
    const wrapper = document.createElement('div');
    wrapper.className = 'paper-page-wrapper';
    wrapper.dataset.page = String(pageNumber);
    wrapper.dataset.rendered = '0';
    wrapper.style.width = `${width}px`;
    wrapper.style.aspectRatio = (width / height).toFixed(6);
    const placeholder = document.createElement('div');
    placeholder.className = 'paper-page-placeholder';
    wrapper.appendChild(placeholder);
    const label = document.createElement('div');
    label.className = 'paper-page-label';
    label.textContent = `${pageNumber} / ${shared._paperTotalPages ?? 0}`;
    wrapper.appendChild(label);
    viewer.appendChild(wrapper);
  }
  if (token !== state()._paperRenderToken) return false;
  log(
    `[Paper] page shells laid out in ${Math.round(now() - started)}ms `
    + `(${shared._paperTotalPages ?? 0} pages, virtualized)`,
  );

  const wrappers = Array.from(
    viewer.querySelectorAll<HTMLElement>('.paper-page-wrapper'),
  );
  if (typeof IntersectionObserver !== 'undefined') {
    const observer = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        const wrapper = entry.target as HTMLElement;
        if (entry.isIntersecting) {
          void rasterizePage(wrapper, token).then((needsReopen) => {
            if (needsReopen) void maybeReopenViaData();
          });
        } else {
          releasePage(wrapper);
        }
      });
    }, {
      root: viewer,
      rootMargin: '150% 0px 150% 0px',
      threshold: 0.01,
    });
    shared._paperIntersectionObserver = observer;
    wrappers.forEach((wrapper) => observer.observe(wrapper));
  } else {
    for (const wrapper of wrappers) {
      if (await rasterizePage(wrapper, token)) {
        await maybeReopenViaData();
        return false;
      }
    }
  }

  if (wrappers[0]) {
    if (await rasterizePage(wrappers[0], token)) {
      await maybeReopenViaData();
      return false;
    }
    if (token === state()._paperRenderToken) {
      log(`[Paper] first page painted in ${Math.round(now() - started)}ms`);
    }
  }
  observePageWrappers(viewer);
  return false;
}

/** Rasterize one page at device resolution and add its selectable text layer. */
export async function rasterizePage(
  wrapper: HTMLElement | null,
  token?: number | null,
): Promise<boolean | undefined> {
  const shared = state();
  const documentHandle = shared._paperPdfDoc;
  if (!wrapper || !documentHandle) return undefined;
  if (wrapper.dataset.rendered === '1' || wrapper.dataset.rendering === '1') {
    return undefined;
  }
  if (token != null && token !== shared._paperRenderToken) return undefined;
  const pageNumber = Number.parseInt(wrapper.dataset.page || '', 10);
  if (!pageNumber) return undefined;
  wrapper.dataset.rendering = '1';
  const scale = shared._paperScale ?? 1.5;
  try {
    const page = await documentHandle.getPage(pageNumber);
    if (token != null && token !== state()._paperRenderToken) {
      wrapper.dataset.rendering = '0';
      return undefined;
    }
    const viewport = page.getViewport({ scale });
    const highResolution = page.getViewport({
      scale: scale * (window.devicePixelRatio || 1),
    });
    wrapper.style.width = `${viewport.width}px`;
    wrapper.style.aspectRatio = (viewport.width / viewport.height).toFixed(6);
    const canvas = document.createElement('canvas');
    canvas.className = 'paper-pdf-canvas';
    canvas.width = highResolution.width;
    canvas.height = highResolution.height;
    canvas.style.width = `${viewport.width}px`;
    const textLayer = document.createElement('div');
    textLayer.className = 'paper-text-layer';
    textLayer.style.width = `${viewport.width}px`;
    textLayer.style.height = `${viewport.height}px`;
    textLayer.style.setProperty('--scale-factor', String(scale));
    await page.render({
      canvasContext: canvas.getContext('2d'),
      viewport: highResolution,
    }).promise;
    if (token != null && token !== state()._paperRenderToken) {
      wrapper.dataset.rendering = '0';
      return undefined;
    }
    wrapper.querySelector('.paper-page-placeholder')?.remove();
    const label = wrapper.querySelector('.paper-page-label');
    wrapper.insertBefore(canvas, label);
    wrapper.insertBefore(textLayer, label);
    const textContent = await page.getTextContent();
    pdfjs().renderTextLayer?.({
      textContentSource: textContent,
      container: textLayer,
      viewport,
      textDivs: [],
    });
    wrapper.dataset.rendered = '1';
    wrapper.dataset.rendering = '0';
    return false;
  } catch (error: unknown) {
    wrapper.dataset.rendering = '0';
    console.warn('[Paper] Failed to render page', pageNumber, ':', error);
    if (!state()._paperViaData) return true;
    const errorNode = document.createElement('div');
    errorNode.className = 'paper-page-error';
    errorNode.textContent = `Page ${pageNumber} failed to render`;
    wrapper.insertBefore(errorNode, wrapper.querySelector('.paper-page-label'));
    return false;
  }
}

/** Release off-screen raster/text memory while preserving page geometry. */
export function releasePage(wrapper: HTMLElement | null): void {
  if (!wrapper || wrapper.dataset.rendered !== '1') return;
  wrapper.querySelector('.paper-pdf-canvas')?.remove();
  wrapper.querySelector('.paper-text-layer')?.remove();
  if (!wrapper.querySelector('.paper-page-placeholder')) {
    const placeholder = document.createElement('div');
    placeholder.className = 'paper-page-placeholder';
    wrapper.insertBefore(placeholder, wrapper.firstChild);
  }
  wrapper.dataset.rendered = '0';
}

/** Single-flight byte-backed recovery for later-page Range failures. */
export async function maybeReopenViaData(): Promise<void> {
  const shared = state();
  if (shared._paperReopenInFlight || shared._paperViaData || !shared._paperCurrentUrl) return;
  shared._paperReopenInFlight = true;
  const generation = shared._paperLoadGen;
  try {
    log(
      '[Paper] A page failed to rasterize — re-opening via client ArrayBuffer '
      + '(range-bypass) and re-rendering…',
      'warning',
    );
    try { shared._paperPdfDoc?.destroy(); } catch { /* best-effort old doc cleanup */ }
    shared._paperPdfDoc = null;
    const reopened = await openPaperPdfDoc(shared._paperCurrentUrl, true);
    if (generation !== state()._paperLoadGen) {
      try { reopened.doc.destroy(); } catch { /* best-effort stale doc cleanup */ }
      return;
    }
    shared._paperPdfDoc = reopened.doc;
    shared._paperViaData = reopened.viaData;
    shared._paperTotalPages = reopened.doc.numPages;
    await renderAllPages();
  } catch (error: unknown) {
    console.error('[Paper] {data} re-open failed:', error);
  } finally {
    shared._paperReopenInFlight = false;
  }
}

/** Keep selectable text aligned when responsive layout constrains a page. */
export function observePageWrappers(viewer: Element): void {
  const shared = state();
  shared._paperResizeObserver?.disconnect();
  shared._paperResizeObserver = null;
  if (typeof ResizeObserver === 'undefined') return;
  const observer = new ResizeObserver((entries) => {
    entries.forEach((entry) => {
      const wrapper = entry.target as HTMLElement;
      const textLayer = wrapper.querySelector<HTMLElement>('.paper-text-layer');
      if (!textLayer) return;
      const originalWidth = Number.parseFloat(textLayer.style.width);
      if (!originalWidth) return;
      const sizes = entry.contentBoxSize as unknown as
        ResizeObserverSize | ResizeObserverSize[];
      const inlineSize = Array.isArray(sizes)
        ? sizes[0]?.inlineSize : sizes?.inlineSize;
      const actualWidth = inlineSize || wrapper.clientWidth;
      const ratio = actualWidth / originalWidth;
      textLayer.style.transform = Math.abs(ratio - 1) < 0.001
        ? '' : `scale(${ratio.toFixed(6)})`;
    });
  });
  shared._paperResizeObserver = observer;
  viewer.querySelectorAll('.paper-page-wrapper').forEach((wrapper) => {
    observer.observe(wrapper);
  });
}

export function paperZoomIn(): void {
  const shared = state();
  shared._paperScale = Math.min((shared._paperScale ?? 1.5) + 0.25, 4);
  syncZoomUI();
  void renderAllPages();
}

export function paperZoomOut(): void {
  const shared = state();
  shared._paperScale = Math.max((shared._paperScale ?? 1.5) - 0.25, 0.25);
  syncZoomUI();
  void renderAllPages();
}

export function paperSetScaleFromSlider(value: string | number): void {
  const shared = state();
  const percentage = Number.parseInt(String(value), 10);
  shared._paperScale = Math.max(0.25, Math.min(4, percentage / 100));
  syncZoomUI();
  if (shared._paperZoomDebounce != null) {
    window.clearTimeout(shared._paperZoomDebounce);
  }
  shared._paperZoomDebounce = window.setTimeout(() => {
    void renderAllPages();
  }, 120);
}

export function paperSetScaleFromInput(value: string): void {
  let percentage = Number.parseInt(value.replace('%', ''), 10);
  if (!Number.isFinite(percentage) || percentage < 25) percentage = 25;
  if (percentage > 400) percentage = 400;
  state()._paperScale = percentage / 100;
  syncZoomUI();
  void renderAllPages();
}

export function paperViewerPadX(container: Element): number {
  try {
    const style = getComputedStyle(container);
    const padding = (Number.parseFloat(style.paddingLeft) || 0)
      + (Number.parseFloat(style.paddingRight) || 0);
    return padding > 0 ? padding : 32;
  } catch (error: unknown) {
    console.warn('[Paper] padding measure failed, using 32:', error);
    return 32;
  }
}

export function paperFitWidth(): void {
  const shared = state();
  const documentHandle = shared._paperPdfDoc;
  const container = document.getElementById('paperPdfViewer');
  if (!documentHandle || !container) return;
  void documentHandle.getPage(1).then((page) => {
    const viewport = page.getViewport({ scale: 1 });
    const width = container.clientWidth - paperViewerPadX(container);
    shared._paperScale = Math.max(0.25, Math.min(4, width / viewport.width));
    syncZoomUI();
    void renderAllPages();
  }).catch((error: unknown) => {
    console.warn('[Paper] fit-width failed:', error);
  });
}

export function syncZoomUI(): void {
  const percentage = Math.round((state()._paperScale ?? 1.5) * 100);
  const input = document.getElementById('paperZoomLevel') as HTMLInputElement | null;
  const slider = document.getElementById('paperZoomSlider') as HTMLInputElement | null;
  if (input) input.value = `${percentage}%`;
  if (slider) slider.value = String(percentage);
}

export function destroyPaperPdfViewer(): void {
  const shared = state();
  shared._paperIntersectionObserver?.disconnect();
  shared._paperIntersectionObserver = null;
  shared._paperResizeObserver?.disconnect();
  shared._paperResizeObserver = null;
  if (shared._paperZoomDebounce != null) {
    window.clearTimeout(shared._paperZoomDebounce);
    shared._paperZoomDebounce = null;
  }
  shared._paperRenderToken = (shared._paperRenderToken ?? 0) + 1;
  shared._paperLoadGen = (shared._paperLoadGen ?? 0) + 1;
}

export function installPaperPdfViewerGlobals(): void {
  const target = state();
  target._ensurePdfJs = ensurePdfJs;
  target._resolvePaperPdfUrl = resolvePaperPdfUrl;
  target._shouldFetchPdfAsData = shouldFetchPdfAsData;
  target._fetchPdfArrayBuffer = fetchPdfArrayBuffer;
  target._openPaperPdfDoc = openPaperPdfDoc;
  target._loadPaperPdf = loadPaperPdf;
  target._renderAllPages = renderAllPages;
  target._rasterizePage = rasterizePage;
  target._releasePage = releasePage;
  target._maybeReopenViaData = maybeReopenViaData;
  target._observePageWrappers = observePageWrappers;
  target.paperZoomIn = paperZoomIn;
  target.paperZoomOut = paperZoomOut;
  target.paperSetScaleFromSlider = paperSetScaleFromSlider;
  target.paperSetScaleFromInput = paperSetScaleFromInput;
  target.paperFitWidth = paperFitWidth;
  target._paperViewerPadX = paperViewerPadX;
  target._syncZoomUI = syncZoomUI;
  target._updateZoomLabel = syncZoomUI;
  target._destroyPaperPdfViewer = destroyPaperPdfViewer;
}

installPaperPdfViewerGlobals();
