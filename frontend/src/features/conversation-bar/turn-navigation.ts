/** Compact main-lane navigation. TurnStore remains authoritative; only selection
 * and disclosure page are local presentation state. No observers or idle work. */
import type { TurnState } from '../../conversation/domain/turn-store';
import { createAnchoredDisclosure } from './disclosure';
import { createExactTurnNavigation, type ExactNavigationPorts } from './round-directory-navigation';

const PAGE_SIZE = 50;
const isInput = (actor: string): boolean => ['human', 'critic', 'virtual_user'].includes(actor);
type Entry = { id: string; preview: string; critic: boolean; writes: boolean; files: Set<string> };

function entriesFrom(state: TurnState | null): Entry[] {
  const entries: Entry[] = [];
  let input: Entry | undefined;
  for (const id of state?.laneOrder.main ?? []) {
    const turn = state?.turnsById[id];
    if (!turn) continue;
    const projection = turn.projection;
    if (isInput(turn.actor)) {
      const segment = projection.segments?.find(item => item.type === 'text' && item.text);
      input = { id, preview: String(projection.content || segment?.text || '').trim().split('conversationBar.\n')[0].slice(0, 120),
        critic: turn.actor === 'critic', writes: false, files: new Set() };
      entries.push(input);
    } else if (input) {
      for (const list of [projection.modifiedFileList, projection.fileChanges?.files]) {
        for (const file of Array.isArray(list) ? list : []) {
          const path = typeof file === 'string' ? file : file?.path;
          // Tooltips are bounded too, even for very large write batches.
          if (path && input.files.size < 20) input.files.add(String(path).split('conversationBar./').pop() || String(path));
          if (path) input.writes = true;
        }
      }
      if (Number(projection.modifiedFiles || 0) > 0) input.writes = true;
    }
  }
  return entries;
}

