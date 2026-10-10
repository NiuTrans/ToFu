import { orchestrationRegistry } from './registry';
export interface OrchestrationRovingItemsOptions {
  root?: Element | null;
  selector?: string;
  entry?: HTMLElement | null;
  available?: (item: HTMLElement) => unknown;
  enabled?: () => unknown;
  wrap?: unknown;
  onFocus?: (item: HTMLElement) => unknown;
  onEntry?: (event: KeyboardEvent) => unknown;
}
type RovingItemsWindow = Window & {
  createOrchestrationRovingItemsController?:
    typeof createOrchestrationRovingItemsController;
};

/** Reusable roving-tabindex controller for filtered item collections. */
export function createOrchestrationRovingItemsController(
  options: OrchestrationRovingItemsOptions = {},
) {
  const root = options.root;
  const selector = options.selector ?? '[data-roving-item]';
  const entry = options.entry ?? null;
  let current: HTMLElement | null = null;
  const bound: HTMLElement[] = [];
  const all = (): HTMLElement[] => root
    ? Array.from(root.querySelectorAll<HTMLElement>(selector)) : [];
  const visible = (): HTMLElement[] => all().filter((item) => {
    if (item.hidden || 'disabled' in item && Boolean(item.disabled)
        || item.getAttribute('aria-disabled') === 'true'
        || item.closest('[hidden]')) return false;
    return !options.available || options.available(item) !== false;
  });
  const bindItems = (): void => {
    all().forEach((item) => {
      if (bound.includes(item)) return;
      bound.push(item);
      item.addEventListener('keydown', itemKeydown);
      item.addEventListener('focus', () => { sync(item); });
    });
  };
  function sync(preferred?: HTMLElement | null): HTMLElement | null {
    bindItems();
    const items = visible();
    const next = preferred && items.includes(preferred) ? preferred
      : current && items.includes(current) ? current : items[0] ?? null;
    all().forEach((item) => { item.tabIndex = item === next ? 0 : -1; });
    current = next;
    return current;
  }
  const focus = (index: number): boolean => {
    const items = visible();
    if (!items.length) return false;
    const bounded = options.wrap
      ? (index % items.length + items.length) % items.length
      : Math.max(0, Math.min(index, items.length - 1));
    current = items[bounded];
    sync(current);
    current.focus();
    options.onFocus?.(current);
    current.scrollIntoView?.({ block: 'nearest', inline: 'nearest' });
    return true;
  };
  const move = (item: HTMLElement, offset: number): boolean => {
    const items = visible();
    return focus(Math.max(0, items.indexOf(item)) + offset);
  };
  function itemKeydown(event: KeyboardEvent): void {
    if (options.enabled && !options.enabled()) return;
    if (event.target !== event.currentTarget) return;
    let handled = true;
    const item = event.currentTarget as HTMLElement;
    if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') move(item, -1);
    else if (event.key === 'ArrowRight' || event.key === 'ArrowDown') move(item, 1);
    else if (event.key === 'Home') focus(0);
    else if (event.key === 'End') focus(visible().length - 1);
    else handled = false;
    if (handled) event.preventDefault();
  }
  bindItems();
  entry?.addEventListener('keydown', (event: KeyboardEvent) => {
    if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
    options.onEntry?.(event);
    const items = visible();
    if (focus(event.key === 'ArrowDown' ? 0 : items.length - 1)) {
      event.preventDefault();
    }
  });
  sync();
  return {
    sync,
    focusFirst: () => focus(0),
    focusLast: () => focus(visible().length - 1),
  };
}

(orchestrationRegistry as unknown as RovingItemsWindow).createOrchestrationRovingItemsController =
  createOrchestrationRovingItemsController;
