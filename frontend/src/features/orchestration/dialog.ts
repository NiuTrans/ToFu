import { orchestrationRegistry } from './registry';
export interface OrchestrationDialogFocusOptions {
  document?: Document;
  window?: Window;
}
type DialogWindow = Window & {
  createOrchestrationDialogFocusManager?:
    typeof createOrchestrationDialogFocusManager;
};

/** Shared modal focus capture, restoration and Tab containment. */
export function createOrchestrationDialogFocusManager(
  options: OrchestrationDialogFocusOptions = {},
) {
  const doc = options.document ?? document;
  const win = options.window ?? window;
  let previousFocus: Element | null = null;
  const hidden = (control: HTMLElement, boundary: Element): boolean => {
    let current: HTMLElement | null = control;
    while (current) {
      if (current.hidden || current.getAttribute('aria-hidden') === 'true'
          || current.style && (current.style.display === 'none'
            || current.style.visibility === 'hidden')) return true;
      if (typeof win.getComputedStyle === 'function') {
        const computed = win.getComputedStyle(current);
        if (computed && (computed.display === 'none'
            || computed.visibility === 'hidden')) return true;
      }
      if (current === boundary) break;
      current = current.parentElement;
    }
    return false;
  };
  const focusable = (dialog: Element | null): HTMLElement[] => {
    if (!dialog) return [];
    const selector = 'a[href],button:not([disabled]),input:not([disabled]),'
      + 'select:not([disabled]),textarea:not([disabled]),'
      + '[tabindex]:not([tabindex="-1"])';
    return Array.from(dialog.querySelectorAll<HTMLElement>(selector))
      .filter((control) => !hidden(control, dialog));
  };
  const trapTab = (
    event: KeyboardEvent | Record<string, unknown>, dialog: HTMLElement,
  ): void => {
    const controls = focusable(dialog);
    const preventDefault = event.preventDefault;
    const prevent = () => typeof preventDefault === 'function'
      && preventDefault.call(event);
    if (!controls.length) {
      prevent();
      dialog.focus();
      return;
    }
    const first = controls[0];
    const last = controls[controls.length - 1];
    const active = doc.activeElement;
    if (event.shiftKey && (active === first || !dialog.contains(active))) {
      prevent();
      last.focus();
    } else if (!event.shiftKey && (active === last || active === dialog
        || !dialog.contains(active))) {
      prevent();
      first.focus();
    }
  };
  const open = (element: HTMLElement | null, display?: string): HTMLElement | null => {
    if (!element) return null;
    if (element.style.display === 'none') previousFocus = doc.activeElement;
    element.style.display = display || 'flex';
    element.querySelector<HTMLElement>('[role="dialog"]')?.focus();
    return element;
  };
  const close = (element: HTMLElement | null): boolean => {
    if (!element) return false;
    element.style.display = 'none';
    const focusablePrevious = previousFocus as (Element & { focus?: () => void }) | null;
    if (focusablePrevious?.focus
        && (typeof doc.contains !== 'function' || doc.contains(previousFocus))) {
      focusablePrevious.focus();
    }
    previousFocus = null;
    return true;
  };
  return { close, focusable, open, trapTab };
}

(orchestrationRegistry as unknown as DialogWindow).createOrchestrationDialogFocusManager =
  createOrchestrationDialogFocusManager;
