/** Shared composer options: visibility, keyboard focus and teardown.
 * Entry: createComposerOptionsSheet. Existing toolbar actions supply content
 * refresh; this owner never copies capability, model or conversation state.
 */
import { createLifecycleScope, type LifecycleScope } from './lifecycle';

interface Ports { document: Document; refresh(): void; label(): string }
interface Attribute { element: HTMLElement; name: string; previous: string | null }

export function createComposerOptionsSheet(ports: Ports): {
  toggle(): void; close(): void; destroy(): void;
} {
  let active: { sheet: HTMLElement; backdrop: HTMLElement | null; opener: HTMLElement | null;
    scope: LifecycleScope; attributes: Attribute[] } | null = null;
  let destroyed = false;
  const close = (restoreFocus = true): void => {
    const session = active;
    if (!session) return;
    active = null;
    session.scope.destroy();
    session.sheet.classList.remove('open');
    session.backdrop?.classList.remove('open');
    for (const { element, name, previous } of session.attributes) {
      if (previous === null) element.removeAttribute(name); else element.setAttribute(name, previous);
    }
    if (restoreFocus && session.opener?.isConnected) session.opener.focus({ preventScroll: true });
  };
  const toggle = (): void => {
    if (destroyed) return;
    if (active) { close(); return; }
    const sheet = ports.document.getElementById('mobileSheet');
    if (!sheet) return;
    const backdrop = ports.document.getElementById('mobileSheetBackdrop');
    const opener = ports.document.activeElement instanceof HTMLElement ? ports.document.activeElement : null;
    const scope = createLifecycleScope();
    const attributes: Attribute[] = [];
    const set = (element: HTMLElement, name: string, value: string): void => {
      attributes.push({ element, name, previous: element.getAttribute(name) });
      element.setAttribute(name, value);
    };
    ports.refresh();
    active = { sheet, backdrop, opener, scope, attributes };
    set(sheet, 'role', 'dialog');
    set(sheet, 'aria-modal', 'true');
    set(sheet, 'aria-label', ports.label());
    set(sheet, 'tabindex', '-1');
    if (opener) set(opener, 'aria-expanded', 'true');
    for (const item of sheet.querySelectorAll<HTMLElement>('.mobile-sheet-item[data-tofu-action]:not(button)')) {
      if (!item.hasAttribute('role')) set(item, 'role', 'button');
      if (!item.hasAttribute('tabindex')) set(item, 'tabindex', '0');
    }
    const focusable = (): HTMLElement[] => [...sheet.querySelectorAll<HTMLElement>(
      'button, input, select, textarea, a[href], [tabindex="0"]')].filter(element =>
        !element.matches(':disabled, [aria-disabled="true"], .disabled') && element.getClientRects().length > 0);
    sheet.classList.add('open');
    backdrop?.classList.add('open');
    scope.listen(ports.document, 'keydown', (raw) => {
      const event = raw as KeyboardEvent;
      if (event.key === 'Escape') {
        event.preventDefault(); event.stopImmediatePropagation(); close(); return;
      }
      if (event.key === 'Tab') {
        const items = focusable();
        const first = items[0] ?? sheet;
        const last = items.at(-1) ?? sheet;
        if (!sheet.contains(ports.document.activeElement) || (event.shiftKey
          ? ports.document.activeElement === first : ports.document.activeElement === last)) {
          event.preventDefault(); (event.shiftKey ? last : first).focus({ preventScroll: true });
        }
      }
      const target = event.target instanceof HTMLElement ? event.target : null;
      if ((event.key === 'Enter' || event.key === ' ') && target?.matches(
        '.mobile-sheet-item[data-tofu-action]:not(button):not(.disabled)')) {
        event.preventDefault(); event.stopImmediatePropagation(); target.click();
      }
    }, { capture: true });
    (focusable()[0] ?? sheet).focus({ preventScroll: true });
  };
  return { toggle, close: () => close(), destroy: () => { destroyed = true; close(false); } };
}
