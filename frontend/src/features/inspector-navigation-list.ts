/** Inspector jump-list projection. Reuses the shared anchored disclosure and
 * lifecycle; keyed rows preserve focus while wire evidence is refreshed.
 * Entry: createInspectorNavigationList. Selection and feed scrolling stay with
 * inspector-navigation; this module never reads conversation or task data.
 */
import {createLifecycleScope} from '../lifecycle';
import {createAnchoredDisclosure} from './conversation-bar/disclosure';

export interface InspectorNavigationEntry {
  key: string; turnId: string; taskId: string; ordinal: number | null;
  preview: string; createdAt: number; expired: boolean; readError: boolean;
}
interface Ports {
  anchor: HTMLElement; panel: HTMLElement;
  translate(key: string, params?: Record<string, number>): string;
  relativeTime(time: number): string; absoluteTime(time: number): string;
  navigate(key: string): void; showTurn(id: string): void;
}
const setText = (node: Element, text: string): void => { if (node.textContent !== text) node.textContent = text; };

export function createInspectorNavigationList(ports: Ports) {
  const {anchor, panel} = ports, doc = panel.ownerDocument, parent = panel.parentElement;
  const scope = createLifecycleScope(doc.defaultView!);
  const disclosure = createAnchoredDisclosure({anchor, panel, horizontalBoundary: parent ?? undefined,
    onClose: () => parent?.append(panel)});
  let entries = new Map<string, InspectorNavigationEntry>();
  const rows = new Map<string, HTMLElement>();
  function activate(event: Event): void {
    const target = event.target as Element;
    const row = target.closest<HTMLElement>('[data-inspector-navigation-key]');
    if (!row || !panel.contains(row)) return;
    const entry = entries.get(row.dataset.inspectorNavigationKey || '');
    if (!entry) return;
    if (event.type === 'keydown') {
      const key = (event as KeyboardEvent).key;
      if (target !== row || (key !== 'Enter' && key !== ' ')) return;
      event.preventDefault();
    }
    if (target.closest('.ri-turn-chip') && entry.turnId) {
      event.stopPropagation(); disclosure.close(true); ports.showTurn(entry.turnId);
    } else ports.navigate(entry.key);
  }
  scope.listen(panel, 'click', activate);
  scope.listen(panel, 'keydown', activate);
  function createRow(entry: InspectorNavigationEntry): HTMLElement {
    const row = doc.createElement('div'); row.className = 'ri-turn-jump-row';
    row.setAttribute('role', 'button'); row.tabIndex = 0; row.dataset.inspectorNavigationKey = entry.key;
    const chip = doc.createElement(entry.turnId ? 'button' : 'span');
    if (chip instanceof doc.defaultView!.HTMLButtonElement) chip.type = 'button';
    const preview = doc.createElement('span'); preview.className = 'ri-turn-jump-preview';
    const time = doc.createElement('span'); time.className = 'ri-turn-jump-time';
    row.append(chip, preview, time); return row;
  }
  function update(wanted: readonly InspectorNavigationEntry[], active: string): void {
    entries = new Map(wanted.map(entry => [entry.key, entry]));
    const focused = doc.activeElement as HTMLElement | null, hadFocus = panel.contains(focused);
    for (const [key, row] of rows) if (!entries.has(key)) { row.remove(); rows.delete(key); }
    [...wanted].sort((a, b) => b.createdAt - a.createdAt).forEach((entry, index) => {
      const row = rows.get(entry.key) ?? createRow(entry); rows.set(entry.key, row);
      row.classList.toggle('ri-sel', entry.key === active);
      if (entry.key === active) row.setAttribute('aria-current', 'true'); else row.removeAttribute('aria-current');
      const [chip, preview, time] = Array.from(row.children) as HTMLElement[];
      chip.className = 'ri-turn-chip' + (entry.ordinal ? '' : ' ri-turn-chip-muted');
      setText(chip, entry.ordinal ? ports.translate('ri.turnChip', {n: entry.ordinal}) : ports.translate('ri.earlierShort'));
      chip.title = entry.turnId ? ports.translate('ri.turnChipTip') : '';
      const text = entry.readError ? ports.translate('ri.loadFailed')
        : entry.expired ? ports.translate('ri.expired') : entry.preview;
      setText(preview, text); preview.title = text;
      setText(time, ports.relativeTime(entry.createdAt)); time.title = ports.absoluteTime(entry.createdAt);
      if (panel.children[index] !== row) panel.insertBefore(row, panel.children[index] ?? null);
    });
    if (hadFocus && doc.activeElement !== focused && disclosure.isOpen()) {
      (focused?.isConnected && panel.contains(focused) ? focused : rows.get(active) ?? anchor).focus({preventScroll: true});
    }
  }
  return {
    update, open: disclosure.open, close: disclosure.close, isOpen: disclosure.isOpen,
    destroy(): void { disclosure.destroy(); scope.destroy(); entries.clear(); rows.clear(); panel.replaceChildren(); },
  };
}
