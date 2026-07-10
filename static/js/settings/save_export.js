/* ═══════════════════════════════════════════════════════════════════
   settings/save export — extracted from settings.js (split 2026-05-28)

   Save/export/import server config: closeSettings, saveSettings, exportServerConfig, importServerConfig.

   This file is concatenated by lib/js_bundler.py — symbols share
   the same window scope as every other static/js/*.js file. No
   exports / imports needed.
   ═══════════════════════════════════════════════════════════════════ */

// ══════════════════════════════════════════════════════
//  Close / Save / Export / Import
// ══════════════════════════════════════════════════════

function closeSettings() {
  _stopBalancePolling();
  _stopLocalMetricsPolling();
  document.getElementById("settingsModal").classList.remove("open");
  // Refresh model dropdown to reflect any visibility changes
  if (typeof _populateModelDropdown === 'function' && typeof _registeredModels !== 'undefined' && _registeredModels.length > 0) {
    _populateModelDropdown(_registeredModels);
    _applyModelUI(config.model);
  }
  // Refresh image gen picker to reflect visibility changes
  if (typeof _loadIgModels === 'function') _loadIgModels();
}

function saveSettings() {
  // 1. Client-side config (General tab)
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
    // ★ Propagate: if current depth was the old default, update it to the new default
    if (config.thinkingDepth === oldDefault) {
      config.thinkingDepth = config.defaultThinkingDepth;
    }
  }
  // Keep Tool History toggle
  var kthCb = document.getElementById('settingKeepToolHistory');
  if (kthCb) {
    config.keepToolHistory = kthCb.checked;
  }

  // Per-tool inline timeline (segment render) toggle — defaults true.
  var segTlCb = document.getElementById('settingSegmentTimeline');
  if (segTlCb) {
    var _segTlOld = config.segmentTimeline !== false;
    config.segmentTimeline = segTlCb.checked;
    // Repaint the active conversation so the change is visible immediately
    // (no reload needed) when the setting actually flipped.
    if (config.segmentTimeline !== _segTlOld && typeof renderChat === 'function'
        && typeof getActiveConv === 'function') {
      var _segConv = getActiveConv();
      if (_segConv) renderChat(_segConv, true);
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

  try { localStorage.setItem("claude_client_config", JSON.stringify(config)); }
  catch (e) { debugLog('[saveSettings] localStorage save failed: ' + e.message, 'error'); }

  // 2. Feature flags (trading toggle)
  var tradingCb = document.getElementById('settingTradingEnabled');
  if (tradingCb) {
    var newVal = tradingCb.checked;
    var curVal = !!(typeof _featureFlags !== 'undefined' && _featureFlags.trading_enabled);
    if (newVal !== curVal) {
      Api.features.set({ trading_enabled: newVal })
        .then(function(r) { return r ? r.json() : {}; }).then(function(data) {
        if (data && data.ok) {
          debugLog('Trading module ' + (newVal ? 'enabled' : 'disabled') + ' — applied', 'success');
          if (typeof _featureFlags !== 'undefined') _featureFlags.trading_enabled = newVal;
          // Server tells us whether the toggle takes effect now or only after
          // a restart (blueprint registration is import-time — see A14).
          var hint = document.getElementById('tradingRestartHint');
          if (hint) hint.style.display = data.needs_restart ? 'block' : 'none';
        }
      }).catch(function(e) { debugLog('Feature flag save failed: ' + e.message, 'error'); });
    }
  }

  // 2b. PPTX translate toggle
  var pptxCb = document.getElementById('settingPptxTranslateEnabled');
  if (pptxCb) {
    var newPptx = pptxCb.checked;
    var curPptx = !!(typeof _featureFlags !== 'undefined' && _featureFlags.pptx_translate_enabled);
    if (newPptx !== curPptx) {
      Api.features.set({ pptx_translate_enabled: newPptx })
        .then(function(r) { return r ? r.json() : {}; }).then(function(data) {
        if (data && data.ok) {
          debugLog('PPTX translate ' + (newPptx ? 'enabled' : 'disabled'), 'success');
          if (typeof _featureFlags !== 'undefined') _featureFlags.pptx_translate_enabled = newPptx;
        }
      }).catch(function(e) { debugLog('Feature flag save failed: ' + e.message, 'error'); });
    }
  }

  // 2c. Debug mode toggle
  var debugCb = document.getElementById('settingDebugMode');
  if (debugCb) {
    var newDbg = debugCb.checked;
    var curDbg = !!(typeof _featureFlags !== 'undefined' && _featureFlags.debug_mode);
    if (newDbg !== curDbg) {
      Api.features.set({ debug_mode: newDbg })
        .then(function(r) { return r ? r.json() : {}; }).then(function(data) {
        if (data && data.ok) {
          debugLog('Debug mode ' + (newDbg ? 'enabled' : 'disabled'), 'success');
          if (typeof _featureFlags !== 'undefined') _featureFlags.debug_mode = newDbg;
          // Re-render sidebar and messages to show/hide debug elements.
          // Former renderMessages() never existed — whole-chat repaint is
          // renderChat(conv). (caught by tsc --checkJs)
          if (typeof renderConversationList === 'function') renderConversationList();
          if (typeof renderChat === 'function' && typeof getActiveConv === 'function') {
            var _dbgConv = getActiveConv();
            if (_dbgConv) renderChat(_dbgConv, true);
          }
        }
      }).catch(function(e) { debugLog('Feature flag save failed: ' + e.message, 'error'); });
    }
  }

  // 2d. Daily Optimizer toggle
  var optCb = document.getElementById('settingOptimizerEnabled');
  if (optCb) {
    var newOpt = optCb.checked;
    var _curFlag = (typeof _featureFlags !== 'undefined') ? _featureFlags.optimizer_enabled : undefined;
    var curOpt = (_curFlag === undefined) ? true : !!_curFlag;
    if (newOpt !== curOpt) {
      Api.features.set({ optimizer_enabled: newOpt })
        .then(function(r) { return r ? r.json() : {}; }).then(function(data) {
        if (data && data.ok) {
          debugLog('Daily Optimizer ' + (newOpt ? 'enabled' : 'disabled'), 'success');
          if (typeof _featureFlags !== 'undefined') _featureFlags.optimizer_enabled = newOpt;
          // Show/hide the topbar badge immediately.
          var badge = document.getElementById('optimizerBadge');
          if (badge) badge.style.display = newOpt ? 'inline-flex' : 'none';
        }
      }).catch(function(e) { debugLog('Feature flag save failed: ' + e.message, 'error'); });
    }
  }

  // 3. Server config (Providers / Presets / Search)
  if (_serverConfig) {
    _saveServerConfig();
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
    providers: _stgProviders,
    presets: cleanPresets,
    models: {},
    search: {},
    hidden_models: (_serverConfig && _serverConfig.hidden_models) || [],
    hidden_ig_models: (_serverConfig && _serverConfig.hidden_ig_models) || [],
    model_defaults: _collectModelDefaults(),
  };
  // Search tab
  var cfCb = document.getElementById('settingLlmContentFilter');
  payload.search.llm_content_filter = cfCb ? cfCb.checked : true;
  payload.search.fetch_top_n = parseInt(document.getElementById('settingFetchTopN')?.value) || 6;
  payload.search.fetch_timeout = parseInt(document.getElementById('settingFetchTimeout')?.value) || 15;
  payload.search.max_chars_search = parseInt(document.getElementById('settingMaxCharsSearch')?.value) || 60000;
  payload.search.max_chars_direct = parseInt(document.getElementById('settingMaxCharsDirect')?.value) || 200000;
  payload.search.max_chars_pdf = parseInt(document.getElementById('settingMaxCharsPdf')?.value) || 0;
  payload.search.max_bytes = parseInt(document.getElementById('settingMaxBytes')?.value) || 20971520;
  if (typeof ChipInput !== 'undefined') payload.search.skip_domains = ChipInput.getValues('settingSkipDomains');

  // Network — proxy address config (no_proxy is auto-managed by bypass domains)
  payload.proxy_config = {
    http_proxy:  (document.getElementById('settingHttpProxy')?.value || '').trim(),
    https_proxy: (document.getElementById('settingHttpsProxy')?.value || '').trim(),
  };

  // Network — unified bypass domains (feeds both proxies_for() and no_proxy env)
  if (typeof ChipInput !== 'undefined') {
    payload.proxy_bypass_domains = ChipInput.getValues('settingProxyBypass');
  }

  // Feishu bot config
  if (typeof _collectFeishuConfig === 'function') {
    payload.feishu = _collectFeishuConfig();
  }

  // Machine translation provider config
  if (typeof _collectMtProviderConfig === 'function') {
    payload.mt_provider = _collectMtProviderConfig();
  }

  // Speech-to-text: fold the dedicated STT provider into the providers list
  // BEFORE it is shipped (payload.providers = _stgProviders below/above).
  // Writes an explicit per-cell key_access capability override — see
  // settings/speech.js header (the DEFAULT_SLOT_CONFIGS trap).
  if (typeof _applySttToProviders === 'function') {
    _applySttToProviders();
    payload.providers = _stgProviders;
  }

  try {
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
      // ★ Re-fetch server config to refresh model dropdown with any new/changed models.
      // Without this, _registeredModels stays stale and newly added providers' models
      // don't appear in the preset toggle until a page refresh.
      if (typeof _loadServerConfigAndPopulate === 'function') {
        _loadServerConfigAndPopulate();
      }
    } else {
      debugLog('[Settings] Save failed: ' + (data.error || 'unknown'), 'error');
    }
  } catch (e) {
    debugLog('[Settings] Save failed: ' + e.message, 'error');
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


