/* ═══════════════════════════════════════════════════════════════════
   settings/core-panel.ts — settings panel core: switchSettingsTab,
   openSettings and _getAllModels. Routing load/CAS lives
   in model-routing-authority.ts; this entry publishes its retained bindings.

   Ported from retained settings/core_panel.js. Shared staged state stays in
   the retained settings.js owner and is reached through live registry
   accessors; tab populators owned by still-retained sections are invoked
   through the same registry.
   ═══════════════════════════════════════════════════════════════════ */

import { featureRegistry } from '../../feature-registry';
import {activateIndependentSettingsTab} from './tab-activation';
import {isMessagingPlatform, openMessagingPlatform} from './messaging-platforms';
import {notifyMcpSettingsVisibility} from './mcp-settings-reads';
import { loadModelRoutingAuthority, refreshCommittedModelRouting, persistModelRoutingNow } from './model-routing-authority';
import type { ModelRoutingAuthorityPorts, ModelRoutingModelRow } from './model-routing-authority';
import {beginSettingsReadSession, currentSettingsReadSession} from './panel-read-session';
import {loadServerConfig, type CoreServerConfig} from './server-config-reader';
import {renderSettingsDesktopCard, renderSettingsClientInfo} from './client-downloads';
import { _i18nLang, t } from '../../i18n';

interface ProjectedModelEntry {
  model: ModelRoutingModelRow & {
    creator_id?: string;
    offering_id?: string;
    pending_identity?: boolean;
  };
  provider: {
    id: string;
    provider_id: string;
    name: string;
    brand: string;
    enabled: boolean;
  };
  provIdx: number;
  modelIdx: number;
}

type CorePanelGlobals = Window & ModelRoutingAuthorityPorts & {
  config?: Record<string, unknown>;
  _featureFlags?: {
    trading_enabled?: boolean;
    pptx_translate_enabled?: boolean;
    debug_mode?: boolean;
    optimizer_enabled?: boolean;
  };
  _getCurrentTheme?: () => string;
  _renderSettingsDesktopCard?: () => void;
  _refreshSystemPromptSummary?: () => void;
  _renderModelCatalogTab?: () => void;
  _renderModelRoutingTabs?: () => void;
  _renderPresetsTab?: (cfg?: unknown) => void;
  _populatePreferencesTab?: () => void;
  _refreshSttStatus?: () => void;
  _populateDevicesTab?: () => void;
  _deactivateKnowledgeSettingsTab?: (tabId: string) => void;
  _populateDatabaseTab?: () => void;
  _populateSpeechTab?: (cfg?: unknown) => void;
  _beginSpeechTabLoad?: () => {complete(cfg?: unknown): void; fail(): void};
  _openCatpawSettings?: () => Promise<void>;
  _populateSkillsTab?: () => void;
  applySectionRequirements?: () => void;
  populateToolsInventory?: () => void;
  _loadOAuthStatus?: () => void;
  _populateSearchTab?: (cfg: unknown) => void;
  _populateNetworkTab?: (cfg: unknown) => void;
  _populateAdvancedTab?: (cfg: unknown) => void;
  _populateFeishuTab?: (cfg: unknown) => void;
  _populateMtProviderSection?: (cfg: unknown) => void;
  _populateMcpTab?: () => void;
  // Shared staged settings state (retained settings.js owner).
  _stgPresets?: Record<string, unknown>;
  // Published bindings.
  _setVal?: (id: string, value: unknown, prop?: string) => void;
  _getAllModels?: () => ProjectedModelEntry[];
  _loadModelRoutingAuthority?: (preserveDraft?: boolean) => Promise<unknown>;
  _persistModelRoutingNow?: () => Promise<unknown>;
  _refreshSubscriptionModelCatalog?: () => Promise<CoreServerConfig | null>;
  switchSettingsTab?: (tabId: string) => void;
  openSettings?: () => void;
};

function globals(): CorePanelGlobals {
  return featureRegistry as unknown as CorePanelGlobals;
}

