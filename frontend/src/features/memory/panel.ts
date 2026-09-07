import { featureRegistry } from '../../feature-registry';
import { escapeHtmlText as escape } from '../../html-safety';
import type { I18nKey } from '../../i18n';
import { attachMemorySkillDropZone } from './skill-package-install';
interface MemoryItem {
  id: string;
  name: string;
  description?: string;
  body?: string;
  scope?: string;
  tags?: string[];
  enabled?: boolean;
  [key: string]: unknown;
}

interface ResponseLike {
  ok: boolean;
  status?: number;
  json(): Promise<Record<string, unknown>>;
}

interface MemoryApi {
  list(scope: string): Promise<{ memories?: MemoryItem[] } | null>;
  get(id: string): Promise<MemoryItem | null>;
  toggle(id: string): Promise<ResponseLike | null>;
  remove(id: string): Promise<ResponseLike | null>;
  create(item: Record<string, unknown>): Promise<ResponseLike | null>;
}

type MemoryWindow = Window & {
  Api?: { memory?: MemoryApi };
  t?: (key: string, values?: Record<string, unknown>) => string;
  marked?: { parse(markdown: string): string };
  debugLog?: (message: string, kind?: string) => void;
  showConfirm?: (message: string, options?: { danger?: boolean }) => Promise<boolean>;
  _applyMemoryUI?: (enabled: boolean) => void;
  captureActiveConversationSettings?: () => void;
  updateSubmenuCounts?: () => void;
  openSettings?: () => void;
  switchSettingsTab?: (tab: string) => void;
  toggleMemory?: () => void;
  toggleMemoryFromModal?: () => void;
  openMemoryModal?: () => void;
  closeMemoryModal?: () => void;
  toggleMemoryAddForm?: () => void;
  switchMemoryTab?: (scope: string) => void;
  filterMemoryList?: (query: string) => void;
  refreshMemoryList?: (scope?: string, targetId?: string) => Promise<void>;
  toggleMemoryBody?: (header: Element) => Promise<void>;
  toggleMemoryEnabled?: (id: string) => Promise<void>;
  deleteMemory?: (id: string) => Promise<void>;
  createMemoryFromModal?: () => Promise<void>;
  _renderMemoryCards?: (items: MemoryItem[]) => void;
  _updateMemoryModalBtn?: () => void;
  _updateMemoryStats?: (memories: MemoryItem[]) => void;
  _openSkillsStoreFromMemory?: () => void;
  _memoryCache?: MemoryItem[];
  readonly memoryEnabled?: boolean;
};

let memoryCache: MemoryItem[] = [];
let memoryFilter = '';
let currentTargetId = 'memoryList';
const MEMORY_PAGE_SIZE = 100;
const memoryPages = new Map<string, number>();
const requestGenerations = new Map<string, number>();
let listenerAttached = false;
let memoryListEpoch = 0;
let activeMemoryListRequest: {
  epoch: number;
  promise: Promise<MemoryItem[]>;
} | undefined;
let trailingMemoryListEpoch = 0;
let trailingMemoryListRequest: Promise<MemoryItem[]> | undefined;

function globals(): MemoryWindow { return featureRegistry as unknown as MemoryWindow; }
function isMemoryEnabled(): boolean { return globals().memoryEnabled ?? true; }
function translate(key: I18nKey, values?: Record<string, unknown>): string {
  return globals().t?.(key, values) || key;
}
function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
function api(): MemoryApi {
  const value = globals().Api?.memory;
  if (!value) throw new Error('Memory API is not ready');
  return value;
}
function input(id: string): HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement | null {
  const element = document.getElementById(id);
  return element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement
    || element instanceof HTMLSelectElement ? element : null;
}

function ensureListener(): void {
  if (listenerAttached) return;
  document.addEventListener('click', onMemoryClick);
  listenerAttached = true;
}

