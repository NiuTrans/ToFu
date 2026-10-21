/** Conversation-bar nonmodal disclosures. Owns viewport placement and temporary
 * listeners only; callers own content, commands and data. No timers or observers.
 */
export function positionDisclosure(anchor: HTMLElement, panel: HTMLElement): void {
  const win = anchor.ownerDocument.defaultView;
  if (!win) return;

  // The body is a flex layout. A newly appended in-flow panel would squeeze
  // the app and invalidate the anchor rect as soon as it becomes fixed.
  panel.style.position = 'fixed';
  const viewport = win.visualViewport;
  const left = viewport?.offsetLeft ?? 0;
  const top = viewport?.offsetTop ?? 0;
  const width = viewport?.width ?? win.innerWidth;
  const height = viewport?.height ?? win.innerHeight;
  const inset = 8;
  const rect = anchor.getBoundingClientRect();
  const above = rect.top - top - inset - 6;
  const below = top + height - rect.bottom - inset - 6;
  const upwards = above >= below;
  panel.style.maxWidth = `${Math.max(0, width - inset * 2)}px`;
  panel.style.maxHeight = `${Math.max(44, upwards ? above : below)}px`;
  const box = panel.getBoundingClientRect();
  panel.style.left = `${Math.max(left + inset, Math.min(rect.right - box.width, left + width - box.width - inset))}px`;
  panel.style.top = `${Math.max(top + inset, Math.min(upwards ? rect.top - box.height - 6 : rect.bottom + 6, top + height - box.height - inset))}px`;
}

const focusSelector = 'button:not(:disabled),a[href],input:not(:disabled),select:not(:disabled),textarea:not(:disabled),[tabindex="0"]';

export function createAnchoredDisclosure(ports: {
  anchor: HTMLElement;
  panel: HTMLElement;
  onClose?: () => void;
}): { open(): void; close(restoreFocus?: boolean): void; destroy(): void; isOpen(): boolean } {
  const { anchor, panel } = ports;
  const doc = anchor.ownerDocument;
  const win = doc.defaultView!;
  let opened = false;
  let disposed = false;
  const remove: Array<() => void> = [];
  anchor.setAttribute('aria-expanded', 'false');
  if (panel.id) anchor.setAttribute('aria-controls', panel.id);
  panel.hidden = true;
  panel.tabIndex = -1;
  function listen(target: EventTarget, type: string, fn: (event: Event) => void, capture = false): void {
    target.addEventListener(type, fn, capture);
    remove.push(() => target.removeEventListener(type, fn, capture));
  }
  function focus(node: HTMLElement): void {
    node.focus({ preventScroll: true });
    if (panel.contains(node)) {
      const frame = panel.getBoundingClientRect();
      const box = node.getBoundingClientRect();
      if (box.top < frame.top) panel.scrollTop -= frame.top - box.top + 8;
      else if (box.bottom > frame.bottom) panel.scrollTop += box.bottom - frame.bottom + 8;
    }
  }
  function close(restoreFocus = false): void {
    if (!opened) return;
    opened = false;
    remove.splice(0).forEach(unsubscribe => unsubscribe());
    anchor.setAttribute('aria-expanded', 'false');
    panel.hidden = true;
    panel.remove();
    if (restoreFocus && anchor.isConnected && anchor.getClientRects().length) focus(anchor);
    ports.onClose?.();
  }
  function open(): void {
    if (disposed || opened || !anchor.isConnected) return;
    opened = true;
    panel.hidden = false;
    doc.body.append(panel);
    panel.scrollTop = 0;
    anchor.setAttribute('aria-expanded', 'true');
    positionDisclosure(anchor, panel);
    focus(panel.querySelector<HTMLElement>('[aria-current="true"]') ?? panel.querySelector<HTMLElement>(focusSelector) ?? panel);
    listen(doc, 'click', event => {
      const target = event.target as Node | null;
      if (target && (panel.contains(target) || anchor.contains(target))) return;
      // Run after native mousedown: never overwrite a newly focused outside
      // control; return only focus stranded on body or the dismissed panel.
      close(doc.activeElement === doc.body || panel.contains(doc.activeElement));
    }, true);
    listen(doc, 'focusin', event => {
      const target = event.target as HTMLElement;
      if (panel.contains(target)) { focus(target); return; }
      if (!anchor.contains(target)) close(false);
    });
    listen(doc, 'keydown', event => {
      const key = event as KeyboardEvent;
      if (key.key === 'Escape') { key.preventDefault(); close(true); return; }
      if (key.key !== 'Tab') return;
      const controls = Array.from(panel.querySelectorAll<HTMLElement>(focusSelector));
      if (key.shiftKey && (doc.activeElement === controls[0] || doc.activeElement === panel)) {
        key.preventDefault(); close(true);
      } else if (!key.shiftKey && (doc.activeElement === controls[controls.length - 1] || !controls.length)) {
        const pageControls = Array.from(doc.querySelectorAll<HTMLElement>(focusSelector)).filter(
          element => !panel.contains(element) && element.getClientRects().length > 0,
        );
        const next = pageControls[pageControls.indexOf(anchor) + 1] ?? anchor;
        key.preventDefault(); close(false); focus(next);
      }
    });
    /* Every conversation-bar panel is anchored to the fixed composer band.
     * The transcript and side lists scroll on their own while a Turn streams,
     * and those scrolls cannot move the anchor: only the document's own
     * scroll can. Dismissing on element scroll tied a static panel to
     * unrelated rendering (a new tool-call row auto-scrolling the transcript
     * closed the turn directory). */
    listen(doc, 'scroll', event => {
      const target = event.target as Node | null;
      if (target && panel.contains(target)) return;
      // A window scroll reports the document as its target, so these two
      // comparisons cover every anchor-moving scroll.
      if (target !== doc && target !== doc.scrollingElement) return;
      close(panel.contains(doc.activeElement));
    }, true);
    listen(win, 'resize', () => close(panel.contains(doc.activeElement)));
    listen(win, 'blur', () => close(false));
    listen(win, 'pagehide', () => close(false));
    listen(doc, 'visibilitychange', () => { if (doc.visibilityState === 'hidden') close(false); });
    if (win.visualViewport) listen(win.visualViewport, 'resize', () => close(panel.contains(doc.activeElement)));
  }
  return { open, close, isOpen: () => opened, destroy: () => { close(false); disposed = true; } };
}
