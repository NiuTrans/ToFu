import { featureRegistry } from '../../feature-registry';
import { createLifecycleScope, type LifecycleScope } from '../../lifecycle';

type ContextType = 'identity' | 'work_rule' | 'response_preference';

interface ContextItem {
  id?: string;
  type: ContextType;
  source?: string;
  created_at?: string;
  updated_at?: string;
  text?: string;
  condition?: string;
  action?: string;
  _editing?: boolean;
}

interface CleanContextItem extends Omit<ContextItem, '_editing'> {}

interface UserContextApi {
  get(): Promise<{ items?: ContextItem[]; cap?: number } | null>;
  replace(items: CleanContextItem[]): Promise<{
    saved?: boolean;
    items?: ContextItem[];
    cap?: number;
  } | null>;
}

interface MemoryClearApi {
  clearPreview(): Promise<{ total?: number; global?: number; project?: number } | null>;
  clearAll(): Promise<{ deleted_ids?: string[]; failed_ids?: string[] } | null>;
}

type PreferencesWindow = Window & {
  Api?: { userContext?: UserContextApi; memory?: MemoryClearApi };
  t?: (key: string, values?: Record<string, unknown>) => string;
  Icon?: (name: string, size?: number) => string;
  escapeHtml?: (value: unknown) => string;
  debugLog?: (message: string, kind?: string) => void;
  showConfirm?: (message: string, options?: { danger?: boolean }) => Promise<boolean>;
  refreshMemoryList?: (scope?: string, targetId?: string) => Promise<void>;
  _populatePreferencesTab?: () => Promise<void>;
  refreshPreferences?: () => Promise<void>;
  savePreferences?: (button?: HTMLButtonElement | null) => Promise<void>;
  clearLegacyMemories?: (button?: HTMLButtonElement | null) => Promise<void>;
  _contextAdd?: (type: ContextType) => void;
  _contextUpdateField?: (index: number, field: string, value: string) => void;
  _contextStartEdit?: (index: number) => void;
  _contextFinishEdit?: (index: number) => void;
  _contextRemove?: (index: number) => Promise<void>;
  _prefsRender?: () => void;
  _destroyPreferences?: () => void;
};

interface ContextSection {
  type: ContextType;
  titleKey: string;
  descKey: string;
  addKey: string;
  emptyKey: string;
  icon: string;
}

const SECTIONS: readonly ContextSection[] = [
  { type: 'identity', titleKey: 'context.identityTitle', descKey: 'context.identityDesc', addKey: 'context.identityAdd', emptyKey: 'context.identityEmpty', icon: 'user' },
  { type: 'work_rule', titleKey: 'context.ruleTitle', descKey: 'context.ruleDesc', addKey: 'context.ruleAdd', emptyKey: 'context.ruleEmpty', icon: 'workflow' },
  { type: 'response_preference', titleKey: 'context.preferenceTitle', descKey: 'context.preferenceDesc', addKey: 'context.preferenceAdd', emptyKey: 'context.preferenceEmpty', icon: 'message' },
];

let capacity = 2500;
let items: ContextItem[] = [];
let savedFingerprint = '[]';
let lifecycle: LifecycleScope | null = null;
let requestGeneration = 0;

function globals(): PreferencesWindow {
  return featureRegistry as unknown as PreferencesWindow;
}

function translate(
  key: string,
  fallback: string,
  values?: Record<string, unknown>,
): string {
  const translated = globals().t?.(key, values || {});
  return translated && translated !== key ? translated : fallback;
}

