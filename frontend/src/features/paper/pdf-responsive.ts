import { featureRegistry } from '../../feature-registry';
import { createLifecycleScope, type LifecycleScope } from '../../lifecycle';

import { scheduleAnimationFrame } from '../../conversation/ui/animation-frame-scheduler';

type LegacyPaperWindow = Window & {
  __tofuPaperResponsiveOwned?: boolean;
  _paperResponsiveOnCrossing?: () => void;
  _paperPdfDoc?: unknown;
  paperFitWidth?: () => void;
  _setPaperMobileView?: (view: string) => void;
};

export interface PaperResponsiveController {
  onCrossing(): void;
  destroy(): void;
}

function legacyWindow(): LegacyPaperWindow {
  return featureRegistry as unknown as LegacyPaperWindow;
}

function htmlElement(element: Element | null): HTMLElement | null {
  return element instanceof HTMLElement ? element : null;
}

/** Attach the split divider and fold/orientation listener as one owner. */
export function attachPaperResponsive(): PaperResponsiveController {
  const scope = createLifecycleScope();
  let dragScope: LifecycleScope | null = null;
  let divider: HTMLElement | null = null;
  let left: HTMLElement | null = null;
  let right: HTMLElement | null = null;
  let body: HTMLElement | null = null;
  let dragging = false;
  let startX = 0;
  let startLeftWidth = 0;
  let crossingPending = false;
  let cancelCrossingFrame: (() => void) | null = null;
  let cancelFitFrame: (() => void) | null = null;
  let destroyed = false;

  let singlePaneQuery: MediaQueryList | null = null;
  try {
    if (typeof window.matchMedia === 'function') {
      singlePaneQuery = window.matchMedia(
        '(max-width:1024px) and (pointer:coarse)');
    }
  } catch (error: unknown) {
    console.warn('[Paper] matchMedia unavailable:', error);
  }

  const getElements = (): void => {
    left = htmlElement(divider?.previousElementSibling ?? null);
    right = htmlElement(divider?.nextElementSibling ?? null);
    body = divider?.parentElement ?? null;
  };

  const fitWidth = (): void => {
    const globals = legacyWindow();
    if (typeof globals.paperFitWidth !== 'function') return;
    try {
      globals.paperFitWidth();
    } catch (error: unknown) {
      console.warn('[Paper] responsive fit failed:', error);
    }
  };

  const autoRefitIfOverflowing = (): void => {
    try {
      const globals = legacyWindow();
      if (!globals._paperPdfDoc) return;
      const viewer = document.getElementById('paperPdfViewer');
      const firstWrapper = viewer?.querySelector<HTMLElement>(
        '.paper-page-wrapper');
      if (!viewer || !firstWrapper) return;
      const pageWidth = Number.parseFloat(firstWrapper.style.width)
        || firstWrapper.clientWidth;
      const availableWidth = viewer.clientWidth - 32;
      if (availableWidth > 0 && pageWidth > availableWidth + 1) fitWidth();
    } catch (error: unknown) {
      console.warn('[Paper] Auto-refit check failed:', error);
    }
  };

  const endDrag = (): void => {
    dragging = false;
    divider?.classList.remove('dragging');
    document.body.style.cursor = '';
    document.body.style.userSelect = '';
    dragScope?.destroy();
    dragScope = null;
    autoRefitIfOverflowing();
  };

  const resizeFromPointer = (clientX: number): void => {
    if (!dragging || !left || !body || !divider) return;
    const available = body.getBoundingClientRect().width
      - divider.getBoundingClientRect().width;
    const width = Math.max(
      250, Math.min(available - 250, startLeftWidth + clientX - startX));
    left.style.width = `${width}px`;
  };

  const beginDrag = (clientX: number): boolean => {
    getElements();
    if (!left || !right || !body || !divider) return false;
    dragScope?.destroy();
    dragScope = createLifecycleScope();
    dragging = true;
    startX = clientX;
    startLeftWidth = left.getBoundingClientRect().width;
    left.style.flex = 'none';
    left.style.width = `${startLeftWidth}px`;
    right.style.flex = '1';
    right.style.width = '';
    right.style.minWidth = '250px';
    divider.classList.add('dragging');
    return true;
  };

  const onMouseDown = (event: Event): void => {
    if (!(event instanceof MouseEvent)) return;
    event.preventDefault();
    if (!beginDrag(event.clientX) || !dragScope) return;
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
    dragScope.listen(document, 'mousemove', (moveEvent) => {
      if (moveEvent instanceof MouseEvent) resizeFromPointer(moveEvent.clientX);
    });
    dragScope.listen(document, 'mouseup', endDrag);
  };

  const onTouchStart = (event: Event): void => {
    if (!(event instanceof TouchEvent) || event.touches.length !== 1) return;
    event.preventDefault();
    if (!beginDrag(event.touches[0].clientX) || !dragScope) return;
    dragScope.listen(document, 'touchmove', (moveEvent) => {
      if (!(moveEvent instanceof TouchEvent)
          || moveEvent.touches.length !== 1) return;
      moveEvent.preventDefault();
      resizeFromPointer(moveEvent.touches[0].clientX);
    }, { passive: false });
    dragScope.listen(document, 'touchend', endDrag);
  };

  const onDoubleClick = (): void => {
    getElements();
    if (!left || !right) return;
    left.style.flex = '1';
    left.style.width = '';
    right.style.flex = '1';
    right.style.width = '';
    right.style.minWidth = '';
  };

  const onCrossing = (): void => {
    const paperBody = document.querySelector<HTMLElement>('.paper-body');
    if (!paperBody) return;
    if (singlePaneQuery?.matches) {
      let current = paperBody.getAttribute('data-paper-view');
      if (current !== 'pdf' && current !== 'reader') current = 'pdf';
      const setView = legacyWindow()._setPaperMobileView;
      if (typeof setView === 'function') setView(current);
      else paperBody.setAttribute('data-paper-view', current);
    }
    if (typeof legacyWindow().paperFitWidth === 'function') {
      cancelFitFrame?.();
      cancelFitFrame = scheduleAnimationFrame(window, () => {
        cancelFitFrame = null;
        fitWidth();
      });
    }
  };

  const scheduleCrossing = (): void => {
    if (crossingPending) return;
    crossingPending = true;
    cancelCrossingFrame = scheduleAnimationFrame(window, () => {
      cancelCrossingFrame = null;
      crossingPending = false;
      onCrossing();
    });
  };

  const initialize = (): void => {
    divider = document.getElementById('paperDivider');
    if (divider) {
      scope.listen(divider, 'mousedown', onMouseDown);
      scope.listen(divider, 'touchstart', onTouchStart, { passive: false });
      scope.listen(divider, 'dblclick', onDoubleClick);
    }
    if (singlePaneQuery) {
      if (typeof singlePaneQuery.addEventListener === 'function') {
        scope.listen(singlePaneQuery, 'change', scheduleCrossing);
      } else {
        singlePaneQuery.addListener(scheduleCrossing);
        scope.add(() => singlePaneQuery?.removeListener(scheduleCrossing));
      }
    }
    scope.listen(window, 'orientationchange', scheduleCrossing);
  };

  if (document.readyState === 'loading') {
    scope.listen(document, 'DOMContentLoaded', initialize, { once: true });
  } else {
    initialize();
  }

  const globals = legacyWindow();
  globals.__tofuPaperResponsiveOwned = true;
  globals._paperResponsiveOnCrossing = onCrossing;

  return {
    onCrossing,
    destroy() {
      if (destroyed) return;
      destroyed = true;
      dragScope?.destroy();
      dragScope = null;
      scope.destroy();
      cancelCrossingFrame?.();
      cancelCrossingFrame = null;
      cancelFitFrame?.();
      cancelFitFrame = null;
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
      if (globals._paperResponsiveOnCrossing === onCrossing) {
        globals._paperResponsiveOnCrossing = undefined;
        globals.__tofuPaperResponsiveOwned = false;
      }
    },
  };
}

const globals = legacyWindow();
if (!globals.__tofuPaperResponsiveOwned) attachPaperResponsive();
