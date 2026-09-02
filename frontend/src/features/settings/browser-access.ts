/**
 * Settings browser-access owner.
 *
 * Renders browser connection, native site-adapter health, read denials, and
 * durable write grants.  An adapter capability mismatch is a recovery state,
 * not a status-only badge: this owner names the browser extension/protocol,
 * translates every missing capability, and opens the canonical Local Control
 * install/upgrade surface.
 */

import { featureRegistry } from '../../feature-registry';
import type { I18nKey } from '../../i18n';
import { createLifecycleScope, type LifecycleScope } from '../../lifecycle';

interface BrowserClient {
  client_id?: string;
  name?: string;
  profile?: string;
  ext_version?: string;
  protocol_version?: number;
  capabilities?: string[];
  last_poll?: number;
}

interface BrowserFallback {
  ok?: boolean;
  host?: string;
}

interface BrowserStatus {
  connected?: boolean;
  clients?: BrowserClient[];
  servedExtVersion?: string;
  clientProtocolVersion?: number;
  capabilities?: string[];
  lastFallback?: BrowserFallback;
}

interface AdapterHealth {
  status?: string;
  healthy?: boolean;
  missing_capabilities?: string[];
  client_id?: string;
  protocol_version?: number;
}

interface BrowserAdapter {
  id?: string;
  name?: string;
  health?: AdapterHealth;
}

interface BrowserAdaptersResponse {
  adapters?: BrowserAdapter[];
  count?: number;
  available_count?: number;
}

interface BrowserWriteGrant {
  domain?: string;
  client_id?: string;
  profile?: string;
}

interface BrowserAccessPolicy {
  read_denied_domains?: string[];
  write_grants?: BrowserWriteGrant[];
}

interface BrowserApi {
  status(): Promise<BrowserStatus | null>;
  adapters(): Promise<BrowserAdaptersResponse | null>;
  access(): Promise<BrowserAccessPolicy | null>;
  updateAccess(body: Record<string, unknown>): Promise<BrowserAccessPolicy | null>;
  test?(): Promise<unknown>;
}

type BrowserAccessWindow = Window & {
  Api?: { browser?: BrowserApi };
  t?: (key: string, values?: Record<string, unknown>) => string;
  showToast?: (message: string) => void;
  closeSettings?: () => void;
  openLocalControlModal?: (options?: { browserUpgrade?: boolean }) => void;
  _renderSearchBrowserAccessOwner?: () => void;
  _testSearchBrowserOwner?: () => Promise<void>;
  _browserAccessDenyReadOwner?: () => Promise<void>;
  _destroyBrowserAccess?: () => void;
};

type TranslationEntry = readonly [I18nKey, string];

const CAPABILITY_LABELS: Readonly<Record<string, TranslationEntry>> = Object.freeze({
  tabs: ['settings.browserCapability.tabs', '标签页管理'],
  navigate: ['settings.browserCapability.navigate', '页面导航'],
  read: ['settings.browserCapability.read', '网页正文读取'],
  snapshot: ['settings.browserCapability.snapshot', '网页结构快照'],
  click: ['settings.browserCapability.click', '点击网页'],
  fill: ['settings.browserCapability.fill', '填写表单'],
  press: ['settings.browserCapability.press', '键盘输入'],
  select: ['settings.browserCapability.select', '选择控件'],
  scroll: ['settings.browserCapability.scroll', '页面滚动'],
  wait: ['settings.browserCapability.wait', '等待页面元素'],
  execute: ['settings.browserCapability.execute', '页面脚本读取'],
  iframes: ['settings.browserCapability.iframes', '内嵌页面读取'],
  network_capture: ['settings.browserCapability.networkCapture', '网络响应读取'],
  network_body: ['settings.browserCapability.networkBody', '网络响应正文读取'],
  deep_collect: ['settings.browserCapability.deepCollect', '深度网页采集'],
  devtools_console: ['settings.browserCapability.devtoolsConsole', 'DevTools 控制台'],
  js_debugger: ['settings.browserCapability.jsDebugger', 'JavaScript 调试器'],
  upload: ['settings.browserCapability.upload', '文件上传'],
  file_export: ['settings.browserCapability.fileExport', '认证文件传入服务端暂存'],
  downloads: ['settings.browserCapability.downloads', '设备下载目录写入'],
  screenshot: ['settings.browserCapability.screenshot', '页面截图'],
});

let renderGeneration = 0;
let viewScope: LifecycleScope | null = null;
let renderedPolicy: BrowserAccessPolicy | null = null;

function globals(): BrowserAccessWindow {
  return featureRegistry as unknown as BrowserAccessWindow;
}

