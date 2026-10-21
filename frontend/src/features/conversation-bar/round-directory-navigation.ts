/** Exact rank navigation: compact directory pages, not loaded-body arithmetic.
 * One instance is scoped to the active owner/runtime; no global cache or timers. */
import type { ConversationRoundDirectory, ConversationRoundEntry } from '../../api/conversation-sync.generated';
import type { TurnState } from '../../conversation/domain/turn-store';
import { createAnchoredDisclosure } from './disclosure';

const PAGE_SIZE = 50;
/* A streamed turn advances the replica head that every directory read is
 * fenced against; a page rejected by a head that moved under it is retried a
 * bounded number of times instead of surfacing a failure the reader cannot act
 * on. */
const ROUND_DIRECTORY_HEAD_RETRIES = 3;
export interface ExactNavigationPorts {
  host: HTMLElement;
  translate(key: string, params?: Record<string, string | number>): string;
  scrollToTurn(turnId: string): void;
  directoryKey?(conversationId: string): string;
  directory?(conversationId: string, offset: number, limit: number, anchor?: string): Promise<ConversationRoundDirectory>;
  hydrateRound?(conversationId: string, entry: ConversationRoundEntry): Promise<void>;
}

export function createExactTurnNavigation(ports: ExactNavigationPorts): {
  update(conversationId: string, state: TurnState | null): void;
  updateActive(container: HTMLElement, inner: HTMLElement): void;
  destroy(): void;
} {
  const { host } = ports;
  const doc = host.ownerDocument;
  const t = ports.translate;
  const button = (className: string, direction?: string): HTMLButtonElement => {
    const node = doc.createElement('button'); node.type = 'button'; node.className = className;
    if (direction) node.dataset.direction = direction; return node;
  };
  const previous = button('turn-nav-step', 'previous'); previous.textContent = '‹';
  const position = button('turn-nav-position');
  const label = doc.createElement('span'); label.className = 'turn-nav-label';
  const readout = doc.createElement('span'); readout.className = 'turn-nav-readout';
  position.append(label, doc.createTextNode(' '), readout);
  const next = button('turn-nav-step', 'next'); next.textContent = '›';
  const panel = doc.createElement('div'); panel.className = 'turn-jump-panel'; panel.id = `${host.id || 'turnNav'}-jump-panel`;
  panel.setAttribute('role', 'group');
  const form = doc.createElement('form'); form.className = 'turn-jump-direct';
  const input = doc.createElement('input'); input.type = 'number'; input.min = '1'; input.step = '1'; input.inputMode = 'numeric';
  const go = button('turn-jump-go'); go.type = 'submit';
  const first = button('turn-jump-first'); const last = button('turn-jump-last');
  form.append(first, input, go, last);
  const older = button('turn-jump-page', 'older'); const newer = button('turn-jump-page', 'newer');
  const list = doc.createElement('div'); list.className = 'turn-jump-list';
  const errorNote = doc.createElement('p'); errorNote.className = 'turn-jump-error'; errorNote.setAttribute('role', 'status');
  const retry = button('turn-jump-load');
  panel.append(form, retry, older, list, newer, errorNote);
  const disclosure = createAnchoredDisclosure({ anchor: position, panel });
  host.replaceChildren(previous, position, next);
  let conversationId = ''; let state: TurnState | null = null;
  let version = ''; let generation = 0; let disposed = false; let busy = false;
  let total: number | null = null; let activeId = ''; let activeRound: number | null = null;
  let page = 0; let entries: ConversationRoundEntry[] = [];
  // Publication generation fences results; slot identity owns cleanup even
  // when that generation changes. Refresh demand is coalesced for both reads
  // and jumps, and only the operation holding the slot may drain it.
  let slot: object | null = null; let queued: number | null = null;
  function release(owned: object): void {
    if (slot !== owned) return;
    slot = null; busy = false;
    if (disposed) return;
    const next = queued; queued = null;
    paint();
    if (next !== null) void readPage(next);
  }
  const ranks = new Map<string, number>();
  const current = (id: string, token: number): boolean => !disposed && id === conversationId && token === generation;

  function paint(): void {
    label.textContent = t('conversationBar.turns');
    if (total === 0) activeRound = 0;
    readout.textContent = `${activeRound ?? '—'} / ${total ?? '—'}`;
    readout.style.width = `${2 * String(Math.max(total ?? 1, 1)).length + 3}ch`;
    previous.disabled = busy || activeRound === null || activeRound <= 1;
    next.disabled = busy || activeRound === null || total === null || activeRound >= total;
    previous.title = t('conversationBar.previousTurn'); previous.setAttribute('aria-label', previous.title);
    next.title = t('conversationBar.nextTurn'); next.setAttribute('aria-label', next.title);
    position.disabled = !conversationId || total === 0;
    position.title = t('conversationBar.jumpToTurn'); position.setAttribute('aria-label', position.title);
    host.setAttribute('aria-busy', String(busy));
    first.textContent = t('conversationBar.firstTurn'); last.textContent = t('conversationBar.lastTurn');
    go.textContent = t('conversationBar.goToTurn'); input.setAttribute('aria-label', t('conversationBar.turnNumber'));
    input.max = String(total ?? 1);
    for (const node of [first, last, go, input]) node.disabled = busy || !total;
    retry.hidden = !errorNote.textContent; retry.disabled = busy; retry.textContent = t('conversationBar.retryHistory');
    older.textContent = t('conversationBar.olderTurns'); newer.textContent = t('conversationBar.newerTurns');
    panel.setAttribute('aria-label', t('conversationBar.jumpToTurn'));
    older.disabled = busy || page === 0; newer.disabled = busy || total === null || (page + 1) * PAGE_SIZE >= total;
    if (!disclosure.isOpen()) return;
    const existing = new Map(Array.from(list.children).map(node => [(node as HTMLElement).dataset.turnId, node as HTMLButtonElement]));
    const keep = new Set(entries.map(entry => entry.turnId));
    for (const node of Array.from(list.children)) if (!keep.has((node as HTMLElement).dataset.turnId || '')) node.remove();
    entries.forEach((entry, offset) => {
      const row = existing.get(entry.turnId) ?? button('turn-jump-entry'); row.dataset.turnId = entry.turnId;
      row.dataset.roundNumber = String(entry.roundNumber); row.disabled = busy;
      const projection = state?.turnsById[entry.turnId]?.projection;
      const text = String(projection?.content || projection?.segments?.find(segment => segment.type === 'text')?.text || '').trim().slice(0, 120);
      row.textContent = [t('conversationBar.turnPosition', { current: entry.roundNumber, total: total ?? 0 }), text,
        entry.actor === 'critic' ? t('conversationBar.critic') : ''].filter(Boolean).join(' · ');
      row.title = row.textContent; row.classList.toggle('turn-jump-critic', entry.actor === 'critic');
      if (entry.turnId === activeId) row.setAttribute('aria-current', 'true'); else row.removeAttribute('aria-current');
      if (list.children[offset] !== row) list.insertBefore(row, list.children[offset] ?? null);
    });
  }

  async function directory(id: string, offset: number, limit: number, anchor?: string): Promise<ConversationRoundDirectory> {
    if (!ports.directory) throw new Error('Round directory unavailable');
    // Defer until the coordinator has committed the sync head of this render.
    await Promise.resolve();
    return ports.directory(id, offset, limit, anchor);
  }
  /* Every directory read is fenced against the replica head it started under,
   * and a streamed turn advances that head continuously. A fence that moved
   * under the read is not a failure the reader can act on: read once more
   * under the head observed now. Bounded, because a racing head converges
   * while a broken projection never will. */
  async function readFenced<T>(id: string, read: () => Promise<T>): Promise<T> {
    for (let retries = 0; ;) {
      const head = ports.directoryKey?.(id);
      try { return await read(); }
      catch (error) {
        if (ports.directoryKey?.(id) === head
            || ++retries > ROUND_DIRECTORY_HEAD_RETRIES) throw error;
      }
    }
  }
  async function readPage(wantedPage: number, anchor = activeId): Promise<void> {
    if (!conversationId) return;
    /* One read in flight per view. A head advance arrives with every streamed
     * turn, so a newer request replaces any queued one instead of stacking
     * reads, and the page index survives the refresh. */
    if (busy) { queued = wantedPage; return; }
    const id = conversationId; const token = generation;
    const owned = {}; slot = owned;
    busy = true; errorNote.textContent = ''; paint();
    let refreshAnchor = false;
    try {
      const result = await readFenced(id, () =>
        directory(id, wantedPage * PAGE_SIZE, PAGE_SIZE, anchor || undefined));
      if (!current(id, token)) return;
      total = result.totalRounds;
      refreshAnchor = anchor !== activeId;
      if (!refreshAnchor) activeRound = result.activeRound ?? null;
      if (activeRound === null && total > 0 && !activeId) activeRound = total;
      entries = [...result.entries]; page = wantedPage;
      ranks.clear(); for (const entry of entries) ranks.set(entry.turnId, entry.roundNumber);
      if (refreshAnchor) activeRound = ranks.get(activeId) ?? null;
      else if (activeId && activeRound !== null) ranks.set(activeId, activeRound);
    } catch { if (current(id, token)) errorNote.textContent = t('conversationBar.historyFailed'); }
    finally {
      if (current(id, token) && refreshAnchor && activeRound === null && activeId && queued === null) queued = page;
      release(owned);
    }
  }
  async function navigate(round: number): Promise<void> {
    if (busy || total === null || !Number.isSafeInteger(round) || round < 1 || round > total) return;
    const id = conversationId; const token = generation;
    const owned = {}; slot = owned;
    busy = true; errorNote.textContent = ''; paint();
    try {
      // Revalidate the rank under the current head even if its body was loaded.
      const result = await readFenced(id, () => directory(id, round - 1, 1));
      if (!current(id, token)) return;
      const entry = result.entries[0];
      if (!entry || entry.roundNumber !== round) throw new Error('Round was deleted');
      await ports.hydrateRound?.(id, entry);
      if (!current(id, token)) return;
      total = result.totalRounds; activeId = entry.turnId; activeRound = round;
      ranks.set(activeId, round); disclosure.close(true); ports.scrollToTurn(activeId);
    } catch { if (current(id, token)) errorNote.textContent = t('conversationBar.historyFailed'); }
    finally { release(owned); }
  }
  const onPrevious = (): void => { if (activeRound !== null) void navigate(activeRound - 1); };
  const onNext = (): void => { if (activeRound !== null) void navigate(activeRound + 1); };
  const onPosition = (): void => {
    if (disclosure.isOpen()) { disclosure.close(true); return; }
    input.value = String(activeRound ?? 1); disclosure.open(); paint();
    void readPage(Math.floor(Math.max(0, (activeRound ?? 1) - 1) / PAGE_SIZE));
  };
  const onSubmit = (event: Event): void => { event.preventDefault(); void navigate(Number(input.value)); };
  const onPanel = (event: Event): void => {
    const target = (event.target as Element).closest<HTMLButtonElement>('button');
    if (!target || !panel.contains(target)) return;
    if (target === first) void navigate(1);
    else if (target === last && total !== null) void navigate(total);
    else if (target === older || target === newer) void readPage(page + (target === older ? -1 : 1));
    else if (target === retry) void readPage(page);
    else if (target.dataset.roundNumber) void navigate(Number(target.dataset.roundNumber));
  };
  previous.addEventListener('click', onPrevious); next.addEventListener('click', onNext);
  position.addEventListener('click', onPosition); panel.addEventListener('click', onPanel); form.addEventListener('submit', onSubmit);
  return {
    update(id, nextState) {
      if (disposed) return;
      const switched = id !== conversationId;
      const nextVersion = `${id}:${ports.directoryKey?.(id) ?? nextState?.conversationRevision ?? ''}`;
      state = nextState;
      if (switched || nextVersion !== version) {
        /* A replica head advance is not a new directory. Streaming commits one
         * per tool round; blanking the readout and page here made an open
         * directory empty itself on every call. Keep the last good page and
         * refresh the *shown* page in place. Only a conversation switch drops
         * the projection, since those ranks belong to another thread.
         * `busy` stays as it is: a read may still be in flight and hands its
         * slot to the queued refresh when it settles. */
        generation++; errorNote.textContent = ''; version = nextVersion;
        if (switched) {
          entries = []; ranks.clear(); total = null; activeRound = null;
          disclosure.close(false); activeId = ''; page = 0;
        }
        conversationId = id;
        if (!activeId || !state?.turnsById[activeId]) {
          activeId = [...(state?.laneOrder.main ?? [])].reverse().find(turnId =>
            ['human', 'critic', 'virtual_user'].includes(state?.turnsById[turnId]?.actor ?? '')) ?? '';
        }
        if (id) void readPage(switched ? 0 : page);
      }
      paint();
    },
    updateActive(container, inner) {
      if (disposed) return;
      const rect = container.getBoundingClientRect(); const boundary = rect.top + rect.height * 0.3;
      let candidate = '';
      for (const node of inner.querySelectorAll<HTMLElement>('[data-turn-id]')) {
        const id = node.dataset.turnId || '';
        if (!['human', 'critic', 'virtual_user'].includes(state?.turnsById[id]?.actor ?? '')) continue;
        if (node.getBoundingClientRect().top <= boundary) candidate = id; else break;
      }
      if (candidate && candidate !== activeId) {
        activeId = candidate; activeRound = ranks.get(candidate) ?? null; paint();
        if (activeRound === null) void readPage(page, candidate);
      }
    },
    destroy() {
      disposed = true; generation++; disclosure.destroy(); ranks.clear(); entries = []; state = null;
      previous.removeEventListener('click', onPrevious); next.removeEventListener('click', onNext);
      position.removeEventListener('click', onPosition); panel.removeEventListener('click', onPanel); form.removeEventListener('submit', onSubmit);
      host.replaceChildren();
    },
  };
}
