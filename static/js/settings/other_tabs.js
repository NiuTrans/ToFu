/* ═══════════════════════════════════════════════════════════════════
   settings/other tabs — extracted from settings.js (split 2026-05-28)

   Other settings tabs: Search, Network, MT-provider, Feishu, Advanced + cache stats.

   This file is concatenated by lib/js_bundler.py — symbols share
   the same window scope as every other static/js/*.js file. No
   exports / imports needed.
   ═══════════════════════════════════════════════════════════════════ */

// ══════════════════════════════════════════════════════
//  Search / Advanced tabs
// ══════════════════════════════════════════════════════

function _populateSearchTab(cfg) {
  var s = cfg.search || {};
  var cb = document.getElementById('settingLlmContentFilter');
  if (cb) cb.checked = s.llm_content_filter !== false;  // default: on
  _setVal('settingFetchTopN', s.fetch_top_n || 6);
  _setVal('settingFetchTimeout', s.fetch_timeout || 15);
  _setVal('settingMaxCharsSearch', s.max_chars_search || 60000);
  _setVal('settingMaxCharsDirect', s.max_chars_direct || 200000);
  _setVal('settingMaxCharsPdf', s.max_chars_pdf || 0);
  // Max download size is stored in BYTES but displayed in MB — humans do not
  // think in 20971520. Save converts back (save_export.js).
  _setVal('settingMaxBytesMB', _bytesToMB(s.max_bytes || 20971520));
  if (typeof ChipInput !== 'undefined') ChipInput.init('settingSkipDomains', s.skip_domains || []);
  _renderSearchBackendStatus(cfg.search_status);
  _wireSearchPipelinePreview();
  if (typeof _renderAuthSources === 'function') _renderAuthSources();
  if (typeof _renderPrivateHosts === 'function') _renderPrivateHosts();
}

/** bytes → MB for display (round to 1 decimal, trim trailing .0). */
function _bytesToMB(bytes) {
  var mb = (parseInt(bytes, 10) || 0) / 1048576;
  var rounded = Math.round(mb * 10) / 10;
  return (rounded === Math.floor(rounded)) ? Math.floor(rounded) : rounded;
}

/** Render the live backend status strip (tofu-search version, engines,
 *  extension reachability, filter mode/model, deadlines). This is the piece
 *  that tells the user these knobs drive a SERVER pipeline, not UI cosmetics. */
function _renderSearchBackendStatus(st) {
  var box = document.getElementById('searchBackendStatus');
  if (!box) return;
  if (!st || !st.ok) {
    box.innerHTML = String(safeHtml`<span class="search-status-badge off">${t('settings.searchStatusUnavailable') || '后端状态不可用'}</span>`);
    return;
  }
  var extBadge = st.extension_connected
    ? safeHtml`<span class="search-status-badge on">${t('settings.searchStatusExtOn') || '浏览器扩展在线'}</span>`
    : safeHtml`<span class="search-status-badge off">${t('settings.searchStatusExtOff') || '扩展离线（浏览器兜底不可用）'}</span>`;
  box.innerHTML = String(safeHtml`
    <span class="search-status-label">${t('settings.searchBackendLive') || '后端实况'}</span>
    ${extBadge}
    <span class="search-status-badge">tofu-search v${st.tofu_search_version || '?'}</span>
    <span class="search-status-badge">SearXNG ×${st.searxng_instances || 0}</span>
    <span class="search-status-badge">${(t('settings.searchStatusFilter') || '过滤 {mode} · {model}').replace('{mode}', st.filter_mode || '?').replace('{model}', st.filter_model || '?')}</span>
    <span class="search-status-badge">${(t('settings.searchStatusDeadline') || '限时 整轮 {call}s · 单页 {url}s').replace('{call}', st.search_deadline_secs || 0).replace('{url}', st.fetch_url_deadline_secs || 0)}</span>
  `);
}

/** The pipeline preview says in one sentence what the backend WILL DO with
 *  the current knob values — the frontend↔backend bridge. Live-updates as
 *  the user edits the inputs (wired once). */
