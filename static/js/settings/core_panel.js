/* ═══════════════════════════════════════════════════════════════════
   settings/core panel — extracted from settings.js (split 2026-05-28)

   Settings panel core: switchSettingsTab, _loadServerConfig, openSettings, _getAllModels.

   This file is concatenated by lib/js_bundler.py — symbols share
   the same window scope as every other static/js/*.js file. No
   exports / imports needed.
   ═══════════════════════════════════════════════════════════════════ */

// ══════════════════════════════════════════════════════
//  Helpers
// ══════════════════════════════════════════════════════

/** Collect all models from all providers (flat list with provider info) */
function _getAllModels() {
  var result = [];
  for (var pi = 0; pi < _stgProviders.length; pi++) {
    var p = _stgProviders[pi];
    var models = p.models || [];
    for (var mi = 0; mi < models.length; mi++) {
      result.push({ model: models[mi], provider: p, provIdx: pi, modelIdx: mi });
    }
  }
  return result;
}

function _setVal(id, value, prop) {
  var el = document.getElementById(id);
  if (!el) return;
  if (prop === 'checked') el.checked = !!value;
  else el.value = value;
}

// ══════════════════════════════════════════════════════
//  Model list ordering — cold sort + insertion sort
// ══════════════════════════════════════════════════════
//
// Each provider's model list is kept alphabetically ordered by model_id.
// To avoid re-sorting on every render (which would make rows jump around
// while editing), the full sort runs only ONCE per editor session — a
// "cold sort" when the working copy is loaded (_coldSortAllProviderModels
// in openSettings). In-session additions (auto-discover / template sync /
// add / rename) keep the order via _insertModelSorted (binary-search
// insertion). The next settings-open cold-sorts again from scratch.

/** Case-insensitive sort key for a model entry. */
function _modelSortKey(m) {
  return ((m && m.model_id) || '').toLowerCase();
}

/** One-time full sort of a provider's model list (in place, by model_id). */
function _coldSortModels(models) {
  if (!Array.isArray(models)) return models;
  models.sort(function(a, b) {
    var ka = _modelSortKey(a), kb = _modelSortKey(b);
    return ka < kb ? -1 : (ka > kb ? 1 : 0);
  });
  return models;
}

/** Cold-sort every provider's model list (called once on config load). */
function _coldSortAllProviderModels() {
  for (var i = 0; i < _stgProviders.length; i++) {
    if (_stgProviders[i] && Array.isArray(_stgProviders[i].models)) {
      _coldSortModels(_stgProviders[i].models);
    }
  }
}

/** Insert one model into an already-sorted list at its alphabetical
 *  position (binary search). Cheap incremental upkeep so freshly added or
 *  renamed models land correctly without re-sorting the whole list. */
function _insertModelSorted(models, m) {
  if (!Array.isArray(models)) return;
  var key = _modelSortKey(m);
  var lo = 0, hi = models.length;
  while (lo < hi) {
    var mid = (lo + hi) >> 1;
    if (_modelSortKey(models[mid]) <= key) lo = mid + 1;
    else hi = mid;
  }
  models.splice(lo, 0, m);
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
}

