/**
 * tofu-ui button factory.
 *
 * Responsibility: create `<button class="ui-btn…">` elements with the
 * design-system variant/size contract baked in. Callers never hand-write
 * ui-* class strings.
 * Entry points: createButton, createIconButton.
 * Dependencies: none (styles come from 30-ui-kit.css; no runtime imports).
 */

export type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger';
export type ButtonSize = 'sm' | 'md' | 'lg';

export interface ButtonOptions {
  label: string;
  variant?: ButtonVariant;
  size?: ButtonSize;
  type?: 'button' | 'submit';

  /** Long localized actions may wrap at narrow widths. */
  wrap?: boolean;
  disabled?: boolean;
  title?: string;
  onClick?: (event: MouseEvent) => void;
}

const VARIANT_CLASS: Record<ButtonVariant, string> = {
  primary: ' ui-btn--primary',
  secondary: ' ui-btn--secondary',
  ghost: ' ui-btn--ghost',
  danger: ' ui-btn--danger',
};

const SIZE_CLASS: Record<ButtonSize, string> = {
  sm: ' ui-btn--sm',
  md: '',
  lg: ' ui-btn--lg',
};

export function createButton(
  options: ButtonOptions,
  ownerDocument: Document = document,
): HTMLButtonElement {
  const variant = options.variant ?? 'secondary';
  const size = options.size ?? 'md';
  const button = ownerDocument.createElement('button');
  button.type = options.type ?? 'button';
  button.className = `ui-btn${VARIANT_CLASS[variant]}${SIZE_CLASS[size]}`;

  if (options.wrap) button.classList.add('ui-btn--wrap');
  button.textContent = options.label;
  if (options.title) button.title = options.title;
  if (options.disabled) button.disabled = true;
  if (options.onClick) button.addEventListener('click', options.onClick);
  return button;
}

export interface IconButtonOptions {
  icon: Node;
  label: string; // accessible name — icon-only buttons still need one
  size?: ButtonSize;
  title?: string;
  disabled?: boolean;
  onClick?: (event: MouseEvent) => void;
}

const ICON_SIZE_CLASS: Record<ButtonSize, string> = {
  sm: ' ui-icon-btn--sm',
  md: '',
  lg: ' ui-icon-btn--lg',
};

export function createIconButton(options: IconButtonOptions): HTMLButtonElement {
  const size = options.size ?? 'md';
  const button = document.createElement('button');
  button.type = 'button';
  button.className = `ui-icon-btn${ICON_SIZE_CLASS[size]}`;
  button.setAttribute('aria-label', options.label);
  button.title = options.title ?? options.label;
  button.appendChild(options.icon);
  if (options.disabled) button.disabled = true;
  if (options.onClick) button.addEventListener('click', options.onClick);
  return button;
}
