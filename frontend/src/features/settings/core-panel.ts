/* ═══════════════════════════════════════════════════════════════════
   settings/core-panel.ts — settings panel core: switchSettingsTab,
   _loadServerConfig, openSettings, _getAllModels, routing-authority
   load/persist.

   Ported from retained settings/core_panel.js. Shared staged state stays in
   the retained settings.js owner and is reached through live registry
   accessors; tab populators owned by still-retained sections are invoked
   through the same registry.
   ═══════════════════════════════════════════════════════════════════ */

import { featureRegistry } from '../../feature-registry';
import { escapeHtml } from '../../html-safety';
import { _i18nLang, _syncLangPicker, t } from '../../i18n';

interface DesktopDownloadPick {
  url?: string;
  arch?: string;
  os?: string;
  label?: string;
  filename?: string;
  size?: number;
}

interface DesktopStatus {
  download_url?: string;
  downloads?: DesktopDownloadPick[];
}

interface CoreServerConfig {
  presets?: Record<string, unknown>;
  model_pricing?: unknown;
  model_price_display?: ModelPriceDisplayPolicy;
  [key: string]: unknown;
}

interface ModelPriceDisplayPolicy {
  base_currency?: string;
  usd_rates?: Record<string, number>;
  updated_at?: number;
  source?: string;
}

interface ModelRoutingProviderRow {
  provider_id: string;
  name?: string;
  brand?: string;
}

interface ModelRoutingAccessRow {
  provider_access_id: string;
  provider_id: string;
  display_name?: string;
  enabled?: boolean;
}

interface ModelRoutingModelRow {
  creator_id?: string;
  model_id: string;
  capabilities?: string[];
  [key: string]: unknown;
}

interface ModelRoutingOfferingRow {
  offering_id: string;
  provider_access_id: string;
  model?: { creator_id?: string; model_id?: string };
  capabilities?: string[];
  enabled?: boolean;
  stale?: boolean;
}

interface ModelRoutingDocument {
  contract_version?: string;
  providers?: ModelRoutingProviderRow[];
  provider_accesses?: ModelRoutingAccessRow[];
  models?: ModelRoutingModelRow[];
  offerings?: ModelRoutingOfferingRow[];
  deployments?: { offering_id: string }[];
  revision?: number;
}

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

type CorePanelGlobals = Window & {
  Api?: {
    desktop?: { status(): Promise<DesktopStatus | null> };
    serverConfig?: { get(): Promise<CoreServerConfig | null> };
    modelRouting?: {
      get(): Promise<{ model_routing?: ModelRoutingDocument; revision?: number } | null>;
      replace(doc: ModelRoutingDocument, revision: number): Promise<{
        model_routing?: ModelRoutingDocument; revision?: number;
      } | null>;
    };
    health?: { info(): Promise<{
      version?: string;
      mobile_client_url?: string;
      mobile_client_version?: string;
      ios_client_url?: string;
    } | null> };
  };
  apiUrl?: (path: string) => string;
  config?: Record<string, unknown>;
  debugLog?: (message: string, level?: string) => void;
  _featureFlags?: {
    trading_enabled?: boolean;
    pptx_translate_enabled?: boolean;
    debug_mode?: boolean;
    optimizer_enabled?: boolean;
  };
  _getCurrentTheme?: () => string;
  _setModelPricingCache?: (pricing: unknown) => void;
  _renderSettingsUpdatePill?: () => void;
  _renderSettingsDesktopCard?: () => void;
  _populateModelDropdown?: (models: unknown[]) => void;
  _modelRoutingDropdownModels?: (doc: unknown) => unknown[];
  _refreshSystemPromptSummary?: () => void;
  _renderModelCatalogTab?: () => void;
  _renderModelRoutingTabs?: () => void;
  _renderPresetsTab?: (cfg?: unknown) => void;
  _populatePreferencesTab?: () => void;
  _refreshSttStatus?: () => void;
  _populateDevicesTab?: () => void;
  _populateDatabaseTab?: () => void;
  _populateSpeechTab?: (cfg?: unknown) => void;
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
  t?: (key: string, params?: Record<string, unknown>) => string;
  // Shared staged settings state (retained settings.js owner).
  _serverConfig?: CoreServerConfig | null;
  _modelPriceDisplayPolicy?: ModelPriceDisplayPolicy;
  _stgModelRouting?: ModelRoutingDocument | null;
  _stgModelRoutingRevision?: number;
  _stgModelRoutingSavedJson?: string;
  _stgRoutingPersistChain?: Promise<unknown> | null;
  _stgModelRoutingLoadError?: string;
  _stgModelRoutingLoadPromise?: Promise<unknown> | null;
  _stgPendingCredentialSecrets?: Record<string, string>;
  _stgPresets?: Record<string, unknown>;
  // Published bindings.
  _setVal?: (id: string, value: unknown, prop?: string) => void;
  _getAllModels?: () => ProjectedModelEntry[];
  _loadServerConfig?: () => Promise<CoreServerConfig | null>;
  _loadModelRoutingAuthority?: (preserveDraft?: boolean) => Promise<unknown>;
  _persistModelRoutingNow?: () => Promise<unknown>;
  _refreshSubscriptionModelCatalog?: () => Promise<CoreServerConfig | null>;
  switchSettingsTab?: (tabId: string) => void;
  openSettings?: () => void;
};