// One assembly map owns both initial edit protection and config projection.
const configSections = [
  ['preset', '_renderPresetsTab'], ['search', '_populateSearchTab'],
  ['network', '_populateNetworkTab'], ['advanced', '_populateAdvancedTab'],
  ['feishu', '_populateFeishuTab'], ['translate', '_populateMtProviderSection'],
] as const;

// ══════════════════════════════════════════════════════
//  Helpers
// ══════════════════════════════════════════════════════

/** Project available v2 Offerings into the legacy-shaped rows still consumed
 * by visibility/default selectors. No provider configuration is reconstructed. */
function getAllModels(): ProjectedModelEntry[] {
  const result: ProjectedModelEntry[] = [];
  const routing = globals()._stgModelRouting;
  if (!routing) return result;
  const providers = new Map((routing.providers || []).map((row) => [row.provider_id, row] as const));
  const accesses = new Map((routing.provider_accesses || []).map((row) => [row.provider_access_id, row] as const));
  const models = new Map((routing.models || []).map((row) => [
    (row.creator_id || '') + '\0' + (row.model_id || ''), row,
  ] as const));
  const suppliedOfferingIds = new Set(
    (routing.deployments || []).map((row) => row.offering_id),
  );
  (routing.offerings || []).forEach((offering, offeringIndex) => {
    const access = accesses.get(offering.provider_access_id);
    const provider = access && providers.get(access.provider_id);
    if (!access || !provider || access.enabled === false || offering.stale ||
        offering.enabled === false || !suppliedOfferingIds.has(offering.offering_id)) return;
    const ref = offering.model || {};
    const official = models.get((ref.creator_id || '') + '\0' + (ref.model_id || ''));
    // Bare server-config defaults cannot encode a Provider+Offering identity;
    // pending identity is exposed only by the Provider-scoped quarantine
    // section of the model-first chat picker.
    if (!official) return;
    const projectedModel = Object.assign({}, official, {
      model_id: official.model_id,
      creator_id: official.creator_id,
      capabilities: offering.capabilities || [],
      offering_id: offering.offering_id,
      pending_identity: false,
    });
    const projectedProvider = {
      id: provider.provider_id,
      provider_id: provider.provider_id,
      name: access.display_name || provider.name || provider.provider_id,
      brand: provider.brand || '',
      enabled: (access.enabled as boolean | undefined) !== false,
    };
    result.push({
      model: projectedModel,
      provider: projectedProvider,
      provIdx: 0,
      modelIdx: offeringIndex,
    });
  });
  return result;
}

function setVal(id: string, value: unknown, prop?: string): void {
  const el = document.getElementById(id);
  if (!el) return;
  if (prop === 'checked') {
    if (el instanceof HTMLInputElement) el.checked = !!value;
  } else if (el instanceof HTMLInputElement
      || el instanceof HTMLSelectElement
      || el instanceof HTMLTextAreaElement) {
    el.value = value as string;
  }
}

// ══════════════════════════════════════════════════════
//  Tab switching & config loading
// ══════════════════════════════════════════════════════

function switchSettingsTab(tabId: string): void {
  // Chat platforms are sections of the 消息平台 tab; their old tab ids (deep links) open it there.
  if (isMessagingPlatform(tabId)) {
    switchSettingsTab('messaging');
    openMessagingPlatform(tabId);
    return;
  }
  const bridge = globals();
  bridge._deactivateKnowledgeSettingsTab?.(tabId);
  document.querySelectorAll('.settings-tab').forEach((tab) => {
    tab.classList.toggle('active', (tab as HTMLElement).dataset.tab === tabId);
  });
  document.querySelectorAll('.settings-tab-panel').forEach((panel) => {
    panel.classList.toggle('active', panel.id === 'settingsTab_' + tabId);
  });
  notifyMcpSettingsVisibility();
  activateIndependentSettingsTab(tabId);
  if (tabId === 'speech' && typeof bridge._refreshSttStatus === 'function') {
    bridge._refreshSttStatus();
  }
  if (tabId === 'devices' && typeof bridge._populateDevicesTab === 'function') {
    bridge._populateDevicesTab();
  }

  /* The 模型 tab is a read-only registry view: repaint on activation so it
   * always reflects the current staged authority, even if a future flow
   * mutates creators/models and forgets ``_renderModelRoutingTabs()``. The
   * Providers tab must NOT do this — its cards hold unsaved credential input
   * that a repaint would drop. */
  if (tabId === 'models' && typeof bridge._renderModelCatalogTab === 'function') {
    bridge._renderModelCatalogTab();
  }
}

