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
  _setVal('settingMaxBytes', s.max_bytes || 20971520);
  if (typeof ChipInput !== 'undefined') ChipInput.init('settingSkipDomains', s.skip_domains || []);
  if (typeof _renderAuthSources === 'function') _renderAuthSources();
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
    envBannerText.textContent = '系统环境变量: ' + envParts.join(' · ');
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
    hintText.textContent = '环境变量基线 (PROXY_BYPASS_DOMAINS): ' + n.env_proxy_bypass;
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
      dot.textContent = '🟢'; dot.title = '已连接';
      desc.textContent = 'WebSocket 已连接 · 应用：' + (f.app_id_masked || '—');
    } else if (f.enabled) {
      dot.textContent = '🟡'; dot.title = '已启用但未连接';
      desc.textContent = '凭证已设置但未连接';
    } else {
      dot.textContent = '⚪'; dot.title = '未启用';
      desc.textContent = '请设置 App ID 和 App Secret 以启用';
    }
  }

  // Populate fields
  _setVal('settingFeishuAppId', f.app_id || '');
  // Don't populate secret — show placeholder instead
  var secretInput = document.getElementById('settingFeishuAppSecret');
  if (secretInput) {
    secretInput.value = '';
    secretInput.placeholder = f.has_secret ? '••••••••（已保存 — 留空则保持不变）' : '输入应用密钥';
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
}

/** Refresh the cache statistics display in Settings > Advanced */
function _refreshCacheStatsUI() {
  var el = document.getElementById('settingsCacheStats');
  if (!el) return;
  if (typeof ConvCache === 'undefined' || !ConvCache.isAvailable()) {
    el.textContent = 'IndexedDB 不可用';
    return;
  }
  ConvCache.stats().then(function(s) {
    el.textContent = '已缓存 ' + s.count + ' 个对话';
  });
}

/** Handler for the "Clear Cache" button in settings */
function _clearConvCacheFromSettings() {
  if (typeof ConvCache === 'undefined') return;
  var btn = document.getElementById('settingsClearCacheBtn');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ 清除中…'; }
  ConvCache.clear().then(function() {
    _refreshCacheStatsUI();
    if (btn) { btn.disabled = false; btn.textContent = '🗑 清除缓存'; }
    // Force all in-memory conversations to _needsLoad so next click refetches
    conversations.forEach(function(c) {
      if (c.id !== activeConvId) c._needsLoad = true;
    });
    if (typeof showToast === 'function') showToast('缓存已清除 — 下次点击对话时将重新从服务器加载');
  });
}

