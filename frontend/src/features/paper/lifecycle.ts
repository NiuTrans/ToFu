import { featureRegistry } from '../../feature-registry';
type JsonObject = Record<string, unknown>;

interface ReportView extends JsonObject {
  stream?: JsonObject;
  containerId?: string;
  cache?: string;
}

type PaperLifecycleWindow = Window & {
  paperMode?: boolean;
  _paperScale?: number;
  _paperZoomDebounce?: number | null;
  _paperSearchResults?: unknown[];
  _lastArxivSearchQuery?: string;
  _recStream?: JsonObject | null;
  _handlePaperKeyDown?: (event: KeyboardEvent) => void;
  _handlePaperTextSelection?: () => void;
  _teardownReadingTracker?: (flush: boolean) => void;
  _reportView?: (kind: string) => ReportView | null;
  _paintReportFromState?: (view: ReportView) => void;
  _renderFinalReport?: (
    container: HTMLElement,
    content: string,
    meta?: unknown,
    view?: ReportView,
  ) => void;
  _renderPaperQA?: () => void;
  _paintRecommendFromState?: () => void;
  _renderArxivSearchResults?: (query: string, results: unknown[]) => void;
  _loadPaperLibrary?: () => unknown;
  _handlePaperFileDrop?: (file: File) => Promise<unknown>;
  _syncZoomUI?: () => void;
  _renderAllPages?: () => void;
  _destroyPaperQA?: () => void;
  _destroyResearchRuntime?: () => void;
  _destroyPaperRecommend?: () => void;
  _destroyArxivFetch?: () => void;
  _destroyPodcastRuntime?: () => void;
  _destroyVideoRuntime?: () => void;
  _destroyReportRuntime?: () => void;
  _pcStopPolling?: () => void;
  _pvStopPolling?: () => void;
  _destroyPaperLifecycle?: typeof destroyPaperLifecycle;
  _destroyPaperSession?: typeof destroyPaperSession;
  _installPaperLifecycle?: typeof installPaperLifecycle;
};

type RecElement = Element & { _recSig?: string };

function globals(): PaperLifecycleWindow {
  return featureRegistry as unknown as PaperLifecycleWindow;
}

let cleanup: Array<() => void> = [];
let selectionTimer: number | null = null;
let installGeneration = 0;

function listen<K extends keyof WindowEventMap>(
  target: Window,
  type: K,
  listener: (event: WindowEventMap[K]) => void,
  options?: AddEventListenerOptions | boolean,
): void;
function listen<K extends keyof DocumentEventMap>(
  target: Document,
  type: K,
  listener: (event: DocumentEventMap[K]) => void,
  options?: AddEventListenerOptions | boolean,
): void;
function listen(
  target: Window | Document,
  type: string,
  listener: EventListener,
  options?: AddEventListenerOptions | boolean,
): void;
function listen(
  target: Window | Document,
  type: string,
  listener: EventListener,
  options?: AddEventListenerOptions | boolean,
): void {
  target.addEventListener(type, listener, options);
  cleanup.push(() => target.removeEventListener(type, listener, options));
}

function onMouseUp(): void {
  if (!globals().paperMode) return;
  if (selectionTimer != null) window.clearTimeout(selectionTimer);
  selectionTimer = window.setTimeout(() => {
    selectionTimer = null;
    globals()._handlePaperTextSelection?.();
  }, 10);
}

function onBeforeUnload(): void {
  globals()._teardownReadingTracker?.(true);
}

function onKatexLoaded(): void {
  const state = globals();
  if (!state.paperMode) return;
  for (const kind of ['report', 'review']) {
    const view = state._reportView?.(kind);
    if (!view) continue;
    if (view.stream) {
      view.stream._lastRenderedLen = -1;
      view.stream._lastRenderedStatus = '';
      state._paintReportFromState?.(view);
    } else if (view.containerId && view.cache) {
      const container = document.getElementById(view.containerId);
      if (container) state._renderFinalReport?.(container, view.cache, undefined, view);
    }
  }
  state._renderPaperQA?.();

  if (state._recStream && document.querySelector('[data-rec-shell]')) {
    const list = document.querySelector('[data-rec-list]');
    if (list) {
      Array.from(list.children).forEach((node) => {
        (node as RecElement)._recSig = '';
      });
    }
    state._paintRecommendFromState?.();
    return;
  }
  const results = state._paperSearchResults ?? [];
  if (results.length
      && document.querySelector('.paper-search .paper-result-list')
      && typeof state._lastArxivSearchQuery === 'string') {
    state._renderArxivSearchResults?.(state._lastArxivSearchQuery, results);
  }
}

