/** Independent PDF reader: bounded raster/text residency and atomic document swaps.
 * Public entry: createPdfReader. Owns document/loading/render handles, page shells,
 * reading anchors and teardown. UI chrome owns labels, controls and build tasks.
 */
import '../../styles/application/03-pdf-reader.css';

import { createLifecycleScope } from '../../lifecycle';
import { PAPER_BUILD_LIMITS as limits } from '../../api/paper-build.generated';
import { ensurePdfJs, type PdfJsModule } from './pdf-loader';
import { addPageLinks, columnTextBounds, readOutline, searchDocument, type OutlineItem, type SearchResult } from './pdf-reader-content';
import { bindPdfPinchZoom } from './pdf-reader-gestures';

export type PdfDocument = Awaited<ReturnType<PdfJsModule['getDocument']>['promise']>;
export type PdfPage = Awaited<ReturnType<PdfDocument['getPage']>>;
type LoadingTask = ReturnType<PdfJsModule['getDocument']>;
type RenderingTask = ReturnType<PdfPage['render']>;
type TextLayer = InstanceType<PdfJsModule['TextLayer']>;

export interface ReadingAnchor { page: number; fraction: number; horizontal: number }
export interface ReadingPoint { sourceId: string; page: number; x: number; y: number }
export interface ReaderState {
  sourceId: string; page: number; pages: number; scale: number; fit: boolean;
  loading: boolean; canvasBytes: number;
}
export interface ReaderSource {
  id: string;
  url: string;
  /** Optional proxy fallback; the shared API transport owns this cancellable fetch. */
  loadBytes?: (signal: AbortSignal) => Promise<ArrayBuffer | Uint8Array>;
  preferBytes?: boolean;
  initialView?: ReadingAnchor & { scale: number; fit: boolean };
}
interface ReaderOptions {
  loadPdfJs?: () => Promise<PdfJsModule>;
  onChange?: (state: ReaderState) => void;
  onError?: (error: unknown) => void;
}
interface Surface {
  element: HTMLDivElement; canvas: HTMLCanvasElement; scale: number;
  text?: TextLayer; render?: RenderingTask;
}
interface PageShell {
  number: number; element: HTMLDivElement; width: number; height: number;
  surface?: Surface; pending?: Surface; page?: PdfPage; rendering: boolean; renderScale?: number; revision: number;
}
interface DocumentView {
  id: string; document: PdfDocument; tree: HTMLDivElement; pages: PageShell[]; disposed: boolean;
}
interface LoadAttempt { controller: AbortController; task?: LoadingTask; view?: DocumentView; resumeZoom?: () => void }
const clamp = (value: number, min: number, max: number): number => Math.max(min, Math.min(max, value));

export class PdfReader {
  private readonly scope = createLifecycleScope();
  private readonly canvases = new Map<HTMLCanvasElement, number>();
  private current: DocumentView | null = null;
  private pending: LoadAttempt | null = null;
  private library: PdfJsModule | null = null;
  private scale = 1;
  private fit = true;
  private destroyed = false;
  private frame = 0;
  private pumping = false;
  private pumpAgain = false;
  private searchGeneration = 0;
  private highlightTimer = 0;
  private zooming = false;
  private navigationGeneration = 0;
  private layoutSuspended = false;
  private savedAnchor: ReadingAnchor = { page: 1, fraction: 0, horizontal: 0.5 };
  private lastWidth = 0;
  private readonly originalTabIndex: string | null;

