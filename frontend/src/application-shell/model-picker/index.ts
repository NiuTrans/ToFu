/** Composer model-picker composition. One DOM scope, render-local indices and provider popup; actions delegate to existing model/persistence authority. */
import { createLifecycleScope } from '../../lifecycle';
import { createMenuItem } from '../../ui';
import { createModelPickerFilter } from './filter';
import { planModelPicker } from './plan';
import { createProviderMenu } from './provider-menu';
import { renderModelRow, type ModelRowView } from './row';
import { renderModelUnit } from './unit';
import type { ModelPickerPorts, PickerModel } from './types';
export { projectModelPickerCatalog } from './catalog';
export type { ModelPickerPorts, PickerModel, PickerSelection } from './types';
export function createModelPicker(ports: ModelPickerPorts) {
  const document = ports.document, scope = createLifecycleScope(ports.window);
  const wrapper = document.getElementById('presetWrapper');
  const root = ports.list;
  const search = document.getElementById('presetDropdownSearchInput') as HTMLInputElement | null;
  let filter = createModelPickerFilter(), views: ModelRowView[] = [], generation = 0, selectionKey: string | null = null;

  let catalog: PickerModel[] | null = null, recentKey: string | null = null;
  function refreshOnOpen() {
    if (catalog && JSON.stringify(ports.recentIds()) !== recentKey) populate(catalog);
    refreshSelection();
  }
  function refreshSelection() {
    if (scope.signal.aborted) return;
    const selection = ports.selection(), key = JSON.stringify(selection);
    if (key === selectionKey) return;
    selectionKey = key; for (const view of views) view.refresh(selection);
  }
  function select(model: PickerModel) { if (!scope.signal.aborted) { ports.select(model); refreshSelection(); } }
  const providers = createProviderMenu(ports, select); scope.add(providers.close);
  function close() { wrapper?.classList.remove('open'); providers.close(); }
  scope.add(() => { generation++; close(); views = []; catalog = null; filter = createModelPickerFilter(); });
  try {
    scope.listen(document, 'click', event => { if (wrapper?.classList.contains('open') && !wrapper.contains(event.target as Node)) close(); });
    scope.listen(document, 'keydown', event => {
      const key = event as KeyboardEvent;
      if (key.key === 'Escape' && !key.defaultPrevented && wrapper?.classList.contains('open')) { key.preventDefault(); close(); }
    });
    if (search) {
      scope.listen(search, 'input', () => filter.apply(search.value));
      scope.listen(search, 'click', event => event.stopPropagation());
      scope.listen(search, 'keydown', event => {
        const key = event as KeyboardEvent; key.stopPropagation();
        if (key.key === 'Enter') { key.preventDefault(); filter.firstVisible()?.click(); }
        else if (key.key === 'Escape') { key.preventDefault(); if (search.value) { search.value = ''; filter.apply(''); } else close(); }
      });
    }
  } catch (error) { scope.destroy(); throw error; }
  function populate(models: PickerModel[]) {
    if (scope.signal.aborted || !root || !Array.isArray(models)) return;

    catalog = models.slice(); recentKey = JSON.stringify(ports.recentIds());
    providers.close(); generation++; selectionKey = null; views = []; filter = createModelPickerFilter(); root.replaceChildren();
    const version = generation, plan = planModelPicker(models, ports);
    const row = (model: PickerModel, section: string, options = {}) => renderModelRow(model, section, options, ports, filter, views,
      value => { if (version === generation) select(value); }, (chip, value) => { if (!scope.signal.aborted && version === generation) providers.toggle(chip, value); });
    const label = (name: string, section: string) => {
      const element = document.createElement('div'); element.className = 'ps-dd-section-label'; element.dataset.section = section;
      element.textContent = name; root.append(element); filter.addLabel(section, element);
    };
    if (!models.length) {
      const loading = document.createElement('div'); loading.className = 'ps-dd-loading'; loading.dataset.i18n = 'toolbar.loadingModels';
      loading.textContent = ports.text('toolbar.loadingModels'); root.append(loading);
    }
    if (plan.recents.length) { label(ports.text('toolbar.recentModels'), JSON.stringify(['recent'])); for (const model of plan.recents) root.append(row(model, JSON.stringify(['recent'])).element); }
    for (const group of plan.groups) {
      if (plan.groups.length > 1) label(group.name, group.key);
      for (const unit of group.units) renderModelUnit(unit, group.key, root, ports, filter, row);
    }
    const searchWrap = document.getElementById('presetDropdownSearch');
    if (searchWrap) searchWrap.style.display = plan.visible.length > 10 ? '' : 'none';
    if (search) search.value = '';
    filter.apply(''); refreshSelection();
    if (plan.visible.length > 10) {
      const hint = createMenuItem({ id: 'settings', label: ports.text('msg.tooManyModels') }, document);
      hint.classList.add('ps-dd-hint'); hint.removeAttribute('role'); hint.tabIndex = 0;
      hint.onclick = event => { event.stopPropagation(); if (!scope.signal.aborted && version === generation) { close(); ports.openSettings(); } }; root.append(hint);
    }
  }
  return { populate, refreshSelection, filter: (query: string) => scope.signal.aborted ? 0 : filter.apply(query),
    closeProvider: providers.close, close,
    toggle(event: Event) { event.stopPropagation(); if (scope.signal.aborted || !wrapper) return; if (wrapper.classList.contains('open')) close(); else { refreshOnOpen(); wrapper.classList.add('open'); } },
    destroy() { scope.destroy(); } };
}