function globals(): CorePanelGlobals {
  return featureRegistry as unknown as CorePanelGlobals;
}

// ══════════════════════════════════════════════════════
//  Helpers
// ══════════════════════════════════════════════════════

/* Generic download-link rows for an installer card (desktop full / agent
 * Rows mirror the mobile client card's `.stg-mobile-*` chrome so both cards
 * read as ONE download component. Same payload contract as the Local Control
 * panel: `{download_url, downloads}` from /api/v4/desktop/status. The
 * releases page stays as the escape hatch for an unrecognised platform, a
 * release missing an asset, or an unreachable GitHub API. */
function stgFmtDlSize(bytes: unknown): string {
  const n = Number(bytes);
  if (!isFinite(n) || n <= 0) return '';
  const mb = n / 1048576;
  return (mb >= 100 ? Math.round(mb) : Math.round(mb * 10) / 10) + ' MB';
}

/* Re-base a server-built same-origin URL onto the CURRENT proxy base path:
 * `downloads[].url` is built from request.host_url, which a path-prefixed
 * cloud-IDE proxy strips before forwarding. Strip back to the /api/ tail
 * and re-apply the live base via apiUrl(). */
function stgResolveDlUrl(url: string): string {
  const apiUrl = globals().apiUrl;
  if (!url || typeof apiUrl !== 'function') return url;
  const i = url.indexOf('/api/');
  if (i < 0) return url;
  return apiUrl(url.slice(i));
}

/* t() with a readable fallback — the download copy must never surface a
 * raw i18n key when the locale chunk is absent (jsdom harness, stale
 * bundle). */
function stgDlT(key: 'settings.desktopRecommend' | 'settings.mobileDownload' | 'local.desktopDownloadAll', fallback: string): string {
  // Resolve through the registry (window.t in product, the harness's locale
  // translator in jsdom) so the download card honours the ACTIVE language;
  // the bundled i18n t is keyed to its own initialised locale only.
  const registryT = globals().t;
  const translate = typeof registryT === 'function' ? registryT : t;
  const v = translate(key);
  if (v && v !== key) return v;
  return fallback;
}

/* The architecture THIS machine runs, as reported by the browser itself —
 * the same probe Local Control uses (`_lcResolveArch`), duplicated on
 * purpose: that bundle is lazy-loaded and may never exist on a page that
 * only opens Settings. `getHighEntropyValues` is the only honest source on
 * macOS (Apple Silicon's UA says "Intel Mac OS X"). Resolved once per page;
 * '' when the browser cannot say (Safari) — which means NO recommendation
 * badge, never a guess. */
let stgArch: string | null = null;

function stgResolveArch(): Promise<string> {
  if (stgArch !== null) return Promise.resolve(stgArch);
  const uad = (typeof navigator !== 'undefined')
    ? (navigator as Navigator & { userAgentData?: {
        getHighEntropyValues?(hints: string[]): Promise<{ architecture?: string }>;
      } }).userAgentData
    : null;
  if (!uad || typeof uad.getHighEntropyValues !== 'function') {
    stgArch = '';
    return Promise.resolve(stgArch);
  }
  return Promise.resolve(uad.getHighEntropyValues(['architecture']))
    .then((v) => {
      stgArch = (v && v.architecture) ? String(v.architecture) : '';
      return stgArch;
    })
    .catch(() => { stgArch = ''; return stgArch; });
}

/* getHighEntropyValues answers 'arm'/'x86'; the backend's picks say
 * 'arm64'/'x86_64'. Translate between the two vocabularies. */
