/* ===== migrated source: settings/save_export.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   settings/save export — extracted from settings.js (split 2026-05-28)

   Save/export/import server config: closeSettings, saveSettings, exportServerConfig, importServerConfig.

   This file is concatenated by Vite's module graph — symbols share
   the same window scope as every other frontend/src/runtime/*.js file. No
   exports / imports needed.
   ═══════════════════════════════════════════════════════════════════ */

// ══════════════════════════════════════════════════════
//  Close / Save / Export / Import
// ══════════════════════════════════════════════════════

function closeSettings() {
  for (const name of [
    '_destroyPrivateHosts', '_destroyBrowserAccess',
    '_destroyDevicesTab',
    '_destroySpeechTab', '_destroyCredentialsVault', '_destroyAuthSources',
    '_destroyModelCatalogPanel',
  ]) {
    const cleanup = runtimeScope[name];
    if (typeof cleanup === 'function') cleanup();
  }
  if (typeof runtimeScope._destroySkills === 'function') {
    runtimeScope._destroySkills();
  }
  if (typeof runtimeScope._destroyPreferences === 'function') {
    runtimeScope._destroyPreferences();
  }
  document.getElementById("settingsModal").classList.remove("open");
  // Refresh model dropdown to reflect any visibility changes
  if (typeof _populateModelDropdown === 'function' && runtimeScope._registeredModels.length > 0) {
    _populateModelDropdown(runtimeScope._registeredModels);
    _applyModelUI(config.model);
  }
  // Refresh image gen picker to reflect visibility changes
  if (typeof runtimeScope._loadIgModels === 'function') {
    void runtimeScope._loadIgModels();
  }
}

/* Save is a long awaited chain (STT persist → model-routing replace →
   credential secrets → server config update → config reload) with no other
   visible feedback: without a busy latch the button looks dead on a slow
   network, and a second click races modelRouting.replace with the same
   expected_revision (409 conflict). While busy the button is disabled and
   the footer hint shows 保存中…; any throw anywhere in the body is surfaced
   in that same hint instead of dying silently in the action registry. */
var _settingsSaveBusy = false;

function _setSettingsSaveBusy(busy) {
  _settingsSaveBusy = busy;
  var btn = document.getElementById('settingsSaveBtn');
  if (btn) btn.disabled = busy;
  var hint = document.getElementById('settingsStatusHint');
  if (!hint) return;
  if (busy) hint.textContent = t('common.saving');
  else if (hint.textContent === t('common.saving')) hint.textContent = '';
}

function _settingsSaveFailed(e) {
  var message = (e && e.message) ? e.message : String(e);
  debugLog('[Settings] Save failed: ' + message, 'error');
  var statusHint = document.getElementById('settingsStatusHint');
  if (statusHint) statusHint.textContent = '保存失败：' + message;
}

async function saveSettings() {
  if (_settingsSaveBusy) return;
  _setSettingsSaveBusy(true);
  try {
    await _saveSettingsBody();
  } catch (e) {
    _settingsSaveFailed(e);
  } finally {
    _setSettingsSaveBusy(false);
  }
}

