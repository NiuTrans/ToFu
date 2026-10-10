/**
 * tofu-ui drawer (right-docked modal panel).
 *
 * Responsibility: native <dialog> variant sharing the dialog's platform
 * semantics (focus trap, Escape, top layer) with an edge-docked layout.
 * Entry points: createDrawer.
 * Dependencies: createIconButton (button.ts); styles from 30-ui-kit.css.
 */

import { createIconButton } from './button';

export interface DrawerOptions {
  title: string;
  body: Node | string; // string is inserted as text, never as HTML
  onClose?: () => void;
}

export interface DrawerHandle {
  el: HTMLDialogElement;
  /** Show the modal drawer; resolves when it closes (any dismissal path). */
  open: () => Promise<void>;
  close: () => void;
}

const CLOSE_ICON_SVG =
  '<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" ' +
  'stroke-width="2" stroke-linecap="round"><path d="M4 4l8 8M12 4l-8 8"/></svg>';

export function createDrawer(options: DrawerOptions): DrawerHandle {
  const el = document.createElement('dialog');
  el.className = 'ui-dialog ui-drawer';

  const header = document.createElement('div');
  header.className = 'ui-dialog__header';
  const title = document.createElement('h2');
  title.className = 'ui-dialog__title';
  title.textContent = options.title;
  header.appendChild(title);
  const icon = document.createElement('span');
  icon.innerHTML = CLOSE_ICON_SVG;
  const closeButton = createIconButton({
    icon: icon.firstElementChild ?? icon,
    label: '关闭面板',
    onClick: () => el.close(''),
  });
  header.appendChild(closeButton);
  el.appendChild(header);

  const body = document.createElement('div');
  body.className = 'ui-dialog__body';
  if (typeof options.body === 'string') {
    body.textContent = options.body;
  } else {
    body.appendChild(options.body);
  }
  el.appendChild(body);

  let resolveOpen: (() => void) | null = null;
  el.addEventListener('close', () => {
    options.onClose?.();
    const resolve = resolveOpen;
    resolveOpen = null;
    resolve?.();
  });
  // Backdrop click: coordinates outside the dialog box mean the backdrop.
  // The gesture must also START outside — a text-selection drag begins
  // inside and releases outside, and must not close the drawer.
  let pressedOutside = false;
  const isOutsideDialog = (event: MouseEvent): boolean => {
    const rect = el.getBoundingClientRect();
    return (
      event.clientX < rect.left || event.clientX > rect.right ||
      event.clientY < rect.top || event.clientY > rect.bottom
    );
  };
  el.addEventListener('mousedown', (event) => {
    pressedOutside = isOutsideDialog(event);
  });
  el.addEventListener('click', (event) => {
    if (pressedOutside && isOutsideDialog(event)) el.close('');
    pressedOutside = false;
  });

  return {
    el,
    open: () => {
      if (el.open) return Promise.resolve();
      const promise = new Promise<void>((resolve) => {
        resolveOpen = resolve;
      });
      el.showModal();
      return promise;
    },
    close: () => {
      if (el.open) el.close('');
    },
  };
}
