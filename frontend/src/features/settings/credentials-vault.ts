import { featureRegistry } from '../../feature-registry';
import { createLifecycleScope, type LifecycleScope } from '../../lifecycle';
import type { I18nKey } from '../../i18n';

interface CredentialMetadata {
  name?: string;
  hint?: string;
  note?: string;
  updated_at?: string | number;
}

interface CredentialListResponse {
  credentials?: CredentialMetadata[];
}

interface CredentialMutationResponse {
  error?: { message?: unknown } | string;
  value?: string;
}

interface CredentialsApi {
  list(): Promise<CredentialListResponse | null>;
  upsert(body: { name: string; value: string; note: string }): Promise<CredentialMutationResponse | null>;
  reveal(name: string): Promise<CredentialMutationResponse | null>;
  remove(name: string): Promise<CredentialMutationResponse | null>;
}

type CredentialWindow = Window & {
  Api?: { credentials?: CredentialsApi };
  t?: (key: string, values?: Record<string, unknown>) => string;
  _renderCredentialsVault?: () => void;
  _credentialAdd?: () => void;
  _credentialReveal?: (name: string) => void;
  _credentialHide?: (name: string) => void;
  _credentialCopy?: (name: string) => void;
  _credentialRemove?: (name: string) => void;
  _destroyCredentialsVault?: () => void;
};

const REVEAL_MS = 30_000;
const revealed = new Map<string, string>();
const hideTimers = new Map<string, number>();
let viewScope: LifecycleScope | null = null;
let renderGeneration = 0;

function globals(): CredentialWindow {
  return featureRegistry as unknown as CredentialWindow;
}

function api(): CredentialsApi {
  const credentials = globals().Api?.credentials;
  if (!credentials) throw new Error('Credential API is not ready');
  return credentials;
}

function translate(key: I18nKey, fallback: string): string {
  return globals().t?.(key) || fallback;
}

function element<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  className = '',
  text = '',
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}

function input(id: string): HTMLInputElement | null {
  const node = document.getElementById(id);
  return node instanceof HTMLInputElement ? node : null;
}

function setMessage(text: string, className = ''): void {
  const message = document.getElementById('credVaultMsg');
  if (!message) return;
  message.textContent = text;
  message.className = `priv-host-msg${className ? ` ${className}` : ''}`;
}

function errorMessage(error: CredentialMutationResponse['error']): string {
  if (typeof error === 'string') return error;
  if (error && typeof error.message === 'string') return error.message;
  return String(error);
}

export function credentialRelativeTime(timestamp: string | number | undefined): string {
  if (!timestamp) return '';
  const milliseconds = typeof timestamp === 'number'
    ? (timestamp < 1e12 ? timestamp * 1000 : timestamp)
    : Date.parse(String(timestamp));
  if (!milliseconds || Number.isNaN(milliseconds)) return String(timestamp);
  const minutes = Math.floor(Math.max(0, Date.now() - milliseconds) / 60_000);
  if (minutes < 1) {
    return translate('settings.credVaultJustNow', '刚刚更新');
  }
  if (minutes < 60) {
    return translate(
      'settings.credVaultMinutesAgo', '{n} 分钟前更新')
      .replace('{n}', String(minutes));
  }
  const hours = Math.floor(minutes / 60);
  if (hours < 24) {
    return translate(
      'settings.credVaultHoursAgo', '{n} 小时前更新')
      .replace('{n}', String(hours));
  }
  return translate(
    'settings.credVaultDaysAgo', '{n} 天前更新')
    .replace('{n}', String(Math.floor(hours / 24)));
}