async function _saveSettingsBody() {
  // 1. Client-side config (General tab)
  if (typeof _collectResponsesExperimentControls === 'function') {
    _collectResponsesExperimentControls();
  }
  config.temperature = parseFloat(document.getElementById("settingTemp").value);
  config.maxTokens = parseInt(document.getElementById("settingMaxTokens").value);
  config.imageMaxWidth = parseInt(document.getElementById("settingImageMaxWidth").value) || 0;
  config.systemPrompt = document.getElementById("settingSystem").value;
  var spModeSel = document.getElementById('settingSystemPromptMode');
  if (spModeSel) config.systemPromptMode = (spModeSel.value === 'replace') ? 'replace' : 'append';
  var spbEl = document.getElementById('settingSystemDisabledBlocks');
  if (spbEl) {
    var _disabledIds = [];
    try { _disabledIds = JSON.parse(spbEl.value || '[]'); } catch (e) { _disabledIds = []; }
    if (!Array.isArray(_disabledIds)) _disabledIds = [];
    config.systemPromptBlocks = { disabled: _disabledIds };
  }
  var dtdEl = document.getElementById('settingDefaultThinkingDepth');
  if (dtdEl) {
    var oldDefault = config.defaultThinkingDepth;
    config.defaultThinkingDepth = dtdEl.value || 'off';
    // Propagate: if current depth was the old default, update it to the new default
    if (config.thinkingDepth === oldDefault) {
      config.thinkingDepth = config.defaultThinkingDepth;
    }
  }
  // Auto-generate conversation title toggle
  var agtCb = document.getElementById('settingAutoGenerateTitle');
  if (agtCb) {
    config.autoGenerateTitle = agtCb.checked;
  }

  // Input send mode
  var ismSel = document.getElementById('settingInputSendMode');
  if (ismSel) {
    config.inputSendMode = (ismSel.value === 'ctrl_enter') ? 'ctrl_enter' : 'enter';
    if (typeof refreshInputSendHint === 'function') refreshInputSendHint();
  }

  try { localStorage.setItem("claude_client_config", JSON.stringify(_configForPersist())); }
  catch (e) { debugLog('[saveSettings] localStorage save failed: ' + e.message, 'error'); }

  // 2. Feature flags (trading toggle)
  var tradingCb = document.getElementById('settingTradingEnabled');
  if (tradingCb) {
    var newVal = tradingCb.checked;
    var curVal = !!runtimeScope._featureFlags?.trading_enabled;
    if (newVal !== curVal) {
      Api.features.set({ trading_enabled: newVal })
        .then(function(r) { return r ? r.json() : {}; }).then(function(data) {
        if (data && data.ok) {
          debugLog('Trading module ' + (newVal ? 'enabled' : 'disabled') + ' — applied', 'success');
          if (runtimeScope._featureFlags) runtimeScope._featureFlags.trading_enabled = newVal;
          // Show/hide the topbar entry immediately. The backend enforces the
          // same flag per request and in its background workers, so this is
          // presentation only — nothing here is what stops the module.
          if (typeof _applyTradingVisibility === 'function') {
            _applyTradingVisibility();
          }
        }
      }).catch(function(e) { debugLog('Feature flag save failed: ' + e.message, 'error'); });
    }
  }

  // 2b. PPTX translate toggle
  var pptxCb = document.getElementById('settingPptxTranslateEnabled');
  if (pptxCb) {
    var newPptx = pptxCb.checked;
    var curPptx = !!runtimeScope._featureFlags?.pptx_translate_enabled;
    if (newPptx !== curPptx) {
      Api.features.set({ pptx_translate_enabled: newPptx })
        .then(function(r) { return r ? r.json() : {}; }).then(function(data) {
        if (data && data.ok) {
          debugLog('PPTX translate ' + (newPptx ? 'enabled' : 'disabled'), 'success');
          if (runtimeScope._featureFlags) runtimeScope._featureFlags.pptx_translate_enabled = newPptx;
        }
      }).catch(function(e) { debugLog('Feature flag save failed: ' + e.message, 'error'); });
    }
  }

  // 2c. Debug mode toggle
  var debugCb = document.getElementById('settingDebugMode');
  if (debugCb) {
    var newDbg = debugCb.checked;
    var curDbg = !!runtimeScope._featureFlags?.debug_mode;
    if (newDbg !== curDbg) {
      Api.features.set({ debug_mode: newDbg })
        .then(function(r) { return r ? r.json() : {}; }).then(function(data) {
        if (data && data.ok) {
          debugLog('Debug mode ' + (newDbg ? 'enabled' : 'disabled'), 'success');
          if (runtimeScope._featureFlags) runtimeScope._featureFlags.debug_mode = newDbg;
          // Show/hide unfinished orchestration surfaces (Flow submenu, Studio /
          // Tasks topbar + mobile-sheet items). See index.html loadFeatureFlags.
          if (typeof _applyDebugModeVisibility === 'function') {
            _applyDebugModeVisibility();
          }
          // Re-render sidebar and the authoritative Surface so debug
          // presentation follows the updated setting.
          if (typeof renderConversationList === 'function') renderConversationList();
          if (typeof getActiveConv === 'function') {
            var _dbgConv = getActiveConv();
            if (_dbgConv) runtimeScope.requestAuthoritativeConversationRender(
              _dbgConv.id, { forceScroll: true },
            );
          }
        }
      }).catch(function(e) { debugLog('Feature flag save failed: ' + e.message, 'error'); });
    }
  }

  // 2d. Daily Optimizer toggle
  var optCb = document.getElementById('settingOptimizerEnabled');
  if (optCb) {
    var newOpt = optCb.checked;
    var _curFlag = runtimeScope._featureFlags?.optimizer_enabled;
    var curOpt = (_curFlag === undefined) ? true : !!_curFlag;
    if (newOpt !== curOpt) {
      Api.features.set({ optimizer_enabled: newOpt })
        .then(function(r) { return r ? r.json() : {}; }).then(function(data) {
        if (data && data.ok) {
          debugLog('Daily Optimizer ' + (newOpt ? 'enabled' : 'disabled'), 'success');
          if (runtimeScope._featureFlags) runtimeScope._featureFlags.optimizer_enabled = newOpt;
          // Show/hide the topbar badge immediately.
          var badge = document.getElementById('optimizerBadge');
          if (badge) badge.style.display = newOpt ? 'inline-flex' : 'none';
        }
      }).catch(function(e) { debugLog('Feature flag save failed: ' + e.message, 'error'); });
    }
  }

  // 3. Server config (Providers / Presets / Search)
  if (_serverConfig) {
    // openSettings() intentionally loads miscellaneous config and the routing
    // authority concurrently. A fast Save must join that authority read; if
    // the initial read failed, this also performs one fresh retry.
    if (_stgModelRoutingLoadPromise || !_stgModelRouting) {
      var readyModelRouting = await _loadModelRoutingAuthority();
      if (!readyModelRouting) {
        throw new Error(
          _stgModelRoutingLoadError || 'model-routing v2 authority is unavailable');
      }
    }
    if (typeof _persistSttProvider === 'function') {
      await _persistSttProvider();
    }
    var saved = await _saveServerConfig();
    if (!saved) return;
  }

  debugLog("Settings saved", "success");
  closeSettings();
}

