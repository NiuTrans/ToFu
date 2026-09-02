import { featureRegistry } from '../../feature-registry';
import { createLifecycleScope, type LifecycleScope } from '../../lifecycle';
import type { I18nKey } from '../../i18n';

interface KeyStatRow {
  total?: number;
  success?: number;
  failure?: number;
  rate_limited?: number;
  gateway_errors?: number;
  consecutive_429?: number;
  success_rate?: number | null;
  enabled?: boolean;
  auto_disabled?: boolean;
  exhausted?: boolean;
  last_resort?: boolean;
  override?: boolean | null;
  exhausted_models?: Record<string, string>;
  last_error?: string;
}

interface KeyStatsCache {
  day: string;
  providers: Record<string, Record<string, KeyStatRow>>;
  min_attempts: number;
  min_success_rate: number;
}

interface ProviderModel {
  model_id?: string;
  request_ids?: string[];
  aliases?: string[];
}

interface SettingsProvider {
  id?: string;
  brand?: string;
  models?: ProviderModel[];
}

interface ModelHealthRow {
  slots?: number;
  available_slots?: number;
  total_requests?: number;
  total_errors?: number;
  contention_errors?: number;
  gateway_errors?: number;
  consecutive_errors?: number;
  inflight?: number;
  cooldown_remaining_s?: number;
  cooldown_reason?: string;
  last_error_msg?: string;
  last_error_ts?: number;
}

interface RuntimeHealthVerdict {
  level?: string;
}

interface ModelHealthAggregate {
  slots: number;
  available_slots: number;
  total_requests: number;
  total_errors: number;
  contention_errors: number;
  gateway_errors: number;
  consecutive_errors: number;
  inflight: number;
  cooldown_remaining_s: number;
  cooldown_reason: string;
  last_error_msg: string;
  last_error_ts: number;
  success_rate: number | null;
  verdict: RuntimeHealthVerdict | null;
}

interface DispatchApi {
  keyStats(): Promise<Partial<KeyStatsCache> | null>;
  keyOverride(body: {
    provider_id: string;
    key_name: string;
    enabled: boolean | null;
  }): Promise<{ row?: KeyStatRow } | null>;
  modelHealth(): Promise<{
    providers?: Record<string, Record<string, ModelHealthRow>>;
  } | null>;
}

type ModelHealthCache = Record<string, Record<string, ModelHealthRow>>;

type KeyStatsWindow = Window & {
  Api?: { dispatch?: DispatchApi };
  t?: (key: string, values?: Record<string, unknown>) => string;
  escapeHtml?: (value: unknown) => string;
  debugLog?: (message: string, level?: string) => void;
  foldRuntimeHealth?: (
    rows: Array<ModelHealthRow & { wire_id: string }>,
  ) => RuntimeHealthVerdict;
  _modelHealthCache?: ModelHealthCache;
  _modelHealthTs?: number;
  _loadKeyStats?: () => Promise<KeyStatsCache>;
  _fmtSuccessRate?: (rate: number | null | undefined) => string;
  _keyStatsClass?: (row: KeyStatRow | null) => string;
  _getKeyStatRow?: (providerId: string, keyName: string) => KeyStatRow | null;
  _getKeyStatRowFor?: (providerIndex: number, keyIndex: number) => KeyStatRow | null;
  _keyStatsHelpText?: (isLocal: boolean) => string;
  _renderKeyCardStatsHTML?: (providerIndex: number, keyIndex: number) => string;
  _renderProviderKeyStats?: (providerIndex: number) => void;
  _onKeyToggle?: (providerIndex: number, keyIndex: number, enabled: boolean) => void;
  _onKeyClearOverride?: (providerIndex: number, keyIndex: number) => void;
  _loadModelHealth?: () => Promise<ModelHealthCache>;
  _startModelHealthPolling?: () => void;
  _stopModelHealthPolling?: () => void;
  _modelWireIds?: (model: ProviderModel) => string[];
  _modelCardHealthRow?: (
    providerIndex: number,
    modelIndex: number,
  ) => ModelHealthAggregate | null;
  _modelCardHealthHTML?: (providerIndex: number, modelIndex: number) => string;
  _modelCardHealthCls?: (providerIndex: number, modelIndex: number) => string;
  _refreshAllModelCardHealth?: () => void;
  _destroyKeyStats?: () => void;
  _stgProviders: SettingsProvider[];
  _keyStatsCache: KeyStatsCache;
  _keyStatsLoading: boolean;
};

