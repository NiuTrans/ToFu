/**
 * tofu-ui field factory.
 *
 * Responsibility: compose label + control + hint/error into a `.ui-field`
 * block with correct aria wiring (labelledby / describedby / invalid).
 * Entry points: createField, setFieldError.
 * Dependencies: none (styles come from 30-ui-kit.css; no runtime imports).
 */

export interface FieldOptions {
  label: string;
  control?: HTMLElement; // an existing compatible control, or use the native adapter below
  value?: string;
  inputType?: 'text' | 'number';
  choices?: Array<{ value: string; label: string }>;
  controlId?: string; // id used for label[for]; generated when omitted
  hint?: string;
  required?: boolean;
}

export interface FieldHandle {
  el: HTMLDivElement;
  control: HTMLElement;
}

let fieldSeq = 0;

export function createField(options: FieldOptions): FieldHandle {
  const control = options.control ?? (() => {
    if (options.choices) {
      const select = document.createElement('select');
      for (const choice of options.choices) {
        const option = document.createElement('option');
        option.value = choice.value; option.textContent = choice.label;
        if (choice.value === (options.value ?? '')) option.setAttribute('selected', '');
        select.appendChild(option);
      }
      return select;
    }
    const input = document.createElement('input');
    input.className = 'ui-input'; input.type = options.inputType ?? 'text';
    input.setAttribute('value', options.value ?? '');
    return input;
  })();
  fieldSeq += 1;
  const controlId = options.controlId ?? `ui-field-control-${fieldSeq}`;
  control.id = controlId;

  const label = document.createElement('label');
  label.className = 'ui-field__label';
  label.htmlFor = controlId;
  label.textContent = options.label;
  if (options.required) {
    const marker = document.createElement('span');
    marker.className = 'ui-field__label-required';
    marker.textContent = ' *';
    label.appendChild(marker);
  }

  const el = document.createElement('div');
  el.className = 'ui-field';
  el.appendChild(label);
  if (options.choices && !options.control) {
    const selectWrapper = document.createElement('div');
    selectWrapper.className = 'ui-select'; selectWrapper.appendChild(control);
    el.appendChild(selectWrapper);
  } else el.appendChild(control);

  if (options.hint) {
    const hint = document.createElement('div');
    hint.className = 'ui-field__hint';
    hint.id = `${controlId}-hint`;
    hint.textContent = options.hint;
    el.appendChild(hint);
    control.setAttribute('aria-describedby', hint.id);
  }

  const error = document.createElement('div');
  error.className = 'ui-field__error';
  error.id = `${controlId}-error`;
  error.setAttribute('role', 'alert');
  el.appendChild(error);

  return { el, control };
}

/**
 * Toggle the error state. A non-empty message shows it (and hides the hint);
 * null restores the neutral state. Wires aria-invalid + aria-describedby so
 * screen readers announce the message.
 */
export function setFieldError(field: FieldHandle, message: string | null): void {
  const error = field.el.querySelector<HTMLElement>('.ui-field__error');
  if (!error) return;
  if (message) {
    error.textContent = message;
    field.el.classList.add('ui-field--error');
    field.control.setAttribute('aria-invalid', 'true');
    field.control.setAttribute('aria-describedby', error.id);
  } else {
    error.textContent = '';
    field.el.classList.remove('ui-field--error');
    field.control.removeAttribute('aria-invalid');
    const hint = field.el.querySelector<HTMLElement>('.ui-field__hint');
    if (hint) {
      field.control.setAttribute('aria-describedby', hint.id);
    } else {
      field.control.removeAttribute('aria-describedby');
    }
  }
}