function refreshSubscriptionModelCatalog(session = currentSettingsReadSession()): Promise<CoreServerConfig | null> {
  const bridge = globals();
  return Promise.all([
    loadServerConfig(session),
    refreshCommittedModelRouting(),
  ]).then((results) => {
    const cfg = results[0];
    if (!cfg || session && !session.isCurrent()) return null;
    if (typeof bridge._renderModelRoutingTabs === 'function') bridge._renderModelRoutingTabs();
    if ((!session || session.isCurrent()) && typeof bridge._renderPresetsTab === 'function') {
      bridge._renderPresetsTab(cfg);
    }
    return cfg;
  });
}

function openSettings(): void {
  // These fields are populated by this opening's miscellaneous-config read.
  // Keep prior-opening values inert until they represent the loaded config.
  const session = beginSettingsReadSession(configSections
    .map(([tab]) => document.getElementById('settingsTab_' + tab))
    .filter((panel): panel is HTMLElement => panel instanceof HTMLElement));
  const bridge = globals();
  const config = (bridge.config || {}) as Record<string, unknown>;
  // ── General tab: populate from local config ──
  setVal('settingSystem', config.systemPrompt || '');
  if (document.getElementById('settingSystemPromptMode')) {
    setVal('settingSystemPromptMode', (config.systemPromptMode === 'replace') ? 'replace' : 'append');
  }
  const spbEl = document.getElementById('settingSystemDisabledBlocks');
  if (spbEl) {
    const blocks = config.systemPromptBlocks as { disabled?: unknown[] } | undefined;
    const disabled = (blocks && Array.isArray(blocks.disabled)) ? blocks.disabled : [];
    setVal('settingSystemDisabledBlocks', JSON.stringify(disabled));
  }
  if (typeof bridge._refreshSystemPromptSummary === 'function') bridge._refreshSystemPromptSummary();

  // Default thinking depth
  setVal('settingDefaultThinkingDepth', config.defaultThinkingDepth || 'off');

  // Language selector sync
  setVal('settingLanguage', _i18nLang || 'zh');

  // Trading module toggle. No restart hint: the flag is enforced live by the
  // plugin (request-time guard + per-pass check in its background workers), so
  // a flip takes effect immediately — see tofu_trading/gate.py.
  setVal('settingTradingEnabled', !!bridge._featureFlags?.trading_enabled, 'checked');

  // PPTX translate module toggle
  setVal('settingPptxTranslateEnabled', !!bridge._featureFlags?.pptx_translate_enabled, 'checked');

  // Debug mode toggle
  setVal('settingDebugMode', !!bridge._featureFlags?.debug_mode, 'checked');

  // Daily Optimizer toggle — default ON when flag not yet in features.json
  const optFlag = bridge._featureFlags?.optimizer_enabled;
  setVal('settingOptimizerEnabled', (optFlag === undefined) ? true : !!optFlag, 'checked');

  // Auto-generate conversation title toggle — defaults to false (manual)
  setVal('settingAutoGenerateTitle', !!config.autoGenerateTitle, 'checked');

  // Input send mode — defaults to 'enter'
  if (document.getElementById('settingInputSendMode')) {
    setVal('settingInputSendMode', (config.inputSendMode === 'ctrl_enter') ? 'ctrl_enter' : 'enter');
  }

  // Theme picker sync
  const ct = bridge._getCurrentTheme ? bridge._getCurrentTheme() : '';
  document.querySelectorAll('.theme-option').forEach((el) => {
    el.classList.toggle('active', (el as HTMLElement).dataset.theme === ct);
  });

  switchSettingsTab('general');
  /* Degraded-section contract: hide the controls of any block whose JS
   * dependency is absent (stale bundle) and show its "needs restart" notice,
   * so no section can look usable while being dead. Runs AFTER the pickers
   * above so a block that DID render is not wrongly degraded. */
  if (typeof bridge.applySectionRequirements === 'function') bridge.applySectionRequirements();
  const speechLoad = bridge._beginSpeechTabLoad?.();
  document.getElementById('settingsModal')?.classList.add('open');
  void bridge._openCatpawSettings?.();
  const statusHint = document.getElementById('settingsStatusHint');
  if (statusHint) statusHint.textContent = t('settings.loadingConfig');

  // Load OAuth status
  bridge._loadOAuthStatus?.();

  renderSettingsClientInfo(session);

  renderSettingsDesktopCard(session);

  // Show loading states
  const provList = document.getElementById('stgProviderList');
  if (provList) provList.innerHTML = '<p class="stg-loading">' + t('settings.loadingConfig') + '</p>';
  const modelCatalog = document.getElementById('stgModelCatalog');
  if (modelCatalog) modelCatalog.innerHTML = '<p class="stg-loading">' + t('settings.loadingConfig') + '</p>';
  const presetTable = document.getElementById('stgPresetTable');
  if (presetTable) presetTable.innerHTML = '<p class="stg-loading">' + t('settings.loading') + '</p>';

  // Load the owner model-routing authority independently from miscellaneous
  // server settings.  The two reads may run together, but only v2 is allowed
  // to populate the provider/model editor.
  const modelRoutingLoad = loadModelRoutingAuthority();
  void loadServerConfig(session).then(async (cfg) => {
    await modelRoutingLoad;
    if (!session.isCurrent()) return;
    if (!cfg) {
      session.completeLoading(false);
      speechLoad?.fail();
      const hint = document.getElementById('settingsStatusHint');
      if (hint) hint.textContent = t('settings.serverConfigFailed');
      if (provList) provList.innerHTML = '<p class="stg-empty">' + t('settings.loadingFailed') + '</p>';
      if (modelCatalog) modelCatalog.innerHTML = '<p class="stg-empty">' + t('settings.loadingFailed') + '</p>';
      if (presetTable) presetTable.innerHTML = '<p class="stg-empty">加载模型预设失败。</p>';
      bridge.debugLog?.('[Settings] Config load failed — provider list and preset table set to error state', 'warning');
      return;
    }
    bridge._stgPresets = JSON.parse(JSON.stringify(cfg.presets || {}));
    if (typeof bridge._renderModelRoutingTabs === 'function') bridge._renderModelRoutingTabs();
    for (const [, populate] of configSections) {
      if (!session.isCurrent()) return;
      bridge[populate]?.(cfg);
    }
    if (speechLoad) {
      if (bridge._stgModelRouting) speechLoad.complete(cfg);
      else speechLoad.fail();
    } else bridge._populateSpeechTab?.(cfg);
    const ready = !!bridge._stgModelRouting;
    const hint = document.getElementById('settingsStatusHint');
    if (hint && !ready) hint.textContent = bridge._stgModelRoutingLoadError || t('settings.loadingFailed');
    else if (hint?.textContent === t('settings.loadingConfig')) hint.textContent = '';
    session.completeLoading(ready);
  }).catch(error => {
    if (!session.isCurrent()) return;
    session.completeLoading(false);
    speechLoad?.fail();
    const hint = document.getElementById('settingsStatusHint');
    if (hint) hint.textContent = t('settings.serverConfigFailed');
    bridge.debugLog?.('[Settings] Initial presentation failed: ' + String(error), 'error');
  });
}

const bridge = globals();
bridge._setVal = setVal;
bridge._getAllModels = getAllModels;
bridge._loadModelRoutingAuthority = loadModelRoutingAuthority;
bridge._persistModelRoutingNow = persistModelRoutingNow;
bridge._refreshSubscriptionModelCatalog = refreshSubscriptionModelCatalog;
bridge.switchSettingsTab = switchSettingsTab;
bridge.openSettings = openSettings;
bridge._renderSettingsDesktopCard = renderSettingsDesktopCard;
