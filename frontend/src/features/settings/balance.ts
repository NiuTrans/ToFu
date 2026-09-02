import { featureRegistry } from '../../feature-registry';
import { createLifecycleScope, type LifecycleScope } from '../../lifecycle';
import type { I18nKey } from '../../i18n';

interface BalanceInfo {
  limit_usd?: number;
  used_usd?: number;
  balance_usd?: number;
  currency?: string;
  balance_local?: number;
  granted_balance?: number;
  is_available?: boolean;
  raw?: unknown;
  [key: string]: unknown;
}

interface BalanceResponse {
  ok?: boolean;
  error?: string;
  balance?: BalanceInfo;
}

interface BalanceProvider {
  balance_url?: string;
  base_url?: string;
  api_keys?: string[];
  enabled?: boolean;
  [key: string]: unknown;
}

interface BalanceApi {
  balance(body: {
    balance_url: string;
    api_key: string;
  }): Promise<BalanceResponse | null>;
}

interface BalanceCacheEntry {
  info: BalanceInfo;
  ts: number;
}

type BalanceWindow = Window & {
  Api?: { providers?: BalanceApi };
  t?: (key: string, values?: Record<string, unknown>) => string;
  Icon?: (name: string, size?: number) => string;
  escapeHtml?: (value: unknown) => string;
  debugLog?: (message: string, level?: string) => void;
  _balanceCache?: Record<number, BalanceCacheEntry>;
  _checkProviderBalance?: (providerIndex: number) => void;
  _renderBalanceInfo?: (info: BalanceInfo) => string;
  _fmtBalanceBadge?: (info: BalanceInfo) => string | null;
  _updateBalanceBadge?: (providerIndex: number, info: BalanceInfo) => void;
  _startBalancePolling?: () => void;
  _stopBalancePolling?: () => void;
  _pollAllBalances?: () => void;
  _checkProviderBalanceSilent?: (providerIndex: number) => void;
  _stgProviders: BalanceProvider[];
  _guessBalanceUrl?: (baseUrl: string) => string;
  _renderProvidersTab?: () => void;
};

const BALANCE_POLL_INTERVAL_MS = 3 * 60 * 1000;
const BALANCE_CACHE_FRESH_MS = 2 * 60 * 1000;
const BALANCE_STAGGER_MS = 500;

const balanceCache: Record<number, BalanceCacheEntry> = {};
let pollingScope: LifecycleScope | null = null;

function globals(): BalanceWindow {
  return featureRegistry as unknown as BalanceWindow;
}

function translate(key: I18nKey, values?: Record<string, unknown>): string {
  return globals().t?.(key, values) || key;
}

function escape(value: unknown): string {
  const helper = globals().escapeHtml;
  if (helper) return helper(value);
  const node = document.createElement('span');
  node.textContent = String(value ?? '');
  return node.innerHTML;
}

function icon(name: string, size: number): string {
  return globals().Icon?.(name, size) || '';
}

function providersApi(): BalanceApi {
  const api = globals().Api?.providers;
  if (!api) throw new Error('Provider API is not ready');
  return api;
}

function providerAt(index: number): BalanceProvider | null {
  return globals()._stgProviders[index] ?? null;
}

function providerIsCurrent(index: number, provider: BalanceProvider): boolean {
  return providerAt(index) === provider;
}