function appendCredentialRow(
  target: HTMLElement,
  metadata: CredentialMetadata,
  scope: LifecycleScope,
): void {
  const name = String(metadata.name ?? '');
  const row = element('div', 'priv-host-row cred-vault-row');
  row.id = `credVaultRow_${name.replace(/[^a-zA-Z0-9]/g, '_')}`;

  const main = element('div', 'cred-vault-main');
  const idLine = element('div', 'cred-vault-idline');
  idLine.append(
    element('span', 'cred-vault-name', name),
    element('span', 'cred-vault-hint', String(metadata.hint ?? '')),
  );
  main.append(idLine);
  if (metadata.note) {
    main.append(element('div', 'cred-vault-note', String(metadata.note)));
  }
  row.append(
    main,
    element('span', 'cred-vault-time',
      credentialRelativeTime(metadata.updated_at)),
  );

  const actions = element('div', 'priv-host-actions');
  const reveal = element(
    'button', 'priv-host-btn',
    translate('settings.credVaultReveal', '查看'));
  reveal.type = 'button';
  scope.listen(reveal, 'click', () => { void credentialReveal(name); });
  const remove = element(
    'button', 'priv-host-btn danger',
    translate('settings.credVaultDelete', '删除'));
  remove.type = 'button';
  scope.listen(remove, 'click', () => { void credentialRemove(name); });
  actions.append(reveal, remove);
  row.append(actions);

  if (revealed.has(name)) {
    const value = element('div', 'cred-vault-value');
    value.append(element('code', 'cred-vault-secret', revealed.get(name) ?? ''));
    const copy = element(
      'button', 'priv-host-btn',
      translate('settings.credVaultCopy', '复制'));
    copy.type = 'button';
    scope.listen(copy, 'click', () => { void credentialCopy(name); });
    const hide = element(
      'button', 'priv-host-btn',
      translate('settings.credVaultHide', '隐藏'));
    hide.type = 'button';
    scope.listen(hide, 'click', () => credentialHide(name));
    value.append(copy, hide);
    row.append(value);
  }
  target.append(row);
}

function appendAddForm(target: HTMLElement, scope: LifecycleScope): void {
  const form = element('div', 'priv-host-add cred-vault-add');
  const fields = [
    ['credVaultNameInput', 'text', 'settings.credVaultNamePlaceholder', '名称（如 github_pat）'],
    ['credVaultValueInput', 'password', 'settings.credVaultValuePlaceholder', '值（只在本机加密落盘）'],
    ['credVaultNoteInput', 'text', 'settings.credVaultNotePlaceholder', '备注（可选）'],
  ] as const;
  for (const [id, type, key, fallback] of fields) {
    const field = element('input', 'priv-host-input');
    field.id = id;
    field.type = type;
    field.placeholder = translate(key, fallback);
    scope.listen(field, 'keydown', (event) => {
      if (event instanceof KeyboardEvent && event.key === 'Enter') {
        event.preventDefault();
        void credentialAdd();
      }
    });
    form.append(field);
  }
  const add = element(
    'button', 'priv-host-btn primary',
    translate('settings.credVaultAdd', '添加'));
  add.type = 'button';
  scope.listen(add, 'click', () => { void credentialAdd(); });
  form.append(add);
  const message = element('div', 'priv-host-msg');
  message.id = 'credVaultMsg';
  form.append(message);
  target.append(form);
}

function paint(
  target: HTMLElement,
  credentials: readonly CredentialMetadata[],
  scope: LifecycleScope,
): void {
  target.replaceChildren();
  if (credentials.length === 0) {
    target.append(element(
      'div', 'priv-host-empty',
      translate('settings.credVaultEmpty', '保管库为空。')));
  } else {
    for (const credential of credentials) {
      appendCredentialRow(target, credential, scope);
    }
  }
  appendAddForm(target, scope);
}

function clearHideTimer(name: string): void {
  const timer = hideTimers.get(name);
  if (timer !== undefined) window.clearTimeout(timer);
  hideTimers.delete(name);
}

export function destroyCredentialsVault(): void {
  renderGeneration += 1;
  viewScope?.destroy();
  viewScope = null;
  for (const timer of hideTimers.values()) window.clearTimeout(timer);
  hideTimers.clear();
  revealed.clear();
  document.querySelectorAll('.cred-vault-secret').forEach((node) => node.remove());
}

