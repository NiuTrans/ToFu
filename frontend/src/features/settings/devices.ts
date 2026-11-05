import { featureRegistry } from '../../feature-registry';
import { createLifecycleScope, type LifecycleScope } from '../../lifecycle';
import type { I18nKey } from '../../i18n';
import { createButton } from '../../ui/button';

interface ShareRoot {
  name?: string;
  path?: string;
}

interface DesktopAgent {
  agent_id?: string;
  name?: string;
  platform?: string;
  online?: boolean;
  share_roots?: ShareRoot[];
}

interface BridgeToken {
  id?: string;
  name?: string;
  created_at?: number;
}

interface DevicesResponse {
  agents?: DesktopAgent[];
  tokens?: BridgeToken[];
}

/** One pending await_device registration (contracts/device_waits_v1.schema.json publicWait). */
interface DeviceWait {
  waitId?: string;
  conversationId?: string;
  reason?: string;
  createdAt?: number;
  device?: { selector?: string; agentId?: string | null; name?: string | null };
}

interface DeviceWaitPage {
  waits?: DeviceWait[];
  nextCursor?: string | null;
}

interface DesktopApi {
  devices(): Promise<DevicesResponse | null>;
  revokeToken(keyId: string): Promise<unknown>;
  deviceWaits(cursor?: string): Promise<DeviceWaitPage | null>;
  cancelDeviceWait(waitId: string): Promise<unknown>;
}

type DevicesWindow = Window & {
  Api?: { desktop?: DesktopApi };
  t?: (key: string, values?: Record<string, unknown>) => string;
  showToast?: (message: string) => void;
  _populateDevicesTab?: () => void;
  _renderDevicesLoadFailed?: (error?: unknown) => void;
  _renderDeviceAgents?: (agents: readonly DesktopAgent[]) => void;
  _renderDeviceTokens?: (tokens: readonly BridgeToken[]) => void;
  _devicesRevokeToken?: (keyId: string | null, button?: HTMLButtonElement | null) => void;
  _destroyDevicesTab?: () => void;
};

let loadGeneration = 0;
let tokenScope: LifecycleScope | null = null;
let waitScope: LifecycleScope | null = null;

function globals(): DevicesWindow {
  return featureRegistry as unknown as DevicesWindow;
}

function desktopApi(): DesktopApi {
  const api = globals().Api?.desktop;
  if (!api) throw new Error('Desktop API is not ready');
  return api;
}