export function toggleMemory(): void {
  if (!isMemoryEnabled()) { openMemoryModal(); return; }
  globals()._applyMemoryUI?.(false);
  globals().captureActiveConversationSettings?.();
  globals().debugLog?.('Memory applied: OFF (AI still accumulates in background)', 'success');
}

export function toggleMemoryFromModal(): void {
  globals()._applyMemoryUI?.(!isMemoryEnabled());
  globals().captureActiveConversationSettings?.();
  globals().updateSubmenuCounts?.();
  globals().debugLog?.(`Memory applied: ${isMemoryEnabled() ? 'ON — existing memories injected into context' : 'OFF — AI still accumulates in background'}`, 'success');
  closeMemoryModal();
}

function updateModalButton(): void {
  const button = document.getElementById('memoryModalToggleBtn');
  if (!(button instanceof HTMLButtonElement)) return;
  button.innerHTML = isMemoryEnabled()
    ? '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18.36 6.64a9 9 0 11-12.73 0"/><line x1="12" y1="2" x2="12" y2="12"/></svg> ' + escape(translate('memory.disable'))
    : '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M5 12h14"/><path d="M12 5v14"/></svg> ' + escape(translate('memory.enable'));
  button.className = `memory-action-btn ${isMemoryEnabled() ? 'memory-btn-off' : 'memory-btn-on'}`;
}

export function openMemoryModal(): void {
  ensureListener();
  document.getElementById('memoryModal')?.classList.add('open');
  memoryFilter = '';
  const search = input('memorySearchInput');
  if (search) search.value = '';
  void refreshMemoryList();
  updateModalButton();
  attachMemorySkillDropZone();
}

export function closeMemoryModal(): void {
  document.getElementById('memoryModal')?.classList.remove('open');
  const add = document.getElementById('memoryAddSection');
  if (add instanceof HTMLElement) add.style.display = 'none';
  requestGenerations.set('memoryList', (requestGenerations.get('memoryList') || 0) + 1);
  memoryListEpoch += 1;
}

export function openSkillsStoreFromMemory(): void {
  closeMemoryModal();
  globals().openSettings?.();
  window.setTimeout(() => globals().switchSettingsTab?.('skills'), 50);
}

export function toggleMemoryAddForm(): void {
  const section = document.getElementById('memoryAddSection');
  if (!(section instanceof HTMLElement)) return;
  const hidden = !section.style.display || section.style.display === 'none';
  section.style.display = hidden ? 'block' : 'none';
  if (hidden) try { section.scrollIntoView({ behavior: 'smooth', block: 'nearest' }); } catch { /* optional */ }
}

export function switchMemoryTab(scope: string): void {
  document.querySelectorAll<HTMLElement>('.memory-tab').forEach((tab) => {
    tab.classList.toggle('active', tab.dataset.scope === scope);
  });
  void refreshMemoryList(scope);
}

export function filterMemoryList(query: string): void {
  memoryFilter = String(query || '').toLowerCase().trim();
  memoryPages.set('memoryList', 0);
  renderMemoryCards(memoryCache, 'memoryList');
}

function updateStats(memories: MemoryItem[]): void {
  const element = document.getElementById('memoryStats');
  if (!element) return;
  const enabled = memories.filter((item) => item.enabled).length;
  const project = memories.filter((item) => item.scope === 'project').length;
  const global = memories.filter((item) => item.scope === 'global').length;
  element.innerHTML = `<div class="memory-stat"><span class="memory-stat-num">${memories.length}</span><span class="memory-stat-label">${escape(translate('memory.statTotal'))}</span></div>
    <div class="memory-stat"><span class="memory-stat-num memory-stat-active">${enabled}</span><span class="memory-stat-label">${escape(translate('memory.statEnabled'))}</span></div>
    <div class="memory-stat"><span class="memory-stat-num">${project}</span><span class="memory-stat-label">${escape(translate('memory.statProject'))}</span></div>
    <div class="memory-stat"><span class="memory-stat-num">${global}</span><span class="memory-stat-label">${escape(translate('memory.statGlobal'))}</span></div>`;
}

