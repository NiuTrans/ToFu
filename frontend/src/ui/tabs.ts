/**
 * tofu-ui tabs.
 *
 * Responsibility: WAI-ARIA tab pattern with automatic activation — roving
 * tabindex, ArrowLeft/Right/Home/End, aria-selected/panel wiring. The
 * platform gives us focusable buttons; this module adds list semantics.
 * Entry points: createTabs.
 * Dependencies: none (styles from 30-ui-kit.css).
 */

export interface TabItem {
  id: string;
  label: string;
  panel: HTMLElement;
  disabled?: boolean;
}

export interface TabsOptions {
  tabs: TabItem[];
  activeId?: string; // defaults to the first enabled tab
  onChange?: (id: string) => void;
}

export interface TabsHandle {
  el: HTMLDivElement;
  activate: (id: string) => void;
  activeId: () => string;
}

let tabsSeq = 0;

export function createTabs(options: TabsOptions): TabsHandle {
  tabsSeq += 1;
  const baseId = `ui-tabs-${tabsSeq}`;
  const enabled = options.tabs.filter((tab) => !tab.disabled);
  let current = options.activeId ?? enabled[0]?.id ?? options.tabs[0]?.id;

  const list = document.createElement('div');
  list.className = 'ui-tabs__list';
  list.setAttribute('role', 'tablist');

  const el = document.createElement('div');
  el.className = 'ui-tabs';
  el.appendChild(list);

  const buttons = new Map<string, HTMLButtonElement>();

  const activate = (id: string, focus = false): void => {
    const tab = options.tabs.find((item) => item.id === id);
    if (!tab || tab.disabled) return;
    current = id;
    for (const item of options.tabs) {
      const selected = item.id === id;
      const button = buttons.get(item.id);
      if (button) {
        button.setAttribute('aria-selected', String(selected));
        button.tabIndex = selected ? 0 : -1;
        if (selected && focus) button.focus();
      }
      item.panel.hidden = !selected;
    }
    options.onChange?.(id);
  };

  for (const [index, tab] of options.tabs.entries()) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'ui-tabs__tab';
    button.id = `${baseId}-tab-${tab.id}`;
    button.textContent = tab.label;
    button.setAttribute('role', 'tab');
    button.setAttribute('aria-controls', `${baseId}-panel-${tab.id}`);
    if (tab.disabled) button.disabled = true;
    button.addEventListener('click', () => activate(tab.id));
    button.addEventListener('keydown', (event) => {
      const ids = enabled.map((item) => item.id);
      const at = ids.indexOf(current);
      let next: string | null = null;
      if (event.key === 'ArrowRight') next = ids[(at + 1) % ids.length];
      else if (event.key === 'ArrowLeft') next = ids[(at - 1 + ids.length) % ids.length];
      else if (event.key === 'Home') next = ids[0];
      else if (event.key === 'End') next = ids[ids.length - 1];
      if (next) {
        event.preventDefault();
        activate(next, true);
      }
    });
    buttons.set(tab.id, button);
    list.appendChild(button);

    tab.panel.id = `${baseId}-panel-${tab.id}`;
    tab.panel.classList.add('ui-tabs__panel');
    tab.panel.setAttribute('role', 'tabpanel');
    tab.panel.setAttribute('aria-labelledby', button.id);
    tab.panel.tabIndex = 0;
    tab.panel.hidden = true;
    el.appendChild(tab.panel);
    void index;
  }

  if (current) activate(current);
  return { el, activate: (id) => activate(id), activeId: () => current };
}