function stgArchMatch(arch: string): string {
  const a = (arch || '').toLowerCase();
  if (a.indexOf('arm') === 0 || a === 'aarch64') return 'arm64';
  if (a === 'x86' || a === 'ia32' || a === 'x86_64' || a === 'amd64') return 'x86_64';
  return '';
}

/* OS glyph for the platform slot — the SAME slot the mobile card gives
 * Android/iOS, so both cards read as one component. */
function stgDesktopOsIcon(osKey: string | undefined): string {
  if (osKey === 'macos') {
    return '<svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">' +
      '<path d="M16.365 1.43c0 1.14-.46 2.2-1.21 3.02-.78.86-2.05 1.52-3.13 1.44-.14-1.1.42-2.26 1.13-3.02.79-.85 2.14-1.46 3.21-1.44zM20.94 17.1c-.57 1.3-.84 1.88-1.57 3.03-1.02 1.6-2.46 3.6-4.25 3.6-1.59.02-2-.99-4.16-.98-2.16.01-2.61 1-4.2.98-1.79-.02-3.16-1.82-4.18-3.42C.71 17.26.36 13.07 1.75 10.8c1.03-1.68 2.66-2.66 4.19-2.66 1.56 0 2.54.99 3.83.99 1.25 0 2.01-.99 3.82-.99 1.36 0 2.8.74 3.83 2.02-3.37 1.85-2.82 6.65.52 7.94z"/></svg>';
  }
  if (osKey === 'windows') {
    return '<svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">' +
      '<path d="M3 5.7l7.2-1v6.8H3zM11.3 4.6L21 3.2v8.3h-9.7zM3 12.5h7.2v6.8l-7.2-1zM11.3 12.5H21v8.3l-9.7-1.4z"/></svg>';
  }
  if (osKey === 'linux') {
    return '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
      '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 9l3 3-3 3"/><path d="M12.5 15H17"/></svg>';
  }
  return '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
    '<rect x="3" y="4" width="18" height="13" rx="2"/><path d="M9 21h6"/><path d="M12 17v4"/></svg>';
}

function stgDownloadLinks(d: DesktopStatus | null, arch: string): string {
  const page = ((d && d.download_url) || '').trim();
  const picks = (d && Array.isArray(d.downloads)) ? d.downloads : [];
  const want = stgArchMatch(arch);
  let html = '';
  if (picks.length) {
    html += '<div class="stg-mobile-rows">';
    for (const rawPick of picks) {
      const p = rawPick || {};
      if (!p.url) continue;
      // The recommendation matters only when there IS a choice, and is
      // shown only when the browser itself said which chip this is — the
      // label already names the arch, so a quiet badge replaces the old
      // explanatory paragraph entirely.
      const rec = picks.length > 1 && want && p.arch === want;
      html += '<a class="stg-mobile-row stg-desktop-row" href="' +
        escapeHtml(stgResolveDlUrl(p.url)) +
        '" target="_blank" rel="noopener noreferrer" title="' +
        escapeHtml(p.filename || '') + '">' +
        '<span class="stg-mobile-platform" aria-hidden="true">' +
          stgDesktopOsIcon(p.os) + '</span>' +
        '<span class="stg-mobile-name">' +
          escapeHtml(p.label || p.arch || '') + '</span>' +
        (p.size
          ? '<span class="stg-mobile-version">' +
            escapeHtml(stgFmtDlSize(p.size)) + '</span>'
          : '') +
        (rec
          ? '<span class="stg-desktop-rec">' +
            escapeHtml(stgDlT('settings.desktopRecommend', '推荐')) + '</span>'
          : '') +
        '<span class="stg-mobile-get">' +
          '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12"/><path d="M6 11l6 6 6-6"/><path d="M5 21h14"/></svg>' +
          '<span>' + escapeHtml(stgDlT('settings.mobileDownload', '下载')) +
          '</span></span></a>';
    }
    html += '</div>';
  }
  if (page) {
    html += '<a class="stg-desktop-all" href="' +
      escapeHtml(page) + '" target="_blank" rel="noopener noreferrer">' +
      escapeHtml(stgDlT('local.desktopDownloadAll', '查看全部下载 ↗')) + '</a>';
  }
  return html;
}

/* The full Windows/macOS desktop clients are a download, not a control —
 * they live in Settings → 关于与更新, next to the mobile client card.
 * Config-gated like the mobile card: hidden until /api/v4/desktop/status
 * answers with at least a releases page. The agent (受控端) stays in Local
 * Control; it is the thing that controls the machine. */
