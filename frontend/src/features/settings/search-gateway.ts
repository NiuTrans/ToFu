/* Settings → 搜索 → 搜索 API card.
 *
 * Renders the keyed search gateway (lib/search/search_gateway.py) from the
 * server-config projection `search.gateway` + `search.gateway_catalog`, and
 * collects the save payload. The card is shown only when a gateway catalog is
 * shipped (release-tiered bundle or user drop-in) or a custom gateway is
 * already saved. The API key is write-only: the projection carries `has_key`
 * and a hint, and an empty key field keeps the stored credential.
 *
 * Copy is Chinese-only on purpose: the card exists only for bundle tiers that
 * ship a gateway catalog. Entry points: renderSearchGateway (called from the
 * retained search tab populate), collectSearchGatewayPayload and
 * markSearchGatewayKeySaved (save path).
 */
import { registerAction } from '../../action-registry';
import { featureRegistry } from '../../feature-registry';

export interface SearchGatewaySource {
  id: string;
  name?: string;
  default?: boolean;
  note?: string;
}

export interface SearchGatewayDescription {
  id: string;
  name?: string;
  key_label?: string;
  key_hint?: string;
  sources: SearchGatewaySource[];
}

export interface SearchGatewaySettings {
  enabled?: boolean;
  id?: string;
  endpoint?: string;
  sources?: string[];
  full_content?: boolean;
  free_engines?: string;
  has_key?: boolean;
  key_hint?: string;
}

export interface SearchGatewayPayload {
  enabled: boolean;
  id: string;
  sources: string[];
  free_engines: string;
  full_content: boolean;
  endpoint?: string;
  api_key?: string;
}

interface SearchSettingsProjection {
  gateway?: SearchGatewaySettings;
  gateway_catalog?: SearchGatewayDescription[];
}

const FREE_ENGINE_SUMMARY: Record<string, string> = {
  fallback: '免费引擎只在搜索 API 没有相关结果时运行',
  parallel: '免费引擎同时并行运行',
  off: '不使用免费引擎',
};

let catalog: SearchGatewayDescription[] = [];
let saved: SearchGatewaySettings = {};

function byId<T extends HTMLInputElement | HTMLSelectElement>(
  id: string, type: new () => T,
): T | null {
  const element = document.getElementById(id);
  return element instanceof type ? element : null;
}

function selectedGateway(): SearchGatewayDescription | undefined {
  const id = byId('settingSearchGatewayId', HTMLSelectElement)?.value || '';
  return catalog.find((gateway) => gateway.id === id);
}

function checkedSources(): string[] {
  return Array.from(document.querySelectorAll<HTMLInputElement>(
    '#settingSearchGatewaySources input[type="checkbox"]'))
    .filter((input) => input.checked)
    .map((input) => input.value);
}

function endpointHost(endpoint: string): string {
  try {
    return new URL(endpoint).host;
  } catch {
    return endpoint;
  }
}

function renderSources(): void {
  const box = document.getElementById('settingSearchGatewaySources');
  if (!box) return;
  const gateway = selectedGateway();
  const gatewayId = gateway ? gateway.id : '';
  const savedHere = (saved.id || '') === gatewayId && Array.isArray(saved.sources)
    && saved.sources.length > 0;
  const chosen = new Set(savedHere
    ? saved.sources
    : (gateway?.sources || []).filter((source) => source.default).map((source) => source.id));
  const sources = [...(gateway?.sources || [])];
  for (const id of savedHere ? saved.sources || [] : []) {
    if (!sources.some((source) => source.id === id)) sources.push({ id });
  }
  box.replaceChildren();
  for (const source of sources) {
    const label = document.createElement('label');
    label.className = 'search-gateway-source';
    const input = document.createElement('input');
    input.type = 'checkbox';
    input.value = source.id;
    input.checked = chosen.has(source.id);
    input.addEventListener('change', refreshSearchGatewayStatus);
    const text = document.createElement('span');
    text.textContent = source.name || source.id;
    // Wire id and catalog note stay one hover away; inline they buried the
    // picker under per-source trivia.
    label.title = source.note ? `${source.id} · ${source.note}` : source.id;
    label.append(input, text);
    box.append(label);
  }
}

function renderKeyField(): void {
  const gateway = selectedGateway();
  const label = document.getElementById('settingSearchGatewayKeyLabel');
  if (label) label.textContent = gateway?.key_label || 'API Key';
  const input = byId('settingSearchGatewayKey', HTMLInputElement);
  if (input) {
    input.value = '';
    input.placeholder = saved.has_key
      ? `已保存（${saved.key_hint || '****'}），留空保持不变`
      : `粘贴${gateway?.key_label || ' API Key'}`;
  }
  const hint = document.getElementById('settingSearchGatewayKeyHint');
  if (hint) hint.textContent = gateway?.key_hint || '';
}