function startMemoryListRequest(epoch: number): Promise<MemoryItem[]> {
  const promise = api().list('all').then((data) => {
    if (!data) throw new Error('empty response');
    return Array.isArray(data.memories) ? data.memories : [];
  });
  const flight = { epoch, promise };
  activeMemoryListRequest = flight;
  void promise.then(
    () => { if (activeMemoryListRequest === flight) activeMemoryListRequest = undefined; },
    () => { if (activeMemoryListRequest === flight) activeMemoryListRequest = undefined; },
  );
  return promise;
}

function loadMemoryList(): Promise<MemoryItem[]> {
  if (!activeMemoryListRequest) return startMemoryListRequest(memoryListEpoch);
  if (activeMemoryListRequest.epoch === memoryListEpoch) {
    return activeMemoryListRequest.promise;
  }

  // Closing/reopening invalidates presentation ownership, so preserve one
  // trailing authoritative refresh. Repeated clicks or tab switches collapse
  // onto that demand instead of starting unbounded network-filesystem scans.
  trailingMemoryListEpoch = memoryListEpoch;
  if (trailingMemoryListRequest) return trailingMemoryListRequest;
  const current = activeMemoryListRequest.promise;
  const trailing = current.catch(() => undefined).then(
    () => startMemoryListRequest(trailingMemoryListEpoch),
  );
  trailingMemoryListRequest = trailing;
  void trailing.then(
    () => { if (trailingMemoryListRequest === trailing) trailingMemoryListRequest = undefined; },
    () => { if (trailingMemoryListRequest === trailing) trailingMemoryListRequest = undefined; },
  );
  return trailing;
}

export async function refreshMemoryList(scope?: string, targetId = 'memoryList'): Promise<void> {
  ensureListener();
  currentTargetId = targetId;
  const list = document.getElementById(targetId);
  if (!list) return;
  const selected = scope || document.querySelector<HTMLElement>('.memory-tab.active')?.dataset.scope || 'all';
  const generation = (requestGenerations.get(targetId) || 0) + 1;
  requestGenerations.set(targetId, generation);
  list.innerHTML = `<div class="memory-loading"><div class="memory-loading-dot"></div><div class="memory-loading-dot"></div><div class="memory-loading-dot"></div><span>${escape(translate('memory.loading'))}</span></div>`;
  try {
    const allMemories = await loadMemoryList();
    if (requestGenerations.get(targetId) !== generation) return;
    const memories = selected === 'all'
      ? allMemories
      : allMemories.filter((item) => item.scope === selected);
    memoryCache = memories;
    globals()._memoryCache = memoryCache;
    updateStats(memories);
    memoryPages.set(targetId, 0);
    renderMemoryCards(memories, targetId);
  } catch (error: unknown) {
    if (requestGenerations.get(targetId) !== generation) return;
    list.innerHTML = `<div class="memory-empty"><span class="memory-empty-icon"></span>
      <div class="memory-empty-title">${escape(translate('memory.loadFailed'))}</div>
      <div style="margin-top:4px;font-size:12px;opacity:.7">${escape(errorMessage(error))}</div>
      <button class="memory-retry-btn" data-memory-action="retry" data-target-id="${escape(targetId)}">${escape(translate('memory.retry'))}</button></div>`;
  }
}

