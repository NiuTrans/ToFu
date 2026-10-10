/** Native modal reading surface. The same controls/canvas remain descendants of
 * their owner, while the browser makes background navigation inert. Exit restores
 * exact node positions and focus; no second reader or fullscreen permission exists.
 */
export class ReadingFocus {
  private dialog: HTMLDialogElement | null = null;
  private positions: Array<{ node: HTMLElement; marker: Comment }> = [];
  private opener: HTMLElement | null = null;

  constructor(private readonly host: HTMLElement, private readonly parts: readonly HTMLElement[],
    private readonly exit: () => void) {}

  enter(label: string, opener: HTMLElement): boolean {
    if (this.dialog) return true;
    const dialog = document.createElement('dialog');
    if (typeof dialog.showModal !== 'function' || !this.host.isConnected) return false;
    const restoreScroll = this.captureScroll();
    dialog.className = 'paper-reader-focus'; dialog.setAttribute('aria-label', label);
    dialog.addEventListener('cancel', event => { event.preventDefault(); this.exit(); });
    this.dialog = dialog; this.opener = opener;
    this.host.append(dialog);
    for (const node of this.parts) {
      const marker = document.createComment('reader-position'); node.replaceWith(marker);
      this.positions.push({ node, marker }); dialog.append(node);
    }
    try { dialog.showModal(); restoreScroll(); opener.focus({ preventScroll: true }); return true; }
    catch { this.leave(false); restoreScroll(); return false; }
  }

  leave(restoreFocus = true): void {
    const dialog = this.dialog;
    if (!dialog) return;
    const restoreScroll = this.captureScroll();
    this.dialog = null;
    if (dialog.open) dialog.close();
    for (const { node, marker } of this.positions) marker.replaceWith(node);
    this.positions = []; dialog.remove();
    restoreScroll();
    if (restoreFocus && this.opener?.isConnected) this.opener.focus({ preventScroll: true });
    this.opener = null;
  }

  private captureScroll(): () => void {
    const positions = this.parts.map(node => ({ node, top: node.scrollTop, left: node.scrollLeft }));
    // Reparenting a scroll container resets its offsets even though its PDF
    // children survive. Restore before ResizeObserver measures the new layout.
    return () => { for (const { node, top, left } of positions) { node.scrollTop = top; node.scrollLeft = left; } };
  }
}