export function createTurnNavigation(ports: ExactNavigationPorts & {
  host: HTMLElement;
  translate(key: string, params?: Record<string, string | number>): string;
  scrollToTurn(turnId: string): void;

  loadEarlier?(conversationId: string, beforeOrdinal: number): Promise<void>;
}): {
  update(conversationId: string, state: TurnState | null): void;
  updateActive(container: HTMLElement, inner: HTMLElement): void;
  destroy(): void;
} {
  if (ports.directory) return createExactTurnNavigation(ports);
  const { host } = ports;
  const doc = host.ownerDocument;
  const t = ports.translate;
  const button = (className: string): HTMLButtonElement => {
    const node = doc.createElement('button'); node.type = 'button'; node.className = className; return node;
  };
  const previous = button('turn-nav-step'); previous.dataset.direction = 'previous'; previous.textContent = '‹';
  const position = button('turn-nav-position');
  const label = doc.createElement('span'); label.className = 'turn-nav-label';
  const readout = doc.createElement('span'); readout.className = 'turn-nav-readout';
  position.append(label, doc.createTextNode(' '), readout);
  const next = button('turn-nav-step'); next.dataset.direction = 'next'; next.textContent = '›';
  const panel = doc.createElement('div'); panel.className = 'turn-jump-panel'; panel.id = `${host.id || 'turnNav'}-jump-panel`;
  panel.setAttribute('role', 'group');
  const older = button('turn-jump-page'); older.dataset.direction = 'older';
  const list = doc.createElement('div'); list.className = 'turn-jump-list';
  const newer = button('turn-jump-page'); newer.dataset.direction = 'newer';
  const loadEarlier = button('turn-jump-load');
  const errorNote = doc.createElement('p'); errorNote.className = 'turn-jump-error'; errorNote.setAttribute('role', 'status');
  panel.append(loadEarlier, older, list, newer, errorNote);
  const disclosure = createAnchoredDisclosure({ anchor: position, panel });
  host.replaceChildren(previous, position, next);
  let entries: Entry[] = [];
  let indexById = new Map<string, number>();
  let conversationId = '';
  let activeId = '';
  let page = 0;
  let disposed = false;

  let beforeOrdinal: number | null = null;
  let hasEarlier = false;
  let loading = false;

  let generation = 0;
  const index = (): number => indexById.get(activeId) ?? -1;
  const description = (entry: Entry, i: number): string => [
    t('conversationBar.turnPosition', { current: i + 1, total: entries.length }), entry.preview,
    entry.critic ? t('conversationBar.critic') : '', entry.writes ? t('conversationBar.writes') : '', ...entry.files,
  ].filter(Boolean).join(' · ');
  function paintBar(): void {
    const current = index();
    const focused = doc.activeElement;
    label.textContent = t('conversationBar.turns');
    readout.textContent = `${current + 1} / ${entries.length}${hasEarlier ? '+' : ''}`;
    /* Slot sized to the total's digits: compact for small sessions, and the
       nav width reflows only once per order of magnitude, never per turn. */
    readout.style.width = `${2 * String(Math.max(entries.length, 1)).length + 3 + (hasEarlier ? 1 : 0)}ch`;
    previous.disabled = current <= 0; next.disabled = current < 0 || current >= entries.length - 1;
    if ((focused === previous && previous.disabled) || (focused === next && next.disabled)) position.focus({ preventScroll: true });
    previous.title = t('conversationBar.previousTurn'); previous.setAttribute('aria-label', t('conversationBar.previousTurn'));
    next.title = t('conversationBar.nextTurn'); next.setAttribute('aria-label', t('conversationBar.nextTurn'));
    position.disabled = !entries.length && !hasEarlier;
    position.title = t('conversationBar.jumpToTurn');
    position.setAttribute('aria-label', entries.length
      ? `${t('conversationBar.jumpToTurn')}: ${t('conversationBar.turnPosition', { current: current + 1, total: entries.length })}` : t('conversationBar.noTurns'));
  }
  function paintPage(): void {
    older.textContent = t('conversationBar.olderTurns'); newer.textContent = t('conversationBar.newerTurns');
    panel.setAttribute('aria-label', t('conversationBar.jumpToTurn'));

    loadEarlier.hidden = !(hasEarlier && page === 0 && ports.loadEarlier);
    loadEarlier.disabled = loading || loadEarlier.hidden;
    loadEarlier.textContent = t(loading ? 'conversationBar.loadingHistory' : 'conversationBar.loadEarlierHistory');
    older.disabled = page === 0; newer.disabled = (page + 1) * PAGE_SIZE >= entries.length;
    const wanted = entries.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE);
    const existing = new Map(Array.from(list.children).map(node => [(node as HTMLElement).dataset.turnId, node as HTMLButtonElement]));
    const keep = new Set(wanted.map(entry => entry.id));
    for (const node of Array.from(list.children)) if (!keep.has((node as HTMLElement).dataset.turnId || '')) node.remove();
    wanted.forEach((entry, offset) => {
      const row = existing.get(entry.id) ?? button('turn-jump-entry');
      row.dataset.turnId = entry.id;
      row.textContent = description(entry, page * PAGE_SIZE + offset);
      row.title = row.textContent;
      row.classList.toggle('turn-jump-critic', entry.critic);
      row.classList.toggle('turn-jump-writes', entry.writes);
      if (entry.id === activeId) row.setAttribute('aria-current', 'true'); else row.removeAttribute('aria-current');
      // Do not detach a focused keyed row on streaming repaint.
      if (list.children[offset] !== row) list.insertBefore(row, list.children[offset] ?? null);
    });
  }
  function navigate(i: number): void {
    const entry = entries[i]; if (!entry) return;
    activeId = entry.id; paintBar();
    disclosure.close(true);
    ports.scrollToTurn(entry.id);
  }
  const onPrevious = (): void => navigate(index() - 1);
  const onNext = (): void => navigate(index() + 1);
  const onPosition = (): void => {
    if (disclosure.isOpen()) { disclosure.close(true); return; }
    page = Math.floor(Math.max(0, index()) / PAGE_SIZE); paintPage(); disclosure.open();
  };
  const requestEarlier = async (): Promise<void> => {
    if (loading || !ports.loadEarlier || beforeOrdinal == null) return;
    const id = conversationId;
    const token = ++generation;
    const current = () => !disposed && id === conversationId && token === generation;
    loading = true; errorNote.textContent = ''; paintPage();
    try { await ports.loadEarlier(id, beforeOrdinal); }
    catch { if (current()) errorNote.textContent = t('conversationBar.historyFailed'); }
    finally { if (current()) {
      loading = false; paintPage();
      if (doc.activeElement === loadEarlier && loadEarlier.disabled) {
        (list.querySelector<HTMLButtonElement>('button') ?? position).focus({ preventScroll: true });
      }
    } }
  };
  const onPanelClick = (event: Event): void => {
    const target = (event.target as Element).closest<HTMLButtonElement>('button');
    if (!target || !panel.contains(target)) return;
    if (target === loadEarlier) { void requestEarlier(); return; }
    if (target === older || target === newer) {
      page += target === older ? -1 : 1; paintPage();
      if (target.disabled) (target === older ? newer : older).focus({ preventScroll: true });
    } else if (target.dataset.turnId) navigate(indexById.get(target.dataset.turnId) ?? -1);
  };
  previous.addEventListener('click', onPrevious); next.addEventListener('click', onNext);
  position.addEventListener('click', onPosition); panel.addEventListener('click', onPanelClick);
  return {
    update(id, state) {
      if (disposed) return;
      const switched = id !== conversationId;
      conversationId = id;
      const history = state?.historyByLane?.main;
      hasEarlier = Boolean(history?.hasMore);
      beforeOrdinal = history?.nextBeforeOrdinal ?? null;
      entries = entriesFrom(state); indexById = new Map(entries.map((entry, i) => [entry.id, i]));
      if (switched) { generation++; disclosure.close(false); activeId = ''; loading = false; errorNote.textContent = ''; }
      if (!indexById.has(activeId)) activeId = entries[entries.length - 1]?.id ?? '';
      page = Math.min(page, Math.max(0, Math.ceil(entries.length / PAGE_SIZE) - 1));
      paintBar(); if (disclosure.isOpen()) paintPage();
    },
    updateActive(container, inner) {
      if (disposed || !entries.length) return;
      const rect = container.getBoundingClientRect();
      const boundary = rect.top + rect.height * 0.3;
      let candidate = '';
      // Scan only mounted nodes once. Missing/windowed nodes never reset selection.
      for (const node of inner.querySelectorAll<HTMLElement>('[data-turn-id]')) {
        const id = node.dataset.turnId || '';
        if (!indexById.has(id)) continue;
        if (node.getBoundingClientRect().top <= boundary) candidate = id;
        else break;
      }
      if (candidate && candidate !== activeId) { activeId = candidate; paintBar(); if (disclosure.isOpen()) paintPage(); }
    },
    destroy() {
      disposed = true; generation++; disclosure.destroy();
      previous.removeEventListener('click', onPrevious); next.removeEventListener('click', onNext);
      position.removeEventListener('click', onPosition); panel.removeEventListener('click', onPanelClick);
      entries = []; indexById.clear(); host.replaceChildren();
    },
  };
}
