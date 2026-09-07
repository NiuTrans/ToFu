import { featureRegistry } from '../../feature-registry';
import type { I18nKey } from '../../i18n';
import { createLifecycleScope, type LifecycleScope } from '../../lifecycle';

interface PrivateHostRow {
  host?: string;
  enabled?: boolean;
}

interface PrivateHostListResponse {
  hosts?: PrivateHostRow[];
}

interface PrivateHostMutationResponse {
  error?: { message?: unknown } | string;
}

interface PrivateHostsApi {
  list(): Promise<PrivateHostListResponse | null>;
  upsert(body: { host: string }): Promise<PrivateHostMutationResponse | null>;
  toggle(host: string, enabled: boolean): Promise<unknown>;
  remove(host: string): Promise<unknown>;
}

type PrivateHostsWindow = Window & {
  Api?: { privateHosts?: PrivateHostsApi };
  t?: (key: string, values?: Record<string, unknown>) => string;
  _renderPrivateHosts?: () => void;
  _privateHostAdd?: () => void;
  _privateHostToggle?: (host: string, enabled: boolean) => void;
  _privateHostRemove?: (host: string) => void;
  _destroyPrivateHosts?: () => void;
};

let viewScope: LifecycleScope | null = null;
let renderGeneration = 0;

function globals(): PrivateHostsWindow {
  return featureRegistry as unknown as PrivateHostsWindow;
}

function privateHostsApi(): PrivateHostsApi {
  const api = globals().Api?.privateHosts;
  if (!api) throw new Error('Private-host API is not ready');
  return api;
}

function translate(key: I18nKey, fallback: string): string {
  const translated = globals().t?.(key);
  return translated || fallback;
}

function makeElement<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  className: string,
  text = '',
): HTMLElementTagNameMap[K] {
  const element = document.createElement(tag);
  element.className = className;
  if (text) element.textContent = text;
  return element;
}

function setMessage(text: string, className = ''): void {
  const message = document.getElementById('privHostMsg');
  if (!message) return;
  message.textContent = text;
  message.className = `priv-host-msg${className ? ` ${className}` : ''}`;
}

function currentInput(): HTMLInputElement | null {
  const input = document.getElementById('privHostInput');
  return input instanceof HTMLInputElement ? input : null;
}

function serverErrorMessage(error: PrivateHostMutationResponse['error']): string {
  if (typeof error === 'string') return error;
  if (error && typeof error.message === 'string') return error.message;
  return String(error);
}

function appendHostRow(
  box: HTMLElement,
  row: PrivateHostRow,
  scope: LifecycleScope,
): void {
  const host = String(row.host ?? '');
  const enabled = Boolean(row.enabled);
  const stateClass = enabled ? 'on' : 'off';
  const rowElement = makeElement('div', 'priv-host-row');
  rowElement.id = `privHostRow_${host.replace(/[^a-zA-Z0-9]/g, '_')}`;

  const main = makeElement('div', 'priv-host-main');
  main.append(
    makeElement('span', `priv-host-dot ${stateClass}`),
    makeElement('span', 'priv-host-name', host),
    makeElement(
      'span',
      `priv-host-state ${stateClass}`,
      enabled
        ? translate('settings.privHostAllowed', '已放行')
        : translate('settings.privHostPaused', '已停用'),
    ),
  );

  const actions = makeElement('div', 'priv-host-actions');
  const toggle = makeElement(
    'button',
    'priv-host-btn',
    enabled
      ? translate('settings.privHostDisable', '停用')
      : translate('settings.privHostEnable', '启用'),
  );
  toggle.type = 'button';
  scope.listen(toggle, 'click', () => privateHostToggle(host, !enabled));

  const remove = makeElement(
    'button', 'priv-host-btn danger',
    translate('settings.privHostRemove', '移除'));
  remove.type = 'button';
  scope.listen(remove, 'click', () => privateHostRemove(host));
  actions.append(toggle, remove);
  rowElement.append(main, actions);
  box.append(rowElement);
}