function currentResult(
  index: number,
  expected?: HTMLElement,
): HTMLElement | null {
  const result = document.getElementById(`stgBalanceResult_${index}`);
  if (!(result instanceof HTMLElement)) return null;
  if (expected && result !== expected) return null;
  return result;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function renderResultMessage(
  result: HTMLElement,
  className: string,
  message: string,
  prefix = '',
): void {
  result.innerHTML = `<span class="${className}">${prefix}${escape(message)}</span>`;
}

export async function checkProviderBalance(providerIndex: number): Promise<void> {
  const provider = providerAt(providerIndex);
  const result = currentResult(providerIndex);
  if (!provider || !result) return;

  const balanceUrl = provider.balance_url
    || globals()._guessBalanceUrl?.(provider.base_url || '');
  if (!balanceUrl) {
    renderResultMessage(
      result, 'stg-balance-err', translate('settings.balNoUrl'));
    return;
  }
  const apiKey = provider.api_keys?.[0];
  if (!apiKey) {
    renderResultMessage(
      result, 'stg-balance-err', translate('settings.balNoKey'));
    return;
  }

  result.innerHTML = `<span class="stg-balance-loading">${icon('hourglass', 12)} ${escape(translate('settings.balLoading'))}</span>`;
  try {
    const response = await providersApi().balance({
      balance_url: balanceUrl,
      api_key: apiKey,
    });
    const liveResult = currentResult(providerIndex, result);
    if (!providerIsCurrent(providerIndex, provider) || !liveResult) return;
    if (!response?.ok) {
      renderResultMessage(
        liveResult,
        'stg-balance-err',
        response?.error || translate('settings.balUnknownError'),
        '❌ ',
      );
      return;
    }

    const info = response.balance ?? {};
    liveResult.innerHTML = renderBalanceInfo(info);
    balanceCache[providerIndex] = { info, ts: Date.now() };
    updateBalanceBadge(providerIndex, info);
    if (!provider.balance_url) {
      provider.balance_url = balanceUrl;
      globals()._renderProvidersTab?.();
    }
  } catch (error: unknown) {
    const liveResult = currentResult(providerIndex, result);
    if (!providerIsCurrent(providerIndex, provider) || !liveResult) return;
    renderResultMessage(
      liveResult,
      'stg-balance-err',
      translate('settings.balNetworkError', { error: errorMessage(error) }),
      '❌ ',
    );
  }
}

export function renderBalanceInfo(info: BalanceInfo): string {
  let html = '<div class="stg-balance-info">';
  if (info.limit_usd != null && info.used_usd != null) {
    const used = info.used_usd;
    const limit = info.limit_usd;
    const remaining = info.balance_usd ?? (limit - used);
    const percent = limit > 0 ? Math.round((used / limit) * 100) : 0;
    const barColor = percent > 90
      ? '#ef4444'
      : percent > 70 ? '#f59e0b' : '#22c55e';
    html += '<div class="stg-balance-bar-wrap">'
      + `<div class="stg-balance-bar" style="width:${Math.min(percent, 100)}%;background:${barColor}"></div>`
      + '</div>';
    html += '<div class="stg-balance-nums">'
      + `<span>${escape(translate('settings.balUsed'))}: <b>$${used.toFixed(2)}</b></span>`
      + `<span>${escape(translate('settings.balRemaining'))}: <b>$${remaining.toFixed(2)}</b></span>`
      + `<span>${escape(translate('settings.balLimit'))}: <b>$${limit.toFixed(2)}</b></span>`
      + '</div>';
  } else if (info.balance_usd != null) {
    const balance = info.balance_usd;
    const color = balance > 10
      ? '#22c55e'
      : balance > 2 ? '#f59e0b' : '#ef4444';
    const currency = escape(info.currency || '');
    html += '<div class="stg-balance-nums">';
    html += `<span>${escape(translate('settings.balBalance'))}: <b style="color:${color}">$${balance.toFixed(2)}</b></span>`;
    if (info.currency && info.currency !== 'USD'
        && info.balance_local != null) {
      html += `<span>（${currency} ${info.balance_local.toFixed(2)}）</span>`;
    }
    if (info.granted_balance != null) {
      html += `<span>${escape(translate('settings.balGranted'))}: ${currency} ${info.granted_balance.toFixed(2)}</span>`;
    }
    if (info.is_available === false) {
      html += `<span style="color:#ef4444;font-weight:800">⚠ ${escape(translate('settings.balInsufficient'))}</span>`;
    }
    html += '</div>';
  } else {
    const raw = info.raw ?? info;
    html += `<span class="stg-balance-raw">${escape(JSON.stringify(raw))}</span>`;
  }
  return `${html}</div>`;
}

export function formatBalanceBadge(info: BalanceInfo): string | null {
  let balance: number | null = null;
  if (info.balance_usd != null) balance = info.balance_usd;
  else if (info.limit_usd != null && info.used_usd != null) {
    balance = info.limit_usd - info.used_usd;
  }
  if (balance == null) return null;
  if (balance >= 1000) return `$${(balance / 1000).toFixed(1)}k`;
  if (balance >= 100) return `$${Math.round(balance)}`;
  return `$${balance.toFixed(2)}`;
}

export function updateBalanceBadge(
  providerIndex: number,
  info: BalanceInfo,
): void {
  const card = document.querySelector(
    `.stg-provider-card[data-prov-idx="${providerIndex}"]`);
  const badges = card?.querySelector('.stg-provider-badges');
  if (!(badges instanceof HTMLElement)) return;
  badges.querySelector('.stg-badge-balance')?.remove();

  const text = formatBalanceBadge(info);
  if (!text) return;
  const balance = info.balance_usd
    ?? (info.limit_usd != null
      ? info.limit_usd - (info.used_usd || 0)
      : null);
  const colorClass = balance == null
    ? 'ok'
    : balance > 10 ? 'ok' : balance > 2 ? 'warn' : 'low';
  const badge = document.createElement('span');
  badge.className = `stg-badge stg-badge-balance stg-badge-bal-${colorClass}`;
  badge.innerHTML = `${icon('chart', 11)} ${escape(text)}`;
  badge.title = translate('settings.balanceClickRefresh');
  badge.style.cursor = 'pointer';
  badge.addEventListener('click', (event) => {
    event.stopPropagation();
    void checkProviderBalance(providerIndex);
  });
  badges.append(badge);
}

export function stopBalancePolling(): void {
  pollingScope?.destroy();
  pollingScope = null;
}

export function pollAllBalances(): void {
  const scope = pollingScope;
  if (!scope) return;
  for (let index = 0; index < globals()._stgProviders.length; index += 1) {
    const provider = providerAt(index);
    if (!provider?.balance_url || !provider.api_keys?.[0]
        || provider.enabled === false) continue;
    const cached = balanceCache[index];
    if (cached && Date.now() - cached.ts < BALANCE_CACHE_FRESH_MS) {
      updateBalanceBadge(index, cached.info);
      continue;
    }
    scope.timeout(() => {
      if (pollingScope !== scope || !providerIsCurrent(index, provider)) return;
      void checkProviderBalanceSilent(index, provider, scope);
    }, index * BALANCE_STAGGER_MS);
  }
}

export function startBalancePolling(): void {
  stopBalancePolling();
  const scope = createLifecycleScope();
  pollingScope = scope;
  pollAllBalances();
  scope.interval(() => {
    if (pollingScope === scope) pollAllBalances();
  }, BALANCE_POLL_INTERVAL_MS);
}

export async function checkProviderBalanceSilent(
  providerIndex: number,
  expectedProvider?: BalanceProvider,
  owner?: LifecycleScope,
): Promise<void> {
  const provider = providerAt(providerIndex);
  if (!provider || (expectedProvider && provider !== expectedProvider)
      || !provider.balance_url || !provider.api_keys?.[0]) return;
  try {
    const response = await providersApi().balance({
      balance_url: provider.balance_url,
      api_key: provider.api_keys[0],
    });
    if ((owner && pollingScope !== owner)
        || !providerIsCurrent(providerIndex, provider)
        || !response?.ok) return;
    const info = response.balance ?? {};
    balanceCache[providerIndex] = { info, ts: Date.now() };
    updateBalanceBadge(providerIndex, info);
    const result = currentResult(providerIndex);
    if (result && result.offsetParent !== null) {
      result.innerHTML = renderBalanceInfo(info);
    }
  } catch (error: unknown) {
    if (owner && pollingScope !== owner) return;
    globals().debugLog?.(
      `[Balance] Silent poll failed for provider ${providerIndex}: ${errorMessage(error)}`,
      'debug',
    );
  }
}

const bridge = globals();
bridge._balanceCache = balanceCache;
bridge._checkProviderBalance = (index) => { void checkProviderBalance(index); };
bridge._renderBalanceInfo = renderBalanceInfo;
bridge._fmtBalanceBadge = formatBalanceBadge;
bridge._updateBalanceBadge = updateBalanceBadge;
bridge._startBalancePolling = startBalancePolling;
bridge._stopBalancePolling = stopBalancePolling;
bridge._pollAllBalances = pollAllBalances;
bridge._checkProviderBalanceSilent = (index) => {
  void checkProviderBalanceSilent(index);
};
