/**
 * Lifecycle-owned scroll follow/suspend and visible block anchoring. Receives
 * viewport and layout scheduling ports; no transport or rendering.
 */
import { scheduleAnimationFrame } from './animation-frame-scheduler';
import type {
  ScrollAnchorPort,
  ConversationViewportOptions,
  ConversationScrollSnapshot,
} from './surface-contracts';
import { positiveInteger } from './surface-windowing';

/** Inner historical evidence owns its gestures, even though wheel/touch bubble. */
export function isConversationViewportEvent(event: Event): boolean {
  const target = event.target as Element | null;
  return !target?.closest?.('.conversation-history-process__body');
}

/** All geometry corrections must finish synchronously, including under themes. */
function writeInstantly(viewport: HTMLElement, write: () => void): void {
  const value = viewport.style.getPropertyValue('scroll-behavior');
  const priority = viewport.style.getPropertyPriority('scroll-behavior');
  viewport.style.setProperty('scroll-behavior', 'auto', 'important');
  try { write(); }
  finally {
    if (value) viewport.style.setProperty('scroll-behavior', value, priority);
    else viewport.style.removeProperty('scroll-behavior');
  }
}

/**
 * Own the user's follow/suspend latch and scoped block anchor for one scroll
 * viewport. Durable Turns remain in the store; only DOM geometry lives here.
 */
