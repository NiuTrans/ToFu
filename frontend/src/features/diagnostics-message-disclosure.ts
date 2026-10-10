/** Native Inspector disclosures. Design-system buttons provide keyboard behavior; aria state follows the retained block state. */
import { createButton } from '../ui/button';
export function createInspectorDisclosure(ownerDocument: Document = document): HTMLButtonElement {
  const button = createButton({ label: '', variant: 'ghost', size: 'sm' }, ownerDocument);
  button.classList.add('debug-msg-header');
  button.setAttribute('aria-expanded', 'false');
  return button;
}
export function syncInspectorDisclosure(block: Element): void {
  block.querySelector<HTMLButtonElement>('.debug-msg-header')?.setAttribute(
    'aria-expanded', String(block.classList.contains('open')));
}

/** Restore only the focused message header, keyed by complete stable identity. */
export function captureInspectorFocus(content: Element): string | null {
  const active = content.ownerDocument.activeElement;
  if (!active || !content.contains(active) || !active.matches('.debug-msg-header')) return null;
  return active.parentElement?.getAttribute('data-mid') ?? null;
}
export function restoreInspectorFocus(content: Element, identity: string | null): void {
  if (identity === null) return;
  for (const block of content.querySelectorAll<HTMLElement>('.debug-msg-block')) {
    if (block.dataset.mid !== identity) continue;
    block.querySelector<HTMLButtonElement>('.debug-msg-header')?.focus({ preventScroll: true });
    return;
  }
}
