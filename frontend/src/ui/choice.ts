/**
 * tofu-ui choice controls: checkbox / radio / switch.
 *
 * Responsibility: labeled native `<input>` with the kit's custom skin —
 * keyboard, focus and form semantics stay with the platform.
 * Entry points: createChoice.
 * Dependencies: none (styles from 30-ui-kit.css).
 */

export type ChoiceType = 'checkbox' | 'radio' | 'switch';

export interface ChoiceOptions {
  type?: ChoiceType;
  label: string;
  name?: string; // radios sharing a name form one group
  value?: string;
  checked?: boolean;
  disabled?: boolean;
  onChange?: (checked: boolean, input: HTMLInputElement) => void;
}

export interface ChoiceHandle {
  el: HTMLLabelElement;
  input: HTMLInputElement;
}

export function createChoice(options: ChoiceOptions): ChoiceHandle {
  const type = options.type ?? 'checkbox';
  const input = document.createElement('input');
  input.type = type === 'radio' ? 'radio' : 'checkbox';
  if (type === 'switch') {
    input.className = 'ui-switch';
    input.setAttribute('role', 'switch');
  }
  if (options.name) input.name = options.name;
  if (options.value) input.value = options.value;
  if (options.checked) input.checked = true;
  if (options.disabled) input.disabled = true;
  if (options.onChange) {
    input.addEventListener('change', () => options.onChange?.(input.checked, input));
  }

  const text = document.createElement('span');
  text.textContent = options.label;

  const el = document.createElement('label');
  el.className = 'ui-choice';
  el.appendChild(input);
  el.appendChild(text);
  return { el, input };
}