function renderSettingsDesktopCard(): void {
  const card = document.getElementById('settingsDesktopCard');
  if (!card) return;
  const desktop = globals().Api?.desktop;
  if (!desktop || typeof desktop.status !== 'function') {
    card.style.display = 'none';
    return;
  }
  void Promise.all([stgResolveArch(), Promise.resolve(desktop.status())])
    .then((res) => {
      const d = res[1];
      if (!d || (!d.download_url && !(d.downloads || []).length)) {
        card.style.display = 'none';
        return;
      }
      const rows = document.getElementById('settingsDesktopRows');
      if (rows) rows.innerHTML = stgDownloadLinks(d, res[0]);
      card.style.display = '';
    }).catch(() => { card.style.display = 'none'; });
}

/** Project available v2 Offerings into the legacy-shaped rows still consumed
 * by visibility/default selectors. No provider configuration is reconstructed. */
function getAllModels(): ProjectedModelEntry[] {
  const result: ProjectedModelEntry[] = [];
  const routing = globals()._stgModelRouting;
  if (!routing) return result;
  const providers = new Map((routing.providers || []).map((row) => [row.provider_id, row] as const));
  const accesses = new Map((routing.provider_accesses || []).map((row) => [row.provider_access_id, row] as const));
  const models = new Map((routing.models || []).map((row) => [
    (row.creator_id || '') + ' ' + (row.model_id || ''), row,
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
    const official = models.get((ref.creator_id || '') + ' ' + (ref.model_id || ''));
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
  const bridge = globals();
  document.querySelectorAll('.settings-tab').forEach((tab) => {
    tab.classList.toggle('active', (tab as HTMLElement).dataset.tab === tabId);
  });
  document.querySelectorAll('.settings-tab-panel').forEach((panel) => {
    panel.classList.toggle('active', panel.id === 'settingsTab_' + tabId);
  });
  if (tabId === 'preferences' && typeof bridge._populatePreferencesTab === 'function') {
    bridge._populatePreferencesTab();
  }
  if (tabId === 'speech' && typeof bridge._refreshSttStatus === 'function') {
    bridge._refreshSttStatus();
  }
  if (tabId === 'devices' && typeof bridge._populateDevicesTab === 'function') {
    bridge._populateDevicesTab();
  }
  if (tabId === 'database' && typeof bridge._populateDatabaseTab === 'function') {
    bridge._populateDatabaseTab();
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

async function loadServerConfig(): Promise<CoreServerConfig | null> {
  const bridge = globals();
  try {
    bridge.debugLog?.('[Settings] Loading server config…', 'info');
    const serverConfig = await bridge.Api?.serverConfig?.get();
    bridge._serverConfig = serverConfig ?? null;
    if (!serverConfig) throw new Error('serverConfig.get returned null');
    bridge.debugLog?.('[Settings] Misc server config loaded: ' + Object.keys(serverConfig.presets || {}).length + ' presets; providers load from model-routing v2', 'info');
    // Populate the read-only card caches: canonical model rates plus the
    // server-owned USD pivots used only for localized Settings presentation.
    if (serverConfig.model_pricing) {
      bridge._setModelPricingCache?.(serverConfig.model_pricing);
    }
    if (serverConfig.model_price_display &&
        serverConfig.model_price_display.usd_rates) {
      bridge._modelPriceDisplayPolicy = serverConfig.model_price_display;
    }
    return serverConfig;
  } catch (e) {
    bridge.debugLog?.('[Settings] Failed to load server config: ' + (e as Error)?.message, 'error');
    return null;
  }
}

function loadModelRoutingAuthority(preserveDraft = false): Promise<unknown> {
  const bridge = globals();
  const original = bridge._stgModelRouting;
  const saved = bridge._stgModelRoutingSavedJson;
  const draftUnchanged = () => bridge._stgModelRouting === original
    && JSON.stringify(original) === saved
    && !Object.keys(bridge._stgPendingCredentialSecrets || {}).length;
  if (preserveDraft && !draftUnchanged()) return Promise.resolve(null);
  // Opening Settings, refreshing the provider catalogue, and Save may all
  // need this authority at once. Share one read so a slower response cannot
  // overwrite a newer staged revision, and let a failed read be retried by
  // the next caller instead of leaving Save permanently unready.
  if (bridge._stgModelRoutingLoadPromise) return bridge._stgModelRoutingLoadPromise;
  const loadPromise = (async () => {
    try {
      const response = await bridge.Api?.modelRouting?.get();
      const document = response && response.model_routing;
      if (!document || document.contract_version !== 'tofu.model-routing/v2') {
        throw new Error('model-routing v2 authority is unavailable');
      }
      if (preserveDraft && !draftUnchanged()) return null;
      bridge._stgModelRouting = JSON.parse(JSON.stringify(document));
      bridge._stgModelRoutingRevision = Number(response.revision || document.revision || 0);
      bridge._stgModelRoutingSavedJson = JSON.stringify(bridge._stgModelRouting);
      bridge._stgModelRoutingLoadError = '';
      bridge._stgPendingCredentialSecrets = {};
      if (preserveDraft && bridge._modelRoutingDropdownModels) {
        bridge._populateModelDropdown?.(bridge._modelRoutingDropdownModels(bridge._stgModelRouting));
      }
      return bridge._stgModelRouting;
    } catch (error) {
      if (preserveDraft) {
        bridge.debugLog?.('[Settings] Probe catalog refresh failed: ' + String(error), 'error');
        return null;
      }
      bridge._stgModelRouting = null;
      bridge._stgModelRoutingLoadError = String((error as Error)?.message || error || 'unknown');
      bridge.debugLog?.('[Settings] Failed to load model-routing v2: ' + bridge._stgModelRoutingLoadError, 'error');
      return null;
    } finally {
      bridge._stgModelRoutingLoadPromise = null;
    }
  })();
  bridge._stgModelRoutingLoadPromise = loadPromise;
  return loadPromise;
}

function modelRoutingDirty(): boolean {
  const bridge = globals();
  return !!bridge._stgModelRouting &&
    JSON.stringify(bridge._stgModelRouting) !== bridge._stgModelRoutingSavedJson;
}

/* Commit staged routing edits immediately (used by model adds) instead of
 * waiting for the global Save. One pass PUTs the whole staged aggregate and
 * adopts the committed document; edits staged while that PUT is in flight
 * keep the doc dirty, so the pass loops until the staged state is fully
 * committed. Callers chain onto _stgRoutingPersistChain, never onto an
 * ad-hoc promise, so passes always run sequentially. */
async function persistModelRoutingPass(): Promise<unknown> {
  const bridge = globals();
  let passes = 0;
  while (modelRoutingDirty()) {
    if (++passes > 5) throw new Error('model-routing persist did not settle');
    const sentJson = JSON.stringify(bridge._stgModelRouting);
    const response = await bridge.Api?.modelRouting?.replace(
      bridge._stgModelRouting as ModelRoutingDocument, bridge._stgModelRoutingRevision ?? 0);
    const committed = response && response.model_routing;
    if (!committed) throw new Error('model-routing save returned no document');
    bridge._stgModelRoutingRevision = Number(response.revision || 0);
    if (JSON.stringify(bridge._stgModelRouting) === sentJson) {
      bridge._stgModelRouting = committed;
    }
    bridge._stgModelRoutingSavedJson = JSON.stringify(committed);
  }
  return bridge._stgModelRouting;
}

function persistModelRoutingNow(): Promise<unknown> {
  const bridge = globals();
  if (!bridge._stgModelRouting) return Promise.resolve(null);
  const run = (bridge._stgRoutingPersistChain || Promise.resolve())
    .catch(() => null)
    .then(persistModelRoutingPass);
  bridge._stgRoutingPersistChain = run;
  return run;
}

function refreshSubscriptionModelCatalog(): Promise<CoreServerConfig | null> {
  const bridge = globals();
  return Promise.all([
    loadServerConfig(),
    loadModelRoutingAuthority(),
  ]).then((results) => {
    const cfg = results[0];
    if (!cfg) return null;
    if (typeof bridge._renderModelRoutingTabs === 'function') bridge._renderModelRoutingTabs();
    if (typeof bridge._renderPresetsTab === 'function') {
      bridge._renderPresetsTab(cfg);
    }
    if (typeof bridge._populateModelDropdown === 'function') {
      const routedModels = (typeof bridge._modelRoutingDropdownModels === 'function')
        ? bridge._modelRoutingDropdownModels(bridge._stgModelRouting) : [];
      bridge._populateModelDropdown(routedModels);
    }
    return cfg;
  });
}

function openSettings(): void {
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
  if (typeof _syncLangPicker === 'function') _syncLangPicker(_i18nLang || 'zh');

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
  document.getElementById('settingsModal')?.classList.add('open');
  const statusHint = document.getElementById('settingsStatusHint');
  if (statusHint) statusHint.textContent = '';

  // Load OAuth status
  bridge._loadOAuthStatus?.();

  // Show version + the mobile-client download link in the footer. Both read
  // from GET /api/health in one call. The link is config-gated: it renders only
  // when the server exposes `mobile_client_url` (TOFU_MOBILE_CLIENT_URL /
  // DEFAULT_MOBILE_CLIENT_URL) — otherwise it stays hidden so no dead link ever
  // ships before a release APK exists. Moved here from the topbar: it's a
  // one-time action, so it belongs in Settings rather than the always-visible
  // bar (see routes/common.py mobile_client_url).
  const verEl = document.getElementById('settingsVersion');
  void bridge.Api?.health?.info().then((d) => {
    if (verEl && d && d.version) verEl.textContent = 'v' + d.version;
    // Mirror the version into the About/Update card. The "New" pill is
    // rendered by update.js's own helper so the availability state has a
    // single source of truth (_updateState) rather than being re-derived here.
    const updVer = document.getElementById('settingsUpdateVersion');
    if (updVer && d && d.version) updVer.textContent = t('settings.updateCurrent', { version: d.version });
    if (typeof bridge._renderSettingsUpdatePill === 'function') {
      bridge._renderSettingsUpdatePill();
    }
    const mcCard = document.getElementById('settingsMobileCard');
    if (mcCard) {
      const url = d && d.mobile_client_url;
      if (url) {
        const androidRow = document.getElementById('settingsMobileAndroid');
        if (androidRow instanceof HTMLAnchorElement) androidRow.href = url;
        // Version badge comes from the backend (routes/common.py
        // MOBILE_CLIENT_VERSION, pinned to android/app/build.gradle.kts
        // versionName by tests/server/test_mobile_client_apk_url.py). Empty → hide
        // the badge rather than render a bare "v".
        const verEl2 = document.getElementById('settingsMobileAndroidVersion');
        if (verEl2) {
          const mcVer = d && d.mobile_client_version;
          verEl2.textContent = mcVer ? ('v' + mcVer) : '';
          verEl2.style.display = mcVer ? '' : 'none';
        }
        // iOS: an inert "coming soon" row until TOFU_IOS_CLIENT_URL ships a
        // real TestFlight/App Store link — then the row flips into an active
        // download and the badge becomes its version.
        const iosRow = document.getElementById('settingsMobileIos');
        if (iosRow instanceof HTMLAnchorElement) {
          const iosUrl = d && d.ios_client_url;
          if (iosUrl) {
            iosRow.href = iosUrl;
            iosRow.target = '_blank';
            iosRow.classList.remove('stg-mobile-row-soon');
            const iosBadge = document.getElementById('settingsMobileIosBadge');
            if (iosBadge) iosBadge.style.display = 'none';
          }
        }
        mcCard.style.display = '';
      } else {
        mcCard.style.display = 'none';
      }
    }
  }).catch(() => {});

  renderSettingsDesktopCard();

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
  void loadServerConfig().then(async (cfg) => {
    await modelRoutingLoad;
    if (!cfg) {
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
    bridge._renderPresetsTab?.(cfg);
    bridge._populateSearchTab?.(cfg);
    bridge._populateNetworkTab?.(cfg);
    bridge._populateAdvancedTab?.(cfg);
    bridge._populateFeishuTab?.(cfg);
    bridge._populateMtProviderSection?.(cfg);
    if (typeof bridge._populateSpeechTab === 'function') bridge._populateSpeechTab(cfg);
    bridge._populateMcpTab?.();
    if (typeof bridge._populateSkillsTab === 'function') bridge._populateSkillsTab();
    if (typeof bridge.populateToolsInventory === 'function') {
      bridge.populateToolsInventory();
    }
    if (typeof bridge._populatePreferencesTab === 'function') bridge._populatePreferencesTab();
    if (typeof bridge._populateDatabaseTab === 'function') bridge._populateDatabaseTab();
  });
}

const bridge = globals();
bridge._setVal = setVal;
bridge._getAllModels = getAllModels;
bridge._loadServerConfig = loadServerConfig;
bridge._loadModelRoutingAuthority = loadModelRoutingAuthority;
bridge._persistModelRoutingNow = persistModelRoutingNow;
bridge._refreshSubscriptionModelCatalog = refreshSubscriptionModelCatalog;
bridge.switchSettingsTab = switchSettingsTab;
bridge.openSettings = openSettings;
bridge._renderSettingsDesktopCard = renderSettingsDesktopCard;