function _refreshSearchPipelinePreview() {
  var el = document.getElementById('searchPipelinePreview');
  if (!el) return;
  var _v = function (id, dflt) {
    var n = parseInt((document.getElementById(id) || {}).value, 10);
    return (isNaN(n) ? dflt : n);
  };
  var n = _v('settingFetchTopN', 6);
  var timeout = _v('settingFetchTimeout', 15);
  var chars = _v('settingMaxCharsSearch', 60000);
  var filterCb = document.getElementById('settingLlmContentFilter');
  var filterOn = filterCb ? filterCb.checked : true;
  var filterTxt = filterOn
    ? (t('settings.searchFilterOnTpl') || 'LLM 过滤杂质')
    : (t('settings.searchFilterOffTpl') || '跳过过滤（原文直送）');
  el.textContent = (t('settings.searchPipelineTpl') ||
    '搜索引擎返回结果 → 抓取前 {n} 个网页（每页 ≤{chars} 字符 · 超时 {timeout}s）→ {filter} → 注入对话')
    .replace('{n}', n).replace('{chars}', (chars || 0).toLocaleString('en-US'))
    .replace('{timeout}', timeout).replace('{filter}', filterTxt);
  el.classList.toggle('filter-off', !filterOn);
}

function _wireSearchPipelinePreview() {
  _refreshSearchPipelinePreview();
  if (/** @type {any} */ (_wireSearchPipelinePreview)._done) return;
  /** @type {any} */ (_wireSearchPipelinePreview)._done = true;
  ['settingFetchTopN', 'settingFetchTimeout', 'settingMaxCharsSearch',
   'settingLlmContentFilter'].forEach(function (id) {
    var el = document.getElementById(id);
    if (el) {
      el.addEventListener('input', _refreshSearchPipelinePreview);
      el.addEventListener('change', _refreshSearchPipelinePreview);
    }
  });
}

// ══════════════════════════════════════════════════════
//  Paper reading-experience toggles (General tab → 功能模块)
// ══════════════════════════════════════════════════════

/** Populate the paper reading-experience toggles from cfg.paper. Values are
 *  the EFFECTIVE resolution (server_config → env → default ON), so the
 *  checkboxes reflect what the reader will actually do; saving writes the
 *  explicit server_config section. */
function _populatePaperXpTab(cfg) {
  var rx = (cfg && cfg.paper && cfg.paper.reading_experience) || {};
  var insCb = document.getElementById('settingPaperInsightEnabled');
  if (insCb) insCb.checked = (rx.insight !== false);   // default ON
  var cpCb = document.getElementById('settingPaperCheckpointsEnabled');
  if (cpCb) cpCb.checked = (rx.checkpoints !== false); // default ON
}

/** Collect the toggle states into the save payload (merged server-side). */
function _collectPaperXpConfig() {
  var insCb = document.getElementById('settingPaperInsightEnabled');
  var cpCb = document.getElementById('settingPaperCheckpointsEnabled');
  return { reading_experience: {
    insight: insCb ? insCb.checked : true,
    checkpoints: cpCb ? cpCb.checked : true,
  } };
}

// ══════════════════════════════════════════════════════
//  Network tab (proxy bypass)
// ══════════════════════════════════════════════════════

function _populateNetworkTab(cfg) {
  var n = cfg.network || {};

  // ── Proxy address fields (editable) ──
  _setVal('settingHttpProxy', n.http_proxy || '');
  _setVal('settingHttpsProxy', n.https_proxy || '');

  // Show env hint banner if env vars are set (so user knows the baseline)
  var envParts = [];
  if (n.env_http_proxy) envParts.push('http_proxy=' + n.env_http_proxy);
  if (n.env_https_proxy && n.env_https_proxy !== n.env_http_proxy)
    envParts.push('https_proxy=' + n.env_https_proxy);

  var envBanner = document.getElementById('proxyEnvBanner');
  var envBannerText = document.getElementById('proxyEnvBannerText');
  if (envBanner && envBannerText && envParts.length > 0) {
    envBanner.style.display = '';
    envBannerText.textContent = t('settings.proxyEnvBanner', { vars: envParts.join(' · ') });
  } else if (envBanner) {
    envBanner.style.display = 'none';
  }

  // ── Unified bypass domains (editable) ──
  if (typeof ChipInput !== 'undefined') ChipInput.init('settingProxyBypass', n.proxy_bypass_domains || []);

  // Show hint if env var PROXY_BYPASS_DOMAINS is set
  var hint = document.getElementById('proxyEnvHint');
  var hintText = document.getElementById('proxyEnvHintText');
  if (hint && hintText && n.env_proxy_bypass) {
    hint.style.display = '';
    hintText.textContent = t('settings.proxyEnvHint', { val: n.env_proxy_bypass });
  } else if (hint) {
    hint.style.display = 'none';
  }
}


// ══════════════════════════════════════════════════════
//  Machine Translation Provider (General tab)
// ══════════════════════════════════════════════════════

