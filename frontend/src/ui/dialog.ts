/**
 * tofu-ui modal dialog.
 *
 * Responsibility: promise-based modal built on the NATIVE <dialog> element —
 * Escape-to-close and top-layer rendering come from the platform; an explicit
 * boundary cycle prevents Tab from landing on the document body. Adds structure (header/body/footer), a
 * footer action row, and backdrop-click dismissal.
 * Entry points: createDialog.
 * Dependencies: createButton (button.ts); styles from 30-ui-kit.css.
 */

import { createButton } from './button';
import type { ButtonVariant } from './button';

export interface DialogAction {
  id: string;
  label: string;
  variant?: ButtonVariant; // default: 'secondary'; last action usually 'primary'
}

export interface DialogOptions {
  title: string;
  body: Node | string; // string is inserted as text, never as HTML

  /** Project workbench chrome; default dialogs remain unchanged. */
  appearance?: 'default' | 'project';
  actions?: DialogAction[]; // empty/omitted → single "OK"-less informational modal
  onClose?: (actionId: string | null) => void;
}

export interface DialogHandle {
  el: HTMLDialogElement;
  /** Show the modal. Resolves with the clicked action id, or null when
   *  dismissed via Escape / backdrop click. */
  open: () => Promise<string | null>;
  close: (result?: string | null) => void;
}

export function createDialog(options: DialogOptions): DialogHandle {
  const el = document.createElement('dialog');
  el.className = 'ui-dialog';

  if (options.appearance === 'project') el.classList.add('ui-dialog--project');

  const header = document.createElement('div');
  header.className = 'ui-dialog__header';
  const title = document.createElement('h2');
  title.className = 'ui-dialog__title';
  title.textContent = options.title;
  header.appendChild(title);
  el.appendChild(header);

  const body = document.createElement('div');
  body.className = 'ui-dialog__body';
  if (typeof options.body === 'string') {
    body.textContent = options.body;
  } else {
    body.appendChild(options.body);
  }
  el.appendChild(body);

  let resolveOpen: ((result: string | null) => void) | null = null;
  let settled = true;

  const settle = (result: string | null): void => {
    if (settled) return;
    settled = true;
    const resolve = resolveOpen;
    resolveOpen = null;
    options.onClose?.(result);
    resolve?.(result);
  };

  const actions = options.actions ?? [];
  if (actions.length > 0) {
    const footer = document.createElement('div');
    footer.className = 'ui-dialog__footer';
    for (const action of actions) {
      footer.appendChild(createButton({
        label: action.label,
        variant: action.variant ?? 'secondary',
        onClick: () => {
          settle(action.id);
          el.close(action.id);
        },
      }));
    }
    el.appendChild(footer);
  }

  // Chromium's native modal cycle may briefly focus the document body after
  // the last control. Keep keyboard users on the meaningful modal controls.
  el.addEventListener('keydown', event => {
    if (event.key !== 'Tab' || !el.open) return;
    const controls = [...el.querySelectorAll<HTMLElement>('a[href],button,input,select,textarea,[tabindex]')]
      .filter(node => node.tabIndex >= 0 && !node.matches(':disabled')
        && !node.closest('[hidden],[inert]') && getComputedStyle(node).display !== 'none'
        && getComputedStyle(node).visibility !== 'hidden');
    const first = controls[0], last = controls.at(-1);
    if (!first || !last) { event.preventDefault(); el.focus(); return; }
    const active = document.activeElement;
    if (event.shiftKey ? active === first || active === el || !el.contains(active)
      : active === last || active === el || !el.contains(active)) {
      event.preventDefault(); (event.shiftKey ? last : first).focus();
    }
  });

  // Native 'cancel' event = Escape key. 'close' fires for every dismissal
  // path; el.returnValue is '' for Escape and programmatic close().
  el.addEventListener('close', () => {
    settle(el.returnValue === '' ? null : el.returnValue);
  });
  // Backdrop click: when the pointer lands outside the dialog's box, the
  // browser reports the click against the backdrop. Retargeting details vary
  // (the event target may be the dialog itself OR a stretched ancestor), so
  // decide from coordinates instead of the target node.
  //
  // Only dismiss when the gesture STARTED outside as well as ended outside:
  // a text-selection drag begins inside the dialog, crosses the boundary,
  // and releases outside — the resulting click must not close the dialog.
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
    if (pressedOutside && isOutsideDialog(event)) {
      settle(null);
      el.close('');
    }
    pressedOutside = false;
  });

  return {
    el,
    open: () => {
      if (el.open) {
        return Promise.resolve(null);
      }
      settled = false;
      const promise = new Promise<string | null>((resolve) => {
        resolveOpen = resolve;
      });
      el.showModal();
      return promise;
    },
    close: (result = null) => {
      if (!el.open) return;
      settle(result);
      el.close(result ?? '');
    },
  };
}