function translate(key: I18nKey): string {
  return globals().t?.(key) || key;
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

function lanes(): { agents: HTMLElement; tokens: HTMLElement } | null {
  const agents = document.getElementById('devicesAgentsList');
  const tokens = document.getElementById('devicesTokensList');
  return agents instanceof HTMLElement && tokens instanceof HTMLElement
    ? { agents, tokens }
    : null;
}

function loadingLine(): HTMLElement {
  return element('p', 'stg-loading', translate('settings.loading'));
}

/** The waits lane is optional: older fragments carry only agents and tokens. */
function waitsLane(): HTMLElement | null {
  const lane = document.getElementById('devicesWaitsList');
  return lane instanceof HTMLElement ? lane : null;
}

function loadFailedBox(error?: unknown): HTMLElement {
  let detail = '';
  if (error instanceof Error) detail = error.message;
  else if (error && typeof error === 'object' && 'message' in error) {
    detail = String((error as { message?: unknown }).message ?? '');
  }
  const box = element('div', 'devices-load-failed');
  box.append(element('span', 'devices-load-failed-icon', '⚠'));
  const text = element('span', '', translate('devices.loadFailed'));
  if (detail) {
    text.append(' ', element('span', 'devices-load-failed-detail', detail));
  }
  box.append(text);
  return box;
}

function formatCreatedAt(timestamp: number | undefined): string {
  if (!timestamp) return '—';
  const date = new Date(timestamp * 1000);
  return Number.isNaN(date.getTime()) ? '—' : date.toISOString().slice(0, 10);
}

export function renderDevicesLoadFailed(error?: unknown): void {
  const current = lanes();
  if (!current) return;
  tokenScope?.destroy();
  tokenScope = null;
  current.agents.replaceChildren(loadFailedBox(error));
  current.tokens.replaceChildren(loadFailedBox(error));
}

export function renderDeviceAgents(agents: readonly DesktopAgent[]): void {
  const target = document.getElementById('devicesAgentsList');
  if (!(target instanceof HTMLElement)) return;
  if (agents.length === 0) {
    const empty = element('p', 'stg-empty', translate('devices.empty'));
    empty.dataset.i18n = 'devices.empty';
    target.replaceChildren(empty);
    return;
  }

  const table = element('table', 'stg-table');
  const header = document.createElement('thead');
  const headerRow = document.createElement('tr');
  for (const key of [
    'devices.colDevice', 'devices.colPlatform',
    'devices.colRoots', 'devices.colStatus',
  ] satisfies readonly I18nKey[]) headerRow.append(element('th', '', translate(key)));
  header.append(headerRow);

  const body = document.createElement('tbody');
  for (const agent of agents) {
    const online = Boolean(agent.online);
    const row = element(
      'tr', `devices-agent-row${online ? '' : ' devices-offline'}`);
    const id = String(agent.agent_id ?? '');
    const device = document.createElement('td');
    device.append(
      String(agent.name ?? agent.agent_id ?? ''),
      ' ',
      element('span', 'stg-dim', `(${id.slice(0, 8)})`),
    );
    row.append(device, element('td', '', String(agent.platform ?? '—')));

    const roots = (agent.share_roots ?? []).map(
      (root) => String(root.name ?? root.path ?? ''));
    const rootCell = document.createElement('td');
    rootCell.append(roots.length
      ? document.createTextNode(roots.join(', '))
      : element('span', 'stg-dim', '—'));
    row.append(rootCell);

    const status = document.createElement('td');
    if (online) {
      status.append(
        element('span', 'devices-online-dot', '●'),
        ` ${translate('devices.online')}`,
      );
    } else {
      status.append(element(
        'span', 'stg-dim', `○ ${translate('devices.offline')}`));
    }
    row.append(status);
    body.append(row);
  }
  table.append(header, body);
  target.replaceChildren(table);
}

export function renderDeviceTokens(tokens: readonly BridgeToken[]): void {
  const target = document.getElementById('devicesTokensList');
  if (!(target instanceof HTMLElement)) return;
  tokenScope?.destroy();
  const scope = createLifecycleScope();
  tokenScope = scope;

  if (tokens.length === 0) {
    target.replaceChildren(element(
      'p', 'stg-empty', translate('devices.noTokens')));
    return;
  }

  const fragment = document.createDocumentFragment();
  for (const token of tokens) {
    const id = String(token.id ?? '');
    const row = element('div', 'stg-row devices-token-row');
    row.dataset.keyId = id;
    const label = element('span');
    label.style.flex = '1';
    label.append(
      String(token.name ?? token.id ?? ''),
      ' ',
      element('span', 'stg-dim', formatCreatedAt(token.created_at)),
    );
    const revoke = element(
      'button', 'stg-btn stg-btn-danger devices-revoke-btn',
      translate('devices.revoke'));
    revoke.type = 'button';
    revoke.dataset.keyId = id;
    scope.listen(revoke, 'click', () => devicesRevokeToken(id, revoke));
    row.append(label, revoke);
    fragment.append(row);
  }
  target.replaceChildren(fragment);
}

export function renderDeviceWaits(waits: readonly DeviceWait[]): void {
  const target = waitsLane();
  if (!target) return;
  waitScope?.destroy();
  const scope = createLifecycleScope();
  waitScope = scope;

  if (waits.length === 0) {
    const empty = element('p', 'stg-empty', translate('devices.noWaits'));
    empty.dataset.i18n = 'devices.noWaits';
    target.replaceChildren(empty);
    return;
  }

  const fragment = document.createDocumentFragment();
  for (const wait of waits) {
    const id = String(wait.waitId ?? '');
    const device = wait.device ?? {};
    const row = element('div', 'stg-row devices-wait-row');
    row.dataset.waitId = id;
    const label = element('span', 'devices-wait-label');
    label.append(
      element('span', 'devices-wait-device',
        String(device.name || device.selector || device.agentId || '')),
      element('span', 'stg-dim', [
        formatCreatedAt(wait.createdAt ? wait.createdAt / 1000 : undefined),
        String(wait.conversationId ?? ''),
      ].join(' · ')),
      element('span', 'devices-wait-reason', String(wait.reason ?? '')),
    );
    const cancel = createButton({
      label: translate('devices.waitCancel'), variant: 'secondary', size: 'sm',
    });
    cancel.classList.add('devices-wait-cancel-btn');
    cancel.dataset.waitId = id;
    scope.listen(cancel, 'click', () => devicesCancelWait(id, cancel));
    row.append(label, cancel);
    fragment.append(row);
  }
  target.replaceChildren(fragment);
}

export function destroyDevicesTab(): void {
  loadGeneration += 1;
  tokenScope?.destroy();
  tokenScope = null;
  waitScope?.destroy();
  waitScope = null;
}

/** Every pending wait: the server pages its pending index by device, so rows are
 * collected across pages and shown in registration order. */
async function populateDeviceWaits(generation: number): Promise<void> {
  const target = waitsLane();
  if (!target) return;
  waitScope?.destroy();
  waitScope = null;
  target.replaceChildren(loadingLine());
  const waits: DeviceWait[] = [];
  let cursor = '';
  try {
    do {
      const page = await desktopApi().deviceWaits(cursor);
      if (generation !== loadGeneration) return;
      if (!page) {
        target.replaceChildren(loadFailedBox());
        return;
      }
      waits.push(...(page.waits ?? []));
      cursor = page.nextCursor ?? '';
    } while (cursor);
  } catch (error: unknown) {
    if (generation !== loadGeneration) return;
    target.replaceChildren(loadFailedBox(error));
    return;
  }
  renderDeviceWaits(waits.sort((a, b) => (a.createdAt ?? 0) - (b.createdAt ?? 0)));
}

export async function populateDevicesTab(): Promise<void> {
  const current = lanes();
  if (!current) return;
  const generation = ++loadGeneration;
  tokenScope?.destroy();
  tokenScope = null;
  current.agents.replaceChildren(loadingLine());
  current.tokens.replaceChildren(loadingLine());
  const waits = populateDeviceWaits(generation);

  try {
    const response = await desktopApi().devices();
    if (generation === loadGeneration) {
      if (response) {
        renderDeviceAgents(response.agents ?? []);
        renderDeviceTokens(response.tokens ?? []);
      } else {
        renderDevicesLoadFailed();
      }
    }
  } catch (error: unknown) {
    if (generation === loadGeneration) renderDevicesLoadFailed(error);
  }
  await waits;
}

export async function devicesRevokeToken(
  keyId: string | null,
  button: HTMLButtonElement | null = null,
): Promise<void> {
  if (!keyId) return;
  if (button) button.disabled = true;
  try {
    await desktopApi().revokeToken(keyId);
    await populateDevicesTab();
    globals().showToast?.(translate('devices.revoked'));
  } catch {
    if (button?.isConnected) button.disabled = false;
    globals().showToast?.(translate('devices.revokeFailed'));
  }
}

export async function devicesCancelWait(
  waitId: string | null,
  button: HTMLButtonElement | null = null,
): Promise<void> {
  if (!waitId) return;
  if (button) button.disabled = true;
  try {
    await desktopApi().cancelDeviceWait(waitId);
    await populateDevicesTab();
    globals().showToast?.(translate('devices.waitCancelled'));
  } catch {
    if (button?.isConnected) button.disabled = false;
    globals().showToast?.(translate('devices.waitCancelFailed'));
  }
}

const bridge = globals();
bridge._populateDevicesTab = () => { void populateDevicesTab(); };
bridge._renderDevicesLoadFailed = renderDevicesLoadFailed;
bridge._renderDeviceAgents = renderDeviceAgents;
bridge._renderDeviceTokens = renderDeviceTokens;
bridge._devicesRevokeToken = (keyId, button) => {
  void devicesRevokeToken(keyId, button);
};
bridge._destroyDevicesTab = destroyDevicesTab;