function _populateMtProviderSection(cfg) {
  var mt = cfg.mt_provider || {};
  var enabledCb = document.getElementById('settingMtEnabled');
  var fieldsDiv = document.getElementById('mtProviderFields');
  if (enabledCb) {
    enabledCb.checked = !!mt.enabled;
    enabledCb.onchange = function() {
      if (fieldsDiv) fieldsDiv.style.display = this.checked ? '' : 'none';
    };
  }
  if (fieldsDiv) fieldsDiv.style.display = mt.enabled ? '' : 'none';

  var provider = mt.provider || 'niutrans';
  _setVal('settingMtProvider', provider);

  // NiuTrans fields (primary)
  _setVal('settingMtApiKey', provider === 'niutrans' ? (mt.api_key || '') : '');
  _setVal('settingMtAppId', provider === 'niutrans' ? (mt.app_id || '') : '');
  _setVal('settingMtApiUrl', provider === 'niutrans' ? (mt.api_url || '') : '');

  // Custom fields
  _setVal('settingMtApiKeyCustom', provider === 'custom' ? (mt.api_key || '') : '');
  _setVal('settingMtAppIdCustom', provider === 'custom' ? (mt.app_id || '') : '');
  _setVal('settingMtApiUrlCustom', provider === 'custom' ? (mt.api_url || '') : '');

  _switchMtProvider(provider);
}

/** Show/hide provider cards based on selection */
function _switchMtProvider(provider) {
  var niuCard = document.getElementById('mtCardNiutrans');
  var customCard = document.getElementById('mtCardCustom');
  if (niuCard) niuCard.style.display = provider === 'niutrans' ? '' : 'none';
  if (customCard) customCard.style.display = provider === 'custom' ? '' : 'none';
}

function _collectMtProviderConfig() {
  var enabledCb = document.getElementById('settingMtEnabled');
  var provider = (document.getElementById('settingMtProvider') || {}).value || 'niutrans';
  var suffix = provider === 'custom' ? 'Custom' : '';
  return {
    enabled: enabledCb ? enabledCb.checked : false,
    provider: provider,
    api_key: (document.getElementById('settingMtApiKey' + suffix) || {}).value || '',
    app_id: (document.getElementById('settingMtAppId' + suffix) || {}).value || '',
    api_url: (document.getElementById('settingMtApiUrl' + suffix) || {}).value || '',
  };
}

function _testMtProvider() {
  var provider = (document.getElementById('settingMtProvider') || {}).value || 'niutrans';
  var suffix = provider === 'custom' ? 'Custom' : '';
  var btn = document.getElementById('mtTestBtn' + suffix);
  var result = document.getElementById('mtTestResult' + suffix);
  if (!btn || !result) return;
  btn.disabled = true;
  result.textContent = t('settings.mtTesting');
  result.style.color = 'var(--text-secondary)';

  Api.translate.mtTest(_collectMtProviderConfig(),
    'Hello, this is a test of the machine translation service.'
  ).then(function(data) {
    btn.disabled = false;
    if (data && data.ok) {
      result.textContent = t('settings.mtTestOk') + (data.translated || '').substring(0, 60);
      result.style.color = 'var(--accent-green, #4caf50)';
    } else {
      result.textContent = '❌ ' + (data.error || t('settings.mtTestFail'));
      result.style.color = 'var(--accent-red, #f44336)';
    }
  }).catch(function(e) {
    btn.disabled = false;
    result.textContent = t('settings.mtTestReqFail') + e.message;
    result.style.color = 'var(--accent-red, #f44336)';
  });
}

// ══════════════════════════════════════════════════════
//  Feishu Bot settings (in General tab → Modules)
// ══════════════════════════════════════════════════════

/** Cached Feishu config for dirty-checking restart hint */
var _feishuOrigConfig = null;