function buildCard(item: MemoryItem): HTMLElement {
  const card = document.createElement('div');
  card.className = `memory-card${item.enabled ? '' : ' is-disabled'}`;
  card.dataset.id = item.id;
  card.innerHTML = `<div class="memory-card-header" data-memory-action="expand">
      <span class="memory-card-expand-icon"><svg width="10" height="10" viewBox="0 0 10 10"><path d="M3 1l4 4-4 4" fill="none" stroke="currentColor" stroke-width="1.5"/></svg></span>
      <span class="memory-card-name">${escape(item.name)}</span>
      <span class="memory-card-scope ${escape(item.scope || '')}">${escape(item.scope === 'global' ? translate('memory.scopeGlobal') : translate('memory.scopeProject'))}</span>
      <div class="memory-card-actions">
        <button class="memory-toggle-switch${item.enabled ? ' on' : ''}" data-memory-action="toggle" title="${escape(translate(item.enabled ? 'memory.toggleOnTip' : 'memory.toggleOffTip'))}"><span class="memory-toggle-track"><span class="memory-toggle-thumb"></span></span></button>
        <button class="memory-delete-btn" data-memory-action="delete" title="${escape(translate('memory.deleteTip'))}"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg></button>
      </div></div>
    ${item.description ? `<div class="memory-card-desc">${escape(item.description)}</div>` : ''}
    ${item.tags?.length ? `<div class="memory-card-tags">${item.tags.map((tag) => `<span class="memory-card-tag">${escape(tag)}</span>`).join('')}</div>` : ''}
    <div class="memory-card-body"><div class="memory-card-body-inner"${typeof item.body === 'string' ? ` data-raw="${escape(item.body || '(empty)')}"` : ''}></div></div>`;
  return card;
}

export function renderMemoryCards(memories: MemoryItem[], targetId = currentTargetId): void {
  const list = document.getElementById(targetId);
  if (!list) return;
  const filtered = memoryFilter && targetId === 'memoryList' ? memories.filter((item) => (
    [item.name, item.description, ...(item.tags || [])].filter(Boolean).join(' ').toLowerCase().includes(memoryFilter)
  )) : memories;
  if (!filtered.length) {
    const title = memoryFilter && memories.length
      ? translate('memory.noMatch', { q: memoryFilter }) : translate('memory.emptyTitle');
    list.innerHTML = `<div class="memory-empty"><span class="memory-empty-icon"></span><div class="memory-empty-title">${escape(title)}</div></div>`;
    return;
  }
  const lastPage = Math.max(0, Math.ceil(filtered.length / MEMORY_PAGE_SIZE) - 1);
  const page = Math.min(Math.max(0, memoryPages.get(targetId) || 0), lastPage);
  memoryPages.set(targetId, page);
  const start = page * MEMORY_PAGE_SIZE;
  const visible = filtered.slice(start, start + MEMORY_PAGE_SIZE);
  const fragment = document.createDocumentFragment();
  if (filtered.length > MEMORY_PAGE_SIZE) {
    const controls = document.createElement('div');
    controls.className = 'memory-page-controls';
    controls.innerHTML = `<button class="memory-retry-btn" type="button" data-memory-action="page-previous" data-target-id="${escape(targetId)}"${page === 0 ? ' disabled' : ''}>${escape(translate('memory.pagePrevious'))}</button>
      <span class="memory-page-status" aria-live="polite">${escape(translate('memory.pageStatus', {
        from: start + 1,
        to: start + visible.length,
        total: filtered.length,
      }))}</span>
      <button class="memory-retry-btn" type="button" data-memory-action="page-next" data-target-id="${escape(targetId)}"${page === lastPage ? ' disabled' : ''}>${escape(translate('memory.pageNext'))}</button>`;
    fragment.appendChild(controls);
  }
  for (const item of visible) fragment.appendChild(buildCard(item));
  list.replaceChildren(fragment);
}

function moveMemoryPage(targetId: string, direction: -1 | 1): void {
  const current = memoryPages.get(targetId) || 0;
  memoryPages.set(targetId, Math.max(0, current + direction));
  renderMemoryCards(memoryCache, targetId);
  const list = document.getElementById(targetId);
  if (typeof list?.scrollTo === 'function') {
    list.scrollTo({ top: 0, behavior: 'auto' });
  }
}