export function createConversationViewportPort(
  viewport: HTMLElement,
  options: ConversationViewportOptions = {},
): ScrollAnchorPort<ConversationScrollSnapshot> & { readonly following: boolean } {
  const threshold = positiveInteger(options.nearBottomThreshold, 80);
  const scheduleAfterLayout = options.scheduleAfterLayout ?? ((callback) => {
    const view = viewport.ownerDocument.defaultView;
    if (view) return scheduleAnimationFrame(view, callback);
    let cancelled = false;
    queueMicrotask(() => { if (!cancelled) callback(); });
    return () => { cancelled = true; };
  });
  let following = true;
  let lastScrollTop = viewport.scrollTop;
  let touchY: number | null = null;
  let cancelScheduled: (() => void) | null = null;
  let disposed = false;
  let layoutEpoch = 0;

  const nearBottom = (distance = threshold): boolean => (
    viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight <= distance
  );
  const cancelLayout = (): void => {
    layoutEpoch += 1;
    const cancel = cancelScheduled;
    cancelScheduled = null;
    try { cancel?.(); }
    catch (error) { console.warn('[ConversationViewport] layout cancellation failed', error); }
  };
  const suspendFollow = (): void => { following = false; cancelLayout(); };
  const onWheel = (event: WheelEvent): void => {
    if (isConversationViewportEvent(event) && event.deltaY < 0) suspendFollow();
  };
  const onTouchStart = (event: TouchEvent): void => {
    touchY = isConversationViewportEvent(event) ? event.touches.item(0)?.clientY ?? null : null;
  };
  const onTouchMove = (event: TouchEvent): void => {
    if (!isConversationViewportEvent(event)) return;
    const nextY = event.touches.item(0)?.clientY;
    if (touchY !== null && nextY !== undefined && nextY > touchY + 4) {
      suspendFollow();
    }
    if (nextY !== undefined) touchY = nextY;
  };
  const onScroll = (): void => {
    const nextTop = viewport.scrollTop;
    // Intent wins even within the near-bottom slack. An upward wheel followed
    // by a 2px scroll must not rearm the latch and fight the next stream paint.
    if (nextTop < lastScrollTop - 0.5) suspendFollow();
    else if (nextTop > lastScrollTop + 0.5 && nearBottom()
      && (options.canFollowLatest?.() ?? true)) following = true;
    lastScrollTop = nextTop;
  };
  const onKeyDown = (event: KeyboardEvent): void => {
    if (!isConversationViewportEvent(event) || event.defaultPrevented) return;
    const target = event.target as Element | null;
    if (target?.closest?.('input, textarea, select, [contenteditable]:not([contenteditable="false"])')) return;
    if (['ArrowUp', 'PageUp', 'Home'].includes(event.key)
      || (event.key === ' ' && event.shiftKey)) suspendFollow();
  };
  viewport.addEventListener('wheel', onWheel, { passive: true });
  viewport.addEventListener('touchstart', onTouchStart, { passive: true });
  viewport.addEventListener('touchmove', onTouchMove, { passive: true });
  viewport.addEventListener('scroll', onScroll, { passive: true });
  viewport.addEventListener('keydown', onKeyDown);

  const writeBottom = (): void => {
    if (disposed || !following) return;
    writeInstantly(viewport, () => { viewport.scrollTop = viewport.scrollHeight; });
    lastScrollTop = viewport.scrollTop;
  };
  const followLatest = (force = true): void => {
    if (disposed || (!force && (!following || !(options.canFollowLatest?.() ?? true)))) return;
    following = true;
    writeBottom();
    // One deferred layout write per frame, regardless of stream burst size.
    if (cancelScheduled) return;
    const epoch = ++layoutEpoch;
    const cancel = scheduleAfterLayout(() => {
      if (disposed || epoch !== layoutEpoch) return;
      cancelScheduled = null;
      layoutEpoch += 1;
      writeBottom();
    });
    if (epoch === layoutEpoch) cancelScheduled = cancel;
  };

  return {
    get following() { return following; },
    isNearBottom: nearBottom,
    capture(root) {
      /* While following the tail, restore() always re-pins to the bottom and
       * never reads the anchor, so the visible-content geometry scan is skipped for
       * the common streaming case. */
      if (following) return { following: true, scrollTop: viewport.scrollTop };
      const viewportTop = viewport.getBoundingClientRect().top;
      let anchor: HTMLElement | undefined;
      let anchorBounds: DOMRect | undefined;
      for (const node of root.querySelectorAll<HTMLElement>('[data-turn-id]')) {
        const bounds = node.getBoundingClientRect();
        if (bounds.bottom <= viewportTop + 1) continue;
        anchor = node;
        anchorBounds = bounds;
        break;
      }
      const turn = anchor;
      const turnBounds = anchorBounds;
      // Turn tops cannot preserve the reader's position when earlier blocks
      // mount inside that same Turn. Prefer the first visible keyed leaf.
      if (turn) {
        for (const node of turn.querySelectorAll<HTMLElement>('[data-block-id]')) {
          if (node.hidden) continue;
          const bounds = node.getBoundingClientRect();
          if (bounds.bottom <= viewportTop + 1) continue;
          anchor = node;
          anchorBounds = bounds;
          break;
        }
      }
      return {
        following,
        scrollTop: viewport.scrollTop,
        ...(turn?.dataset.turnId ? {
          anchorTurnId: turn.dataset.turnId,
          anchorPresentationId: turn.dataset.presentationId,
          anchorBlockId: anchor?.dataset.blockId,
          anchorOffset: anchorBounds!.top - viewportTop,
          anchorTurnOffset: turnBounds!.top - viewportTop,
        } : {}),
      };
    },
    restore(root, snapshot) {
      if (disposed) return;
      if (snapshot.following && following) {
        followLatest(false);
        return;
      }
      if (following || !snapshot.anchorTurnId) return;
      let anchor: HTMLElement | undefined;
      for (const node of root.querySelectorAll<HTMLElement>('[data-turn-id]')) {
        if (snapshot.anchorPresentationId
          ? node.dataset.presentationId !== snapshot.anchorPresentationId
          : node.dataset.turnId !== snapshot.anchorTurnId) continue;
        anchor = node;
        break;
      }
      if (!anchor) return;
      let previousOffset = snapshot.anchorTurnOffset ?? snapshot.anchorOffset ?? 0;
      // Block IDs may repeat in other Turns/presentations. Match only inside
      // the captured owner and retain its Turn as fallback if the leaf vanished.
      if (snapshot.anchorBlockId) {
        for (const node of anchor.querySelectorAll<HTMLElement>('[data-block-id]')) {
          if (node.dataset.blockId !== snapshot.anchorBlockId || node.hidden) continue;
          anchor = node;
          previousOffset = snapshot.anchorOffset ?? 0;
          break;
        }
      }
      const nextOffset = anchor.getBoundingClientRect().top
        - viewport.getBoundingClientRect().top;
      writeInstantly(viewport, () => {
        viewport.scrollTop += nextOffset - previousOffset;
      });
      lastScrollTop = viewport.scrollTop;
    },
    suspendFollow,
    followLatest(_root, intent) { followLatest(intent?.force !== false); },
    dispose() {
      if (disposed) return;
      disposed = true;
      cancelLayout();
      viewport.removeEventListener('wheel', onWheel);
      viewport.removeEventListener('touchstart', onTouchStart);
      viewport.removeEventListener('touchmove', onTouchMove);
      viewport.removeEventListener('scroll', onScroll);
      viewport.removeEventListener('keydown', onKeyDown);
    },
  };
}