function appendAddRow(box: HTMLElement, scope: LifecycleScope): void {
  const addRow = makeElement('div', 'priv-host-add');
  const input = makeElement('input', 'priv-host-input');
  input.type = 'text';
  input.id = 'privHostInput';
  input.placeholder = translate(
    'settings.privHostPlaceholder', 'llm-gateway.example.com');

  const add = makeElement(
    'button', 'priv-host-btn primary',
    translate('settings.privHostAdd', '添加'));
  add.type = 'button';
  scope.listen(input, 'keydown', (event) => {
    if (event instanceof KeyboardEvent && event.key === 'Enter') {
      event.preventDefault();
      privateHostAdd();
    }
  });
  scope.listen(add, 'click', privateHostAdd);

  const message = makeElement('div', 'priv-host-msg');
  message.id = 'privHostMsg';
  addRow.append(input, add, message);
  box.append(addRow);
}

function paintHosts(
  box: HTMLElement,
  hosts: readonly PrivateHostRow[],
  scope: LifecycleScope,
): void {
  box.replaceChildren();
  if (hosts.length === 0) {
    box.append(makeElement(
      'div', 'priv-host-empty',
      translate('settings.privateHostsEmpty', '尚未放行任何内网主机。')));
  } else {
    for (const host of hosts) appendHostRow(box, host, scope);
  }
  appendAddRow(box, scope);
}

export function destroyPrivateHosts(): void {
  renderGeneration += 1;
  viewScope?.destroy();
  viewScope = null;
}

export async function renderPrivateHosts(): Promise<void> {
  const box = document.getElementById('privateHostsList');
  if (!(box instanceof HTMLElement)) return;

  destroyPrivateHosts();
  const generation = renderGeneration;
  const scope = createLifecycleScope();
  viewScope = scope;
  box.replaceChildren(makeElement(
    'div', 'priv-host-loading', translate('common.loading', '加载中…')));

  try {
    const response = await privateHostsApi().list();
    if (generation !== renderGeneration || viewScope !== scope
        || document.getElementById('privateHostsList') !== box) return;
    paintHosts(box, response?.hosts ?? [], scope);
  } catch (error: unknown) {
    if (generation !== renderGeneration || viewScope !== scope
        || document.getElementById('privateHostsList') !== box) return;
    console.warn('[PrivHosts] list failed', error);
    box.replaceChildren(makeElement(
      'div', 'priv-host-empty',
      translate('settings.privateHostsLoadFail', '加载失败')));
  }
}

export async function privateHostAdd(): Promise<void> {
  const input = currentInput();
  if (!input) return;
  const host = input.value.trim();
  if (!host) {
    setMessage(translate('settings.privHostNeedHost', '请输入主机名。'), 'err');
    return;
  }
  setMessage(translate('common.saving', '保存中…'));
  try {
    const response = await privateHostsApi().upsert({ host });
    if (currentInput() !== input) return;
    if (response?.error) {
      setMessage(serverErrorMessage(response.error), 'err');
      return;
    }
    input.value = '';
    setMessage('');
    await renderPrivateHosts();
  } catch (error: unknown) {
    if (currentInput() !== input) return;
    console.warn('[PrivHosts] upsert failed', error);
    setMessage(
      error instanceof Error && error.message
        ? error.message
        : translate('settings.privHostSaveFail', '保存失败'),
      'err',
    );
  }
}

export async function privateHostToggle(host: string, enabled: boolean): Promise<void> {
  try {
    await privateHostsApi().toggle(host, enabled);
    await renderPrivateHosts();
  } catch (error: unknown) {
    console.warn('[PrivHosts] toggle failed', error);
    setMessage(translate('settings.privHostSaveFail', '保存失败'), 'err');
  }
}

export async function privateHostRemove(host: string): Promise<void> {
  try {
    await privateHostsApi().remove(host);
    await renderPrivateHosts();
  } catch (error: unknown) {
    console.warn('[PrivHosts] remove failed', error);
    setMessage(translate('settings.privHostSaveFail', '保存失败'), 'err');
  }
}

const bridge = globals();
bridge._renderPrivateHosts = () => { void renderPrivateHosts(); };
bridge._privateHostAdd = () => { void privateHostAdd(); };
bridge._privateHostToggle = (host, enabled) => {
  void privateHostToggle(host, enabled);
};
bridge._privateHostRemove = (host) => { void privateHostRemove(host); };
bridge._destroyPrivateHosts = destroyPrivateHosts;
