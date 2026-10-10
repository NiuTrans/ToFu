/* ═══════════════════════════════════════════════════════════════════
   settings/save-export.ts — close/save/export/import server config.

   Ported from retained settings/save_export.js. Shared settings state and
   the collectors owned by still-retained sections are reached through the
   feature registry (the same object the lazy presenters bundle uses as its
   runtimeScope), keeping one authority for each value.
   ═══════════════════════════════════════════════════════════════════ */

import { featureRegistry } from '../../feature-registry';
import { t } from '../../i18n';

interface SaveExportServerConfig {
  hidden_models?: string[];
  hidden_ig_models?: string[];
  [key: string]: unknown;
}

interface SaveExportSearchPayload {
  profile?: string;
  llm_content_filter?: boolean;
  fetch_top_n?: number;
  fetch_timeout?: number;
  max_chars_search?: number;
  max_chars_direct?: number;
  max_chars_pdf?: number;
  deepen_enabled?: boolean;
  overrides?: Record<string, unknown>;
  max_bytes?: number;
  skip_domains?: unknown;
}

interface SaveExportPayload {
  presets: Record<string, unknown>;
  search: SaveExportSearchPayload;
  hidden_models: string[];
  hidden_ig_models: string[];
  model_defaults: Record<string, string>;
  cost_experiment?: unknown;
  proxy_pool?: unknown;
  proxy_bypass_domains?: unknown;
  feishu?: unknown;
  mt_provider?: unknown;
}

interface SaveExportFeatureFlags {
  trading_enabled?: boolean;
  pptx_translate_enabled?: boolean;
  debug_mode?: boolean;
  optimizer_enabled?: boolean;
}

type SaveExportGlobals = Window & {
  Api?: {
    features?: { set(flags: Record<string, boolean>): Promise<{ json(): Promise<{ ok?: boolean }> } | null> };
    modelRouting?: {
      putCredentialSecret(id: string, secret: string, revision: number): Promise<{ revision?: number }>;
    };
    serverConfig?: { update(payload: unknown): Promise<{ json(): Promise<{ ok?: boolean; error?: string }> } | null> };
  };
  config?: Record<string, unknown>;
  debugLog?: (message: string, level?: string) => void;
  refreshInputSendHint?: () => void;
  renderConversationList?: () => void;
  getActiveConv?: () => { id: string } | null;
  requestAuthoritativeConversationRender?: (id: string, options: { forceScroll: boolean }) => void;
  _configForPersist?: () => unknown;
  _featureFlags?: SaveExportFeatureFlags;
  _registeredModels?: unknown[];
  _populateModelDropdown?: (models: unknown[]) => void;
  _applyModelUI?: (model: unknown) => void;
  _loadIgModels?: () => Promise<void> | void;
  _applyTradingVisibility?: () => void;
  _applyDebugModeVisibility?: () => void;
  _loadServerConfigAndPopulate?: () => void;
  _persistSttProvider?: () => Promise<void>;
  _destroySkills?: () => void;
  _destroyPreferences?: () => void;
  ChipInput?: { getValues(id: string): unknown };
  openSettings?: () => void;
  closeSettings?: () => void;
  saveSettings?: () => Promise<void>;
  exportServerConfig?: () => void;
  importServerConfig?: (event: Event) => void;
  // Still-retained settings owners (lazy presenters bundle).
  _serverConfig?: SaveExportServerConfig | null;
  _stgModelRouting?: unknown;
  _stgModelRoutingRevision?: number;
  _stgModelRoutingLoadError?: string;
  _stgModelRoutingLoadPromise?: Promise<unknown> | null;
  _stgPendingCredentialSecrets?: Record<string, string>;
  _stgPresets?: Record<string, unknown>;
  _searchLastPreset?: string;
  _loadModelRoutingAuthority?: () => Promise<unknown>;
  _persistModelRoutingNow?: () => Promise<unknown>;
  _loadServerConfig?: () => Promise<unknown>;
  _collectModelDefaults?: () => Record<string, string>;
  _collectResponsesExperimentControls?: () => void;
  _collectCostExperimentConfig?: () => unknown;
  _collectProxyPool?: () => unknown;
  _collectProxyBypassDomains?: () => unknown;
  _collectFeishuConfig?: () => unknown;
  _collectMtProviderConfig?: () => unknown;
};

function globals(): SaveExportGlobals {
  return featureRegistry as unknown as SaveExportGlobals;
}

function elementValue(id: string): string {
  const element = document.getElementById(id);
  return element instanceof HTMLInputElement
      || element instanceof HTMLSelectElement
      || element instanceof HTMLTextAreaElement
    ? element.value
    : '';
}