export async function toggleMemoryBody(header: Element): Promise<void> {
  const card = header.closest<HTMLElement>('.memory-card');
  const body = card?.querySelector<HTMLElement>('.memory-card-body');
  if (!body || !card) return;
  const open = body.classList.toggle('open');
  header.querySelector('.memory-card-expand-icon')?.classList.toggle('expanded', open);
  if (!open) return;
  const inner = body.querySelector<HTMLElement>('.memory-card-body-inner');
  if (!inner || inner.dataset.loaded === '1') return;
  const render = (raw: string): void => {
    try { inner.innerHTML = globals().marked?.parse(raw) || `<pre>${escape(raw)}</pre>`; }
    catch { inner.innerHTML = `<pre>${escape(raw)}</pre>`; }
    inner.dataset.loaded = '1';
  };
  if (inner.dataset.raw != null) {
    const raw = inner.dataset.raw;
    delete inner.dataset.raw;
    render(raw);
    return;
  }
  // Summary-list entries carry no body — fetch this one memory on demand
  // (and cache it back into memoryCache so re-renders stay local).
  const id = card.dataset.id || '';
  const cached = memoryCache.find((entry) => entry.id === id);
  if (typeof cached?.body === 'string') { render(cached.body || '(empty)'); return; }
  inner.innerHTML = `<pre>${escape(translate('memory.loading'))}</pre>`;
  try {
    const mem = await api().get(id);
    if (!inner.isConnected) return;
    const raw = typeof mem?.body === 'string' ? mem.body : '';
    if (cached) cached.body = raw;
    render(raw || '(empty)');
  } catch (error: unknown) {
    if (inner.isConnected) inner.innerHTML = `<pre>${escape(errorMessage(error))}</pre>`;
  }
}

function cardsFor(id: string): HTMLElement[] {
  return [...document.querySelectorAll<HTMLElement>('.memory-card')]
    .filter((card) => card.dataset.id === id);
}

export async function toggleMemoryEnabled(id: string): Promise<void> {
  const item = memoryCache.find((entry) => entry.id === id);
  const previous = item?.enabled;
  const next = item ? !item.enabled : true;
  if (item) item.enabled = next;
  for (const card of cardsFor(id)) {
    card.classList.toggle('is-disabled', !next);
    card.querySelector('.memory-toggle-switch')?.classList.toggle('on', next);
  }
  updateStats(memoryCache);
  try {
    const response = await api().toggle(id);
    if (!response?.ok) throw new Error(`HTTP ${response?.status || 'no response'}`);
    const updated = await response.json();
    if (item && memoryCache.includes(item)) Object.assign(item, updated);
  } catch (error: unknown) {
    globals().debugLog?.(`Toggle memory failed: ${errorMessage(error)}`, 'error');
    if (item && memoryCache.includes(item)) item.enabled = previous;
    for (const card of cardsFor(id)) {
      card.classList.toggle('is-disabled', !previous);
      card.querySelector('.memory-toggle-switch')?.classList.toggle('on', Boolean(previous));
    }
    updateStats(memoryCache);
  }
}

export async function deleteMemory(id: string): Promise<void> {
  const confirmed = await (globals().showConfirm?.(translate('memory.deleteConfirm'), { danger: true }) ?? Promise.resolve(true));
  if (!confirmed) return;
  const cards = cardsFor(id);
  cards.forEach((card) => { card.style.opacity = '0'; card.style.transform = 'scale(.96)'; });
  try {
    const response = await api().remove(id);
    if (!response?.ok) throw new Error(`HTTP ${response?.status || 'no response'}`);
    memoryCache = memoryCache.filter((item) => item.id !== id);
    globals()._memoryCache = memoryCache;
    cards.forEach((card) => card.remove());
    renderMemoryCards(memoryCache, currentTargetId);
    updateStats(memoryCache);
    globals().debugLog?.('Memory deleted', 'success');
  } catch (error: unknown) {
    globals().debugLog?.(`Delete memory failed: ${errorMessage(error)}`, 'error');
    cards.forEach((card) => { card.style.opacity = '1'; card.style.transform = ''; });
  }
}

