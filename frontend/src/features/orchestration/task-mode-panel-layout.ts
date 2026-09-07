import { orchestrationRegistry } from './registry';
import { setOrchestrationPanelState } from './panel-state';
import { createOrchestrationRovingItemsController } from './roving-items';
import { taskModeCompactMedia } from './responsive';
import { createTaskModePanelSelection } from './task-mode-panel-selection';

interface RovingItemsController {
  sync(target?: Element | null): unknown;
}
export interface TaskModePanelLayoutOptions {
  document?: Document;
  window?: Window;
  mediaQuery?: string;
  onChange?: (active: string, compact: boolean) => unknown;
  selection?: ReturnType<typeof createTaskModePanelSelection>;
}
type TaskModePanelLayoutWindow = Window & {
  createTaskModePanelLayoutController?: typeof createTaskModePanelLayoutController;
};

/** Responsive Runs / Run / Inspector workspace policy. */
export function createTaskModePanelLayoutController(
  options: TaskModePanelLayoutOptions = {},
) {
  const doc = options.document ?? document;
  const win = options.window ?? window;
  const names = ['runs', 'run', 'inspector'] as const;
  const selection = options.selection ?? createTaskModePanelSelection({
    names, initial: 'runs',
  });
  let boundTablist: Element | null = null;
  let tabRoving: RovingItemsController | null = null;
  const media = options.mediaQuery && typeof win.matchMedia === 'function'
    ? win.matchMedia(options.mediaQuery)
    : taskModeCompactMedia(win);
  const compact = (): boolean => Boolean(media?.matches);
  const panel = (name: string): HTMLElement | null =>
    doc.querySelector(`[data-tm-panel-view="${name}"]`);
  const trigger = (name: string): HTMLElement | null =>
    doc.querySelector(`[data-tm-panel="${name}"]`);
  const title = (name: string): string =>
    name.charAt(0).toUpperCase() + name.slice(1);
  const tabId = (name: string): string => `tmTab${title(name)}`;
  const panelId = (name: string): string => `tmPanel${title(name)}`;

  const syncSemantics = (
    name: string, isCompact: boolean, selected: boolean,
  ): void => {
    const button = trigger(name);
    const surface = panel(name);
    if (!button || !surface) return;
    if (!button.id) button.id = tabId(name);
    if (!surface.id) surface.id = panelId(name);
    button.setAttribute('aria-controls', surface.id);
    if (isCompact) {
      button.setAttribute('role', 'tab');
      button.setAttribute('aria-selected', selected ? 'true' : 'false');
      button.removeAttribute('aria-pressed');
      button.setAttribute('tabindex', selected ? '0' : '-1');
      surface.setAttribute('role', 'tabpanel');
      surface.setAttribute('aria-labelledby', button.id);
      return;
    }
    button.removeAttribute('role');
    button.removeAttribute('aria-selected');
    button.setAttribute('aria-pressed', selected ? 'true' : 'false');
    button.removeAttribute('tabindex');
    surface.removeAttribute('role');
    surface.removeAttribute('aria-labelledby');
  };

  const bindTablist = (): void => {
    const tablist = doc.querySelector('.tm-mobile-tabs');
    if (!tablist || tablist === boundTablist) return;
    boundTablist = tablist;
    tabRoving = createOrchestrationRovingItemsController({
      root: tablist,
      selector: '[data-tm-panel]',
      enabled: compact,
      wrap: true,
      onFocus: (button) => select(button.getAttribute('data-tm-panel')),
    }) as RovingItemsController;
  };

  function sync(): string {
    const isCompact = compact();
    const active = selection.active();
    const tablist = doc.querySelector('.tm-mobile-tabs');
    const activeTrigger = trigger(active);
    bindTablist();
    if (tablist) {
      if (isCompact) {
        tablist.setAttribute('role', 'tablist');
        tablist.setAttribute('aria-orientation', 'horizontal');
      } else {
        tablist.removeAttribute('role');
        tablist.removeAttribute('aria-orientation');
      }
    }
    names.forEach((name) => {
      const button = trigger(name);
      setOrchestrationPanelState(
        panel(name) as Parameters<typeof setOrchestrationPanelState>[0],
        !isCompact || active === name,
        { document: doc, focusTarget: activeTrigger },
      );
      if (!button) return;
      const selected = active === name;
      button.classList.toggle('is-active', selected);
      syncSemantics(name, isCompact, selected);
    });
    tabRoving?.sync(activeTrigger);
    doc.querySelector('.tm-body')?.setAttribute('data-tm-active-panel', active);
    options.onChange?.(active, isCompact);
    return active;
  }
  const selectInternal = (name: unknown): string => {
    if (!names.includes(name as typeof names[number])) return selection.active();
    selection.select(name);
    return sync();
  };
  const select = (name: unknown): string => selectInternal(name);
  const present = (name: unknown, owner: unknown): string => {
    if (!names.includes(name as typeof names[number]) || !owner) {
      return selection.active();
    }
    const before = selection.snapshot();
    selection.present(name, owner);
    const after = selection.snapshot();
    return before.active === after.active && before.owner === after.owner
      && before.preferred === after.preferred ? after.active : sync();
  };
  const release = (owner: unknown, fallback: unknown): string => {
    const before = selection.snapshot();
    selection.release(owner, fallback);
    const after = selection.snapshot();
    return before.active === after.active && before.owner === after.owner
      && before.preferred === after.preferred ? after.active : sync();
  };
  media?.addEventListener?.('change', sync);

  return {
    active: selection.active,
    compact,
    present,
    release,
    select,
    selection: selection.snapshot,
    sync,
  };
}

(orchestrationRegistry as unknown as TaskModePanelLayoutWindow).createTaskModePanelLayoutController =
  createTaskModePanelLayoutController;