function _populateFeishuTab(cfg) {
  var f = cfg.feishu || {};
  _feishuOrigConfig = JSON.parse(JSON.stringify(f));

  // Status dot
  var dot = document.getElementById('feishuStatusDot');
  var label = document.getElementById('feishuStatusLabel');
  var desc = document.getElementById('feishuStatusDesc');
  if (dot && label && desc) {
    if (f.connected) {
      dot.innerHTML = IconDot('green'); dot.title = t('settings.feishuConnected');
      desc.textContent = t('settings.feishuConnectedDesc', { app: (f.app_id_masked || '—') });
    } else if (f.enabled) {
      dot.innerHTML = IconDot('yellow'); dot.title = t('settings.feishuEnabledNotConnected');
      desc.textContent = t('settings.feishuCredsNotConnected');
    } else {
      dot.innerHTML = IconDot('grey'); dot.title = t('settings.feishuDisabled');
      desc.textContent = t('settings.feishuDisabledDesc');
    }
  }

  // Populate fields
  _setVal('settingFeishuAppId', f.app_id || '');
  // Don't populate secret — show placeholder instead
  var secretInput = document.getElementById('settingFeishuAppSecret');
  if (secretInput) {
    secretInput.value = '';
    secretInput.placeholder = f.has_secret ? t('settings.feishuSecretSaved') : t('settings.feishuSecretPlaceholder');
  }
  _setVal('settingFeishuDefaultProject', f.default_project || '');
  _setVal('settingFeishuWorkspaceRoot', f.workspace_root || '');
  var au = document.getElementById('settingFeishuAllowedUsers');
  if (au) au.value = (f.allowed_users || []).join('\n');

  // Restart hint on credential change
  var appIdInput = document.getElementById('settingFeishuAppId');
  if (appIdInput) {
    appIdInput.oninput = _checkFeishuRestartHint;
  }
  if (secretInput) {
    secretInput.oninput = _checkFeishuRestartHint;
  }
}

function _checkFeishuRestartHint() {
  var hint = document.getElementById('feishuRestartHint');
  if (!hint || !_feishuOrigConfig) return;
  var appId = (document.getElementById('settingFeishuAppId') || {}).value || '';
  var secret = (document.getElementById('settingFeishuAppSecret') || {}).value || '';
  var changed = appId !== (_feishuOrigConfig.app_id || '') || secret.length > 0;
  hint.style.display = changed ? 'block' : 'none';
}

function _collectFeishuConfig() {
  var appId = (document.getElementById('settingFeishuAppId') || {}).value || '';
  var secret = (document.getElementById('settingFeishuAppSecret') || {}).value || '';
  var defProj = (document.getElementById('settingFeishuDefaultProject') || {}).value || '';
  var wsRoot = (document.getElementById('settingFeishuWorkspaceRoot') || {}).value || '';
  var au = (document.getElementById('settingFeishuAllowedUsers') || {}).value || '';
  var allowedUsers = au.split('\n').map(function(s) { return s.trim(); }).filter(Boolean);

  var cfg = {
    app_id: appId.trim(),
    default_project: defProj.trim(),
    workspace_root: wsRoot.trim(),
    allowed_users: allowedUsers,
  };
  // Only include secret if user typed something new
  if (secret.trim()) {
    cfg.app_secret = secret.trim();
  }
  return cfg;
}

function _populateAdvancedTab(cfg) {
  var pr = document.getElementById('settingPricing');
  if (pr && cfg.pricing) {
    var lines = [];
    for (var model in cfg.pricing) {
      var info = cfg.pricing[model];
      lines.push(model + ': in=$' + info.input + ' out=$' + info.output);
    }
    pr.value = lines.join('\n');
  }
  var si = document.getElementById('settingsServerInfo');
  if (si && cfg.server_info) {
    var html = '';
    for (var k in cfg.server_info) {
      html += '<div class="stg-info-row"><span class="stg-info-label">' + escapeHtml(k) + '</span><span class="stg-info-value">' + escapeHtml(String(cfg.server_info[k])) + '</span></div>';
    }
    si.innerHTML = html;
  }
  /* ★ Populate IndexedDB cache stats */
  _refreshCacheStatsUI();
  if (typeof _renderCredentialsVault === 'function') _renderCredentialsVault();
}

/** Refresh the cache statistics display in Settings > Advanced */
function _refreshCacheStatsUI() {
  var el = document.getElementById('settingsCacheStats');
  if (!el) return;
  if (typeof ConvCache === 'undefined' || !ConvCache.isAvailable()) {
    el.textContent = t('settings.cacheUnavailable');
    return;
  }
  ConvCache.stats().then(function(s) {
    el.textContent = t('settings.cacheCached', { n: s.count });
  });
}

/** Handler for the "Clear Cache" button in settings */
function _clearConvCacheFromSettings() {
  if (typeof ConvCache === 'undefined') return;
  var btn = document.getElementById('settingsClearCacheBtn');
  if (btn) { btn.disabled = true; btn.textContent = t('settings.cacheClearing'); }
  ConvCache.clear().then(function() {
    _refreshCacheStatsUI();
    if (btn) { btn.disabled = false; btn.innerHTML = Icon('trash', 12) + ' ' + t('settings.cacheClearBtn'); }
    // Force all in-memory conversations to _needsLoad so next click refetches
    conversations.forEach(function(c) {
      if (c.id !== activeConvId) c._needsLoad = true;
    });
    if (typeof showToast === 'function') showToast(t('settings.cacheCleared'));
  });
}