export async function renderCredentialsVault(): Promise<void> {
  const target = document.getElementById('credentialsVaultList');
  if (!(target instanceof HTMLElement)) return;
  viewScope?.destroy();
  const generation = ++renderGeneration;
  const scope = createLifecycleScope();
  viewScope = scope;
  target.replaceChildren(element(
    'div', 'priv-host-loading', translate('common.loading', '加载中…')));
  try {
    const response = await api().list();
    if (generation !== renderGeneration || viewScope !== scope
        || document.getElementById('credentialsVaultList') !== target) return;
    paint(target, response?.credentials ?? [], scope);
  } catch (error: unknown) {
    if (generation !== renderGeneration || viewScope !== scope) return;
    console.warn('[CredVault] list failed', error);
    target.replaceChildren(element(
      'div', 'priv-host-empty',
      translate('settings.credVaultLoadFail', '加载失败')));
  }
}

export async function credentialAdd(): Promise<void> {
  const nameField = input('credVaultNameInput');
  const valueField = input('credVaultValueInput');
  const noteField = input('credVaultNoteInput');
  if (!nameField || !valueField) return;
  const name = nameField.value.trim();
  const value = valueField.value;
  const note = noteField?.value.trim() ?? '';
  if (!name || !value) {
    setMessage(translate(
      'settings.credVaultNeedNameValue', '请填写名称和值。'), 'err');
    return;
  }
  setMessage(translate('common.saving', '保存中…'));
  try {
    const response = await api().upsert({ name, value, note });
    if (input('credVaultNameInput') !== nameField) return;
    if (response?.error) {
      setMessage(errorMessage(response.error), 'err');
      return;
    }
    nameField.value = '';
    valueField.value = '';
    if (noteField) noteField.value = '';
    await renderCredentialsVault();
  } catch (error: unknown) {
    if (input('credVaultNameInput') !== nameField) return;
    console.warn('[CredVault] upsert failed', error);
    setMessage(
      error instanceof Error && error.message
        ? error.message
        : translate('settings.credVaultSaveFail', '保存失败'),
      'err',
    );
  }
}

export async function credentialReveal(name: string): Promise<void> {
  try {
    const response = await api().reveal(name);
    if (response?.error) {
      setMessage(errorMessage(response.error), 'err');
      return;
    }
    revealed.set(name, response?.value ?? '');
    clearHideTimer(name);
    hideTimers.set(name, window.setTimeout(
      () => credentialHide(name), REVEAL_MS));
    await renderCredentialsVault();
  } catch (error: unknown) {
    console.warn('[CredVault] reveal failed', error);
    setMessage(translate('settings.credVaultRevealFail', '读取失败'), 'err');
  }
}

export function credentialHide(name: string): void {
  revealed.delete(name);
  clearHideTimer(name);
  void renderCredentialsVault();
}

export async function credentialCopy(name: string): Promise<void> {
  const value = revealed.get(name);
  if (value === undefined) return;
  try {
    if (!window.navigator.clipboard?.writeText) throw new Error('clipboard unavailable');
    await window.navigator.clipboard.writeText(value);
    setMessage(translate('settings.credVaultCopied', '已复制'));
  } catch (error: unknown) {
    console.debug('[CredVault] clipboard write failed', error);
    setMessage(translate('settings.credVaultCopyFail', '复制失败'), 'err');
  }
}

export async function credentialRemove(name: string): Promise<void> {
  const question = translate(
    'settings.credVaultConfirmDelete', '确定删除凭证「{name}」？')
    .replace('{name}', name);
  if (!window.confirm(question)) return;
  try {
    const response = await api().remove(name);
    if (response?.error) {
      setMessage(errorMessage(response.error), 'err');
      return;
    }
    revealed.delete(name);
    clearHideTimer(name);
    await renderCredentialsVault();
  } catch (error: unknown) {
    console.warn('[CredVault] remove failed', error);
    setMessage(translate('settings.credVaultSaveFail', '保存失败'), 'err');
  }
}

const bridge = globals();
bridge._renderCredentialsVault = () => { void renderCredentialsVault(); };
bridge._credentialAdd = () => { void credentialAdd(); };
bridge._credentialReveal = (name) => { void credentialReveal(name); };
bridge._credentialHide = credentialHide;
bridge._credentialCopy = (name) => { void credentialCopy(name); };
bridge._credentialRemove = (name) => { void credentialRemove(name); };
bridge._destroyCredentialsVault = destroyCredentialsVault;
