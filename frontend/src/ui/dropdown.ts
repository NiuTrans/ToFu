/**
 * tofu-ui dropdown menu.
 *
 * Responsibility: trigger button + role=menu popup with keyboard support
 * (arrows/Home/End/Escape/Tab), outside-click dismissal and focus return.
 * Positioning is layout-simple (anchored below the trigger, optional end
 * alignment); collision handling belongs to a future floating layer.
 * Entry points: createDropdown.
 * Dependencies: createButton (button.ts); styles from 30-ui-kit.css.
 */

import { createButton } from './button';
import type { ButtonSize, ButtonVariant } from './button';

export interface MenuItem {
  id: string;
  label: string;
  danger?: boolean;
  disabled?: boolean;
}

export type MenuEntry = MenuItem | 'separator';

/** Shared existing menu-row primitive; independent menus reuse the same control. */
export function createMenuItem(entry: MenuItem, ownerDocument: Document = document): HTMLButtonElement {
  const item = ownerDocument.createElement('button');
  item.type = 'button';
  item.className = `ui-menu__item${entry.danger ? ' ui-menu__item--danger' : ''}`;
  item.textContent = entry.label;
  item.setAttribute('role', 'menuitem');
  item.tabIndex = -1;
  if (entry.disabled) item.disabled = true;
  return item;
}

export interface DropdownOptions {
  label: string;
  items: MenuEntry[];
  onSelect: (id: string) => void;
  variant?: ButtonVariant;
  size?: ButtonSize;
  align?: 'start' | 'end';
  disabled?: boolean;
}

export interface DropdownHandle {
  el: HTMLDivElement;
  open: () => void;
  close: (returnFocus?: boolean) => void;
  isOpen: () => boolean;
}

export function createDropdown(options: DropdownOptions): DropdownHandle {
  const el = document.createElement('div');
  el.className = 'ui-dropdown';

  const trigger = createButton({
    label: options.label,
    variant: options.variant ?? 'secondary',
    size: options.size,
    disabled: options.disabled,
  });
  trigger.setAttribute('aria-haspopup', 'menu');
  trigger.setAttribute('aria-expanded', 'false');
  el.appendChild(trigger);

  const menu = document.createElement('div');
  menu.className = `ui-menu${options.align === 'end' ? ' ui-menu--align-end' : ''}`;
  menu.setAttribute('role', 'menu');
  menu.hidden = true;
  el.appendChild(menu);

  const itemButtons: HTMLButtonElement[] = [];
  for (const entry of options.items) {
    if (entry === 'separator') {
      const separator = document.createElement('hr');
      separator.className = 'ui-menu__separator';
      menu.appendChild(separator);
      continue;
    }
    const item = createMenuItem(entry);
    item.addEventListener('click', () => {
      close();
      options.onSelect(entry.id);
    });
    itemButtons.push(item);
    menu.appendChild(item);
  }

  let open = false;

  const close = (returnFocus = false): void => {
    if (!open) return;
    open = false;
    menu.hidden = true;
    trigger.setAttribute('aria-expanded', 'false');
    document.removeEventListener('pointerdown', onOutsidePointer, true);
    if (returnFocus) trigger.focus();
  };

  const onOutsidePointer = (event: PointerEvent): void => {
    if (!el.contains(event.target as Node)) close();
  };

  const focusItem = (index: number): void => {
    const enabled = itemButtons.filter((button) => !button.disabled);
    if (enabled.length === 0) return;
    const clamped = ((index % enabled.length) + enabled.length) % enabled.length;
    enabled[clamped].focus();
  };

  const openMenu = (focusFirst = true): void => {
    if (open || trigger.disabled) return;
    open = true;
    menu.hidden = false;
    trigger.setAttribute('aria-expanded', 'true');
    document.addEventListener('pointerdown', onOutsidePointer, true);
    if (focusFirst) focusItem(0);
  };

  trigger.addEventListener('click', () => (open ? close(true) : openMenu(false)));
  trigger.addEventListener('keydown', (event) => {
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      if (open) {
        focusItem(0);
      } else {
        openMenu(true);
      }
    } else if (event.key === 'Enter' || event.key === ' ') {
      if (!open) {
        event.preventDefault();
        openMenu(true);
      }
    } else if (event.key === 'Escape' && open) {
      // Focus is on the trigger here (menu keydown covers in-menu Escape).
      event.preventDefault();
      close();
    }
  });
  menu.addEventListener('keydown', (event) => {
    const enabled = itemButtons.filter((button) => !button.disabled);
    const at = enabled.indexOf(document.activeElement as HTMLButtonElement);
    if (event.key === 'Escape') {
      event.preventDefault();
      close(true);
    } else if (event.key === 'ArrowDown') {
      event.preventDefault();
      focusItem(at + 1);
    } else if (event.key === 'ArrowUp') {
      event.preventDefault();
      focusItem(at - 1);
    } else if (event.key === 'Home') {
      event.preventDefault();
      focusItem(0);
    } else if (event.key === 'End') {
      event.preventDefault();
      focusItem(enabled.length - 1);
    } else if (event.key === 'Tab') {
      close();
    }
  });

  return { el, open: openMenu, close, isOpen: () => open };
}