  constructor(readonly container: HTMLElement, private readonly options: ReaderOptions = {}) {
    this.originalTabIndex = container.getAttribute('tabindex');
    container.classList.add('paper-reader');
    container.tabIndex = 0;
    bindPdfPinchZoom(container, this.scope, {
      scale: () => this.scale, point: (x, y) => this.readingPoint(x, y),
      zoom: (point, scale, x, y) => this.zoomAt(point, scale, x, y),
      active: (active) => {
        this.zooming = active;
        if (!active) {
          const resume = this.pending?.resumeZoom;
          if (this.pending) this.pending.resumeZoom = undefined;
          resume?.(); this.schedule();
        }
      },
    });
    this.scope.listen(container, 'scroll', () => { this.anchor(); this.schedule(); }, { passive: true });
    this.scope.listen(container, 'keydown', (event) => {
      const key = event as KeyboardEvent;
      if (key.target !== container || key.ctrlKey || key.metaKey || key.altKey) return;
      if (key.key === 'PageDown' || key.key === 'PageUp') {
        key.preventDefault();
        this.goToPage(this.state.page + (key.key === 'PageDown' ? 1 : -1));
      }
    });
    const resized = (): void => {
      if (!this.current) return;
      if (!container.clientWidth || !container.clientHeight) { this.layoutSuspended = true; return; }
      if (container.clientWidth === this.lastWidth && !this.layoutSuspended) return;
      const anchor = this.layoutSuspended ? { ...this.savedAnchor } : this.anchor();
      this.layoutSuspended = false;
      this.lastWidth = container.clientWidth;
      if (this.fit) this.scale = this.viewFitScale(this.current);
      this.resizePages(this.current);
      this.restore(anchor);
      this.emit();
      this.schedule();
    };
    if (typeof ResizeObserver !== 'undefined') {
      const observer = new ResizeObserver(resized);
      observer.observe(container);
      this.scope.add(() => observer.disconnect());
    } else this.scope.listen(window, 'resize', resized);
  }

  get state(): ReaderState {
    return { sourceId: this.current?.id ?? '', page: this.anchor().page,
      pages: this.current?.pages.length ?? 0, scale: this.scale, fit: this.fit,
      loading: this.pending !== null, canvasBytes: this.canvasBytes() };
  }
  get document(): PdfDocument | null { return this.current?.document ?? null; }

  anchor(): ReadingAnchor {
    const view = this.current;
    if (!view?.pages.length) return { page: 1, fraction: 0, horizontal: 0.5 };
    if (!this.container.clientWidth || !this.container.clientHeight) return { ...this.savedAnchor };
    let low = 0, high = view.pages.length - 1;
    const top = this.container.scrollTop;
    while (low < high) {
      const mid = Math.ceil((low + high) / 2);
      if (this.pageTop(view, view.pages[mid]) <= top + 1) low = mid;
      else high = mid - 1;
    }
    const page = view.pages[low];
    const rect = page.element.getBoundingClientRect();
    const center = this.container.getBoundingClientRect().left + this.container.clientWidth / 2;
    this.savedAnchor = { page: page.number,
      fraction: clamp((top - this.pageTop(view, page)) / (page.height * this.scale), 0, 1),
      horizontal: rect.width ? clamp((center - rect.left) / rect.width, 0, 1) : 0.5 };
    return { ...this.savedAnchor };
  }

