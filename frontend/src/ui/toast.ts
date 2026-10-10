/**
 * tofu-ui toast notifications.
 *
 * Responsibility: transient, bounded (MAX_VISIBLE) status messages in a
 * single shared aria-live region appended to <body> on first use.
 * Entry points: toast, dismissAllToasts.
 * Dependencies: createIconButton (button.ts); styles from 30-ui-kit.css.
 */

import { createIconButton } from './button';

export type ToastVariant = 'default' | 'success' | 'warning' | 'error' | 'info';

export interface ToastOptions {
  variant?: ToastVariant;
  title?: string;
  /** ms before auto-dismiss; 0 keeps the toast until dismissed manually. */
  duration?: number;
}

export interface ToastHandle {
  el: HTMLDivElement;
  dismiss: () => void;
}

const MAX_VISIBLE = 4;
const DEFAULT_DURATION = 4000;

let region: HTMLDivElement | null = null;

function toastRegion(): HTMLDivElement {
  if (!region || !region.isConnected) {
    region = document.createElement('div');
    region.className = 'ui-toast-region';
    region.setAttribute('role', 'status');
    region.setAttribute('aria-live', 'polite');
    document.body.appendChild(region);
  }
  return region;
}

const CLOSE_ICON_SVG =
  '<svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" ' +
  'stroke-width="2" stroke-linecap="round"><path d="M4 4l8 8M12 4l-8 8"/></svg>';

export function toast(message: string, options: ToastOptions = {}): ToastHandle {
  const container = toastRegion();

  const el = document.createElement('div');
  const variant = options.variant ?? 'default';
  el.className = `ui-toast${variant === 'default' ? '' : ` ui-toast--${variant}`}`;

  const body = document.createElement('div');
  body.className = 'ui-toast__body';
  if (options.title) {
    const title = document.createElement('div');
    title.className = 'ui-toast__title';
    title.textContent = options.title;
    body.appendChild(title);
  }
  const text = document.createElement('div');
  text.textContent = message;
  body.appendChild(text);
  el.appendChild(body);

  const dismiss = (): void => {
    window.clearTimeout(timer);
    el.remove();
  };

  const icon = document.createElement('span');
  icon.innerHTML = CLOSE_ICON_SVG;
  const closeButton = createIconButton({
    icon: icon.firstElementChild ?? icon,
    label: '关闭通知',
    size: 'sm',
    onClick: dismiss,
  });
  el.appendChild(closeButton);

  const timer = window.setTimeout(
    dismiss,
    options.duration ?? DEFAULT_DURATION,
  );
  if ((options.duration ?? DEFAULT_DURATION) === 0) window.clearTimeout(timer);

  // Bounded stream: drop the oldest when the region is full.
  while (container.children.length >= MAX_VISIBLE) {
    container.firstElementChild?.remove();
  }
  container.appendChild(el);
  return { el, dismiss };
}

/** Test/teardown hook: remove every toast and detach the region. */
export function dismissAllToasts(): void {
  region?.remove();
  region = null;
}
