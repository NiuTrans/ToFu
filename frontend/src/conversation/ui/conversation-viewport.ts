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
  let lastClientHeight = viewport.clientHeight;
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
    /* The context card scrolls itself and stays pinned beside the reading, so
     * a wheel over it is no intent to leave the transcript's end. Once the
     * card cannot scroll further the wheel moves the transcript, which the
     * scroll handler judges like any other movement. */
    if ((event.target as Element | null)?.closest?.('[data-conversation-part="turn-context-card"]')) return;
    if (event.deltaY < 0 || (!following && event.deltaY !== 0)) suspendFollow();
  };
  const onTouchStart = (event: TouchEvent): void => {
    touchY = event.touches.item(0)?.clientY ?? null;
    if (!following && touchY !== null) cancelLayout();
  };
  const onTouchMove = (event: TouchEvent): void => {
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
    const delta = nextTop - lastScrollTop;
    // Retire a jump correction without suppressing a reader's single drag
    // back to the logical tail. Physical bottom alone cannot rearm follow.
    if (!following && cancelScheduled && Math.abs(delta) > 0.5) cancelLayout();
    /* Layout clamps a pinned tail when the pane grows or the transcript reflows
     * shorter, and that scroll can precede the resize observation. A clamp ends
     * at the physical bottom, which no upward reader movement does, unless the
     * pane also shrank in that layout (chrome above it grew): then the observed
     * pane height changed, which no reader movement does either. */
    const paneResized = viewport.clientHeight !== lastClientHeight;
    lastClientHeight = viewport.clientHeight;
    if (delta < -0.5 && !(following && (nearBottom(1) || paneResized))) suspendFollow();
    else if (delta > 0.5 && nearBottom()
      && (options.canFollowLatest?.() ?? true)) following = true;
    lastScrollTop = nextTop;
  };
  const onKeyDown = (event: KeyboardEvent): void => {
    if (event.defaultPrevented) return;
    const target = event.target as Element | null;
    if (target?.closest?.('input, textarea, select, [contenteditable]:not([contenteditable="false"])')) return;
    if (['ArrowUp', 'PageUp', 'Home'].includes(event.key)
      || (event.key === ' ' && event.shiftKey)
      || (!following && ['ArrowDown', 'PageDown', 'End', ' '].includes(event.key))) suspendFollow();
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
  /* Chrome around the pane (a composer notice rendered after the commit, the
   * project strip above it, the window) resizes it without a Surface write.
   * Observations arrive after layout and before paint, so a followed tail is
   * re-pinned in the frame that first shows the new geometry; the next-frame
   * correction painted one frame with the tail clipped. Pinning here also
   * records the offset a growing pane clamps, which is not reader intent. */
  const Resize = viewport.ownerDocument.defaultView?.ResizeObserver;
  const paneResize = Resize ? new Resize(() => {
    lastClientHeight = viewport.clientHeight;
    if (options.canFollowLatest?.() ?? true) writeBottom();
  }) : null;
  paneResize?.observe(viewport);
  const followLatest = (force = true): void => {
    if (disposed || (!force && (!following || !(options.canFollowLatest?.() ?? true)))) return;
    if (!following) cancelLayout();
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
    scrollToTurn(root, turnId, onScroll) {
      if (disposed || !viewport.contains(root)) return false;
      const findTarget = (): HTMLElement | undefined => {
        for (const node of root.querySelectorAll<HTMLElement>('[data-turn-id]')) {
          if (node.dataset.turnId === turnId) return node;
        }
        return undefined;
      };
      if (!findTarget()) return false;
      suspendFollow();
      const align = (): void => {
        const target = findTarget();
        if (!target || !viewport.contains(root)) return;
        // Exact navigation must not race instant anchor corrections with a
        // native smooth animator. Only this viewport moves, never the page.
        writeInstantly(viewport, () => {
          viewport.scrollTop += target.getBoundingClientRect().top
            - viewport.getBoundingClientRect().top - viewport.clientTop;
        });
        lastScrollTop = viewport.scrollTop;
        onScroll?.();
      };
      align();
      // Content-visibility and newly mounted leaves settle at the next layout.
      // Reuse the sole layout slot: reader intent, Latest, another jump and
      // disposal all retire this delivery, even if cancellation is best-effort.
      const epoch = ++layoutEpoch;
      const cancel = scheduleAfterLayout(() => {
        if (disposed || epoch !== layoutEpoch) return;
        cancelScheduled = null;
        layoutEpoch += 1;
        align();
      });
      if (epoch === layoutEpoch) cancelScheduled = cancel;
      return true;
    },
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
      const correction = nextOffset - previousOffset;
      // Native scroll anchoring adjusts during the layout just forced. When it
      // moved the viewport, it holds the reader: its anchor may differ from
      // this leaf by a few pixels, and any write here (even a no-op) cancels
      // in-flight smooth or momentum scrolling. Correct fully only where the
      // browser could not anchor: scroll offset 0, opted out or unsupported.
      const nativelyAnchored = Math.abs(viewport.scrollTop - snapshot.scrollTop) > 0.5;
      const tolerance = nativelyAnchored ? viewport.clientHeight / 2 : 0.5;
      if (Math.abs(correction) > tolerance) writeInstantly(viewport, () => {
        viewport.scrollTop += correction;
      });
      lastScrollTop = viewport.scrollTop;
    },
    suspendFollow,
    followLatest(_root, intent) { followLatest(intent?.force !== false); },
    dispose() {
      if (disposed) return;
      disposed = true;
      paneResize?.disconnect();
      cancelLayout();
      viewport.removeEventListener('wheel', onWheel);
      viewport.removeEventListener('touchstart', onTouchStart);
      viewport.removeEventListener('touchmove', onTouchMove);
      viewport.removeEventListener('scroll', onScroll);
      viewport.removeEventListener('keydown', onKeyDown);
    },
  };
}
