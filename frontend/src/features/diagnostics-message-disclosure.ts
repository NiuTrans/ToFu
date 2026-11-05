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
