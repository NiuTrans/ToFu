/** One body-anchored provider menu. Reads current preference on every open; scope owns dismissal, keyboard focus and rollback. */
import { createLifecycleScope } from '../../lifecycle';
import { createMenuItem } from '../../ui';
import { PROVIDER_CHECK } from './glyphs';
import { preferredProvider, type ModelPickerPorts, type PickerModel } from './types';
export function createProviderMenu(ports: ModelPickerPorts, select: (model: PickerModel) => void) {
  let current: { scope: ReturnType<typeof createLifecycleScope>; chip: HTMLElement; menu: HTMLElement } | null = null;
  function close() {
    const owned = current; current = null;
    if (!owned) return;
    owned.scope.destroy(); owned.menu.remove(); owned.chip.setAttribute('aria-expanded', 'false');
  }
  function toggle(chip: HTMLElement, model: PickerModel) {
    const wasOpen = current?.chip === chip; close(); if (wasOpen) return;
    const document = ports.document, scope = createLifecycleScope(ports.window);
    const menu = document.createElement('div'); menu.id = 'psProviderMenu'; menu.className = 'ps-dd-provider-menu';
    menu.setAttribute('role', 'listbox'); menu.setAttribute('aria-label', chip.getAttribute('aria-label') || '服务商偏好');
    current = { scope, chip, menu };
    try {
      const selected = preferredProvider(model, ports.selection());
      // Preference targets a Provider, not one of its interchangeable Offerings.
      const first = new Set<string>();
      const options = [{ provider_id: '', provider_name: '自动服务商', offering_id: '' }, ...(model.provider_options || []).filter(option => {
        if (first.has(option.provider_id)) return false; first.add(option.provider_id); return true;
      })];
      const buttons = options.map(option => {
        const entry = createMenuItem({ id: option.provider_id, label: '' }, document);
        entry.classList.add('ps-dd-provider-menu-item'); entry.setAttribute('role', 'option');
        const active = option.provider_id === selected; entry.classList.toggle('active', active);
        entry.setAttribute('aria-selected', String(active));
        const check = document.createElement('span'); check.className = 'ps-dd-provider-check'; check.setAttribute('aria-hidden', 'true');
        if (active) check.innerHTML = PROVIDER_CHECK;
        const label = document.createElement('span'); label.className = 'ps-dd-provider-menu-label'; label.textContent = option.provider_name; label.title = option.provider_name;
        entry.append(check, label); menu.append(entry);
        scope.listen(entry, 'click', event => { event.stopPropagation(); close(); select(option.provider_id ? { ...model, ...option } : model); });
        return entry;
      });
      document.body.append(menu); chip.setAttribute('aria-expanded', 'true');
      const tokens = ports.window.getComputedStyle(document.documentElement);
      const gap = parseFloat(tokens.getPropertyValue('--space-1')) || 0, edge = parseFloat(tokens.getPropertyValue('--space-2')) || 0;
      menu.style.maxHeight = `${Math.max(0, ports.window.innerHeight - edge * 2)}px`;
      menu.style.maxWidth = `${Math.max(0, ports.window.innerWidth - edge * 2)}px`;
      const rect = chip.getBoundingClientRect(), height = menu.offsetHeight, width = menu.offsetWidth;
      const above = rect.top - height - gap;
      menu.style.top = `${Math.max(edge, Math.min(above >= edge ? above : rect.bottom + gap, ports.window.innerHeight - height - edge))}px`;
      menu.style.left = `${Math.max(edge, Math.min(rect.right - width, ports.window.innerWidth - width - edge))}px`;
      (buttons.find(button => button.classList.contains('active')) || buttons[0])?.focus({ preventScroll: true });
      scope.listen(document, 'click', event => { if (!menu.contains(event.target as Node) && !chip.contains(event.target as Node)) close(); }, { capture: true });
      scope.listen(document, 'scroll', event => { if (!menu.contains(event.target as Node)) close(); }, { capture: true });
      scope.listen(ports.window, 'resize', close);
      scope.listen(document, 'keydown', event => {
        const key = event as KeyboardEvent;
        if (key.key === 'Escape') { key.preventDefault(); key.stopPropagation(); close(); chip.focus(); return; }
        if (!menu.contains(key.target as Node) || !['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(key.key)) return;
        key.preventDefault(); key.stopPropagation();
        const at = buttons.indexOf(document.activeElement as HTMLButtonElement);
        const next = key.key === 'Home' ? 0 : key.key === 'End' ? buttons.length - 1 : (at + (key.key === 'ArrowDown' ? 1 : -1) + buttons.length) % buttons.length;
        buttons[next]?.focus();
      }, { capture: true });
    } catch (error) { close(); throw error; }
  }
  return { close, toggle };
}