async function _saveServerConfig() {
  // Strip empty preset mappings — especially 'opus' should never be pinned
  // to a specific version; leaving it unset lets the code default (LLM_MODEL) apply.
  var cleanPresets = {};
  for (var k in _stgPresets) {
    if (_stgPresets[k]) cleanPresets[k] = _stgPresets[k];
  }

  var payload = {
    presets: cleanPresets,
    search: {},
    hidden_models: (_serverConfig && _serverConfig.hidden_models) || [],
    hidden_ig_models: (_serverConfig && _serverConfig.hidden_ig_models) || [],
    model_defaults: _collectModelDefaults(),
  };
  if (typeof _collectCostExperimentConfig === 'function') {
    var _costExperimentCfg = _collectCostExperimentConfig();
    if (_costExperimentCfg !== null) payload.cost_experiment = _costExperimentCfg;
  }
  // Search tab
  var cfCb = document.getElementById('settingLlmContentFilter');
  var profileEl = document.getElementById('settingSearchProfile');
  var profileVal = (profileEl && profileEl.value) || 'balanced';
  // 自定义 is a UI state, not a wire profile: send the last real preset as
  // the base plus the four concrete knob values as overrides.
  var customProfile = profileVal === 'custom';
  payload.search.profile = customProfile
    ? (typeof _searchLastPreset !== 'undefined' ? _searchLastPreset : 'balanced')
    : profileVal;
  payload.search.llm_content_filter = cfCb ? cfCb.checked : true;
  payload.search.fetch_top_n = parseInt(document.getElementById('settingFetchTopN')?.value) || 6;
  payload.search.fetch_timeout = parseInt(document.getElementById('settingFetchTimeout')?.value) || 15;
  payload.search.max_chars_search = parseInt(document.getElementById('settingMaxCharsSearch')?.value) || 60000;
  payload.search.max_chars_direct = parseInt(document.getElementById('settingMaxCharsDirect')?.value) || 200000;
  payload.search.max_chars_pdf = parseInt(document.getElementById('settingMaxCharsPdf')?.value) || 0;
  payload.search.deepen_enabled = !!document.getElementById('settingSearchDeepen')?.checked;
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
  var _mbVal = parseFloat(document.getElementById('settingMaxBytesMB')?.value);
  payload.search.max_bytes = (_mbVal > 0) ? Math.round(_mbVal * 1048576) : 20971520;
  if (typeof runtimeScope.ChipInput !== 'undefined') payload.search.skip_domains = runtimeScope.ChipInput.getValues('settingSkipDomains');

  // Network — proxy pool (ordered, scoped). The pool editor owns
  // proxying: the backend retires the legacy single-proxy slot whenever
  // the key is present. Container absent (legacy/other surfaces) → leave
  // the server's proxy config untouched.
  var _pool = (typeof _collectProxyPool === 'function') ? _collectProxyPool() : null;
  if (_pool !== null) {
    payload.proxy_pool = _pool;
  }

  // Network — directly editable bypass rows (feeds proxies_for + no_proxy).
  var _bypass = (typeof _collectProxyBypassDomains === 'function')
    ? _collectProxyBypassDomains() : null;
  if (_bypass !== null) payload.proxy_bypass_domains = _bypass;

  // Feishu bot config
  if (typeof _collectFeishuConfig === 'function') {
    payload.feishu = _collectFeishuConfig();
  }

  // Machine translation provider config
  if (typeof _collectMtProviderConfig === 'function') {
    payload.mt_provider = _collectMtProviderConfig();
  }

  try {
    if (!_stgModelRouting) {
      throw new Error('model-routing v2 authority is not loaded');
    }
    var routingResponse = await Api.modelRouting.replace(
      _stgModelRouting, _stgModelRoutingRevision);
    _stgModelRouting = routingResponse.model_routing;
    _stgModelRoutingRevision = Number(routingResponse.revision || 0);
    var pendingCredentialIds = Object.keys(_stgPendingCredentialSecrets);
    for (var secretIndex = 0; secretIndex < pendingCredentialIds.length; secretIndex++) {
      var credentialId = pendingCredentialIds[secretIndex];
      var secretResponse = await Api.modelRouting.putCredentialSecret(
        credentialId,
        _stgPendingCredentialSecrets[credentialId],
        _stgModelRoutingRevision,
      );
      _stgModelRoutingRevision = Number(secretResponse.revision || _stgModelRoutingRevision);
    }
    if (pendingCredentialIds.length) {
      await _loadModelRoutingAuthority();
    }
    _stgPendingCredentialSecrets = {};
    var r = await Api.serverConfig.update(payload);
    var data = r ? await r.json().catch(function() { return {}; }) : {};
    if (data.ok) {
      var msg = t('settings.configSaved');
      debugLog('[Settings] ' + msg, 'success');
      document.getElementById('settingsStatusHint').textContent = t('settings.saved');
      setTimeout(function() {
        var hint = document.getElementById('settingsStatusHint');
        if (hint && hint.textContent === t('settings.saved')) hint.textContent = '';
      }, 3000);
      // Re-fetch server config to refresh model dropdown with any new/changed models.
      // Without this, _registeredModels stays stale and newly added providers' models
      // don't appear in the preset toggle until a page refresh.
      if (typeof _loadServerConfigAndPopulate === 'function') {
        _loadServerConfigAndPopulate();
      }
      return true;
    } else {
      debugLog('[Settings] Save failed: ' + (data.error || 'unknown'), 'error');
      return false;
    }
  } catch (e) {
    _settingsSaveFailed(e);
    return false;
  }
}

function exportServerConfig() {
  _loadServerConfig().then(function(cfg) {
    if (!cfg) return;
    var blob = new Blob([JSON.stringify(cfg, null, 2)], { type: 'application/json' });
    var a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'tofu-config-' + new Date().toISOString().slice(0, 10) + '.json';
    a.click();
    URL.revokeObjectURL(a.href);
    debugLog('[Settings] Config exported', 'success');
  });
}

function importServerConfig(event) {
  var file = event.target.files[0];
  if (!file) return;
  var reader = new FileReader();
  reader.onload = async function(e) {
    try {
      var imported = JSON.parse(String(e.target.result || ""));
      var r = await Api.serverConfig.update(imported);
      var data = r ? await r.json().catch(function() { return {}; }) : {};
      if (data.ok) {
        debugLog('[Settings] Config imported successfully', 'success');
        _serverConfig = null;
        openSettings();
      } else {
        debugLog('[Settings] Import failed: ' + (data.error || 'unknown'), 'error');
      }
    } catch (err) {
      debugLog('[Settings] Invalid JSON file: ' + err.message, 'error');
    }
  };
  reader.readAsText(file);
  event.target.value = '';
}