function elementChecked(id: string): boolean {
  const element = document.getElementById(id);
  return element instanceof HTMLInputElement ? element.checked : false;
}

// ══════════════════════════════════════════════════════
//  Close / Save / Export / Import
// ══════════════════════════════════════════════════════

function closeSettings(): void {
  const bridge = globals() as unknown as Record<string, unknown>;
  for (const name of [
    '_destroyPrivateHosts', '_destroyBrowserAccess',
    '_destroyDevicesTab',
    '_destroySpeechTab', '_destroyCredentialsVault', '_destroyAuthSources',
    '_destroyModelCatalogPanel',

    '_destroyProviderTemplatePicker',
  ]) {
    const cleanup = bridge[name];
    if (typeof cleanup === 'function') (cleanup as () => void)();
  }
  if (typeof bridge._destroySkills === 'function') {
    (bridge._destroySkills as () => void)();
  }
  if (typeof bridge._destroyPreferences === 'function') {
    (bridge._destroyPreferences as () => void)();
  }
  document.getElementById('settingsModal')?.classList.remove('open');
  // Refresh model dropdown to reflect any visibility changes
  if (typeof bridge._populateModelDropdown === 'function' && (bridge._registeredModels as unknown[]).length > 0) {
    (bridge._populateModelDropdown as (models: unknown[]) => void)(bridge._registeredModels as unknown[]);
    (bridge._applyModelUI as (model: unknown) => void)((bridge.config as Record<string, unknown>).model);
  }
  // Refresh image gen picker to reflect visibility changes
  if (typeof bridge._loadIgModels === 'function') {
    void (bridge._loadIgModels as () => Promise<void>)();
  }
}

/* Save is a long awaited chain (STT persist → model-routing replace →
   credential secrets → server config update → config reload) with no other
   visible feedback: without a busy latch the button looks dead on a slow
   network, and a second click races modelRouting.replace with the same
   expected_revision (409 conflict). While busy the button is disabled and
   the footer hint shows 保存中…; any throw anywhere in the body is surfaced
   in that same hint instead of dying silently in the action registry. */
let settingsSaveBusy = false;

function setSettingsSaveBusy(busy: boolean): void {
  settingsSaveBusy = busy;
  const btn = document.getElementById('settingsSaveBtn');
  if (btn instanceof HTMLButtonElement) btn.disabled = busy;
  const hint = document.getElementById('settingsStatusHint');
  if (!hint) return;
  if (busy) hint.textContent = t('common.saving');
  else if (hint.textContent === t('common.saving')) hint.textContent = '';
}

function settingsSaveFailed(e: unknown): void {
  const message = (e && (e as Error).message) ? (e as Error).message : String(e);
  globals().debugLog?.('[Settings] Save failed: ' + message, 'error');
  const statusHint = document.getElementById('settingsStatusHint');
  if (statusHint) statusHint.textContent = '保存失败：' + message;
}

async function saveSettings(): Promise<void> {
  if (settingsSaveBusy) return;
  setSettingsSaveBusy(true);
  try {
    await saveSettingsBody();
  } catch (e) {
    settingsSaveFailed(e);
  } finally {
    setSettingsSaveBusy(false);
  }
}

async function persistFeatureFlag(
  flag: keyof SaveExportFeatureFlags,
  newVal: boolean,
  onApplied?: () => void,
): Promise<void> {
  const bridge = globals();
  const response = await bridge.Api?.features?.set({ [flag]: newVal });
  const data = response ? await response.json() : null;
  if (data?.ok !== true) throw new Error(t('settings.featureSaveFailed'));
  const labels: Record<string, [string, string]> = {
    trading_enabled: ['Trading module enabled — applied', 'Trading module disabled — applied'],
    pptx_translate_enabled: ['PPTX translate enabled', 'PPTX translate disabled'],
    debug_mode: ['Debug mode enabled', 'Debug mode disabled'],
    optimizer_enabled: ['Daily Optimizer enabled', 'Daily Optimizer disabled'],
  };
  const label = labels[flag];
  bridge.debugLog?.(newVal ? label[0] : label[1], 'success');
  if (bridge._featureFlags) bridge._featureFlags[flag] = newVal;
  onApplied?.();
}

