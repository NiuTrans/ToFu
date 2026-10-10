import { featureRegistry } from '../../feature-registry';
import type { I18nKey } from '../../i18n';
import { createLifecycleScope, type LifecycleScope } from '../../lifecycle';

interface SitePackOperation {
  name?: string;
  description?: string;
  access?: string;
}

interface SitePackRow {
  id?: string;
  name?: string;
  version?: number;
  domains?: string[];
  layer?: string;
  writable?: boolean;
  auth_projected?: boolean;
  operations?: SitePackOperation[];
}

interface SitePackListResponse {
  packs?: SitePackRow[];
}

interface SitePackMutationResponse {
  error?: { message?: unknown } | string;
}

interface SitePacksApi {
  list(): Promise<SitePackListResponse | null>;
  install(body: unknown): Promise<SitePackMutationResponse | null>;
  remove(packId: string): Promise<unknown>;
}

type SitePacksWindow = Window & {
  Api?: { sitePacks?: SitePacksApi };
  t?: (key: string, values?: Record<string, unknown>) => string;
  _renderSitePacks?: () => void;
  _sitePackInstall?: () => void;
  _sitePackRemove?: (packId: string) => void;
  _destroySitePacks?: () => void;
};

let viewScope: LifecycleScope | null = null;
let renderGeneration = 0;

function globals(): SitePacksWindow {
  return featureRegistry as unknown as SitePacksWindow;
}

function sitePacksApi(): SitePacksApi {
  const api = globals().Api?.sitePacks;
  if (!api) throw new Error('Site-packs API is not ready');
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
  const message = document.getElementById('sitePackMsg');
  if (!message) return;
  message.textContent = text;
  message.className = `site-pack-msg${className ? ` ${className}` : ''}`;
}

function currentInput(): HTMLTextAreaElement | null {
  const input = document.getElementById('sitePackInput');
  return input instanceof HTMLTextAreaElement ? input : null;
}

function serverErrorMessage(error: SitePackMutationResponse['error']): string {
  if (typeof error === 'string') return error;
  if (error && typeof error.message === 'string') return error.message;
  return String(error);
}

function layerLabel(layer: string): string {
  if (layer === 'user') return translate('settings.sitePackLayerUser', '用户');
  if (layer === 'bundle') return translate('settings.sitePackLayerBundle', '发行');
  return translate('settings.sitePackLayerCore', '内置');
}

function appendPackRow(
  box: HTMLElement,
  row: SitePackRow,
  scope: LifecycleScope,
): void {
  const packId = String(row.id ?? '');
  const rowElement = makeElement('div', 'site-pack-row');
  rowElement.id = `sitePackRow_${packId.replace(/[^a-zA-Z0-9]/g, '_')}`;

  const main = makeElement('div', 'site-pack-main');
  const layer = String(row.layer ?? 'core');
  const title = `${String(row.name ?? packId)} · ${packId}`;
  main.append(
    makeElement('span', `site-pack-layer ${layer}`, layerLabel(layer)),
    makeElement('span', 'site-pack-name', title),
  );
  const operations = Array.isArray(row.operations) ? row.operations : [];
  const summary = operations
    .map((op) => `${String(op.name ?? '')}${op.access === 'write' ? ' ✎' : ''}`)
    .filter(Boolean)
    .join(' · ');
  if (summary) {
    main.append(makeElement('span', 'site-pack-ops', summary));
  }

  const actions = makeElement('div', 'site-pack-actions');
  if (row.writable) {
    const remove = makeElement(
      'button', 'site-pack-btn danger',
      translate('settings.sitePackRemove', '移除'));
    remove.type = 'button';
    scope.listen(remove, 'click', () => sitePackRemove(packId));
    actions.append(remove);
  }
  rowElement.append(main, actions);
  box.append(rowElement);
}

