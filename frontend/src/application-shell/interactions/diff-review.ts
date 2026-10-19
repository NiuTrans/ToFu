/** Two scoped gesture listeners quote edit evidence into the existing composer.
 * Entry point: installDiffReview. Dependencies: lifecycle and tool-diff-review.
 * Reuses the pending-quote owner. No dispatch, draft write, timer or observer.
 */
import type { LifecycleScope } from '../../lifecycle';
import {
  captureToolDiffReview, formatToolDiffReview, isToolDiffReviewCurrent,
  type DiffReviewQuote, type DiffReviewText,
} from '../../conversation/ui/tool-diff-review';

export interface DiffReviewPorts {
  contextIdentity(): string | null;
  text: DiffReviewText;
  addQuote(quote: string): void;
  reportFailure(error: unknown): void;
}
export function installDiffReview(
  scope: LifecycleScope, document: Document, input: HTMLTextAreaElement, ports: DiffReviewPorts,
): void {
  let pending: { button: HTMLButtonElement; quote: DiffReviewQuote | null; error?: unknown } | null = null;
  const buttonFor = (event: Event): HTMLButtonElement | null => {
    const target = event.target as Element | null;
    return target?.closest?.<HTMLButtonElement>('button[data-diff-review-action="quote"]') ?? null;
  };
  scope.listen(document, 'pointerdown', event => {
    pending = null;
    const button = buttonFor(event);
    if (!button || (event as PointerEvent).button !== 0) return;
    try { pending = { button, quote: captureToolDiffReview(button, ports.text) }; }
    catch (error) { pending = { button, quote: null, error }; }
  });
  scope.listen(document, 'click', event => {
    const button = buttonFor(event);
    if (!button) return;
    const captured = pending?.button === button ? pending : null;
    pending = null;
    try {
      if (captured?.error) throw captured.error;
      const quote = captured ? captured.quote : captureToolDiffReview(button, ports.text);
      if (!quote) return;
      if (!isToolDiffReviewCurrent(button, quote) || quote.context.conversationId !== ports.contextIdentity()) {
        throw new Error(ports.text('diffReview.stale', 'This change is no longer current. Reopen it in its original conversation.'));
      }
      if (!input.isConnected || input.disabled || input.readOnly) {
        throw new Error(ports.text('diffReview.unavailable', 'The message box is unavailable. Your draft was kept.'));
      }
      const reference = formatToolDiffReview(quote, ports.text);
      ports.addQuote(reference);
      document.getSelection()?.removeAllRanges();
      input.focus();
      input.setSelectionRange(input.value.length, input.value.length);
    } catch (error) { ports.reportFailure(error); }
  });
  scope.add(() => { pending = null; });
}