  async load(source: ReaderSource, preservePosition = true): Promise<boolean> {
    if (this.destroyed) return false;
    this.cancelLoad();
    const attempt: LoadAttempt = { controller: new AbortController() };
    this.pending = attempt;
    const initialView = source.initialView;
    const readingAnchor = (): ReadingAnchor => initialView ? { ...initialView } : preservePosition ? this.anchor()
      : { page: 1, fraction: 0, horizontal: 0.5 };
    const fit = (): boolean => initialView?.fit ?? this.fit;
    const desiredScale = (): number => initialView ? clamp(initialView.scale, .25, 4) : this.scale;
    let usedBytes = false;
    this.emit();
    const stale = (): boolean => this.destroyed || this.pending !== attempt;
    try {
      const library = await (this.options.loadPdfJs ?? ensurePdfJs)();
      if (stale()) return false;
      this.library = library;
      let document: PdfDocument;
      const renderLimits = { isEvalSupported: false, maxImageSize: 16777216,
        canvasMaxAreaInBytes: limits.reader_canvas_bytes };
      const fromBytes = async (): Promise<PdfDocument> => {
        if (!source.loadBytes) throw new Error('PDF byte transport is unavailable');
        usedBytes = true;
        await attempt.task?.destroy();
        const bytes = await source.loadBytes(attempt.controller.signal);
        if (stale()) throw new DOMException('PDF load cancelled', 'AbortError');
        attempt.task = library.getDocument({ data: bytes, ...renderLimits });
        return attempt.task.promise;
      };
      if (source.preferBytes) document = await fromBytes();
      else {
        try {
          attempt.task = library.getDocument({ url: source.url, withCredentials: true, ...renderLimits });
          document = await attempt.task.promise;
          await document.getPage(1);
        } catch (error) {
          if (stale() || !source.loadBytes) throw error;
          document = await fromBytes();
        }
      }
      if (stale()) { await document.destroy(); return false; }
      if (!Number.isSafeInteger(document.numPages) || document.numPages < 1) {
        throw new Error('PDF has an invalid page count');
      }
      const first = await document.getPage(1);
      if (stale()) return false;
      const viewport = first.getViewport({ scale: 1 });
      const tree = window.document.createElement('div');
      tree.className = 'paper-reader-pages';
      const view: DocumentView = { id: source.id, document, tree, pages: [], disposed: false };
      attempt.view = view;
      for (let number = 1; number <= document.numPages; number++) {
        const element = window.document.createElement('div');
        element.className = 'paper-reader-page';
        element.dataset.page = String(number);
        element.setAttribute('role', 'region');
        element.setAttribute('aria-label', `${number} / ${document.numPages}`);
        tree.append(element);
        view.pages.push({ number, element, width: viewport.width, height: viewport.height, rendering: false, revision: 0 });
      }
      view.pages[0].page = first;
      let anchor = readingAnchor();
      let scale = this.scale;
      while (!stale()) {
        const selected = view.pages[clamp(anchor.page, 1, document.numPages) - 1];
        // A moving reader may change page while the replacement downloads.
        // Keep only its next visible surface in the detached document.
        for (const other of view.pages) if (other !== selected) this.releasePage(other);
        selected.page ??= await document.getPage(selected.number);
        if (stale()) return false;
        this.acceptDimensions(view, selected, selected.page.getViewport({ scale: 1 }));
        scale = fit() ? this.viewFitScale(view) : desiredScale();
        await this.renderPage(view, selected, scale);
        if (stale()) return false;
        if (this.zooming) {
          await new Promise<void>((resolve) => { attempt.resumeZoom = resolve; });
          if (stale()) return false;
        }
        anchor = readingAnchor();
        if (clamp(anchor.page, 1, document.numPages) === selected.number
          && scale === (fit() ? this.viewFitScale(view) : desiredScale())) break;
      }
      if (stale()) return false;
      // Replace only after the page at the retained reading position is ready.
      const previous = this.current;
      this.current = view;
      this.fit = fit();
      this.scale = scale;
      this.pending = null;
      this.searchGeneration++;
      this.resizePages(view);
      this.container.replaceChildren(tree);
      this.restore(anchor);
      if (previous) this.disposeView(previous);
      this.emit();
      this.schedule();
      return true;
    } catch (error) {
      // A proxy can deliver metadata successfully and fail only when rendering
      // pulls more ranges. Retry the owned byte transport at most once.
      if (!stale() && attempt.view && source.loadBytes && !usedBytes) {
        return await this.load({ ...source, preferBytes: true }, preservePosition);
      }
      if (!stale()) {
        this.options.onError?.(error);
        throw error;
      }
      return false;
    } finally {
      if (this.pending === attempt) {
        this.pending = null;
        this.disposeAttempt(attempt);
        this.emit();
      }
    }
  }

