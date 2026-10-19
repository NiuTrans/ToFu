/** Property form projection and deterministic patch construction; no API/state owner. */
import { createButton, createField } from '../../ui';
import type { SlideElement, SlidePatch, Translate } from './contracts';

export function renderProperties(side: HTMLElement, element: SlideElement | undefined, t: Translate, busy: boolean): void {
  const doc = side.ownerDocument;
  side.replaceChildren();
  if (!element) {
    const hint = doc.createElement('div'); hint.className = 'sled-side-empty';
    hint.textContent = t('sled.selectHint');
    const ask = createButton({ label: t('sled.askAi'), size: 'sm' }, doc);
    ask.dataset.sledAsk = '1'; side.append(hint, ask); return;
  }
  const id = doc.createElement('div'); id.className = 'sled-side-id'; id.textContent = element.elementId;
  const type = doc.createElement('span'); type.className = 'sled-side-type'; type.textContent = element.elementType;
  id.append(type); side.append(id);
  if (element.elementType === 'text') {
    const text = doc.createElement('textarea'); text.rows = 5; text.value = element.text ?? '';
    const size = doc.createElement('input'); size.type = 'number'; size.min = '1'; size.max = '400'; size.step = '1';
    size.value = String(element.style?.fontSize ?? '');
    const color = doc.createElement('input'); color.type = 'text'; color.maxLength = 32;
    color.placeholder = '#1B2430 ' + t('sled.colorToken'); color.value = element.style?.color ?? '';
    const field = (control: HTMLElement, controlId: string, label: string) => {
      const handle = createField({ control, controlId, label }); handle.el.classList.add('sled-label'); return handle.el;
    };
    side.append(field(text, 'sledText', t('sled.text')));
    const row = doc.createElement('div'); row.className = 'sled-row';
    row.append(field(size, 'sledFontSize', t('sled.fontSize')), field(color, 'sledColor', t('sled.color')));
    side.append(row);
  } else {
    const note = doc.createElement('div'); note.className = 'sled-side-note';
    note.textContent = t('sled.nonText');
    side.append(note);
  }
  const actions = doc.createElement('div'); actions.className = 'sled-actions';
  if (element.elementType === 'text') {
    const apply = createButton({ label: t('sled.apply'), variant: 'primary', size: 'sm', disabled: busy }, doc);
    apply.classList.add('sled-btn', 'sled-btn--primary'); apply.dataset.sledApply = '1'; actions.append(apply);
  }
  const ask = createButton({ label: t('sled.askAi'), size: 'sm' }, doc);
  ask.classList.add('sled-btn'); ask.dataset.sledAsk = '1'; actions.append(ask);
  const errors = doc.createElement('div'); errors.id = 'sledErrors'; errors.className = 'sled-errors'; errors.setAttribute('role', 'status');
  side.append(actions, errors);
}

export function collectPatch(side: HTMLElement, element: SlideElement): SlidePatch[] {
  const ops: SlidePatch[] = [];
  const text = side.querySelector<HTMLTextAreaElement>('#sledText')?.value.trim() ?? '';
  if (text && text !== (element.text ?? '').trim()) ops.push({ op: 'set_text', target: element.elementId, text });
  const sizeValue = side.querySelector<HTMLInputElement>('#sledFontSize')?.value ?? '';
  const size = Number(sizeValue);
  const color = side.querySelector<HTMLInputElement>('#sledColor')?.value.trim() ?? '';
  const props: { fontSize?: number; color?: string } = {};
  if (sizeValue && Number.isFinite(size) && size > 0 && size !== element.style?.fontSize) props.fontSize = size;
  if (color && color !== (element.style?.color ?? '')) props.color = color;
  if (Object.keys(props).length) ops.push({ op: 'set_style', target: element.elementId, props });
  return ops;
}
