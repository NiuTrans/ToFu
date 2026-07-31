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
// Each provider's model list is kept ordered by the DISPLAY name the user
// actually reads (_modelShortName), NOT the raw model_id — sorting by id put
// `yuju-claude-opus-5-evaDaily` (label "Claude Opus 5") under 'y', far from the
// other Claude entries. The comparator itself lives in settings/branding.js
// (_compareModelsByDisplayName) and is shared with the toolbar model picker so
// the two lists can never disagree about order.
//
// To avoid re-sorting on every render (which would make rows jump around
// while editing), the full sort runs only ONCE per editor session — a
// "cold sort" when the working copy is loaded (_coldSortAllProviderModels
// in openSettings). In-session additions (auto-discover / template sync /
// add / rename) keep the order via _insertModelSorted (binary-search
// insertion). The next settings-open cold-sorts again from scratch.

/** Order two model entries by display name. Delegates to the shared
 *  comparator; falls back to a raw model_id compare only if branding.js is
 *  absent (stale bundle) so the list still renders in a stable order. */
function _compareModelEntries(a, b) {
  if (typeof _compareModelsByDisplayName === 'function') {
    return _compareModelsByDisplayName(a, b);
  }
  var ka = ((a && a.model_id) || '').toLowerCase();
  var kb = ((b && b.model_id) || '').toLowerCase();
  return ka < kb ? -1 : (ka > kb ? 1 : 0);
}

/** One-time full sort of a provider's model list (in place, by display name). */
function _coldSortModels(models) {
  if (!Array.isArray(models)) return models;
  models.sort(_compareModelEntries);
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

/** Insert one model into an already-sorted list at its display-name position
 *  (binary search). Cheap incremental upkeep so freshly added or renamed
 *  models land correctly without re-sorting the whole list. */
function _insertModelSorted(models, m) {
  if (!Array.isArray(models)) return;
  var lo = 0, hi = models.length;
  while (lo < hi) {
    var mid = (lo + hi) >> 1;
    if (_compareModelEntries(models[mid], m) <= 0) lo = mid + 1;
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
  // The matrix-wide panel class only makes sense on the providers tab —
  // re-fit so switching away shrinks the panel back to 860px.
  if (typeof _fitMatrixPanelWidth === 'function') _fitMatrixPanelWidth();
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

  // Trading module toggle. No restart hint: the flag is enforced live by the
  // plugin (request-time guard + per-pass check in its background workers), so
  // a flip takes effect immediately — see tofu_trading/gate.py.
  var tradingCb = document.getElementById('settingTradingEnabled');
  if (tradingCb) {
    tradingCb.checked = !!(typeof _featureFlags !== 'undefined' && _featureFlags.trading_enabled);
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
    if (typeof _renderSettingsUpdatePill === 'function') _renderSettingsUpdatePill();
    var mcEl = document.getElementById('settingsMobileClient');
    if (mcEl) {
      var url = d && d.mobile_client_url;
      if (url) {
        mcEl.href = url;
        mcEl.innerHTML = '<img ' + brandLogoImgAttrs(15) + '> ' + t('settings.mobileClient');
        mcEl.style.display = '';
      } else {
        mcEl.style.display = 'none';
      }
    }
  }).catch(function(){});

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
    _stgFaceRefusals = Array.isArray(cfg.face_refusals) ? cfg.face_refusals : [];
    _stgPresets = JSON.parse(JSON.stringify(cfg.presets || {}));

    // One-time cold sort: order every provider's model list by the DISPLAY
    // name (shared comparator, same one the toolbar picker uses). In-session
    // additions stay ordered via _insertModelSorted, and the next
    // settings-open cold-sorts again from scratch.
    _coldSortAllProviderModels();

    // Pre-load external templates so sync buttons appear on first render
    _loadExternalProviderTemplates().finally(function() {
      _renderProvidersTab();
      // Start auto-polling balance for all eligible providers
      _startBalancePolling();
      // Fetch today's per-key success-rate stats and refresh inline badges.
      _loadKeyStats();
      // Fetch per-model runtime health (success rate / error-throttle
      // cooldowns) and keep it fresh while the panel is open.
      if (typeof _startModelHealthPolling === 'function') _startModelHealthPolling();
      // Resolve each provider's wire faces (backend resolve_face — the SAME
      // resolver the dispatcher uses) so the model cards can show which
      // protocol each model actually dispatches over.
      if (typeof _refreshAllFaceResolutions === 'function') _refreshAllFaceResolutions();
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
    if (typeof _populateSpeechTab === 'function') _populateSpeechTab(cfg);
    _populateMcpTab();
    if (typeof _populateSkillsTab === 'function') _populateSkillsTab();
    if (typeof _populatePreferencesTab === 'function') _populatePreferencesTab();
  });
}

// ══════════════════════════════════════════════════════
//  Providers Tab — Provider CRUD + nested model list
// ══════════════════════════════════════════════════════

