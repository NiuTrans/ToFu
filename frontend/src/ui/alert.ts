/**
 * tofu-ui inline alert banner.
 *
 * Responsibility: persistent (until dismissed) contextual feedback with
 * feedback-variant styling; optionally dismissible.
 * Entry points: createAlert.
 * Dependencies: createIconButton (button.ts); styles from 30-ui-kit.css.
 */

import { createIconButton } from './button';

export type AlertVariant = 'info' | 'success' | 'warning' | 'error';

export interface AlertOptions {
  variant: AlertVariant;
  title: string;
  body?: string;
  dismissible?: boolean;
  onDismiss?: () => void;
}

export interface AlertHandle {
  el: HTMLDivElement;
  dismiss: () => void;
}

const CLOSE_ICON_SVG =
  '<svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" ' +
  'stroke-width="2" stroke-linecap="round"><path d="M4 4l8 8M12 4l-8 8"/></svg>';

export function createAlert(options: AlertOptions): AlertHandle {
  const el = document.createElement('div');
  el.className = `ui-alert ui-alert--${options.variant}`;
  el.setAttribute('role', options.variant === 'error' ? 'alert' : 'status');

  const title = document.createElement('div');
  title.className = 'ui-alert__title';
  title.textContent = options.title;
  el.appendChild(title);

  if (options.body) {
    const body = document.createElement('div');
    body.textContent = options.body;
    el.appendChild(body);
  }

  const dismiss = (): void => {
    el.remove();
    options.onDismiss?.();
  };

  if (options.dismissible) {
    const icon = document.createElement('span');
    icon.innerHTML = CLOSE_ICON_SVG;
    const button = createIconButton({
      icon: icon.firstElementChild ?? icon,
      label: '关闭提示',
      size: 'sm',
      onClick: dismiss,
    });
    button.classList.add('ui-alert__dismiss');
    el.appendChild(button);
  }

  return { el, dismiss };
}