  setScale(value: number | 'fit'): void {
    if (!this.current || (value !== 'fit' && !Number.isFinite(value))) return;
    const anchor = this.anchor();
    this.navigationGeneration++;
    this.fit = value === 'fit';
    this.scale = value === 'fit' ? this.viewFitScale(this.current) : clamp(value, 0.25, 4);
    this.resizePages(this.current);
    this.restore(anchor);
    this.emit();
    this.schedule();
  }

  /** Capture a PDF coordinate under a viewport point, including page margins. */
  readingPoint(clientX: number, clientY: number): ReadingPoint | null {
    const view = this.current;
    if (!view || !Number.isFinite(clientX + clientY)) return null;
    const y = this.container.scrollTop + clientY - this.container.getBoundingClientRect().top;
    let low = 0, high = view.pages.length - 1;
    while (low < high) {
      const mid = Math.ceil((low + high) / 2);
      if (this.pageTop(view, view.pages[mid]) <= y) low = mid; else high = mid - 1;
    }
    const page = view.pages[low]; const rect = page.element.getBoundingClientRect();
    if (!rect.width || !rect.height) return null;
    return { sourceId: view.id, page: page.number, x: clamp((clientX - rect.left) / rect.width, 0, 1),
      y: clamp((clientY - rect.top) / rect.height, 0, 1) };
  }

  /** Keep the supplied PDF coordinate under the same (possibly moving) fingers. */
  zoomAt(point: ReadingPoint, scale: number, clientX: number, clientY: number): boolean {
    const view = this.current;
    if (!view || point.sourceId !== view.id || !Number.isFinite(scale + clientX + clientY + point.x + point.y)) return false;
    const page = view.pages[point.page - 1];
    if (!page) return false;
    this.navigationGeneration++;
    this.fit = false; this.scale = clamp(scale, 0.1, 4);
    this.resizePages(view);
    const rect = page.element.getBoundingClientRect();
    this.container.scrollLeft += rect.left + clamp(point.x, 0, 1) * rect.width - clientX;
    this.container.scrollTop += rect.top + clamp(point.y, 0, 1) * rect.height - clientY;
    this.emit(); this.schedule();
    return true;
  }

  async focusColumn(side: 'left' | 'right'): Promise<boolean> {
    const view = this.current;
    if (!view) return false;
    const generation = ++this.navigationGeneration;
    const shell = view.pages[this.anchor().page - 1];
    const page = shell.page ?? await view.document.getPage(shell.number);
    const bounds = await columnTextBounds(page, side);
    if (this.current !== view || generation !== this.navigationGeneration || this.anchor().page !== shell.number) return false;
    this.acceptDimensions(view, shell, page.getViewport({ scale: 1 }));
    const rect = this.container.getBoundingClientRect();
    return this.zoomAt({ sourceId: view.id, page: shell.number,
      x: (bounds.left + bounds.right) / 2 / shell.width, y: 0 },
    (this.container.clientWidth - 32) / (bounds.right - bounds.left), rect.left + this.container.clientWidth / 2, rect.top);
  }

  /** PDF-point page dimensions for coordinate mapping (SyncTeX bp units). */
  pageSize(pageNumber: number): { width: number; height: number } | null {
    const shell = this.current?.pages[pageNumber - 1];
    return shell ? { width: shell.width, height: shell.height } : null;
  }

  goToPage(page: number, fraction = 0): void {
    if (!Number.isFinite(page) || !Number.isFinite(fraction)) return;
    this.navigationGeneration++;
    this.restore({ page, fraction, horizontal: 0.5 });
    this.schedule();
  }

  async goToDestination(destination: string | unknown[]): Promise<void> {
    const view = this.current;
    if (!view) return;
    const resolved = typeof destination === 'string' ? await view.document.getDestination(destination) : destination;
    if (!resolved || this.current !== view) return;
    const target = resolved[0];
    const number = typeof target === 'number' ? target + 1
      : await view.document.getPageIndex(target as { num: number; gen: number }) + 1;
    if (this.current === view) this.goToPage(number);
  }