function escape(value: unknown): string {
  const helper = globals().escapeHtml;
  if (helper) return helper(String(value ?? ''));
  return String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;').replaceAll('"', '&quot;').replaceAll("'", '&#39;');
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function contextApi(): UserContextApi {
  const value = globals().Api?.userContext;
  if (!value) throw new Error('User-context API is not ready');
  return value;
}

function memoryApi(): MemoryClearApi {
  const value = globals().Api?.memory;
  if (!value) throw new Error('Memory API is not ready');
  return value;
}

function ensureLifecycle(): LifecycleScope {
  if (lifecycle && !lifecycle.signal.aborted) return lifecycle;
  lifecycle = createLifecycleScope();
  const panel = document.getElementById('settingsTab_preferences');
  if (panel) {
    lifecycle.listen(panel, 'click', onClick);
    lifecycle.listen(panel, 'input', onInput);
  }
  return lifecycle;
}

export function destroyPreferences(): void {
  requestGeneration += 1;
  lifecycle?.destroy();
  lifecycle = null;
}

export function cleanItems(): CleanContextItem[] {
  return items.map((item) => {
    const clean: CleanContextItem = {
      id: item.id || '', type: item.type, source: item.source || 'manual',
      created_at: item.created_at || '', updated_at: item.updated_at || '',
    };
    if (item.type === 'work_rule') {
      clean.condition = (item.condition || '').trim();
      clean.action = (item.action || '').trim();
    } else {
      clean.text = (item.text || '').trim();
    }
    return clean;
  }).filter((item) => item.type === 'work_rule'
    ? Boolean(item.condition || item.action)
    : Boolean(item.text));
}

function fingerprint(): string {
  return JSON.stringify(cleanItems());
}

export function estimateChars(): number {
  const labels: Record<ContextType, string> = {
    work_rule: 'Work rules',
    response_preference: 'Response preferences',
    identity: 'About the user',
  };
  const order: ContextType[] = ['work_rule', 'response_preference', 'identity'];
  return order.map((type) => {
    const lines = cleanItems().filter((item) => item.type === type).map((item) => (
      type === 'work_rule'
        ? `- WHEN: ${item.condition}\n  DO: ${item.action}`
        : `- ${item.text}`
    ));
    return lines.length ? `## ${labels[type]}\n${lines.join('\n')}` : '';
  }).filter(Boolean).join('\n\n').length;
}

function validationError(): string {
  for (const item of items) {
    if (item.type === 'work_rule'
      && (!(item.condition || '').trim() || !(item.action || '').trim())) {
      return translate('context.ruleRequired', '工作规则需要同时填写条件和动作');
    }
    if (item.type !== 'work_rule' && !(item.text || '').trim()) {
      return translate('context.textRequired', '请填写内容或删除空条目');
    }
  }
  return estimateChars() > capacity
    ? translate('context.overCap', '已超出上下文容量，请精简后再保存') : '';
}

function updateCharCount(): void {
  const count = estimateChars();
  const badge = document.getElementById('prefsCharCount');
  const fill = document.getElementById('ctxCapacityFill');
  if (badge) {
    badge.textContent = `${count} / ${capacity}`;
    badge.classList.toggle('is-over', count > capacity);
  }
  if (fill instanceof HTMLElement) {
    fill.style.width = `${Math.min(100, Math.round(count / capacity * 100))}%`;
    fill.classList.toggle('is-over', count > capacity);
  }
}

function markDirty(): void {
  const dirty = fingerprint() !== savedFingerprint;
  const state = document.getElementById('ctxDirtyState');
  const save = document.getElementById('ctxSaveBtn');
  const status = document.getElementById('prefsStatus');
  const validation = validationError();
  if (state) state.textContent = dirty
    ? translate('context.unsaved', '有未保存的更改') : '';
  if (save instanceof HTMLButtonElement) save.disabled = !dirty || Boolean(validation);
  if (status && validation) {
    status.textContent = validation;
    status.className = 'ctx-status is-error';
  } else if (status?.classList.contains('is-error')) {
    status.textContent = '';
    status.className = 'ctx-status';
  }
  updateCharCount();
}

function contextIcon(name: string): string {
  const mapped = name === 'workflow' ? 'wrench' : name === 'message' ? 'messageCircle' : 'brain';
  try {
    const rendered = globals().Icon?.(mapped, 18);
    if (rendered) return rendered;
  } catch {
    // The static SVG fallback keeps the editor usable without the icon set.
  }
  if (name === 'workflow') return '<svg viewBox="0 0 24 24"><circle cx="6" cy="6" r="2"/><circle cx="18" cy="18" r="2"/><path d="M8 6h4a4 4 0 0 1 4 4v6"/></svg>';
  if (name === 'message') return '<svg viewBox="0 0 24 24"><path d="M21 15a4 4 0 0 1-4 4H8l-5 3V7a4 4 0 0 1 4-4h10a4 4 0 0 1 4 4z"/></svg>';
  return '<svg viewBox="0 0 24 24"><circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/></svg>';
}

function sourceLabel(source?: string): string {
  if (source === 'assistant') return translate('context.sourceAssistant', '助手学习');
  if (source === 'legacy_migration') return translate('context.sourceMigrated', '旧档案迁移');
  return translate('context.sourceManual', '手动添加');
}

function contextDate(value?: string): string {
  if (!value) return '';
  try { return new Date(value).toLocaleDateString(); } catch { return ''; }
}

function editor(item: ContextItem, index: number): string {
  if (!item._editing) return '';
  const done = `<button class="ctx-editor-done" data-context-action="finish" data-index="${index}">${escape(translate('context.doneEditing', '完成'))}</button>`;
  if (item.type === 'work_rule') {
    return `<div class="ctx-item-editor">
      <label><span>${escape(translate('context.whenLabel', '当以下情况发生'))}</span>
        <textarea rows="2" data-context-field="condition" data-index="${index}">${escape(item.condition)}</textarea></label>
      <label><span>${escape(translate('context.doLabel', '助手应当'))}</span>
        <textarea rows="2" data-context-field="action" data-index="${index}">${escape(item.action)}</textarea></label>${done}</div>`;
  }
  return `<div class="ctx-item-editor"><label><span>${escape(translate('context.contentLabel', '内容'))}</span>
    <textarea rows="2" data-context-field="text" data-index="${index}">${escape(item.text)}</textarea></label>${done}</div>`;
}

function renderItem(item: ContextItem, index: number): string {
  const content = item.type === 'work_rule'
    ? `<div class="ctx-rule-line"><span>${escape(translate('context.whenBadge', '当'))}</span><p>${escape(item.condition || translate('context.ruleConditionPlaceholder', '填写触发条件'))}</p></div>
       <div class="ctx-rule-line is-action"><span>${escape(translate('context.thenBadge', '则'))}</span><p>${escape(item.action || translate('context.ruleActionPlaceholder', '填写执行动作'))}</p></div>`
    : `<p class="ctx-item-text">${escape(item.text || translate('context.textPlaceholder', '填写一条长期信息'))}</p>`;
  const date = contextDate(item.updated_at);
  const meta = `${sourceLabel(item.source)}${date ? ` · ${date}` : ''}`;
  return `<article class="ctx-item${item._editing ? ' is-editing' : ''}" data-ctx-idx="${index}">
    <div class="ctx-item-view" data-context-action="edit" data-index="${index}">
      <div class="ctx-item-content">${content}<div class="ctx-item-meta">${escape(meta)}</div></div>
      <div class="ctx-item-actions">
        <button title="${escape(translate('context.edit', '编辑'))}" data-context-action="edit" data-index="${index}">${globals().Icon?.('edit', 14) || '✎'}</button>
        <button class="is-danger" title="${escape(translate('context.remove', '删除'))}" data-context-action="remove" data-index="${index}">${globals().Icon?.('trash', 14) || '×'}</button>
      </div>
    </div>${editor(item, index)}</article>`;
}

export function renderPreferences(): void {
  const list = document.getElementById('prefsList');
  if (!list) return;
  list.innerHTML = SECTIONS.map((section) => {
    let count = 0;
    const rows = items.map((item, index) => {
      if (item.type !== section.type) return '';
      count += 1;
      return renderItem(item, index);
    }).join('');
    const add = `<button class="ctx-add-btn" data-context-action="add" data-context-type="${section.type}">${globals().Icon?.('plus', 14) || '+'}<span>${escape(translate(section.addKey, '添加'))}</span></button>`;
    const empty = `<button class="ctx-empty" data-context-action="add" data-context-type="${section.type}">${escape(translate(section.emptyKey, '还没有内容，点击添加'))}</button>`;
    return `<section class="ctx-group ctx-group-${section.type}">
      <header class="ctx-group-head"><span class="ctx-group-icon">${contextIcon(section.icon)}</span>
        <div class="ctx-group-copy"><div class="ctx-group-title-row"><h3>${escape(translate(section.titleKey, section.type))}</h3><span>${count}</span></div>
        <p>${escape(translate(section.descKey, ''))}</p></div>${add}</header>
      <div class="ctx-items">${rows || empty}</div></section>`;
  }).join('');
  markDirty();
}

export function addContext(type: ContextType): void {
  const item: ContextItem = { id: '', type, source: 'manual', created_at: '', updated_at: '', _editing: true };
  if (type === 'work_rule') { item.condition = ''; item.action = ''; } else item.text = '';
  items.push(item);
  renderPreferences();
  const row = document.querySelector<HTMLElement>(`.ctx-item[data-ctx-idx="${items.length - 1}"]`);
  try { row?.scrollIntoView({ behavior: 'smooth', block: 'nearest' }); } catch { /* optional */ }
  row?.querySelector<HTMLTextAreaElement>('textarea')?.focus();
}

export function updateField(index: number, field: string, value: string): void {
  const item = items[index];
  if (!item || !['text', 'condition', 'action'].includes(field)) return;
  item[field as 'text' | 'condition' | 'action'] = value;
  item.source = 'manual';
  item.updated_at = '';
  markDirty();
}

export function startEdit(index: number): void {
  if (!items[index]) return;
  items[index]._editing = true;
  renderPreferences();
  document.querySelector<HTMLTextAreaElement>(`.ctx-item[data-ctx-idx="${index}"] textarea`)?.focus();
}

export function finishEdit(index: number): void {
  if (items[index]) items[index]._editing = false;
  renderPreferences();
}

export async function removeContext(index: number): Promise<void> {
  if (!items[index]) return;
  const ok = await (globals().showConfirm?.(
    translate('context.removeConfirm', '删除这条长期上下文？'), { danger: true },
  ) ?? Promise.resolve(true));
  if (!ok) return;
  items.splice(index, 1);
  renderPreferences();
}

function refreshMemorySection(): void {
  if (!document.getElementById('prefsMemoryList')) return;
  try {
    void globals().refreshMemoryList?.('all', 'prefsMemoryList');
  } catch (error: unknown) {
    globals().debugLog?.(`[Context] memory refresh failed: ${errorMessage(error)}`, 'warn');
  }
}

export async function populatePreferencesTab(): Promise<void> {
  await refreshPreferences();
  refreshMemorySection();
}

export async function refreshPreferences(): Promise<void> {
  const owner = ensureLifecycle();
  const generation = ++requestGeneration;
  const status = document.getElementById('prefsStatus');
  const list = document.getElementById('prefsList');
  list?.setAttribute('aria-busy', 'true');
  if (status) {
    status.textContent = translate('context.loading', '正在加载你的上下文…');
    status.className = 'ctx-status';
  }
  try {
    const data = await contextApi().get();
    if (owner.signal.aborted || generation !== requestGeneration) return;
    if (!Array.isArray(data?.items)) throw new Error('empty response');
    capacity = data.cap || capacity;
    items = data.items.map((item) => ({ ...item, _editing: false }));
    savedFingerprint = fingerprint();
    renderPreferences();
    if (status) status.textContent = '';
  } catch (error: unknown) {
    if (owner.signal.aborted || generation !== requestGeneration) return;
    if (status) {
      status.textContent = `${translate('context.loadFailed', '加载失败')}: ${errorMessage(error)}`;
      status.className = 'ctx-status is-error';
    }
    globals().debugLog?.(`[Context] load failed: ${errorMessage(error)}`, 'error');
  } finally {
    if (!owner.signal.aborted && generation === requestGeneration) {
      list?.setAttribute('aria-busy', 'false');
    }
  }
}

export async function savePreferences(button?: HTMLButtonElement | null): Promise<void> {
  const validation = validationError();
  if (validation) { markDirty(); return; }
  const status = document.getElementById('prefsStatus');
  if (button) button.disabled = true;
  if (status) {
    status.textContent = translate('context.saving', '正在保存…');
    status.className = 'ctx-status';
  }
  try {
    const result = await contextApi().replace(cleanItems());
    if (!result || result.saved === false || !Array.isArray(result.items)) {
      throw new Error('server reported save failed');
    }
    items = result.items.map((item) => ({ ...item, _editing: false }));
    savedFingerprint = fingerprint();
    renderPreferences();
    if (status) {
      status.textContent = translate('context.saved', '已保存，下一轮对话开始生效');
      status.className = 'ctx-status is-success';
    }
  } catch (error: unknown) {
    if (status) {
      status.textContent = `${translate('context.saveFailed', '保存失败')}: ${errorMessage(error)}`;
      status.className = 'ctx-status is-error';
    }
  } finally {
    markDirty();
  }
}

export async function clearLegacyMemories(button?: HTMLButtonElement | null): Promise<void> {
  const status = document.getElementById('prefsStatus');
  try {
    if (button) button.disabled = true;
    const preview = await memoryApi().clearPreview();
    const total = Number(preview?.total || 0);
    if (!total) {
      if (status) {
        status.textContent = translate('context.noMemories', '没有可清空的经验记忆');
        status.className = 'ctx-status';
      }
      return;
    }
    const confirmed = await (globals().showConfirm?.(translate(
      'context.clearConfirm',
      '将删除 {total} 条经验记忆（全局 {global} 条，当前项目 {project} 条）。此操作不会删除“我的上下文”和技能包。确认继续？',
      { total, global: preview?.global || 0, project: preview?.project || 0 },
    ), { danger: true }) ?? Promise.resolve(true));
    if (!confirmed) return;
    const result = await memoryApi().clearAll();
    const deleted = result?.deleted_ids?.length || 0;
    const failed = result?.failed_ids?.length || 0;
    if (status) {
      status.textContent = failed
        ? translate('context.clearPartial', '已删除 {deleted} 条，{failed} 条删除失败', { deleted, failed })
        : translate('context.clearDone', '已清空 {n} 条旧记忆', { n: deleted });
      status.className = failed ? 'ctx-status is-error' : 'ctx-status is-success';
    }
    refreshMemorySection();
  } catch (error: unknown) {
    if (status) {
      status.textContent = `${translate('context.clearFailed', '清空失败')}: ${errorMessage(error)}`;
      status.className = 'ctx-status is-error';
    }
  } finally {
    if (button) button.disabled = false;
  }
}

function onClick(event: Event): void {
  const target = event.target instanceof Element
    ? event.target.closest<HTMLElement>('[data-context-action]') : null;
  if (!target) return;
  event.stopPropagation();
  const index = Number(target.dataset.index || 0);
  const action = target.dataset.contextAction;
  if (action === 'add') addContext(target.dataset.contextType as ContextType);
  else if (action === 'edit') startEdit(index);
  else if (action === 'finish') finishEdit(index);
  else if (action === 'remove') void removeContext(index);
}

function onInput(event: Event): void {
  const target = event.target;
  if (!(target instanceof HTMLTextAreaElement) || !target.dataset.contextField) return;
  updateField(Number(target.dataset.index || 0), target.dataset.contextField, target.value);
}

const bridge = globals();
bridge._populatePreferencesTab = populatePreferencesTab;
bridge.refreshPreferences = refreshPreferences;
bridge.savePreferences = savePreferences;
bridge.clearLegacyMemories = clearLegacyMemories;
bridge._contextAdd = addContext;
bridge._contextUpdateField = updateField;
bridge._contextStartEdit = startEdit;
bridge._contextFinishEdit = finishEdit;
bridge._contextRemove = removeContext;
bridge._prefsRender = renderPreferences;
bridge._destroyPreferences = destroyPreferences;
