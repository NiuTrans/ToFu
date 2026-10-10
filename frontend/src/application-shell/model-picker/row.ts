/** Model row presentation. Native menu/button factories own controls; routing selection and current preference are explicit ports. */
import { createButton } from '../../ui';
import { isCurrentModel, preferredProvider, type ModelPickerPorts, type PickerModel, type PickerSelection } from './types';
import { PROVIDER_ARROW } from './glyphs';
import type { ModelPickerFilter, SearchRow } from './filter';
export interface ModelRowView { model: PickerModel; element: HTMLElement; chipLabel?: HTMLElement; refresh(selection?: PickerSelection): void }
export function renderModelRow(model: PickerModel, section: string, options: { sub?: boolean; rawId?: boolean; extraSearch?: string },
  ports: ModelPickerPorts, filter: ModelPickerFilter, views: ModelRowView[], select: (model: PickerModel) => void,
  provider: (chip: HTMLElement, model: PickerModel) => void): SearchRow {
  const document = ports.document, row = document.createElement('div');
  row.className = 'preset-dropdown-item' + (options.sub ? ' ps-dd-sub-item' : '');
  row.dataset.value = model.model_id; row.dataset.section = section;
  row.dataset.routeKey = [model.provider_id || 'auto', model.creator_id || '', model.offering_id || '', model.model_id].join('::');
  const label = options.rawId ? model.model_id : ports.shortName(model.model_id);
  const choose = createButton({ label: '', variant: 'ghost', size: 'sm' }, document);
  choose.classList.add('ps-dd-model-choice'); choose.removeAttribute('role'); choose.tabIndex = 0;
  const icon = document.createElement('span'); icon.className = 'ps-dd-icon'; icon.innerHTML = ports.icon(ports.brand(model), 14) || '✦';
  const name = document.createElement('span'); name.className = 'ps-dd-label'; name.textContent = label; name.title = model.model_id;
  choose.append(icon, name); row.append(choose); row.onclick = () => select(model);
  const haystack = [label, model.model_id, model.creator_name || '',
    ...(model.provider_options || []).map(option => option.provider_name), options.extraSearch || ''].join(' ').toLowerCase();
  row.dataset.search = haystack;
  const view: ModelRowView = { model, element: row, refresh(selection = ports.selection()) {
    const active = isCurrentModel(model, selection);
    row.classList.toggle('active', active); choose.setAttribute('aria-pressed', String(active));
    if (view.chipLabel) {
      const name = model.provider_options?.find(option => option.provider_id === preferredProvider(model, selection))?.provider_name || '自动服务商';
      view.chipLabel.textContent = name; view.chipLabel.title = name;
    }
  } };
  if (model.routing_v2 && !model.pending_identity && Array.isArray(model.provider_options)) {
    const chip = createButton({ label: '', variant: 'ghost', size: 'sm' }, document);
    chip.classList.add('ps-dd-provider-chip'); chip.setAttribute('aria-haspopup', 'listbox'); chip.setAttribute('aria-expanded', 'false');
    chip.setAttribute('aria-label', label + ' 服务商偏好'); chip.title = '服务商偏好；自动模式会在同一模型的可用服务商间轮转';
    const chipLabel = document.createElement('span'); chipLabel.className = 'ps-dd-provider-chip-label';
    const arrow = document.createElement('span'); arrow.className = 'ps-dd-provider-chip-arrow'; arrow.innerHTML = PROVIDER_ARROW; arrow.setAttribute('aria-hidden', 'true');
    chip.append(chipLabel, arrow); chip.onclick = event => { event.stopPropagation(); provider(chip, model); };
    chip.onkeydown = event => { if (event.key !== 'Escape') event.stopPropagation(); };
    view.chipLabel = chipLabel; row.append(chip);
  } else if (model.pending_identity) {
    const tag = document.createElement('span'); tag.className = 'ps-dd-provider-tag'; tag.textContent = model.provider_name || model.provider_id || ''; row.append(tag);
  }
  view.refresh(); views.push(view);
  const searchRow = { element: row, haystack, section }; filter.addRow(searchRow); return searchRow;
}