  async outline(): Promise<OutlineItem[]> {
    const view = this.current;
    if (!view) return [];
    const result = await readOutline(view.document);
    return this.current === view ? result : [];
  }

  async search(query: string, signal?: AbortSignal): Promise<SearchResult> {
    const view = this.current;
    const generation = ++this.searchGeneration;
    if (!view) return { matches: [], truncated: false };
    return searchDocument(view.document, query, () => signal?.aborted === true
      || this.current !== view || generation !== this.searchGeneration);
  }

  highlight(location: { build_id: string; page: number | null; rects: Array<{
    page: number; h: number; v: number; width: number; height: number }> }): void {
    const view = this.current;
    if (!view || view.id !== location.build_id || location.page === null) return;
    this.clearHighlight();
    const first = location.rects[0];
    const page = view.pages[location.page - 1];
    if (!page) return;
    this.goToPage(location.page, first ? Math.max(0, first.v / page.height - 0.15) : 0);
    for (const rect of location.rects.slice(0, 32)) {
      const target = view.pages[rect.page - 1];
      if (!target) continue;
      const marker = document.createElement('div');
      marker.className = 'paper-reader-highlight';
      marker.style.cssText = `left:${rect.h / target.width * 100}%;top:${rect.v / target.height * 100}%;`
        + `width:${rect.width / target.width * 100}%;height:${Math.max(0.6, rect.height / target.height * 100)}%`;
      target.element.append(marker);
    }
    this.highlightTimer = window.setTimeout(() => this.clearHighlight(), 2600);
  }

  cancelLoad(): void {
    const attempt = this.pending;
    this.pending = null;
    if (attempt) { this.disposeAttempt(attempt); this.emit(); }
  }

  destroy(): void {
    if (this.destroyed) return;
    this.destroyed = true;
    this.cancelLoad();
    this.searchGeneration++;
    this.clearHighlight();
    this.scope.destroy();
    if (this.frame) cancelAnimationFrame(this.frame);
    if (this.current) this.disposeView(this.current);
    this.current = null;
    this.container.replaceChildren();
    this.container.classList.remove('paper-reader');
    if (this.originalTabIndex === null) this.container.removeAttribute('tabindex');
    else this.container.setAttribute('tabindex', this.originalTabIndex);
  }