async function _loadServerConfig() {
  try {
    debugLog('[Settings] Loading server config…', 'info');
    _serverConfig = await Api.serverConfig.get();
    if (!_serverConfig) throw new Error('serverConfig.get returned null');
    debugLog('[Settings] Server config loaded: ' + (_serverConfig.providers || []).length + ' providers, ' + Object.keys(_serverConfig.presets || {}).length + ' presets', 'info');
    // Populate pricing cache so model cards can look up input/output costs
    if (_serverConfig.model_pricing && typeof _modelPricingCache !== 'undefined') {
      _modelPricingCache = _serverConfig.model_pricing;
    }
    return _serverConfig;
  } catch (e) {
    debugLog('[Settings] Failed to load server config: ' + (e && e.message), 'error');
    return null;
  }
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

  // Trading module toggle
  var tradingCb = document.getElementById('settingTradingEnabled');
  if (tradingCb) {
    tradingCb.checked = !!(typeof _featureFlags !== 'undefined' && _featureFlags.trading_enabled);
    tradingCb.onchange = function() {
      document.getElementById('tradingRestartHint').style.display =
        (this.checked !== !!(typeof _featureFlags !== 'undefined' && _featureFlags.trading_enabled)) ? 'block' : 'none';
    };
  }

  // PPTX translate module toggle
  var pptxCb = document.getElementById('settingPptxTranslateEnabled');
  if (pptxCb) {
    pptxCb.checked = !!(typeof _featureFlags !== 'undefined' && _featureFlags.pptx_translate_enabled);
  }

  // Debug mode toggle
  var debugCb = document.getElementById('settingDebugMode');
  if (debugCb) {
    debugCb.checked = !!(typeof _featureFlags !== 'undefined' && _featureFlags.debug_mode);
  }

  // Daily Optimizer toggle — default ON when flag not yet in features.json
  var optCb = document.getElementById('settingOptimizerEnabled');
  if (optCb) {
    var _optFlag = (typeof _featureFlags !== 'undefined') ? _featureFlags.optimizer_enabled : undefined;
    optCb.checked = (_optFlag === undefined) ? true : !!_optFlag;
  }

  // Keep Tool History toggle — defaults to true
  var kthCb = document.getElementById('settingKeepToolHistory');
  if (kthCb) {
    kthCb.checked = config.keepToolHistory !== false; // default true
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
  document.getElementById("settingsModal").classList.add("open");
  document.getElementById('settingsStatusHint').textContent = '';

  // Load OAuth status
  _loadOAuthStatus();

  // Show version in footer
  var verEl = document.getElementById('settingsVersion');
  if (verEl) {
    Api.health.info().then(function(d){
      if(d && d.version) verEl.textContent = 'v' + d.version;
    }).catch(function(){});
  }

  // Show loading states
  var provList = document.getElementById('stgProviderList');
  if (provList) provList.innerHTML = '<p class="stg-loading">' + t('settings.loadingConfig') + '</p>';
  var presetTable = document.getElementById('stgPresetTable');
  if (presetTable) presetTable.innerHTML = '<p class="stg-loading">' + t('settings.loading') + '</p>';

  // ── Load server config for other tabs ──
  _loadServerConfig().then(function(cfg) {
    if (!cfg) {
      document.getElementById('settingsStatusHint').textContent = t('settings.serverConfigFailed');
      if (provList) provList.innerHTML = '<p class="stg-empty">' + t('settings.loadingFailed') + '</p>';
      if (presetTable) presetTable.innerHTML = '<p class="stg-empty">加载模型预设失败。</p>';
      debugLog('[Settings] Config load failed — provider list and preset table set to error state', 'warning');
      return;
    }
    // Deep-copy providers (they include nested models now)
    _stgProviders = JSON.parse(JSON.stringify(cfg.providers || []));
    _stgPresets = JSON.parse(JSON.stringify(cfg.presets || {}));

    // One-time cold sort: order every provider's model list alphabetically
    // by model_id. In-session additions stay ordered via _insertModelSorted,
    // and the next settings-open cold-sorts again from scratch.
    _coldSortAllProviderModels();

    // Pre-load external templates so sync buttons appear on first render
    _loadExternalProviderTemplates().finally(function() {
      _renderProvidersTab();
      // Start auto-polling balance for all eligible providers
      _startBalancePolling();
      // Fetch today's per-key success-rate stats and refresh inline badges.
      _loadKeyStats();
      // Auto-poll per-endpoint live metrics (TTFT/latency/throughput/success)
      // so local-deployment status stays fresh without manual probing.
      _startLocalMetricsPolling();
    });
    _renderPresetsTab(cfg);
    _populateSearchTab(cfg);
    _populateNetworkTab(cfg);
    _populateAdvancedTab(cfg);
    _populateFeishuTab(cfg);
    _populateMtProviderSection(cfg);
    _populateMcpTab();
    if (typeof _populateSkillsTab === 'function') _populateSkillsTab();
    if (typeof _populatePreferencesTab === 'function') _populatePreferencesTab();
  });
}

// ══════════════════════════════════════════════════════
//  Providers Tab — Provider CRUD + nested model list
// ══════════════════════════════════════════════════════

