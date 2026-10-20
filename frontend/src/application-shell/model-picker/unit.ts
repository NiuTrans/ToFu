/** Folded model-unit DOM. Alias mirrors and version families retain every selectable member, active-member disclosure and native keyboard controls. */
import { createButton, createMenuItem } from '../../ui';
import type { ModelDisplayUnit } from '../../core/model-display-fold';
import { FOLD_ARROW } from './glyphs';
import { isCurrentModel, type ModelPickerPorts, type PickerModel } from './types';
import type { ModelPickerFilter, SearchRow } from './filter';
export function renderModelUnit(unit: ModelDisplayUnit<PickerModel>, section: string, root: HTMLElement, ports: ModelPickerPorts,
  filter: ModelPickerFilter, row: (model: PickerModel, section: string, options?: { sub?: boolean; rawId?: boolean; extraSearch?: string }) => SearchRow): void {
  const face = unit.face, document = ports.document;
  const extraSearch = unit.members.map(model => [model.model_id, model.display_name || '', ports.shortName(model.model_id)]
    .filter(Boolean).join(' ')).join(' ');
  const faceRow = row(face, section, { extraSearch });
  if (unit.kind === 'single' || unit.members.length < 2) { root.append(faceRow.element); return; }
  const sub = document.createElement('div'); sub.className = 'ps-dd-sub'; sub.dataset.section = section;
  const members: SearchRow[] = [];
  const append = (model: PickerModel, rawId = false) => { const item = row(model, section, { sub: true, rawId }); members.push(item); sub.append(item.element); };
  let control: HTMLButtonElement;
  if (unit.kind === 'alias') {
    control = createButton({ label: '×' + unit.members.length, variant: 'ghost', size: 'sm' }, document);
    control.classList.add('ps-dd-fold-badge'); control.title = ports.text('msg.mirrorFoldTip', { count: unit.members.length });
    control.setAttribute('aria-label', control.title); faceRow.element.append(control);
    for (const model of unit.members) if (model.model_id !== face.model_id) append(model, true);
  } else if (unit.kind === 'family') {
    for (const child of unit.children) {
      if (child.face.model_id === face.model_id && child.kind === 'single') continue;
      if (child.face.model_id === face.model_id) { for (const model of child.members) if (model.model_id !== face.model_id) append(model, true); }
      else append(child.face);
    }
    control = createMenuItem({ id: section, label: ports.text('msg.moreVersions', { count: unit.members.length - 1 }) }, document);
    control.classList.add('ps-dd-expander'); control.removeAttribute('role'); control.tabIndex = 0; control.dataset.section = section;
    const arrow = document.createElement('span'); arrow.className = 'ps-dd-expander-arrow'; arrow.setAttribute('aria-hidden', 'true'); arrow.innerHTML = FOLD_ARROW; control.append(arrow);
  } else return;
  const selection = ports.selection(), active = unit.members.some(model => model.model_id !== face.model_id && isCurrentModel(model, selection));
  sub.classList.toggle('open', active); control.classList.toggle('open', active); control.setAttribute('aria-expanded', String(active));
  control.onclick = event => { event.stopPropagation(); const open = sub.classList.toggle('open'); control.classList.toggle('open', open); control.setAttribute('aria-expanded', String(open)); };
  root.append(faceRow.element); if (unit.kind === 'family') root.append(control); root.append(sub);
  filter.addFold({ element: sub, control, rows: members });
}