export function refreshSearchGatewayStatus(): void {
  const status = document.getElementById('searchGatewayStatus');
  if (!status) return;
  const enabled = byId('settingSearchGatewayEnabled', HTMLInputElement)?.checked === true;
  const typedKey = (byId('settingSearchGatewayKey', HTMLInputElement)?.value || '').trim();
  const sources = checkedSources();
  const policy = byId('settingSearchFreeEngines', HTMLSelectElement)?.value || 'fallback';
  if (!enabled) {
    status.textContent = '未启用：搜索只使用免费引擎。';
  } else if (!saved.has_key && !typedKey) {
    status.textContent = '还差 API Key：填写并保存后才会生效。';
  } else if (!sources.length) {
    status.textContent = '请至少勾选一个来源。';
  } else {
    status.textContent = `保存后立即生效：每次搜索并行查询 ${sources.length} 个来源；`
      + `${FREE_ENGINE_SUMMARY[policy] || FREE_ENGINE_SUMMARY.fallback}。`;
  }
}

export function searchGatewayChanged(): void {
  renderSources();
  renderKeyField();
  refreshSearchGatewayStatus();
}

export function renderSearchGateway(search: SearchSettingsProjection | undefined): void {
  const card = document.getElementById('searchGatewayCard');
  const select = byId('settingSearchGatewayId', HTMLSelectElement);
  if (!card || !select) return;
  catalog = Array.isArray(search?.gateway_catalog) ? search.gateway_catalog : [];
  saved = search?.gateway && typeof search.gateway === 'object' ? search.gateway : {};
  const customEndpoint = saved.endpoint && !catalog.some((gateway) => gateway.id === saved.id)
    ? saved.endpoint : '';
  card.hidden = !catalog.length && !customEndpoint;
  if (customEndpoint) card.dataset.customEndpoint = customEndpoint;
  else delete card.dataset.customEndpoint;
  if (card.hidden) return;

  select.replaceChildren();
  for (const gateway of catalog) {
    select.append(new Option(gateway.name || gateway.id, gateway.id));
  }
  if (customEndpoint) select.append(new Option(`自定义：${endpointHost(customEndpoint)}`, ''));
  select.value = customEndpoint ? '' : (
    catalog.some((gateway) => gateway.id === saved.id) ? saved.id || '' : catalog[0].id);

  const enabled = byId('settingSearchGatewayEnabled', HTMLInputElement);
  if (enabled) enabled.checked = saved.enabled === true;
  const freeEngines = byId('settingSearchFreeEngines', HTMLSelectElement);
  if (freeEngines) freeEngines.value = saved.free_engines || 'fallback';
  const content = byId('settingSearchGatewayContent', HTMLSelectElement);
  if (content) content.value = saved.full_content ? 'provider' : 'fetch';
  searchGatewayChanged();
}

/** Save payload for `search.gateway`, or null when the card is not in use (the
 *  server then keeps the saved gateway untouched). */
export function collectSearchGatewayPayload(): SearchGatewayPayload | null {
  const card = document.getElementById('searchGatewayCard');
  if (!card || card.hidden) return null;
  const payload: SearchGatewayPayload = {
    enabled: byId('settingSearchGatewayEnabled', HTMLInputElement)?.checked === true,
    id: byId('settingSearchGatewayId', HTMLSelectElement)?.value || '',
    sources: checkedSources(),
    free_engines: byId('settingSearchFreeEngines', HTMLSelectElement)?.value || 'fallback',
    full_content: byId('settingSearchGatewayContent', HTMLSelectElement)?.value === 'provider',
  };
  if (!payload.id && card.dataset.customEndpoint) payload.endpoint = card.dataset.customEndpoint;
  const key = (byId('settingSearchGatewayKey', HTMLInputElement)?.value || '').trim();
  if (key) payload.api_key = key;
  return payload;
}

/** Acknowledge only the submitted key for the still-selected gateway. */
export function markSearchGatewayKeySaved(payload: SearchGatewayPayload): void {
  if (!payload.api_key) return;
  const current = collectSearchGatewayPayload();
  if (!current || current.id !== payload.id || current.endpoint !== payload.endpoint
      || current.api_key !== payload.api_key) return;
  saved = { ...saved, has_key: true, key_hint: saved.key_hint || '****' };
  renderKeyField();
  refreshSearchGatewayStatus();
}

registerAction('_searchGatewayChanged', searchGatewayChanged);
registerAction('_refreshSearchGatewayStatus', refreshSearchGatewayStatus);
(featureRegistry as unknown as { _renderSearchGateway?: typeof renderSearchGateway })
  ._renderSearchGateway = renderSearchGateway;
