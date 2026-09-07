/* ===== migrated source: settings/core_panel.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   settings/core panel — extracted from settings.js (split 2026-05-28)

   Settings panel core: switchSettingsTab, _loadServerConfig, openSettings, _getAllModels.

   This file is concatenated by Vite's module graph — symbols share
   the same window scope as every other frontend/src/runtime/*.js file. No
   exports / imports needed.
   ═══════════════════════════════════════════════════════════════════ */

// ══════════════════════════════════════════════════════
//  Helpers
// ══════════════════════════════════════════════════════

/** Project available v2 Offerings into the legacy-shaped rows still consumed
 * by visibility/default selectors. No provider configuration is reconstructed. */
function _getAllModels() {
  var result = [];
  if (!_stgModelRouting) return result;
  var providers = new Map((_stgModelRouting.providers || []).map(function(row) {
    return [row.provider_id, row];
  }));
  var accesses = new Map((_stgModelRouting.provider_accesses || []).map(function(row) {
    return [row.provider_access_id, row];
  }));
  var models = new Map((_stgModelRouting.models || []).map(function(row) {
    return [(row.creator_id || '') + '\u0000' + (row.model_id || ''), row];
  }));
  var enabledOfferingIds = new Set(
    (_stgModelRouting.deployments || []).filter(function(row) {
      return row.enabled === true;
    }).map(function(row) { return row.offering_id; })
  );
  (_stgModelRouting.offerings || []).forEach(function(offering, offeringIndex) {
    var access = accesses.get(offering.provider_access_id);
    var provider = access && providers.get(access.provider_id);
    if (!access || !provider || access.enabled === false || offering.stale ||
        offering.enabled === false || !enabledOfferingIds.has(offering.offering_id)) return;
    var ref = offering.model || {};
    var official = models.get((ref.creator_id || '') + '\u0000' + (ref.model_id || ''));
    // Bare server-config defaults cannot encode a Provider+Offering identity;
    // pending identity is exposed only by the Provider-scoped quarantine
    // section of the model-first chat picker.
    if (!official) return;
    var projectedModel = Object.assign({}, official, {
      model_id: official.model_id,
      creator_id: official.creator_id,
      capabilities: offering.capabilities || [],
      offering_id: offering.offering_id,
      pending_identity: false,
    });
    var projectedProvider = {
      id: provider.provider_id,
      provider_id: provider.provider_id,
      name: access.display_name || provider.name || provider.provider_id,
      brand: provider.brand || '',
      enabled: access.enabled !== false,
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

function _setVal(id, value, prop) {
  var el = document.getElementById(id);
  if (!el) return;
  if (prop === 'checked') el.checked = !!value;
  else el.value = value;
}

// ══════════════════════════════════════════════════════
//  Tab switching & config loading
// ══════════════════════════════════════════════════════

function switchSettingsTab(tabId) {
  document.querySelectorAll('.settings-tab').forEach(function(t) {
    t.classList.toggle('active', t.dataset.tab === tabId);
  });
  document.querySelectorAll('.settings-tab-panel').forEach(function(p) {
    p.classList.toggle('active', p.id === 'settingsTab_' + tabId);
  });
  if (tabId === 'preferences' && typeof _populatePreferencesTab === 'function') {
    _populatePreferencesTab();
  }
  if (tabId === 'speech' && typeof _refreshSttStatus === 'function') {
    _refreshSttStatus();
  }
  if (tabId === 'devices' && typeof _populateDevicesTab === 'function') {
    _populateDevicesTab();
  }
}

async function _loadServerConfig() {
  try {
    debugLog('[Settings] Loading server config…', 'info');
    _serverConfig = await Api.serverConfig.get();
    if (!_serverConfig) throw new Error('serverConfig.get returned null');
    debugLog('[Settings] Misc server config loaded: ' + Object.keys(_serverConfig.presets || {}).length + ' presets; providers load from model-routing v2', 'info');
    // Populate the read-only card caches: canonical model rates plus the
    // server-owned USD pivots used only for localized Settings presentation.
    if (_serverConfig.model_pricing) {
      runtimeScope._setModelPricingCache(_serverConfig.model_pricing);
    }
    if (_serverConfig.model_price_display &&
        _serverConfig.model_price_display.usd_rates) {
      _modelPriceDisplayPolicy = _serverConfig.model_price_display;
    }
    return _serverConfig;
  } catch (e) {
    debugLog('[Settings] Failed to load server config: ' + (e && e.message), 'error');
    return null;
  }
}

function _loadModelRoutingAuthority() {
  // Opening Settings, refreshing the provider catalogue, and Save may all
  // need this authority at once. Share one read so a slower response cannot
  // overwrite a newer staged revision, and let a failed read be retried by
  // the next caller instead of leaving Save permanently unready.
  if (_stgModelRoutingLoadPromise) return _stgModelRoutingLoadPromise;
  _stgModelRoutingLoadPromise = (async function() {
    try {
      var response = await Api.modelRouting.get();
      var document = response && response.model_routing;
      if (!document || document.contract_version !== 'tofu.model-routing/v2') {
        throw new Error('model-routing v2 authority is unavailable');
      }
      _stgModelRouting = JSON.parse(JSON.stringify(document));
      _stgModelRoutingRevision = Number(response.revision || document.revision || 0);
      _stgModelRoutingLoadError = '';
      _stgPendingCredentialSecrets = {};
      return _stgModelRouting;
    } catch (error) {
      _stgModelRouting = null;
      _stgModelRoutingLoadError = String(error && error.message || error || 'unknown');
      debugLog('[Settings] Failed to load model-routing v2: ' + _stgModelRoutingLoadError, 'error');
      return null;
    } finally {
      _stgModelRoutingLoadPromise = null;
    }
  })();
  return _stgModelRoutingLoadPromise;
}

function _refreshSubscriptionModelCatalog() {
  return Promise.all([
    _loadServerConfig(),
    _loadModelRoutingAuthority(),
  ]).then(function(results) {
    var cfg = results[0];
    if (!cfg) return null;
    if (typeof _renderProvidersTab === 'function') _renderProvidersTab();
    if (typeof _renderPresetsTab === 'function') {
      _renderPresetsTab(cfg);
    }
    if (typeof _populateModelDropdown === 'function') {
      var routedModels = (typeof _modelRoutingDropdownModels === 'function')
        ? _modelRoutingDropdownModels(_stgModelRouting) : [];
      _populateModelDropdown(routedModels);
    }
    return cfg;
  });
}

function openSettings() {
  // ── General tab: populate from local config ──
  document.getElementById("settingTemp").value = config.temperature;
  document.getElementById("tempVal").textContent = config.temperature;
  document.getElementById("settingMaxTokens").value = config.maxTokens;
  // imageMaxWidth: 0 = follow server policy (recommended). >0 = user override that
  // can only TIGHTEN the server cap. Show 0 in the field for users on the new default.
  document.getElementById("settingImageMaxWidth").value =
    (typeof config.imageMaxWidth === 'number' ? config.imageMaxWidth : 0);
  document.getElementById("settingSystem").value = config.systemPrompt || "";
  var spModeSel = document.getElementById('settingSystemPromptMode');
  if (spModeSel) spModeSel.value = (config.systemPromptMode === 'replace') ? 'replace' : 'append';
  var spbEl = document.getElementById('settingSystemDisabledBlocks');
  if (spbEl) {
    var _disabled = (config.systemPromptBlocks && Array.isArray(config.systemPromptBlocks.disabled))
      ? config.systemPromptBlocks.disabled : [];
    spbEl.value = JSON.stringify(_disabled);
  }
  if (typeof _refreshSystemPromptSummary === 'function') _refreshSystemPromptSummary();

  // Default thinking depth
  var dtd = document.getElementById('settingDefaultThinkingDepth');
  if (dtd) dtd.value = config.defaultThinkingDepth || 'off';

  // Language selector sync
  var langSel = document.getElementById('settingLanguage');
  if (langSel) langSel.value = typeof _i18nLang !== 'undefined' ? _i18nLang : 'zh';
  if (typeof _syncLangPicker === 'function') _syncLangPicker(typeof _i18nLang !== 'undefined' ? _i18nLang : 'zh');

  // Trading module toggle. No restart hint: the flag is enforced live by the
  // plugin (request-time guard + per-pass check in its background workers), so
  // a flip takes effect immediately — see tofu_trading/gate.py.
  var tradingCb = document.getElementById('settingTradingEnabled');
  if (tradingCb) {
    tradingCb.checked = !!runtimeScope._featureFlags?.trading_enabled;
  }

  // PPTX translate module toggle
  var pptxCb = document.getElementById('settingPptxTranslateEnabled');
  if (pptxCb) {
    pptxCb.checked = !!runtimeScope._featureFlags?.pptx_translate_enabled;
  }

  // Debug mode toggle
  var debugCb = document.getElementById('settingDebugMode');
  if (debugCb) {
    debugCb.checked = !!runtimeScope._featureFlags?.debug_mode;
  }

  // Daily Optimizer toggle — default ON when flag not yet in features.json
  var optCb = document.getElementById('settingOptimizerEnabled');
  if (optCb) {
    var _optFlag = runtimeScope._featureFlags?.optimizer_enabled;
    optCb.checked = (_optFlag === undefined) ? true : !!_optFlag;
  }

  // Auto-generate conversation title toggle — defaults to false (manual)
  var agtCb = document.getElementById('settingAutoGenerateTitle');
  if (agtCb) {
    agtCb.checked = !!config.autoGenerateTitle;
  }

  // Input send mode — defaults to 'enter'
  var ismSel = document.getElementById('settingInputSendMode');
  if (ismSel) {
    ismSel.value = (config.inputSendMode === 'ctrl_enter') ? 'ctrl_enter' : 'enter';
  }

  // Theme picker sync
  var ct = _getCurrentTheme();
  document.querySelectorAll(".theme-option").forEach(function(el) {
    el.classList.toggle("active", el.dataset.theme === ct);
  });

  switchSettingsTab('general');
  /* Degraded-section contract: hide the controls of any block whose JS
   * dependency is absent (stale bundle) and show its "needs restart" notice,
   * so no section can look usable while being dead. Runs AFTER the pickers
   * above so a block that DID render is not wrongly degraded. */
  if (typeof applySectionRequirements === 'function') applySectionRequirements();
  document.getElementById("settingsModal").classList.add("open");
  document.getElementById('settingsStatusHint').textContent = '';

  // Load OAuth status
  _loadOAuthStatus();

  // Show version + the mobile-client download link in the footer. Both read
  // from GET /api/health in one call. The link is config-gated: it renders only
  // when the server exposes `mobile_client_url` (TOFU_MOBILE_CLIENT_URL /
  // DEFAULT_MOBILE_CLIENT_URL) — otherwise it stays hidden so no dead link ever
  // ships before a release APK exists. Moved here from the topbar: it's a
  // one-time action, so it belongs in Settings rather than the always-visible
  // bar (see routes/common.py mobile_client_url).
  var verEl = document.getElementById('settingsVersion');
  Api.health.info().then(function(d){
    if (verEl && d && d.version) verEl.textContent = 'v' + d.version;
    // Mirror the version into the About/Update card. The "New" pill is
    // rendered by update.js's own helper so the availability state has a
    // single source of truth (_updateState) rather than being re-derived here.
    var updVer = document.getElementById('settingsUpdateVersion');
    if (updVer && d && d.version) updVer.textContent = t('settings.updateCurrent', { version: d.version });
    if (typeof runtimeScope._renderSettingsUpdatePill === 'function') {
      runtimeScope._renderSettingsUpdatePill();
    }
    var mcCard = document.getElementById('settingsMobileCard');
    if (mcCard) {
      var url = d && d.mobile_client_url;
      if (url) {
        var androidRow = document.getElementById('settingsMobileAndroid');
        if (androidRow) androidRow.href = url;
        // Version badge comes from the backend (routes/common.py
        // MOBILE_CLIENT_VERSION, pinned to android/app/build.gradle.kts
        // versionName by tests/test_mobile_client_apk_url.py). Empty → hide
        // the badge rather than render a bare "v".
        var verEl2 = document.getElementById('settingsMobileAndroidVersion');
        if (verEl2) {
          var mcVer = d && d.mobile_client_version;
          verEl2.textContent = mcVer ? ('v' + mcVer) : '';
          verEl2.style.display = mcVer ? '' : 'none';
        }
        // iOS: an inert "coming soon" row until TOFU_IOS_CLIENT_URL ships a
        // real TestFlight/App Store link — then the row flips into an active
        // download and the badge becomes its version.
        var iosRow = document.getElementById('settingsMobileIos');
        if (iosRow) {
          var iosUrl = d && d.ios_client_url;
          if (iosUrl) {
            iosRow.href = iosUrl;
            iosRow.target = '_blank';
            iosRow.classList.remove('stg-mobile-row-soon');
            var iosBadge = document.getElementById('settingsMobileIosBadge');
            if (iosBadge) iosBadge.style.display = 'none';
          }
        }
        mcCard.style.display = '';
      } else {
        mcCard.style.display = 'none';
      }
    }
  }).catch(function(){});

  // Show loading states
  var provList = document.getElementById('stgProviderList');
  if (provList) provList.innerHTML = '<p class="stg-loading">' + t('settings.loadingConfig') + '</p>';
  var modelCatalog = document.getElementById('stgModelCatalog');
  if (modelCatalog) modelCatalog.innerHTML = '<p class="stg-loading">' + t('settings.loadingConfig') + '</p>';
  var presetTable = document.getElementById('stgPresetTable');
  if (presetTable) presetTable.innerHTML = '<p class="stg-loading">' + t('settings.loading') + '</p>';

  // Load the owner model-routing authority independently from miscellaneous
  // server settings.  The two reads may run together, but only v2 is allowed
  // to populate the provider/model editor.
  var _modelRoutingLoad = _loadModelRoutingAuthority();
  _loadServerConfig().then(async function(cfg) {
    await _modelRoutingLoad;
    if (!cfg) {
      document.getElementById('settingsStatusHint').textContent = t('settings.serverConfigFailed');
      if (provList) provList.innerHTML = '<p class="stg-empty">' + t('settings.loadingFailed') + '</p>';
      if (modelCatalog) modelCatalog.innerHTML = '<p class="stg-empty">' + t('settings.loadingFailed') + '</p>';
      if (presetTable) presetTable.innerHTML = '<p class="stg-empty">加载模型预设失败。</p>';
      debugLog('[Settings] Config load failed — provider list and preset table set to error state', 'warning');
      return;
    }
    _stgPresets = JSON.parse(JSON.stringify(cfg.presets || {}));
    _renderProvidersTab();
    _renderPresetsTab(cfg);
    _populateSearchTab(cfg);
    _populateNetworkTab(cfg);
    _populateAdvancedTab(cfg);
    _populateFeishuTab(cfg);
    _populateMtProviderSection(cfg);
    if (typeof _populateSpeechTab === 'function') _populateSpeechTab(cfg);
    _populateMcpTab();
    if (typeof _populateSkillsTab === 'function') _populateSkillsTab();
    if (typeof runtimeScope.populateToolsInventory === 'function') {
      runtimeScope.populateToolsInventory();
    }
    if (typeof _populatePreferencesTab === 'function') _populatePreferencesTab();
  });
}

// ══════════════════════════════════════════════════════
//  Providers Tab — Provider CRUD + nested model list
// ══════════════════════════════════════════════════════