function addDropZone(element: HTMLElement): void {
  const dragover = (event: DragEvent): void => {
    const types = event.dataTransfer?.types;
    if (!globals().paperMode || !types || !Array.from(types).includes('Files')) return;
    event.preventDefault();
    event.stopPropagation();
    element.classList.add('paper-drag-over');
  };
  const dragleave = (event: DragEvent): void => {
    const related = event.relatedTarget;
    if (related instanceof Node && element.contains(related)) return;
    element.classList.remove('paper-drag-over');
  };
  const drop = (event: DragEvent): void => {
    event.preventDefault();
    event.stopPropagation();
    element.classList.remove('paper-drag-over');
    if (!globals().paperMode) return;
    const file = Array.from(event.dataTransfer?.files ?? []).find(
      (candidate) => candidate.type === 'application/pdf'
        || candidate.name.toLowerCase().endsWith('.pdf'),
    );
    if (file) void globals()._handlePaperFileDrop?.(file);
  };
  element.addEventListener('dragover', dragover);
  element.addEventListener('dragleave', dragleave);
  element.addEventListener('drop', drop);
  cleanup.push(() => {
    element.removeEventListener('dragover', dragover);
    element.removeEventListener('dragleave', dragleave);
    element.removeEventListener('drop', drop);
    element.classList.remove('paper-drag-over');
  });
}

function onWheel(event: WheelEvent): void {
  const state = globals();
  if (!state.paperMode || !event.ctrlKey) return;
  event.preventDefault();
  const delta = event.deltaY > 0 ? -0.1 : 0.1;
  state._paperScale = Math.max(0.25, Math.min(4, (state._paperScale ?? 1) + delta));
  state._syncZoomUI?.();
  if (state._paperZoomDebounce != null) {
    window.clearTimeout(state._paperZoomDebounce);
  }
  state._paperZoomDebounce = window.setTimeout(() => {
    state._paperZoomDebounce = null;
    state._renderAllPages?.();
  }, 150);
}

function installReadyBindings(generation: number): void {
  if (generation !== installGeneration) return;
  const state = globals();
  state._loadPaperLibrary?.();
  for (const id of ['paperPdfViewer', 'paperModeContainer', 'paperSidebarOverlay']) {
    const element = document.getElementById(id);
    if (element) addDropZone(element);
  }
  const viewer = document.getElementById('paperPdfViewer');
  if (viewer) {
    viewer.addEventListener('wheel', onWheel, { passive: false });
    cleanup.push(() => viewer.removeEventListener('wheel', onWheel));
  }
}

export function destroyPaperLifecycle(): void {
  installGeneration += 1;
  for (const remove of cleanup.splice(0).reverse()) remove();
  if (selectionTimer != null) {
    window.clearTimeout(selectionTimer);
    selectionTimer = null;
  }
  const state = globals();
  if (state._paperZoomDebounce != null) {
    window.clearTimeout(state._paperZoomDebounce);
    state._paperZoomDebounce = null;
  }
}

/** Release work scoped to the open Paper session while server tasks remain durable. */
export function destroyPaperSession(): void {
  const state = globals();
  state._destroyPaperQA?.();
  state._destroyResearchRuntime?.();
  state._destroyPaperRecommend?.();
  state._destroyArxivFetch?.();
  state._destroyPodcastRuntime?.();
  state._destroyVideoRuntime?.();
  state._destroyReportRuntime?.();
  state._pcStopPolling?.();
  state._pvStopPolling?.();
}

export function installPaperLifecycle(): void {
  destroyPaperLifecycle();
  const generation = installGeneration;
  const keydown = (event: KeyboardEvent): void => {
    globals()._handlePaperKeyDown?.(event);
  };
  listen(document, 'keydown', keydown);
  listen(document, 'mouseup', onMouseUp);
  listen(window, 'beforeunload', onBeforeUnload);
  listen(window, 'katex:loaded', onKatexLoaded);

  if (document.readyState === 'loading') {
    const ready = (): void => installReadyBindings(generation);
    document.addEventListener('DOMContentLoaded', ready, { once: true });
    cleanup.push(() => document.removeEventListener('DOMContentLoaded', ready));
  } else {
    installReadyBindings(generation);
  }
}

const target = globals();
target._destroyPaperLifecycle = destroyPaperLifecycle;
target._destroyPaperSession = destroyPaperSession;
target._installPaperLifecycle = installPaperLifecycle;
if (typeof target._handlePaperKeyDown === 'function') installPaperLifecycle();