async function saveSettingsBody(): Promise<void> {
  const bridge = globals();
  const config = bridge.config as Record<string, unknown>;

  // 1. Client-side config (General tab)
  if (typeof bridge._collectResponsesExperimentControls === 'function') {
    bridge._collectResponsesExperimentControls();
  }
  config.systemPrompt = elementValue('settingSystem');
  if (document.getElementById('settingSystemPromptMode')) {
    config.systemPromptMode = (elementValue('settingSystemPromptMode') === 'replace') ? 'replace' : 'append';
  }
  if (document.getElementById('settingSystemDisabledBlocks')) {
    let disabledIds: unknown = [];
    try { disabledIds = JSON.parse(elementValue('settingSystemDisabledBlocks') || '[]'); } catch { disabledIds = []; }
    if (!Array.isArray(disabledIds)) disabledIds = [];
    config.systemPromptBlocks = { disabled: disabledIds };
  }
  if (document.getElementById('settingDefaultThinkingDepth')) {
    const oldDefault = config.defaultThinkingDepth;
    config.defaultThinkingDepth = elementValue('settingDefaultThinkingDepth') || 'off';
    // Propagate: if current depth was the old default, update it to the new default
    if (config.thinkingDepth === oldDefault) {
      config.thinkingDepth = config.defaultThinkingDepth;
    }
  }
  // Auto-generate conversation title toggle
  if (document.getElementById('settingAutoGenerateTitle')) {
    config.autoGenerateTitle = elementChecked('settingAutoGenerateTitle');
  }

  // Input send mode
  if (document.getElementById('settingInputSendMode')) {
    config.inputSendMode = (elementValue('settingInputSendMode') === 'ctrl_enter') ? 'ctrl_enter' : 'enter';
    if (typeof bridge.refreshInputSendHint === 'function') bridge.refreshInputSendHint();
  }

  try { localStorage.setItem('claude_client_config', JSON.stringify(bridge._configForPersist?.())); }
  catch (e) { bridge.debugLog?.('[saveSettings] localStorage save failed: ' + (e as Error).message, 'error'); }

  // 2. Feature flags (trading toggle)
  if (document.getElementById('settingTradingEnabled')) {
    const newVal = elementChecked('settingTradingEnabled');
    const curVal = !!bridge._featureFlags?.trading_enabled;
    if (newVal !== curVal) {
      await persistFeatureFlag('trading_enabled', newVal, () => {
        // Show/hide the topbar entry immediately. The backend enforces the
        // same flag per request and in its background workers, so this is
        // presentation only — nothing here is what stops the module.
        if (typeof bridge._applyTradingVisibility === 'function') {
          bridge._applyTradingVisibility();
        }
      });
    }
  }

  // 2b. PPTX translate toggle
  if (document.getElementById('settingPptxTranslateEnabled')) {
    const newPptx = elementChecked('settingPptxTranslateEnabled');
    const curPptx = !!bridge._featureFlags?.pptx_translate_enabled;
    if (newPptx !== curPptx) {
      await persistFeatureFlag('pptx_translate_enabled', newPptx);
    }
  }

  // 2c. Debug mode toggle
  if (document.getElementById('settingDebugMode')) {
    const newDbg = elementChecked('settingDebugMode');
    const curDbg = !!bridge._featureFlags?.debug_mode;
    if (newDbg !== curDbg) {
      await persistFeatureFlag('debug_mode', newDbg, () => {
        // Show/hide unfinished orchestration surfaces (Flow submenu, Studio /
        // Tasks topbar + mobile-sheet items). See index.html loadFeatureFlags.
        if (typeof bridge._applyDebugModeVisibility === 'function') {
          bridge._applyDebugModeVisibility();
        }
        // Re-render sidebar and the authoritative Surface so debug
        // presentation follows the updated setting.
        if (typeof bridge.renderConversationList === 'function') bridge.renderConversationList();
        if (typeof bridge.getActiveConv === 'function') {
          const dbgConv = bridge.getActiveConv();
          if (dbgConv) bridge.requestAuthoritativeConversationRender?.(
            dbgConv.id, { forceScroll: true },
          );
        }
      });
    }
  }

  // 2d. Daily Optimizer toggle
  if (document.getElementById('settingOptimizerEnabled')) {
    const newOpt = elementChecked('settingOptimizerEnabled');
    const curFlag = bridge._featureFlags?.optimizer_enabled;
    const curOpt = (curFlag === undefined) ? true : !!curFlag;
    if (newOpt !== curOpt) {
      await persistFeatureFlag('optimizer_enabled', newOpt, () => {
        // Show/hide the topbar badge immediately.
        const badge = document.getElementById('optimizerBadge');
        if (badge) badge.style.display = newOpt ? 'inline-flex' : 'none';
      });
    }
  }

  // 3. Server config (Providers / Presets / Search)
  if (bridge._serverConfig) {
    // openSettings() intentionally loads miscellaneous config and the routing
    // authority concurrently. A fast Save must join that authority read; if
    // the initial read failed, this also performs one fresh retry.
    if (bridge._stgModelRoutingLoadPromise || !bridge._stgModelRouting) {
      const readyModelRouting = await bridge._loadModelRoutingAuthority?.();
      if (!readyModelRouting) {
        throw new Error(
          bridge._stgModelRoutingLoadError || 'model-routing v2 authority is unavailable');
      }
    }
    if (typeof bridge._persistSttProvider === 'function') {
      await bridge._persistSttProvider();
    }
    const saved = await saveServerConfig();
    if (!saved) return;
  }

  bridge.debugLog?.('Settings saved', 'success');
  closeSettings();
}