const MODEL_HEALTH_POLL_MS = 10_000;
const HEALTH_REASON_KEYS: Record<string, I18nKey> = {
  rate_limit: 'settings.mhReasonRateLimit',
  upstream: 'settings.mhReasonUpstream',
  error: 'settings.mhReasonError',
  quota: 'settings.mhReasonQuota',
  contention: 'settings.mhReasonContention',
};

const modelHealthCache: ModelHealthCache = {};
let modelHealthTimestamp = 0;
let healthScope: LifecycleScope | null = null;
let keyActionScope: LifecycleScope | null = null;
let keyStatsRequest: Promise<KeyStatsCache> | null = null;
let keyStatsRequestGeneration = -1;
let modelHealthRequest: Promise<ModelHealthCache> | null = null;
let modelHealthRequestOwner: LifecycleScope | undefined;
let generation = 0;

function globals(): KeyStatsWindow {
  return featureRegistry as unknown as KeyStatsWindow;
}

function dispatchApi(): DispatchApi {
  const api = globals().Api?.dispatch;
  if (!api) throw new Error('Dispatch API is not ready');
  return api;
}

function translate(key: I18nKey, values?: Record<string, unknown>): string {
  return globals().t?.(key, values) || key;
}

function escape(value: unknown): string {
  const helper = globals().escapeHtml;
  if (helper) return helper(value);
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

function logFailure(message: string, error: unknown, level: string): void {
  const detail = error instanceof Error ? error.message : String(error);
  globals().debugLog?.(`${message}${detail}`, level);
}

function providerAt(index: number): SettingsProvider | null {
  return globals()._stgProviders[index] ?? null;
}

function replaceRecord<T>(target: Record<string, T>, source: Record<string, T>): void {
  for (const key of Object.keys(target)) delete target[key];
  Object.assign(target, source);
}

function ensureKeyActionScope(): void {
  if (keyActionScope) return;
  const root = document.getElementById('settingsModal');
  if (!(root instanceof HTMLElement)) return;
  const scope = createLifecycleScope();
  keyActionScope = scope;
  scope.listen(root, 'change', (event) => {
    const input = event.target;
    if (!(input instanceof HTMLInputElement)
        || input.dataset.keyAction !== 'toggle') return;
    const providerIndex = Number.parseInt(input.dataset.providerIndex || '', 10);
    const keyIndex = Number.parseInt(input.dataset.keyIndex || '', 10);
    if (Number.isNaN(providerIndex) || Number.isNaN(keyIndex)) return;
    void onKeyToggle(providerIndex, keyIndex, input.checked);
  });
  scope.listen(root, 'click', (event) => {
    const target = event.target instanceof Element
      ? event.target.closest<HTMLElement>('[data-key-action="clear"]')
      : null;
    if (!target) return;
    const providerIndex = Number.parseInt(target.dataset.providerIndex || '', 10);
    const keyIndex = Number.parseInt(target.dataset.keyIndex || '', 10);
    if (Number.isNaN(providerIndex) || Number.isNaN(keyIndex)) return;
    void onKeyClearOverride(providerIndex, keyIndex);
  });
}

export function destroyKeyStats(): void {
  generation += 1;
  keyActionScope?.destroy();
  keyActionScope = null;
}

export async function loadKeyStats(): Promise<KeyStatsCache> {
  ensureKeyActionScope();
  if (keyStatsRequest && keyStatsRequestGeneration === generation) {
    return keyStatsRequest;
  }
  const requestGeneration = generation;
  globals()._keyStatsLoading = true;
  const request = dispatchApi().keyStats().then((data) => {
    if (requestGeneration === generation && data && typeof data === 'object') {
      globals()._keyStatsCache = {
        day: data.day || '',
        providers: data.providers || {},
        min_attempts: data.min_attempts || 5,
        min_success_rate: data.min_success_rate || 0.5,
      };
    }
    return globals()._keyStatsCache;
  }).catch((error: unknown) => {
    logFailure('[Settings] Failed to load key stats: ', error, 'warning');
    return globals()._keyStatsCache;
  }).finally(() => {
    globals()._keyStatsLoading = false;
    if (keyStatsRequest === request) {
      keyStatsRequest = null;
      keyStatsRequestGeneration = -1;
    }
    if (requestGeneration === generation) {
      for (let index = 0; index < globals()._stgProviders.length; index += 1) {
        renderProviderKeyStats(index);
      }
    }
  });
  keyStatsRequest = request;
  keyStatsRequestGeneration = requestGeneration;
  return request;
}

export function formatSuccessRate(rate: number | null | undefined): string {
  return rate == null ? '—' : `${Math.round(rate * 100)}%`;
}

export function keyStatsClass(row: KeyStatRow | null): string {
  if (!row) return 'stg-keystat-idle';
  if (row.exhausted && row.override !== true) return 'stg-keystat-exhausted';
  if (!row.enabled) return 'stg-keystat-disabled';
  if (row.auto_disabled) return 'stg-keystat-warn';
  if (row.success_rate == null) return 'stg-keystat-idle';
  if (row.success_rate >= 0.9) return 'stg-keystat-good';
  if (row.success_rate >= globals()._keyStatsCache.min_success_rate) return 'stg-keystat-ok';
  return 'stg-keystat-warn';
}

export function getKeyStatRow(
  providerId: string,
  keyName: string,
): KeyStatRow | null {
  const providers = globals()._keyStatsCache.providers || {};
  const provider = providers[providerId] || providers.default;
  return provider?.[keyName] ?? null;
}

export function getKeyStatRowFor(
  providerIndex: number,
  keyIndex: number,
): KeyStatRow | null {
  const provider = providerAt(providerIndex);
  if (!provider) return null;
  const providerId = provider.id || 'default';
  return getKeyStatRow(providerId, `${providerId}_key_${keyIndex}`);
}

export function keyStatsHelpText(isLocal: boolean): string {
  const base = translate(
    isLocal ? 'settings.apiKeysHintLocal' : 'settings.apiKeysHint');
  if (!globals()._keyStatsCache.day) return base;
  const percent = Math.round(
    (globals()._keyStatsCache.min_success_rate || 0.5) * 100);
  return `${base}\n\n${translate('settings.keyStatAutoDisablePolicy', {
    day: globals()._keyStatsCache.day,
    pct: percent,
  })}`;
}

export function renderKeyCardStatsHtml(
  providerIndex: number,
  keyIndex: number,
): string {
  const row = getKeyStatRowFor(providerIndex, keyIndex);
  const total = row?.total || 0;
  const success = row?.success || 0;
  const failure = row?.failure || 0;
  const rateLimited = row?.rate_limited || 0;
  const gatewayErrors = row?.gateway_errors || 0;
  const consecutive429 = row?.consecutive_429 || 0;
  const rateText = row ? formatSuccessRate(row.success_rate) : '—';
  const enabled = row ? Boolean(row.enabled) : true;
  const autoOff = Boolean(row?.auto_disabled && row.override == null);
  const exhausted = Boolean(row?.exhausted);
  const lastResort = Boolean(row?.last_resort);

  let badge = '';
  if (row?.override === false) {
    badge = `<span class="stg-keystat-badge off">${escape(
      translate('settings.keyStatOverrideOff'))}</span>`;
  } else if (row?.override === true) {
    badge = `<span class="stg-keystat-badge on">${escape(
      translate('settings.keyStatOverrideOn'))}</span>`;
  } else if (lastResort) {
    badge = `<span class="stg-keystat-badge warn" title="${escape(
      translate('settings.keyStatLastResortTip'))}">${escape(
        translate('settings.keyStatLastResort'))}</span>`;
  } else if (exhausted) {
    badge = `<span class="stg-keystat-badge warn" title="${escape(
      translate('settings.keyStatExhaustedTip'))}">${escape(
        translate('settings.keyStatExhausted'))}</span>`;
  } else if (autoOff) {
    badge = `<span class="stg-keystat-badge warn">${escape(
      translate('settings.keyStatAutoOff'))}</span>`;
  }

  let streakBadge = '';
  if (!exhausted && consecutive429 >= 10) {
    streakBadge = `<span class="stg-keystat-badge warn" title="${escape(
      translate('settings.keyStat429StreakTip'))}">${escape(
        translate('settings.keyStat429Streak', { n: consecutive429 }))}</span>`;
  }

  const exhaustedModels = row?.exhausted_models
    ? Object.keys(row.exhausted_models)
    : [];
  let exhaustedModelsBadge = '';
  if (exhaustedModels.length && row?.exhausted_models) {
    const reasons = exhaustedModels.map((model) => {
      const reason = String(row.exhausted_models?.[model] || '').slice(0, 80);
      return `${model}${reason ? `: ${reason}` : ''}`;
    }).join('\n');
    exhaustedModelsBadge = `<span class="stg-keystat-badge warn" title="${escape(
      translate('settings.keyStatModelExhaustedTip', { reasons }))}">${escape(
        translate('settings.keyStatModelExhausted', {
          models: exhaustedModels.join(', '),
        }))}</span>`;
  }
  const conflictBadge = row?.override === true
      && (exhausted || exhaustedModels.length)
    ? `<span class="stg-keystat-badge warn" title="${escape(
      translate('settings.keyStatOverrideVsExhaustedTip'))}">${escape(
        translate('settings.keyStatOverrideVsExhausted'))}</span>`
    : '';
  const showError = row?.last_error && (failure > 0 || exhausted);
  const lastError = showError
    ? `<span class="stg-keystat-err" title="${escape(row.last_error)}">${escape(
      translate('settings.keyStatLastError'))}</span>`
    : '';
  const rateTitle = total > 0
    ? translate('settings.keyStatRateTip', { succ: success, total })
    : translate('settings.keyStatNoCallsTip');
  const countChip = total > 0
    ? `<span class="stg-keystat-count" title="${escape(
      translate('settings.keyStatCountTip'))}">${escape(
        translate('settings.keyStatCount', { n: total }))}</span>`
    : `<span class="stg-keystat-count" title="${escape(
      translate('settings.keyStatNoCallsTip'))}">—</span>`;
  const metric = (
    className: string,
    titleKey: I18nKey,
    valueKey: I18nKey,
    value: number,
  ): string => value > 0
    ? `<span class="${className}" title="${escape(translate(titleKey))}">${escape(
      translate(valueKey, { n: value }))}</span>`
    : '';
  const reset = row?.override != null
    ? `<button type="button" class="stg-btn-link" title="${escape(
      translate('settings.keyStatClearOverrideTip'))}" data-key-action="clear"
       data-provider-index="${providerIndex}" data-key-index="${keyIndex}">${escape(
         translate('settings.keyStatReset'))}</button>`
    : '';
  return `<span class="stg-keystat-metrics">
      <span class="stg-keystat-rate" title="${escape(rateTitle)}">${rateText}</span>
      ${countChip}
      ${metric('stg-keystat-fail', 'settings.keyStatFailTip', 'settings.keyStatFail', failure)}
      ${metric('stg-keystat-429', 'settings.keyStat429Tip', 'settings.keyStat429', rateLimited)}
      ${metric('stg-keystat-gateway', 'settings.keyStatGatewayTip', 'settings.keyStatGateway', gatewayErrors)}
    </span>${streakBadge}${exhaustedModelsBadge}${conflictBadge}${badge}${lastError}
    <span class="stg-keystat-actions">
      <label class="stg-toggle stg-key-toggle" title="${escape(
        translate('settings.keyStatToggleTip'))}">
        <input type="checkbox" ${enabled ? 'checked' : ''} data-key-action="toggle"
               data-provider-index="${providerIndex}" data-key-index="${keyIndex}">
        <span class="stg-toggle-track"><span class="stg-toggle-thumb"></span></span>
      </label>${reset}
    </span>`;
}

export function renderProviderKeyStats(providerIndex: number): void {
  const card = document.querySelector(
    `.stg-provider-card[data-prov-idx="${providerIndex}"]`);
  const field = card?.querySelector(
    `.stg-keys-field[data-prov-idx="${providerIndex}"]`);
  if (!(field instanceof HTMLElement)) return;
  const provider = providerAt(providerIndex);
  const info = field.querySelector('.stg-keys-info');
  if (info) {
    const text = keyStatsHelpText(provider?.brand === 'local');
    info.setAttribute('title', text);
    info.setAttribute('aria-label', text);
  }
  const cards = field.querySelectorAll<HTMLElement>('.stg-key-card');
  cards.forEach((keyCard, keyIndex) => {
    const stateClass = keyStatsClass(
      getKeyStatRowFor(providerIndex, keyIndex));
    const classes = keyCard.className.split(/\s+/).filter(
      (name) => name && !name.startsWith('stg-keystat-'));
    classes.push(stateClass);
    keyCard.className = classes.join(' ');
    const stats = keyCard.querySelector('.stg-key-card-stats');
    if (stats instanceof HTMLElement) {
      stats.innerHTML = renderKeyCardStatsHtml(providerIndex, keyIndex);
    }
  });
}

function storeOverrideRow(
  providerIndex: number,
  provider: SettingsProvider,
  providerId: string,
  keyName: string,
  row: KeyStatRow,
): void {
  if (providerAt(providerIndex) !== provider) return;
  globals()._keyStatsCache.providers[providerId] ??= {};
  globals()._keyStatsCache.providers[providerId][keyName] = row;
  renderProviderKeyStats(providerIndex);
}

export async function onKeyToggle(
  providerIndex: number,
  keyIndex: number,
  enabled: boolean,
): Promise<void> {
  const provider = providerAt(providerIndex);
  if (!provider) return;
  const providerId = provider.id || 'default';
  const keyName = `${providerId}_key_${keyIndex}`;
  try {
    const response = await dispatchApi().keyOverride({
      provider_id: providerId,
      key_name: keyName,
      enabled: Boolean(enabled),
    });
    if (response?.row) {
      storeOverrideRow(
        providerIndex, provider, providerId, keyName, response.row);
    }
  } catch (error: unknown) {
    logFailure('[Settings] Key toggle failed: ', error, 'error');
    if (providerAt(providerIndex) === provider) renderProviderKeyStats(providerIndex);
  }
}

export async function onKeyClearOverride(
  providerIndex: number,
  keyIndex: number,
): Promise<void> {
  const provider = providerAt(providerIndex);
  if (!provider) return;
  const providerId = provider.id || 'default';
  const keyName = `${providerId}_key_${keyIndex}`;
  try {
    const response = await dispatchApi().keyOverride({
      provider_id: providerId,
      key_name: keyName,
      enabled: null,
    });
    if (response?.row) {
      storeOverrideRow(
        providerIndex, provider, providerId, keyName, response.row);
    }
  } catch (error: unknown) {
    logFailure('[Settings] Key override clear failed: ', error, 'error');
  }
}

export async function loadModelHealth(
  owner?: LifecycleScope,
): Promise<ModelHealthCache> {
  if (modelHealthRequest && modelHealthRequestOwner === owner) {
    return modelHealthRequest;
  }
  const request = dispatchApi().modelHealth().then((data) => {
    if (owner && healthScope !== owner) return modelHealthCache;
    if (data?.providers) {
      replaceRecord(modelHealthCache, data.providers);
      modelHealthTimestamp = Date.now();
      globals()._modelHealthTs = modelHealthTimestamp;
    }
    return modelHealthCache;
  }).catch((error: unknown) => {
    if (!owner || healthScope === owner) {
      logFailure('[Settings] Failed to load model health: ', error, 'warning');
    }
    return modelHealthCache;
  }).finally(() => {
    if (modelHealthRequest === request) {
      modelHealthRequest = null;
      modelHealthRequestOwner = undefined;
    }
    if (!owner || healthScope === owner) refreshAllModelCardHealth();
  });
  modelHealthRequest = request;
  modelHealthRequestOwner = owner;
  return request;
}

export function startModelHealthPolling(): void {
  if (healthScope) return;
  const scope = createLifecycleScope();
  healthScope = scope;
  void loadModelHealth(scope);
  scope.interval(() => {
    if (healthScope === scope
        && (!modelHealthRequest || modelHealthRequestOwner !== scope)) {
      void loadModelHealth(scope);
    }
  }, MODEL_HEALTH_POLL_MS);
}

export function stopModelHealthPolling(): void {
  healthScope?.destroy();
  healthScope = null;
}

export function modelWireIds(model: ProviderModel): string[] {
  if (model.request_ids?.length) return model.request_ids.slice();
  const ids = model.model_id ? [model.model_id] : [];
  return ids.concat(model.aliases || []);
}

export function modelCardHealthRow(
  providerIndex: number,
  modelIndex: number,
): ModelHealthAggregate | null {
  const provider = providerAt(providerIndex);
  const model = provider?.models?.[modelIndex];
  if (!model) return null;
  const rows = modelHealthCache[provider.id || 'default'] || {};
  const ids = modelWireIds(model);
  const elapsed = modelHealthTimestamp
    ? (Date.now() - modelHealthTimestamp) / 1000
    : 0;
  let aggregate: ModelHealthAggregate | null = null;
  for (const id of ids) {
    const row = rows[id];
    if (!row) continue;
    aggregate ??= {
      slots: 0,
      available_slots: 0,
      total_requests: 0,
      total_errors: 0,
      contention_errors: 0,
      gateway_errors: 0,
      consecutive_errors: 0,
      inflight: 0,
      cooldown_remaining_s: 0,
      cooldown_reason: '',
      last_error_msg: '',
      last_error_ts: 0,
      success_rate: null,
      verdict: null,
    };
    aggregate.slots += row.slots || 0;
    aggregate.available_slots += row.available_slots || 0;
    aggregate.total_requests += row.total_requests || 0;
    aggregate.total_errors += row.total_errors || 0;
    aggregate.contention_errors += row.contention_errors || 0;
    aggregate.gateway_errors += row.gateway_errors || 0;
    aggregate.inflight += row.inflight || 0;
    aggregate.consecutive_errors = Math.max(
      aggregate.consecutive_errors, row.consecutive_errors || 0);
    const remaining = Math.max(
      0, (row.cooldown_remaining_s || 0) - elapsed);
    if (remaining > aggregate.cooldown_remaining_s) {
      aggregate.cooldown_remaining_s = remaining;
      aggregate.cooldown_reason = row.cooldown_reason || '';
    }
    if ((row.last_error_ts || 0) > aggregate.last_error_ts) {
      aggregate.last_error_ts = row.last_error_ts || 0;
      aggregate.last_error_msg = row.last_error_msg || '';
    }
  }
  if (!aggregate) return null;
  aggregate.success_rate = aggregate.total_requests >= 3
    ? Math.max(0, 1 - aggregate.total_errors / aggregate.total_requests)
    : null;
  const fold = globals().foldRuntimeHealth;
  if (fold) {
    aggregate.verdict = fold(ids.flatMap((id) => {
      const row = rows[id];
      return row ? [{
        ...row,
        wire_id: id,
        available_slots: row.available_slots || 0,
        total_requests: row.total_requests || 0,
        total_errors: row.total_errors || 0,
        cooldown_reason: row.cooldown_reason || '',
        last_error_msg: row.last_error_msg || '',
      }] : [];
    }));
  }
  return aggregate;
}

export function modelCardHealthHtml(
  providerIndex: number,
  modelIndex: number,
): string {
  if (!modelHealthTimestamp) return '';
  const aggregate = modelCardHealthRow(providerIndex, modelIndex);
  if (!aggregate
      || (aggregate.total_requests === 0
          && aggregate.cooldown_remaining_s <= 0)) {
    return `<span class="stg-mh-chip muted">${escape(
      translate('settings.mhNoTraffic'))}</span>`;
  }
  let html = '';
  if (aggregate.cooldown_remaining_s > 0) {
    const reasonKey = HEALTH_REASON_KEYS[aggregate.cooldown_reason];
    const reason = reasonKey
      ? translate(reasonKey)
      : aggregate.cooldown_reason;
    html += `<span class="stg-mh-chip cool" title="${escape(
      aggregate.last_error_msg)}">⏳ ${escape(translate(
        'settings.mhCooldown', {
          s: Math.ceil(aggregate.cooldown_remaining_s),
        }))}${reason ? ` · ${escape(reason)}` : ''}</span>`;
  }
  if (aggregate.success_rate != null) {
    const percent = Math.round(aggregate.success_rate * 100);
    const className = percent >= 98 ? 'good' : percent >= 90 ? 'ok' : 'warn';
    html += `<span class="stg-mh-chip ${className}" title="${escape(
      translate('settings.mhRequestsTip', {
        n: aggregate.total_requests,
      }))}">${escape(translate('settings.mhSuccessRate'))} ${percent}%</span>`;
  }
  if (aggregate.contention_errors > 0) {
    html += `<span class="stg-mh-chip muted" title="${escape(
      translate('settings.mhContentionTip'))}">${escape(translate(
        'settings.mhContention', {
          n: aggregate.contention_errors,
        }))}</span>`;
  }
  if (aggregate.gateway_errors > 0) {
    html += `<span class="stg-mh-chip muted" title="${escape(
      translate('settings.mhGatewayTip'))}">${escape(translate(
        'settings.mhGateway', {
          n: aggregate.gateway_errors,
        }))}</span>`;
  }
  if (aggregate.cooldown_remaining_s <= 0
      && aggregate.consecutive_errors > 0) {
    html += `<span class="stg-mh-chip warn" title="${escape(
      aggregate.last_error_msg)}">${escape(translate(
        'settings.mhConsecErrors', {
          n: aggregate.consecutive_errors,
        }))}</span>`;
  }
  if (aggregate.inflight > 0) {
    html += `<span class="stg-mh-chip muted">${escape(translate(
      'settings.mhInflight', { n: aggregate.inflight }))}</span>`;
  }
  return html;
}

export function modelCardHealthClass(
  providerIndex: number,
  modelIndex: number,
): string {
  const aggregate = modelCardHealthRow(providerIndex, modelIndex);
  if (!aggregate) return 'muted';
  if (aggregate.cooldown_remaining_s > 0) return 'cool';
  if (aggregate.verdict) {
    if (aggregate.verdict.level === 'ok') return 'good';
    if (aggregate.verdict.level === 'degraded') return 'ok';
    if (aggregate.verdict.level === 'down') return 'warn';
    return 'muted';
  }
  if (aggregate.success_rate != null && aggregate.success_rate < 0.9) {
    return 'warn';
  }
  if (aggregate.consecutive_errors > 0) return 'warn';
  if (aggregate.total_requests > 0) return 'good';
  return 'muted';
}

export function refreshAllModelCardHealth(): void {
  const strips = document.querySelectorAll<HTMLElement>('.stg-mcard-health');
  for (const strip of strips) {
    const providerIndex = Number.parseInt(strip.dataset.prov || '', 10);
    const modelIndex = Number.parseInt(strip.dataset.model || '', 10);
    if (Number.isNaN(providerIndex) || Number.isNaN(modelIndex)) continue;
    strip.innerHTML = modelCardHealthHtml(providerIndex, modelIndex);
    strip.className = `stg-mcard-health ${
      modelCardHealthClass(providerIndex, modelIndex)}`;
  }
}

const bridge = globals();
bridge._modelHealthCache = modelHealthCache;
bridge._modelHealthTs = modelHealthTimestamp;
bridge._loadKeyStats = loadKeyStats;
bridge._fmtSuccessRate = formatSuccessRate;
bridge._keyStatsClass = keyStatsClass;
bridge._getKeyStatRow = getKeyStatRow;
bridge._getKeyStatRowFor = getKeyStatRowFor;
bridge._keyStatsHelpText = keyStatsHelpText;
bridge._renderKeyCardStatsHTML = renderKeyCardStatsHtml;
bridge._renderProviderKeyStats = renderProviderKeyStats;
bridge._onKeyToggle = (providerIndex, keyIndex, enabled) => {
  void onKeyToggle(providerIndex, keyIndex, enabled);
};
bridge._onKeyClearOverride = (providerIndex, keyIndex) => {
  void onKeyClearOverride(providerIndex, keyIndex);
};
bridge._loadModelHealth = () => loadModelHealth();
bridge._startModelHealthPolling = startModelHealthPolling;
bridge._stopModelHealthPolling = stopModelHealthPolling;
bridge._modelWireIds = modelWireIds;
bridge._modelCardHealthRow = modelCardHealthRow;
bridge._modelCardHealthHTML = modelCardHealthHtml;
bridge._modelCardHealthCls = modelCardHealthClass;
bridge._refreshAllModelCardHealth = refreshAllModelCardHealth;
bridge._destroyKeyStats = destroyKeyStats;