function appendImportRow(box: HTMLElement, scope: LifecycleScope): void {
  const importRow = makeElement('div', 'site-pack-import');
  const input = makeElement('textarea', 'site-pack-input');
  input.id = 'sitePackInput';
  input.rows = 5;
  input.spellcheck = false;
  input.placeholder = translate(
    'settings.sitePackImportPh',
    '{"pack":"tofu.site-pack/v1","id":"my-site","name":"…","domains":["…"],"operations":[…]}');

  const controls = makeElement('div', 'site-pack-import-controls');
  const install = makeElement(
    'button', 'site-pack-btn primary',
    translate('settings.sitePackInstall', '校验并安装'));
  install.type = 'button';
  scope.listen(install, 'click', sitePackInstall);
  const message = makeElement('div', 'site-pack-msg');
  message.id = 'sitePackMsg';
  controls.append(install, message);
  importRow.append(input, controls);
  box.append(importRow);
}

function paintPacks(
  box: HTMLElement,
  packs: readonly SitePackRow[],
  scope: LifecycleScope,
): void {
  box.replaceChildren();
  if (packs.length === 0) {
    box.append(makeElement(
      'div', 'site-pack-empty',
      translate('settings.sitePackEmpty', '尚未注册任何站点包。')));
  } else {
    for (const pack of packs) appendPackRow(box, pack, scope);
  }
  appendImportRow(box, scope);
}

export function destroySitePacks(): void {
  renderGeneration += 1;
  viewScope?.destroy();
  viewScope = null;
}

export async function renderSitePacks(): Promise<void> {
  const box = document.getElementById('sitePacksList');
  if (!(box instanceof HTMLElement)) return;

  destroySitePacks();
  const generation = renderGeneration;
  const scope = createLifecycleScope();
  viewScope = scope;
  box.replaceChildren(makeElement(
    'div', 'site-pack-loading', translate('common.loading', '加载中…')));

  try {
    const response = await sitePacksApi().list();
    if (generation !== renderGeneration || viewScope !== scope
        || document.getElementById('sitePacksList') !== box) return;
    paintPacks(box, response?.packs ?? [], scope);
  } catch (error: unknown) {
    if (generation !== renderGeneration || viewScope !== scope
        || document.getElementById('sitePacksList') !== box) return;
    console.warn('[SitePacks] list failed', error);
    box.replaceChildren(makeElement(
      'div', 'site-pack-empty',
      translate('settings.sitePackLoadFail', '加载失败')));
  }
}

export async function sitePackInstall(): Promise<void> {
  const input = currentInput();
  if (!input) return;
  const text = input.value.trim();
  if (!text) {
    setMessage(translate('settings.sitePackNeedJson', '请粘贴站点包 JSON。'), 'err');
    return;
  }
  let manifest: unknown;
  try {
    manifest = JSON.parse(text);
  } catch {
    setMessage(translate('settings.sitePackBadJson', 'JSON 解析失败，请检查格式。'), 'err');
    return;
  }
  setMessage(translate('settings.sitePackInstalling', '校验并安装中…'));
  try {
    const response = await sitePacksApi().install(manifest);
    if (currentInput() !== input) return;
    if (response?.error) {
      setMessage(serverErrorMessage(response.error), 'err');
      return;
    }
    input.value = '';
    setMessage(translate('settings.sitePackInstalled', '已安装，热生效。'), 'ok');
    await renderSitePacks();
  } catch (error: unknown) {
    if (currentInput() !== input) return;
    console.warn('[SitePacks] install failed', error);
    setMessage(
      error instanceof Error && error.message
        ? error.message
        : translate('settings.sitePackInstallFail', '安装失败'),
      'err',
    );
  }
}

export async function sitePackRemove(packId: string): Promise<void> {
  try {
    await sitePacksApi().remove(packId);
    await renderSitePacks();
  } catch (error: unknown) {
    console.warn('[SitePacks] remove failed', error);
    setMessage(translate('settings.sitePackInstallFail', '安装失败'), 'err');
  }
}

const bridge = globals();
bridge._renderSitePacks = () => { void renderSitePacks(); };
bridge._sitePackInstall = () => { void sitePackInstall(); };
bridge._sitePackRemove = (packId) => { void sitePackRemove(packId); };
bridge._destroySitePacks = destroySitePacks;