export async function createMemoryFromModal(): Promise<void> {
  const name = input('memoryNewName')?.value.trim() || '';
  const description = input('memoryNewDesc')?.value.trim() || '';
  const body = input('memoryNewBody')?.value.trim() || '';
  const scope = input('memoryNewScope')?.value || 'project';
  const tags = (input('memoryNewTags')?.value || '').split(',').map((tag) => tag.trim()).filter(Boolean);
  const status = document.getElementById('memoryModalStatus');
  if (!name || !body) { if (status) status.textContent = translate('memory.nameBodyRequired'); return; }
  try {
    const response = await api().create({ name, description, body, scope, tags });
    const data = response ? await response.json() : {};
    if (!response?.ok) { if (status) status.textContent = String(data.error || translate('memory.createFailed')); return; }
    for (const id of ['memoryNewName', 'memoryNewDesc', 'memoryNewBody', 'memoryNewTags']) {
      const field = input(id); if (field) field.value = '';
    }
    const section = document.getElementById('memoryAddSection');
    if (section instanceof HTMLElement) section.style.display = 'none';
    if (status) status.textContent = '';
    const created = (data.memory && typeof data.memory === 'object' ? data.memory : data) as unknown as MemoryItem;
    memoryCache.unshift(created);
    memoryPages.set('memoryList', 0);
    renderMemoryCards(memoryCache, 'memoryList');
    updateStats(memoryCache);
    globals().debugLog?.(`Memory created: ${name}`, 'success');
  } catch (error: unknown) {
    if (status) status.textContent = translate('memory.errorPrefix', { err: errorMessage(error) });
  }
}

function onMemoryClick(event: Event): void {
  const target = event.target instanceof Element
    ? event.target.closest<HTMLElement>('[data-memory-action]') : null;
  if (!target) return;
  const action = target.dataset.memoryAction;
  const card = target.closest<HTMLElement>('.memory-card');
  const id = card?.dataset.id || '';
  if (action === 'expand') void toggleMemoryBody(target);
  else if (action === 'toggle') { event.stopPropagation(); void toggleMemoryEnabled(id); }
  else if (action === 'delete') { event.stopPropagation(); void deleteMemory(id); }
  else if (action === 'retry') void refreshMemoryList(undefined, target.dataset.targetId || 'memoryList');
  else if (action === 'page-previous') {
    moveMemoryPage(target.dataset.targetId || currentTargetId, -1);
  } else if (action === 'page-next') {
    moveMemoryPage(target.dataset.targetId || currentTargetId, 1);
  }
}

ensureListener();
const bridge = globals();
bridge.toggleMemory = toggleMemory;
bridge.toggleMemoryFromModal = toggleMemoryFromModal;
bridge.openMemoryModal = openMemoryModal;
bridge.closeMemoryModal = closeMemoryModal;
bridge._openSkillsStoreFromMemory = openSkillsStoreFromMemory;
bridge.toggleMemoryAddForm = toggleMemoryAddForm;
bridge.switchMemoryTab = switchMemoryTab;
bridge.filterMemoryList = filterMemoryList;
bridge.refreshMemoryList = refreshMemoryList;
bridge.toggleMemoryBody = toggleMemoryBody;
bridge.toggleMemoryEnabled = toggleMemoryEnabled;
bridge.deleteMemory = deleteMemory;
bridge.createMemoryFromModal = createMemoryFromModal;
bridge._renderMemoryCards = renderMemoryCards;
bridge._updateMemoryModalBtn = updateModalButton;
globals()._updateMemoryStats = updateStats;
bridge._memoryCache = memoryCache;

window.addEventListener('tofu:language-change', () => {
  const modal = document.getElementById('memoryModal');
  if (!modal?.classList.contains('open')) return;
  updateModalButton();
  updateStats(memoryCache);
  renderMemoryCards(memoryCache);
});