  private fitScale(width: number): number {
    return this.container.clientWidth ? clamp((this.container.clientWidth - 32) / width, 0.1, 4) : this.scale;
  }
  private viewFitScale(view: DocumentView): number {
    return this.fitScale(view.pages.reduce((width, page) => Math.max(width, page.width), 1));
  }
  private pageTop(view: DocumentView, page: PageShell): number { return view.tree.offsetTop + page.element.offsetTop; }
  private canvasBytes(): number { return [...this.canvases.values()].reduce((sum, bytes) => sum + bytes, 0); }
  private emit(): void { if (!this.destroyed) this.options.onChange?.(this.state); }
  private restore(anchor: ReadingAnchor): void {
    const view = this.current;
    if (!view) return;
    const page = view.pages[clamp(Math.floor(anchor.page), 1, view.pages.length) - 1];
    this.savedAnchor = { page: page.number, fraction: clamp(anchor.fraction, 0, 1), horizontal: clamp(anchor.horizontal, 0, 1) };
    if (!this.container.clientWidth || !this.container.clientHeight) { this.layoutSuspended = true; return; }
    this.container.scrollTop = this.pageTop(view, page) + clamp(anchor.fraction, 0, 1) * page.height * this.scale;
    const rect = page.element.getBoundingClientRect();
    if (rect.width) this.container.scrollLeft += rect.left + clamp(anchor.horizontal, 0, 1) * rect.width
      - this.container.getBoundingClientRect().left - this.container.clientWidth / 2;
  }
  private resizePages(view: DocumentView): void {
    for (const page of view.pages) {
      // getPage may still be pending, before a raster surface exists.
      if (page.rendering && page.renderScale !== this.scale) {
        page.revision++;
        if (page.pending) this.releaseSurface(page.pending);
        page.pending = undefined;
      }
      page.element.style.width = `${page.width * this.scale}px`;
      page.element.style.height = `${page.height * this.scale}px`;
      if (page.surface) page.surface.element.style.transform = `scale(${this.scale / page.surface.scale})`;
    }
  }
  private acceptDimensions(view: DocumentView, page: PageShell, size: { width: number; height: number }): void {
    if (page.width === size.width && page.height === size.height) return;
    const anchor = this.current === view ? this.anchor() : null;
    page.width = size.width;
    page.height = size.height;
    if (!anchor) return;
    if (this.fit) this.scale = this.viewFitScale(view);
    this.resizePages(view);
    this.restore(anchor);
    this.schedule();
  }
  private schedule(): void {
    if (this.destroyed || this.frame) return;
    this.frame = requestAnimationFrame(() => { this.frame = 0; void this.pump(); });
  }
  private visiblePages(view: DocumentView): PageShell[] {
    const first = this.anchor().page - 1;
    const bottom = this.container.scrollTop + this.container.clientHeight;
    const selected: PageShell[] = [];
    for (let i = first; i < view.pages.length && selected.length < limits.reader_resident_pages - 1; i++) {
      selected.push(view.pages[i]);
      if (this.pageTop(view, view.pages[i]) > bottom) break;
    }
    if (first > 0) selected.push(view.pages[first - 1]);
    return selected;
  }
  private async pump(): Promise<void> {
    if (this.pumping) { this.pumpAgain = true; return; }
    if (!this.current || this.destroyed || this.zooming || !this.container.clientWidth || !this.container.clientHeight) return;
    this.pumping = true;
    const view = this.current;
    try {
      const wanted = new Set(this.visiblePages(view));
      for (const page of view.pages) if (!wanted.has(page)) this.releasePage(page);
      for (const page of wanted) {
        if (this.current !== view || this.destroyed || this.zooming) break;
        if (page.surface?.scale === this.scale || page.rendering) continue;
        await this.renderPage(view, page, this.scale);
      }
      this.emit();
    } catch (error) {
      if (!view.disposed) this.options.onError?.(error);
    } finally {
      this.pumping = false;
      if (this.pumpAgain) { this.pumpAgain = false; this.schedule(); }
    }
  }
  private async renderPage(view: DocumentView, shell: PageShell, scale: number): Promise<void> {
    if (shell.rendering || view.disposed || !this.library) return;
    shell.rendering = true;
    shell.renderScale = scale;
    const revision = shell.revision;
    const stale = (): boolean => view.disposed || revision !== shell.revision;
    let surface: Surface | undefined;
    try {
      const page = shell.page ?? await view.document.getPage(shell.number);
      if (stale()) return;
      shell.page = page;
      const base = page.getViewport({ scale: 1 });
      this.acceptDimensions(view, shell, base);
      if (stale()) return;
      const viewport = page.getViewport({ scale });
      const ratio = Math.min(window.devicePixelRatio || 1,
        Math.sqrt(limits.reader_page_canvas_bytes / 4 / viewport.width / viewport.height));
      const width = Math.max(1, Math.floor(viewport.width * ratio));
      const height = Math.max(1, Math.floor(viewport.height * ratio));
      const bytes = width * height * 4;
      this.makeRoom(bytes, shell);
      const canvas = document.createElement('canvas');
      canvas.width = width;
      canvas.height = height;
      this.canvases.set(canvas, bytes);
      const element = document.createElement('div');
      element.className = 'paper-reader-surface';
      element.style.width = `${viewport.width}px`;
      element.style.height = `${viewport.height}px`;
      canvas.style.width = `${viewport.width}px`;
      canvas.style.height = `${viewport.height}px`;
      canvas.setAttribute('aria-hidden', 'true');
      element.append(canvas);
      surface = { element, canvas, scale };
      shell.pending = surface;
      const context = canvas.getContext('2d', { alpha: false });
      if (!context) throw new Error('PDF canvas is unavailable');
      surface.render = page.render({ canvasContext: context, viewport,
        transform: [canvas.width / viewport.width, 0, 0, canvas.height / viewport.height, 0, 0] });
      await surface.render.promise;
      if (stale()) return;
      const content = await page.getTextContent();
      {
        const text = document.createElement('div');
        text.className = 'paper-reader-text textLayer';
        text.style.setProperty('--scale-factor', String(scale));
        element.append(text);
        surface.text = new this.library.TextLayer({ textContentSource: content, container: text, viewport });
        await surface.text.render();
      }
      if (stale()) return;
      await addPageLinks(page, viewport, element, (dest) => {
        if (this.current === view) void this.goToDestination(dest).catch(this.options.onError);
      });
      if (stale()) return;
      if (shell.surface) this.releaseSurface(shell.surface);
      shell.surface = surface;
      shell.pending = undefined;
      shell.element.prepend(element);
      shell.element.dataset.rendered = '1';
      shell.element.style.width = `${shell.width * scale}px`;
      shell.element.style.height = `${shell.height * scale}px`;
      surface = undefined;
    } catch (error) {
      if (!stale()) throw error;
    } finally {
      if (surface) this.releaseSurface(surface);
      shell.pending = undefined;
      shell.rendering = false;
      shell.renderScale = undefined;
      if (stale()) { shell.page?.cleanup(); shell.page = undefined; }
    }
  }
  private makeRoom(bytes: number, protectedPage: PageShell): void {
    if (this.canvasBytes() + bytes <= limits.reader_canvas_bytes) return;
    const anchor = this.anchor().page;
    for (const page of this.current?.pages ?? []) {
      if (page === protectedPage || page.number === anchor) continue;
      this.releasePage(page);
      if (this.canvasBytes() + bytes <= limits.reader_canvas_bytes) return;
    }
    if (this.canvasBytes() + bytes > limits.reader_canvas_bytes) throw new Error('PDF canvas budget exhausted');
  }
  private releaseSurface(surface: Surface): void {
    surface.render?.cancel();
    surface.text?.cancel();
    surface.element.remove();
    surface.canvas.width = 0; surface.canvas.height = 0;
    this.canvases.delete(surface.canvas);
  }
  private releasePage(page: PageShell): void {
    page.revision++;
    if (page.surface) this.releaseSurface(page.surface);
    page.surface = undefined;
    if (page.pending) this.releaseSurface(page.pending);
    page.pending = undefined;
    page.element.dataset.rendered = '0';
    if (!page.rendering) { page.page?.cleanup(); page.page = undefined; }
  }
  private disposeView(view: DocumentView): void {
    if (view.disposed) return;
    view.disposed = true;
    for (const page of view.pages) this.releasePage(page);
    view.tree.remove();
    void view.document.destroy().catch(() => {});
  }
  private disposeAttempt(attempt: LoadAttempt): void {
    attempt.controller.abort();
    attempt.resumeZoom?.(); attempt.resumeZoom = undefined;
    if (attempt.view) this.disposeView(attempt.view);
    else void attempt.task?.destroy().catch(() => {});
  }
  private clearHighlight(): void {
    if (this.highlightTimer) window.clearTimeout(this.highlightTimer);
    this.highlightTimer = 0;
    this.current?.tree.querySelectorAll('.paper-reader-highlight').forEach((node) => node.remove());
  }
}

export function createPdfReader(container: HTMLElement, options: ReaderOptions = {}): PdfReader {
  return new PdfReader(container, options);
}
