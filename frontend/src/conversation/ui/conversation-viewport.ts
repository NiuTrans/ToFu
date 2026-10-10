/**
 * Lifecycle-owned scroll follow/suspend and stable Turn anchoring. Receives
 * viewport and layout scheduling ports; no transport or rendering.
 */
import { scheduleAnimationFrame } from './animation-frame-scheduler';
import type {
  ScrollAnchorPort,
  ConversationViewportOptions,
  ConversationScrollSnapshot,
} from './surface-contracts';
import { positiveInteger } from './surface-windowing';

/**
 * Own the user's follow/suspend latch and stable Turn anchor for one scroll
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
  let lastScrollTop: number | null = null;
  let touchY: number | null = null;
  let cancelScheduled: (() => void) | null = null;
  let disposed = false;

  const nearBottom = (): boolean => (
    viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight <= threshold
  );
  const onWheel = (event: WheelEvent): void => {
    if (event.deltaY < 0) following = false;
  };
  const onTouchStart = (event: TouchEvent): void => {
    touchY = event.touches.item(0)?.clientY ?? null;
  };
  const onTouchMove = (event: TouchEvent): void => {
    const nextY = event.touches.item(0)?.clientY;
    if (touchY !== null && nextY !== undefined && nextY > touchY + 4) {
      following = false;
    }
    if (nextY !== undefined) touchY = nextY;
  };
  const onScroll = (): void => {
    const nextTop = viewport.scrollTop;
    if (nearBottom()) following = true;
    else if (lastScrollTop !== null && nextTop < lastScrollTop - 4) {
      following = false;
    }
    lastScrollTop = nextTop;
  };
  viewport.addEventListener('wheel', onWheel, { passive: true });
  viewport.addEventListener('touchstart', onTouchStart, { passive: true });
  viewport.addEventListener('touchmove', onTouchMove, { passive: true });
  viewport.addEventListener('scroll', onScroll, { passive: true });

  const writeBottom = (): void => {
    if (disposed || !following) return;
    viewport.scrollTop = viewport.scrollHeight;
    lastScrollTop = viewport.scrollTop;
  };
  const followLatest = (): void => {
    if (disposed) return;
    following = true;
    writeBottom();
    cancelScheduled?.();
    cancelScheduled = scheduleAfterLayout(() => {
      cancelScheduled = null;
      writeBottom();
    });
  };

  return {
    get following() { return following; },
    capture(root) {
      /* While following the tail, restore() always re-pins to the bottom and
       * never reads the anchor, so the per-Turn geometry scan is skipped for
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
      return {
        following,
        scrollTop: viewport.scrollTop,
        ...(anchor?.dataset.turnId ? {
          anchorTurnId: anchor.dataset.turnId,
          anchorOffset: anchorBounds!.top - viewportTop,
        } : {}),
      };
    },
    restore(root, snapshot) {
      if (snapshot.following && following) {
        followLatest();
        return;
      }
      if (following || !snapshot.anchorTurnId) return;
      let anchor: HTMLElement | undefined;
      for (const node of root.querySelectorAll<HTMLElement>('[data-turn-id]')) {
        if (node.dataset.turnId !== snapshot.anchorTurnId) continue;
        anchor = node;
        break;
      }
      if (!anchor) return;
      const nextOffset = anchor.getBoundingClientRect().top
        - viewport.getBoundingClientRect().top;
      viewport.scrollTop += nextOffset - Number(snapshot.anchorOffset || 0);
      lastScrollTop = viewport.scrollTop;
    },
    suspendFollow() {
      following = false;
      cancelScheduled?.();
      cancelScheduled = null;
    },
    followLatest() { followLatest(); },
    dispose() {
      if (disposed) return;
      disposed = true;
      cancelScheduled?.();
      cancelScheduled = null;
      viewport.removeEventListener('wheel', onWheel);
      viewport.removeEventListener('touchstart', onTouchStart);
      viewport.removeEventListener('touchmove', onTouchMove);
      viewport.removeEventListener('scroll', onScroll);
    },
  };
}