async function saveServerConfig(): Promise<boolean> {
  const bridge = globals();
  // Strip empty preset mappings — especially 'opus' should never be pinned
  // to a specific version; leaving it unset lets the code default (LLM_MODEL) apply.
  const cleanPresets: Record<string, unknown> = {};
  const stagedPresets = bridge._stgPresets || {};
  for (const k in stagedPresets) {
    if (stagedPresets[k]) cleanPresets[k] = stagedPresets[k];
  }

  const serverConfig = bridge._serverConfig;
  const payload: SaveExportPayload = {
    presets: cleanPresets,
    search: {},
    hidden_models: (serverConfig && serverConfig.hidden_models) || [],
    hidden_ig_models: (serverConfig && serverConfig.hidden_ig_models) || [],
    model_defaults: bridge._collectModelDefaults ? bridge._collectModelDefaults() : {},
  };
  if (typeof bridge._collectCostExperimentConfig === 'function') {
    const costExperimentCfg = bridge._collectCostExperimentConfig();
    if (costExperimentCfg !== null) payload.cost_experiment = costExperimentCfg;
  }
  // Search tab
  const cfCb = document.getElementById('settingLlmContentFilter');
  const profileEl = document.getElementById('settingSearchProfile');
  const profileVal = elementValue('settingSearchProfile') || 'balanced';
  // 自定义 is a UI state, not a wire profile: send the last real preset as
  // the base plus the four concrete knob values as overrides.
  const customProfile = profileVal === 'custom';
  payload.search.profile = customProfile
    ? (typeof bridge._searchLastPreset !== 'undefined' ? bridge._searchLastPreset : 'balanced')
    : profileVal;
  payload.search.llm_content_filter = cfCb ? elementChecked('settingLlmContentFilter') : true;
  payload.search.fetch_top_n = parseInt(elementValue('settingFetchTopN')) || 6;
  payload.search.fetch_timeout = parseInt(elementValue('settingFetchTimeout')) || 15;
  payload.search.max_chars_search = parseInt(elementValue('settingMaxCharsSearch')) || 60000;
  payload.search.max_chars_direct = parseInt(elementValue('settingMaxCharsDirect')) || 200000;
  payload.search.max_chars_pdf = parseInt(elementValue('settingMaxCharsPdf')) || 0;
  payload.search.deepen_enabled = !!document.getElementById('settingSearchDeepen')
    && elementChecked('settingSearchDeepen');
  payload.search.overrides = customProfile ? {
    fetch_top_n: payload.search.fetch_top_n,
    max_chars_search: payload.search.max_chars_search,
    llm_content_filter: payload.search.llm_content_filter,
    deepen_enabled: payload.search.deepen_enabled,
  } : {};
  // On the new profile UI, concrete preset-owned keys are omitted unless the
  // select sits on 自定义. Legacy/embedded surfaces have no select and keep
  // sending the old concrete wire shape for backward compatibility.
  if (profileEl && !customProfile) {
    delete payload.search.fetch_top_n;
    delete payload.search.max_chars_search;
    delete payload.search.llm_content_filter;
    delete payload.search.deepen_enabled;
  }
  // Displayed in MB, stored in bytes (the pipeline's native unit).
  const mbVal = parseFloat(elementValue('settingMaxBytesMB'));
  payload.search.max_bytes = (mbVal > 0) ? Math.round(mbVal * 1048576) : 20971520;
  if (typeof bridge.ChipInput !== 'undefined') payload.search.skip_domains = bridge.ChipInput?.getValues('settingSkipDomains');

  // Network — proxy pool (ordered, scoped). The pool editor owns
  // proxying: the backend retires the legacy single-proxy slot whenever
  // the key is present. Container absent (legacy/other surfaces) → leave
  // the server's proxy config untouched.
  const pool = (typeof bridge._collectProxyPool === 'function') ? bridge._collectProxyPool() : null;
  if (pool !== null) {
    payload.proxy_pool = pool;
  }

  // Network — directly editable bypass rows (feeds proxies_for + no_proxy).
  const bypass = (typeof bridge._collectProxyBypassDomains === 'function')
    ? bridge._collectProxyBypassDomains() : null;
  if (bypass !== null) payload.proxy_bypass_domains = bypass;

  // Feishu bot config
  if (typeof bridge._collectFeishuConfig === 'function') {
    payload.feishu = bridge._collectFeishuConfig();
  }

  // Machine translation provider config
  if (typeof bridge._collectMtProviderConfig === 'function') {
    payload.mt_provider = bridge._collectMtProviderConfig();
  }

  try {
    if (!bridge._stgModelRouting) {
      throw new Error('model-routing v2 authority is not loaded');
    }
    // Join any immediate persist still in flight (model adds commit on their
    // own), persist remaining staged edits through the same serialized chain,
    // and skip the whole-document PUT entirely when the staged aggregate
    // already matches the committed one — a no-op PUT still paid a full
    // synchronous dispatcher slot rebuild server-side.
    await bridge._persistModelRoutingNow?.();
    const stagedSecrets = bridge._stgPendingCredentialSecrets || {};
    const pendingCredentialIds = Object.keys(stagedSecrets);
    for (const credentialId of pendingCredentialIds) {
      const secretResponse = await bridge.Api?.modelRouting?.putCredentialSecret(
        credentialId,
        stagedSecrets[credentialId],
        bridge._stgModelRoutingRevision ?? 0,
      );
      bridge._stgModelRoutingRevision = Number(secretResponse?.revision || bridge._stgModelRoutingRevision || 0);
    }
    if (pendingCredentialIds.length) {
      await bridge._loadModelRoutingAuthority?.();
    }
    bridge._stgPendingCredentialSecrets = {};
    const r = await bridge.Api?.serverConfig?.update(payload);
    const data = r ? await r.json().catch(() => ({} as { ok?: boolean; error?: string })) : {};
    if (data.ok) {
      const msg = t('settings.configSaved');
      bridge.debugLog?.('[Settings] ' + msg, 'success');
      const hint = document.getElementById('settingsStatusHint');
      if (hint) hint.textContent = t('settings.saved');
      setTimeout(() => {
        const lateHint = document.getElementById('settingsStatusHint');
        if (lateHint && lateHint.textContent === t('settings.saved')) lateHint.textContent = '';
      }, 3000);
      // Re-fetch server config to refresh model dropdown with any new/changed models.
      // Without this, _registeredModels stays stale and newly added providers' models
      // don't appear in the preset toggle until a page refresh.
      if (typeof bridge._loadServerConfigAndPopulate === 'function') {
        bridge._loadServerConfigAndPopulate();
      }
      return true;
    }
    bridge.debugLog?.('[Settings] Save failed: ' + (data.error || 'unknown'), 'error');
    return false;
  } catch (e) {
    settingsSaveFailed(e);
    return false;
  }
}