function browserApi(): BrowserApi {
  const api = globals().Api?.browser;
  if (!api) throw new Error('Browser API is not ready');
  return api;
}

function interpolate(
  template: string,
  values: Record<string, unknown> | undefined,
): string {
  if (!values) return template;
  return template.replace(/\{([A-Za-z0-9_]+)\}/g, (token, name: string) => (
    Object.prototype.hasOwnProperty.call(values, name)
      ? String(values[name] ?? '')
      : token
  ));
}

function translate(
  key: I18nKey,
  fallback: string,
  values?: Record<string, unknown>,
): string {
  const translated = globals().t?.(key, values);
  return translated && translated !== key
    ? translated
    : interpolate(fallback, values);
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

function badge(text: string, state: 'on' | 'off' | 'warn' | '' = ''): HTMLSpanElement {
  return element(
    'span', `search-status-badge${state ? ` ${state}` : ''}`, text);
}

function freshestClient(
  status: BrowserStatus,
  requestedClientId = '',
): BrowserClient {
  const clients = Array.isArray(status.clients) ? status.clients : [];
  const exact = requestedClientId
    ? clients.find((client) => String(client.client_id ?? '') === requestedClientId)
    : undefined;
  if (exact) return exact;
  return clients.reduce<BrowserClient>((freshest, client) => (
    Number(client.last_poll ?? 0) > Number(freshest.last_poll ?? 0)
      ? client
      : freshest
  ), {});
}

function capabilityLabel(capability: string): string {
  const normalized = String(capability ?? '').trim();
  const entry = CAPABILITY_LABELS[normalized];
  if (entry) return translate(entry[0], entry[1]);
  return normalized.replaceAll('_', ' ') || translate(
    'settings.browserCapability.newer', '此适配器所需的新版读取能力');
}

export function browserAdapterUpgradeDetail(
  health: AdapterHealth,
  status: BrowserStatus,
): string {
  const capabilities = (health.missing_capabilities ?? [])
    .map(capabilityLabel)
    .filter(Boolean)
    .join(' · ') || translate(
      'settings.browserCapability.newer', '此适配器所需的新版读取能力');
  const client = freshestClient(status, String(health.client_id ?? ''));
  const currentVersion = String(client.ext_version ?? '').trim();
  const servedVersion = String(status.servedExtVersion ?? '').trim();
  if (currentVersion && servedVersion && currentVersion !== servedVersion) {
    return translate(
      'settings.browserAdapterUpgradeVersionTpl',
      '当前浏览器扩展 v{current}，服务器提供 v{target}；缺少：{capabilities}。',
      { current: currentVersion, target: servedVersion, capabilities },
    );
  }
  const reportedProtocolVersion = Number(
    health.protocol_version
      ?? client.protocol_version
      ?? status.clientProtocolVersion
      ?? 1,
  );
  const protocolVersion = (
    Number.isFinite(reportedProtocolVersion) && reportedProtocolVersion > 0
  ) ? reportedProtocolVersion : 1;
  return translate(
    'settings.browserAdapterUpgradeProtocolTpl',
    '当前浏览器扩展（协议 v{version}）缺少：{capabilities}；请升级或重新加载最新版。',
    { version: protocolVersion, capabilities },
  );
}

export function openBrowserExtensionUpgrade(): void {
  const openUpgradeSurface = globals().openLocalControlModal;
  if (typeof openUpgradeSurface !== 'function') {
    globals().showToast?.(translate(
      'settings.browserUpgradeUnavailable',
      '暂时无法打开升级页，请从「本机控制」进入浏览器扩展安装。',
    ));
    return;
  }
  destroyBrowserAccess();
  globals().closeSettings?.();
  openUpgradeSurface({ browserUpgrade: true });
}

function appendAdapterRow(
  target: HTMLElement,
  adapter: BrowserAdapter,
  status: BrowserStatus,
  scope: LifecycleScope,
): void {
  const health = adapter.health ?? {};
  const upgradeRequired = health.status === 'upgrade_required';
  const row = element(
    'div', `auth-source-card browser-adapter-row${upgradeRequired ? ' needs-upgrade' : ''}`);
  const copy = element('div', 'browser-adapter-copy');
  copy.append(element(
    'strong', 'browser-adapter-name', String(adapter.name ?? adapter.id ?? '')));
  if (upgradeRequired) {
    copy.append(element(
      'span', 'browser-adapter-detail',
      browserAdapterUpgradeDetail(health, status)));
  }
  row.append(copy);

  const stateText = health.healthy
    ? translate('settings.browserAdapterReady', '只读可用')
    : upgradeRequired
      ? translate('settings.browserAdapterCapabilityMissing', '扩展缺少能力')
      : health.status === 'error'
        ? translate('settings.browserStatusUnavailable', '浏览器状态不可用')
        : translate('settings.browserOffline', '扩展离线');
  row.append(badge(stateText, health.healthy ? 'on' : upgradeRequired ? 'warn' : 'off'));

  if (upgradeRequired) {
    const upgrade = element(
      'button', 'stg-btn-secondary browser-adapter-upgrade-button',
      translate('settings.browserUpgradeAction', '升级浏览器扩展'));
    upgrade.type = 'button';
    scope.listen(upgrade, 'click', openBrowserExtensionUpgrade);
    row.append(upgrade);
  }
  target.append(row);
}

function appendDeniedDomainRow(
  target: HTMLElement,
  domain: string,
  scope: LifecycleScope,
): void {
  const row = element('div', 'auth-source-card');
  row.append(
    element('strong', '', domain),
    element('span', '', translate(
      'settings.browserReadDenied', '浏览器读取已拒绝')),
  );
  const restore = element(
    'button', 'stg-btn-secondary',
    translate('settings.browserRestore', '恢复'));
  restore.type = 'button';
  scope.listen(restore, 'click', () => {
    void browserAccessAllowRead(domain).catch((error: unknown) => {
      console.warn('[BrowserAccess] restore failed', error);
      globals().showToast?.(error instanceof Error && error.message
        ? error.message
        : translate('settings.browserStatusUnavailable', '浏览器状态不可用'));
    });
  });
  row.append(restore);
  target.append(row);
}

function appendWriteGrantRow(
  target: HTMLElement,
  grant: BrowserWriteGrant,
  scope: LifecycleScope,
): void {
  const domain = String(grant.domain ?? '');
  const clientId = String(grant.client_id ?? '');
  const profile = String(grant.profile ?? '');
  const row = element('div', 'auth-source-card');
  row.append(
    element('strong', '', domain),
    element('span', '', translate(
      'settings.browserWriteGrantTpl', '长期写授权 · {browser}',
      { browser: profile || clientId })),
  );
  const revoke = element(
    'button', 'stg-btn-secondary',
    translate('settings.browserRevoke', '撤销'));
  revoke.type = 'button';
  scope.listen(revoke, 'click', () => {
    void browserAccessRevoke(domain, clientId, profile).catch((error: unknown) => {
      console.warn('[BrowserAccess] revoke failed', error);
      globals().showToast?.(error instanceof Error && error.message
        ? error.message
        : translate('settings.browserStatusUnavailable', '浏览器状态不可用'));
    });
  });
  row.append(revoke);
  target.append(row);
}

function paintConnection(statusBox: HTMLElement, status: BrowserStatus): void {
  statusBox.replaceChildren();
  if (!status.connected) {
    statusBox.append(badge(
      translate('settings.browserOffline', '扩展离线'), 'off'));
    return;
  }
  const client = freshestClient(status);
  statusBox.append(
    badge(translate(
      'settings.browserConnectedTpl', '已连接 · {name}',
      {
        name: client.profile || client.name || client.client_id
          || translate('settings.browserConnection', '浏览器连接'),
      }), 'on'),
    badge(translate(
      'settings.browserProtocolTpl', '协议 v{version}',
      {
        version: String(
          client.protocol_version ?? status.clientProtocolVersion ?? 1),
      })),
  );
  const capabilities = new Set(
    (client.capabilities ?? status.capabilities ?? []).map(String));
  if (capabilities.has('devtools_console') && capabilities.has('js_debugger')) {
    statusBox.append(badge(translate(
      'settings.browserDevtoolsReady', 'DevTools Bridge 就绪'), 'on'));
  }
}

function paintSummary(
  summary: HTMLElement,
  status: BrowserStatus,
  adapters: BrowserAdaptersResponse,
): void {
  let text = translate(
    'settings.browserAdapterSummaryTpl',
    '{available} / {total} 个站点适配器可用',
    {
      available: Number(adapters.available_count ?? 0),
      total: Number(adapters.count ?? 0),
    },
  );
  const fallback = status.lastFallback;
  if (fallback) {
    text += translate(
      'settings.browserLastFallbackTpl',
      ' · 最近浏览器兜底：{status}{host}',
      {
        status: fallback.ok
          ? translate('settings.browserFallbackSuccess', '成功')
          : translate('settings.browserFallbackFailure', '失败'),
        host: fallback.host ? ` (${fallback.host})` : '',
      },
    );
  }
  summary.textContent = text;
}

function paintAccessRows(
  target: HTMLElement,
  status: BrowserStatus,
  adapters: BrowserAdaptersResponse,
  policy: BrowserAccessPolicy,
  scope: LifecycleScope,
): void {
  target.replaceChildren();
  for (const adapter of adapters.adapters ?? []) {
    appendAdapterRow(target, adapter, status, scope);
  }
  for (const domain of policy.read_denied_domains ?? []) {
    appendDeniedDomainRow(target, String(domain), scope);
  }
  for (const grant of policy.write_grants ?? []) {
    appendWriteGrantRow(target, grant, scope);
  }
  if (!target.childElementCount) {
    target.append(element(
      'div', 'auth-src-empty',
      translate('settings.browserAccessEmpty', '尚未发现可用适配器或授权。')));
  }
}

export function destroyBrowserAccess(): void {
  renderGeneration += 1;
  viewScope?.destroy();
  viewScope = null;
  renderedPolicy = null;
}

export async function renderSearchBrowserAccess(): Promise<void> {
  const statusBox = document.getElementById('searchBrowserStatus');
  const summary = document.getElementById('searchAdapterSummary');
  const accessBox = document.getElementById('browserAccessList');
  if (!(statusBox instanceof HTMLElement) && !(accessBox instanceof HTMLElement)) return;

  destroyBrowserAccess();
  const generation = renderGeneration;
  const scope = createLifecycleScope();
  viewScope = scope;
  try {
    const [statusValue, adaptersValue, policyValue] = await Promise.all([
      browserApi().status(), browserApi().adapters(), browserApi().access(),
    ]);
    if (generation !== renderGeneration || viewScope !== scope) return;
    const status = statusValue ?? {};
    const adapters = adaptersValue ?? {};
    const policy = policyValue ?? {};
    renderedPolicy = policy;
    if (statusBox instanceof HTMLElement) paintConnection(statusBox, status);
    if (summary instanceof HTMLElement) paintSummary(summary, status, adapters);
    if (accessBox instanceof HTMLElement) {
      paintAccessRows(accessBox, status, adapters, policy, scope);
    }
  } catch (error: unknown) {
    if (generation !== renderGeneration || viewScope !== scope) return;
    console.warn('[BrowserAccess] render failed', error);
    if (statusBox instanceof HTMLElement) {
      statusBox.replaceChildren(badge(translate(
        'settings.browserStatusUnavailable', '浏览器状态不可用'), 'off'));
    }
    if (summary instanceof HTMLElement) {
      summary.textContent = translate(
        'settings.browserStatusUnavailable', '浏览器状态不可用');
    }
  }
}

export async function testSearchBrowser(): Promise<void> {
  const test = browserApi().test;
  if (typeof test !== 'function') return;
  await test.call(browserApi());
  await renderSearchBrowserAccess();
}

export async function browserAccessDenyRead(): Promise<void> {
  const input = document.getElementById('browserReadDenyInput');
  if (!(input instanceof HTMLInputElement)) return;
  const domain = input.value.trim();
  if (!domain) return;
  try {
    const policy = renderedPolicy ?? await browserApi().access() ?? {};
    const domains = [...(policy.read_denied_domains ?? [])];
    if (!domains.includes(domain)) domains.push(domain);
    await browserApi().updateAccess({ read_denied_domains: domains });
    input.value = '';
    await renderSearchBrowserAccess();
  } catch (error: unknown) {
    console.warn('[BrowserAccess] deny read failed', error);
    globals().showToast?.(error instanceof Error && error.message
      ? error.message
      : translate('settings.browserStatusUnavailable', '浏览器状态不可用'));
  }
}

async function browserAccessAllowRead(domain: string): Promise<void> {
  const policy = await browserApi().access() ?? {};
  await browserApi().updateAccess({
    read_denied_domains: (policy.read_denied_domains ?? []).filter(
      (candidate) => candidate !== domain),
  });
  await renderSearchBrowserAccess();
}

async function browserAccessRevoke(
  domain: string,
  clientId: string,
  profile: string,
): Promise<void> {
  await browserApi().updateAccess({
    revoke: { domain, client_id: clientId, profile },
  });
  await renderSearchBrowserAccess();
}

const bridge = globals();
bridge._renderSearchBrowserAccessOwner = () => {
  void renderSearchBrowserAccess();
};
bridge._testSearchBrowserOwner = testSearchBrowser;
bridge._browserAccessDenyReadOwner = browserAccessDenyRead;
bridge._destroyBrowserAccess = destroyBrowserAccess;