function exportServerConfig(): void {
  const bridge = globals();
  bridge._loadServerConfig?.().then((cfg) => {
    if (!cfg) return;
    const blob = new Blob([JSON.stringify(cfg, null, 2)], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'tofu-config-' + new Date().toISOString().slice(0, 10) + '.json';
    a.click();
    URL.revokeObjectURL(a.href);
    bridge.debugLog?.('[Settings] Config exported', 'success');
  });
}

function importServerConfig(event: Event): void {
  const target = event.target as HTMLInputElement;
  const file = target.files?.[0];
  if (!file) return;
  const bridge = globals();
  const reader = new FileReader();
  reader.onload = async (e) => {
    try {
      const imported = JSON.parse(String(e.target?.result || ''));
      const r = await bridge.Api?.serverConfig?.update(imported);
      const data = r ? await r.json().catch(() => ({} as { ok?: boolean; error?: string })) : {};
      if (data.ok) {
        bridge.debugLog?.('[Settings] Config imported successfully', 'success');
        bridge._serverConfig = null;
        bridge.openSettings?.();
      } else {
        bridge.debugLog?.('[Settings] Import failed: ' + (data.error || 'unknown'), 'error');
      }
    } catch (err) {
      bridge.debugLog?.('[Settings] Invalid JSON file: ' + (err as Error).message, 'error');
    }
  };
  reader.readAsText(file);
  target.value = '';
}

const bridge = globals();
bridge.closeSettings = closeSettings;
bridge.saveSettings = saveSettings;
bridge.exportServerConfig = exportServerConfig;
bridge.importServerConfig = importServerConfig;
