/* ===== migrated source: settings/other_tabs.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   settings/other tabs — extracted from settings.js (split 2026-05-28)

   Other settings tabs: Search, Network, MT-provider, Feishu, Advanced + cache stats.

   This file is concatenated by Vite's module graph — symbols share
   the same window scope as every other frontend/src/runtime/*.js file. No
   exports / imports needed.
   ═══════════════════════════════════════════════════════════════════ */

// ══════════════════════════════════════════════════════
//  Search / Advanced tabs
// ══════════════════════════════════════════════════════

function _populateSearchTab(cfg) {
  var s = cfg.search || {};
  var overrides = (s.overrides && typeof s.overrides === 'object') ? s.overrides : {};
  _searchLastPreset = s.profile || 'balanced';
  // Saved overrides ⇔ the select's 自定义 option — one source of truth.
  _setVal('settingSearchProfile', Object.keys(overrides).length > 0 ? 'custom' : _searchLastPreset);
  var cb = document.getElementById('settingLlmContentFilter');
  if (cb) cb.checked = s.llm_content_filter !== false;  // default: on
  _setVal('settingFetchTopN', s.fetch_top_n || 6);
  _setVal('settingFetchTimeout', s.fetch_timeout || 15);
  _setVal('settingMaxCharsSearch', s.max_chars_search || 60000);
  _setVal('settingMaxCharsDirect', s.max_chars_direct || 200000);
  _setVal('settingMaxCharsPdf', s.max_chars_pdf || 0);
  var deepen = document.getElementById('settingSearchDeepen');
  if (deepen) deepen.checked = s.deepen_enabled === true;
  // Max download size is stored in BYTES but displayed in MB — humans do not
  // think in 20971520. Save converts back (save_export.js).
  _setVal('settingMaxBytesMB', _bytesToMB(s.max_bytes || 20971520));
  if (typeof runtimeScope.ChipInput !== 'undefined') runtimeScope.ChipInput.init('settingSkipDomains', s.skip_domains || []);
  _wireSearchPipelinePreview();
  _syncSearchOverrideControls();
  _renderSearchBrowserAccess();
  if (typeof _renderAuthSources === 'function') _renderAuthSources();
  if (typeof _renderPrivateHosts === 'function') _renderPrivateHosts();
}

// Last real (non-custom) preset — the wire never carries profile='custom':
// saving 自定义 sends this preset as the base plus the four-knob overrides.
var _searchLastPreset = 'balanced';

var _SEARCH_PROFILE_PRESETS = {
  fast: { fetch_top_n: 3, max_chars_search: 30000,
          llm_content_filter: false, deepen_enabled: false },
  balanced: { fetch_top_n: 6, max_chars_search: 60000,
              llm_content_filter: true, deepen_enabled: false },
  deep: { fetch_top_n: 10, max_chars_search: 100000,
          llm_content_filter: true, deepen_enabled: true }
};

function _searchProfileChanged() {
  var profile = (document.getElementById('settingSearchProfile') || {}).value || 'balanced';
  if (profile !== 'custom') {
    _searchLastPreset = profile;
    var p = _SEARCH_PROFILE_PRESETS[profile] || _SEARCH_PROFILE_PRESETS.balanced;
    _setVal('settingFetchTopN', p.fetch_top_n);
    _setVal('settingMaxCharsSearch', p.max_chars_search);
    var filter = document.getElementById('settingLlmContentFilter');
    var deepen = document.getElementById('settingSearchDeepen');
    if (filter) filter.checked = p.llm_content_filter;
    if (deepen) deepen.checked = p.deepen_enabled;
  }
  _syncSearchOverrideControls();
  _refreshSearchPipelinePreview();
}

function _syncSearchOverrideControls() {
  var sel = document.getElementById('settingSearchProfile');
  // A legacy/embedded settings surface has no profile select — its concrete
  // knobs stay live; only the new profile UI gates them on 自定义.
  var custom = !sel || sel.value === 'custom';
  document.querySelectorAll('.search-override-control input').forEach(function (el) {
    el.disabled = !custom;
  });
}

function _renderSearchBrowserAccess() {
  var owner = runtimeScope._renderSearchBrowserAccessOwner;
  if (typeof owner === 'function') return owner();
  var statusBox = document.getElementById('searchBrowserStatus');
  if (statusBox) {
    statusBox.innerHTML = '<span class="search-status-badge off">' +
      escapeHtml(t('settings.browserStatusUnavailable')) + '</span>';
  }
}

async function _testSearchBrowser() {
  var owner = runtimeScope._testSearchBrowserOwner;
  if (typeof owner === 'function') return owner();
}

async function _browserAccessDenyRead() {
  var owner = runtimeScope._browserAccessDenyReadOwner;
  if (typeof owner === 'function') return owner();
}

/** bytes → MB for display (round to 1 decimal, trim trailing .0). */
function _bytesToMB(bytes) {
  var mb = (parseInt(bytes, 10) || 0) / 1048576;
  var rounded = Math.round(mb * 10) / 10;
  return (rounded === Math.floor(rounded)) ? Math.floor(rounded) : rounded;
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
//  Network tab (proxy bypass)
// ══════════════════════════════════════════════════════

function _populateNetworkTab(cfg) {
  var n = cfg.network || {};

  // ── Proxy pool editor (ordered, scoped) ──
  _renderProxyPool(n);

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

  // ── Unified bypass addresses (every row is directly editable) ──
  _renderProxyBypassDomains(n.proxy_bypass_domains || []);

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


// ── Direct-connection exceptions ──────────────────────────────────────────

function _renderProxyBypassDomains(values) {
  var list = document.getElementById('proxyBypassList');
  if (!list) return;
  var items = Array.isArray(values) && values.length ? values : [''];
  list.innerHTML = items.map(_proxyBypassRowHtml).join('');
  _proxyBypassRefreshOrder();
  var addBtn = document.getElementById('proxyBypassAddBtn');
  if (addBtn && !addBtn._wired) {
    addBtn._wired = true;
    addBtn.innerHTML = (typeof Icon === 'function' ? Icon('plus', 12) + ' ' : '') +
      escapeHtml(t('settings.proxyBypassAdd') || '添加直连地址');
    addBtn.onclick = _proxyBypassAppendRow;
  }
}

function _proxyBypassRowHtml(value, index) {
  var trash = (typeof Icon === 'function') ? Icon('trash', 12) : '×';
  return String(safeHtml`
    <div class="proxy-bypass-row">
      <span class="pb-order">${String((index == null ? 0 : index) + 1).padStart(2, '0')}</span>
      <input type="text" class="pb-value settings-mono" value="${value || ''}"
             placeholder="${t('settings.proxyBypassRowPlaceholder') || '.internal.example.com 或完整 URL'}"
             spellcheck="false" autocomplete="off" data-tofu-action-input="_proxyBypassRefreshCount()">
      <button type="button" class="auth-src-btn ghost danger sm icon-box pb-delete-btn"
              title="${t('settings.proxyBypassDelete') || '删除此项'}"
              data-tofu-action="_proxyBypassDelete(this)">${raw(trash)}</button>
    </div>`);
}

function _proxyBypassAppendRow() {
  var list = document.getElementById('proxyBypassList');
  if (!list) return;
  var rows = list.querySelectorAll('.proxy-bypass-row');
  var only = rows.length === 1 && !(rows[0].querySelector('.pb-value') || {}).value;
  if (!only) list.insertAdjacentHTML('beforeend', _proxyBypassRowHtml('', rows.length));
  _proxyBypassRefreshOrder();
  var inputs = list.querySelectorAll('.pb-value');
  if (inputs.length) inputs[inputs.length - 1].focus();
}

function _proxyBypassDelete(btn) {
  var row = btn && btn.closest ? btn.closest('.proxy-bypass-row') : null;
  var list = row && row.parentNode;
  if (!row || !list) return;
  row.remove();
  if (!list.querySelector('.proxy-bypass-row')) {
    list.insertAdjacentHTML('beforeend', _proxyBypassRowHtml('', 0));
  }
  _proxyBypassRefreshOrder();
}

function _proxyBypassRefreshOrder() {
  var rows = document.querySelectorAll('#proxyBypassList .proxy-bypass-row');
  for (var i = 0; i < rows.length; i++) {
    var order = rows[i].querySelector('.pb-order');
    if (order) order.textContent = String(i + 1).padStart(2, '0');
  }
  _proxyBypassRefreshCount();
}

function _proxyBypassRefreshCount() {
  var countEl = document.getElementById('proxyBypassCount');
  if (!countEl) return;
  var inputs = document.querySelectorAll('#proxyBypassList .pb-value');
  var count = 0;
  for (var i = 0; i < inputs.length; i++) {
    if ((inputs[i].value || '').trim()) count += 1;
  }
  countEl.textContent = (t('settings.proxyBypassCount') || '{count} 项')
    .replace('{count}', String(count));
}

function _collectProxyBypassDomains() {
  var list = document.getElementById('proxyBypassList');
  if (!list) return null;
  var out = [];
  var inputs = list.querySelectorAll('.pb-value');
  for (var i = 0; i < inputs.length; i++) {
    var value = (inputs[i].value || '').trim();
    if (value) out.push(value);
  }
  return out;
}


// ══════════════════════════════════════════════════════
//  Network tab — proxy pool editor (ordered, scoped, 2026-08-07)
// ══════════════════════════════════════════════════════
// Rows render from cfg.network.proxy_pool (credential-free; the backend
// holds user:pass in the credentials vault). A legacy single-proxy config
// (proxy_config) surfaces as ONE synthetic global row so saving migrates
// it into the pool seamlessly. All entry points null-guard the container
// so stripped-down jsdom harnesses (and a stale cached DOM) never crash.

function _renderProxyPool(n) {
  var list = document.getElementById('proxyPoolList');
  if (!list) return;
  var pool = ((n && n.proxy_pool) || []).slice();
  // Legacy migration: a configured legacy single proxy with no global pool
  // row rides the editor as one synthetic global row; saving retires the
  // legacy slot server-side.
  var hasGlobal = pool.some(function (e) { return e && e.scope === 'global'; });
  if (n && n.proxy_configured && n.http_proxy && !hasGlobal) {
    pool.push({
      id: 'legacy', name: (t('settings.proxyLegacyRow') || '旧版单代理（迁移）'),
      url: n.http_proxy, scope: 'global', enabled: true,
      has_credential: false, credential_vault: '', _legacy: true,
    });
  }
  list.innerHTML = pool.length
    ? pool.map(_proxyPoolRowHtml).join('')
    : String(safeHtml`<div class="proxy-pool-empty">
        <strong>${t('settings.proxyPoolEmpty') || '还没有代理'}</strong>
        <span>${t('settings.proxyPoolEmptyHint') || '不添加时，将继续使用系统环境变量中的代理配置。'}</span>
      </div>`);
  _proxyPoolRefreshOrder();
  var addBtn = document.getElementById('proxyPoolAddBtn');
  if (addBtn && !addBtn._wired) {
    addBtn._wired = true;
    addBtn.innerHTML = (typeof Icon === 'function' ? Icon('plus', 12) + ' ' : '') +
      escapeHtml(t('settings.proxyPoolAdd') || '添加代理');
    addBtn.onclick = function () { _proxyPoolAppendRow(); };
  }
}

function _proxyPoolRowHtml(e, index) {
  var id = e.id || '';
  var scope = e.scope === 'global' ? 'global' : 'subscription';
  var hasCred = !!(e.has_credential || e.credential_vault);
  var order = String((index == null ? 0 : index) + 1).padStart(2, '0');
  var displayName = e.name || ((t('settings.proxyCardTitle') || '代理') + ' ' + order);
  var hostLabel = _proxyPoolDisplayHost(e.url || '') || (t('settings.proxyUrlEmpty') || '尚未填写链接');
  var scopeLabel = scope === 'global'
    ? (t('settings.proxyScopeGlobal') || '全部出站流量')
    : (t('settings.proxyScopeSub') || '仅订阅流量');
  var trash = (typeof Icon === 'function') ? Icon('trash', 12) : '×';
  var eye = (typeof Icon === 'function') ? Icon('eye', 14) : '◉';
  var edit = (typeof Icon === 'function') ? Icon('edit', 13) : '✎';
  var chevron = (typeof Icon === 'function') ? Icon('chevronDown', 12) : '⌄';
  return String(safeHtml`
    <div class="proxy-pool-row" data-id="${id}" data-has-credential="${hasCred ? '1' : '0'}" data-url-dirty="0">
      <div class="proxy-pool-head">
        <div class="proxy-pool-identity">
          <span class="pp-order">${order}</span>
          <div class="pp-identity-copy">
            <strong class="pp-display-name" title="${displayName}">${displayName}</strong>
            <span class="pp-display-host" title="${hostLabel}">${hostLabel}</span>
          </div>
          <span class="pp-scope-badge">${scopeLabel}</span>
        </div>
        <label class="pp-enabled-control">
          <input type="checkbox" class="pp-enabled" ${raw(e.enabled !== false ? 'checked' : '')}>
          <span class="pp-switch-track"><span></span></span>
          <span class="pp-enabled-text">${t('settings.proxyEnable') || '启用'}</span>
        </label>
        <button type="button" class="pp-edit-toggle"
                title="${t('settings.proxyEdit') || '编辑代理'}"
                aria-label="${t('settings.proxyEdit') || '编辑代理'}" aria-expanded="false"
                data-tofu-action="_proxyPoolToggleEditor(this)">${raw(edit)}</button>
      </div>

      <div class="proxy-pool-detail" hidden>
        <div class="pp-detail-grid">
          <label class="pp-field pp-field-name">
            <span>${t('settings.proxyNameLabel') || '名称（可选）'}</span>
            <input type="text" class="pp-name" value="${e.name || ''}"
                   placeholder="${t('settings.proxyPhName') || '例如：公司出口'}" maxlength="40"
                   data-tofu-action-input="_proxyPoolSyncMeta(this)">
          </label>
          <label class="pp-field pp-field-scope">
            <span>${t('settings.proxyScopeLabel') || '使用范围'}</span>
            <select class="pp-scope" data-tofu-action-change="_proxyPoolSyncMeta(this)">
              <option value="subscription" ${raw(scope === 'subscription' ? 'selected' : '')}>${t('settings.proxyScopeSub') || '仅订阅流量'}</option>
              <option value="global" ${raw(scope === 'global' ? 'selected' : '')}>${t('settings.proxyScopeGlobal') || '全部出站流量'}</option>
            </select>
          </label>
        </div>
        <label class="pp-url-field">
          <span class="pp-url-label">
            <strong>${t('settings.proxyUrlLabel') || '代理链接'}</strong>
            <span class="pp-url-state">${hasCred ? (t('settings.proxyUrlProtected') || '敏感信息已隐藏') : 'HTTP / HTTPS'}</span>
          </span>
          <span class="pp-url-input">
            <input type="${hasCred ? 'password' : 'text'}" class="pp-url settings-mono" value="${e.url || ''}"
                   placeholder="http://host:port" spellcheck="false" autocomplete="off"
                   data-tofu-action-input="_proxyPoolUrlChanged(this)">
            <button type="button" class="pp-url-visibility"
                    title="${hasCred ? (t('settings.proxyShowUrl') || '显示完整链接') : (t('settings.proxyHideUrl') || '隐藏链接')}"
                    aria-label="${hasCred ? (t('settings.proxyShowUrl') || '显示完整链接') : (t('settings.proxyHideUrl') || '隐藏链接')}"
                    data-tofu-action="_proxyPoolToggleUrlVisibility(this)">${raw(eye)}</button>
          </span>
          <span class="pp-url-help">${t('settings.proxyUrlHelp') || '复制完整链接后直接粘贴，账号密码会自动安全保存。'}</span>
        </label>

        <div class="proxy-pool-card-footer">
          <div class="pp-reorder" aria-label="${t('settings.proxyReorder') || '调整顺序'}">
            <span>${t('settings.proxyOrderLabel') || '优先级'}</span>
            <button type="button" class="auth-src-btn ghost sm icon-box pp-move pp-move-up"
                    title="${t('settings.proxyMoveUp') || '上移'}" data-tofu-action="_proxyPoolMove(this, -1)">${raw(chevron)}</button>
            <button type="button" class="auth-src-btn ghost sm icon-box pp-move pp-move-down"
                    title="${t('settings.proxyMoveDown') || '下移'}" data-tofu-action="_proxyPoolMove(this, 1)">${raw(chevron)}</button>
          </div>
          <div class="pp-card-actions">
            <button type="button" class="auth-src-btn sm pp-test-btn" data-tofu-action="_proxyPoolTest(this)">${t('settings.proxyTest') || '测试连接'}</button>
            <button type="button" class="auth-src-btn ghost danger sm pp-delete-btn" title="${t('settings.proxyDel') || '删除'}"
                    data-tofu-action="_proxyPoolDelete(this)">${raw(trash)} <span>${t('settings.proxyDel') || '删除'}</span></button>
          </div>
        </div>
        <div class="pp-result" style="display:none"></div>
      </div>
      <input type="hidden" class="pp-credvault" value="${e.credential_vault || ''}">
      <input type="hidden" class="pp-clearcred" value="0">
    </div>`);
}

function _proxyPoolDisplayHost(url) {
  var value = String(url || '').trim();
  if (!value) return '';
  try {
    var parsed = new URL(value.indexOf('://') === -1 ? 'http://' + value : value);
    return parsed.host || value;
  } catch (_) {
    return value.replace(/^\w+:\/\//, '').replace(/^.*@/, '').split('/')[0];
  }
}

function _proxyPoolUrlHasCredential(url) {
  var value = String(url || '').trim();
  if (!value) return false;
  try {
    var parsed = new URL(value.indexOf('://') === -1 ? 'http://' + value : value);
    return !!(parsed.username || parsed.password);
  } catch (_) {
    return /:\/\/[^/@]+@/.test(value);
  }
}

function _proxyPoolUrlChanged(input) {
  var row = input && input.closest ? input.closest('.proxy-pool-row') : null;
  if (!row) return;
  row.setAttribute('data-url-dirty', '1');
  var sensitive = _proxyPoolUrlHasCredential(input.value);
  input.type = sensitive ? 'password' : 'text';
  var state = row.querySelector('.pp-url-state');
  if (state) state.textContent = sensitive
    ? (t('settings.proxyUrlProtected') || '敏感信息已隐藏')
    : 'HTTP / HTTPS';
  var visibility = row.querySelector('.pp-url-visibility');
  if (visibility) {
    visibility.title = sensitive
      ? (t('settings.proxyShowUrl') || '显示完整链接')
      : (t('settings.proxyHideUrl') || '隐藏链接');
    visibility.setAttribute('aria-label', visibility.title);
  }
  var host = row.querySelector('.pp-display-host');
  var hostLabel = _proxyPoolDisplayHost(input.value) ||
    (t('settings.proxyUrlEmpty') || '尚未填写链接');
  if (host) { host.textContent = hostLabel; host.title = hostLabel; }
}

function _proxyPoolAttachCredential(url, secret) {
  var rawSecret = String(secret || '');
  var pivot = rawSecret.indexOf(':');
  var username = pivot < 0 ? rawSecret : rawSecret.slice(0, pivot);
  var password = pivot < 0 ? '' : rawSecret.slice(pivot + 1);
  if (!username) return url;
  try {
    var parsed = new URL(String(url || '').indexOf('://') === -1 ? 'http://' + url : url);
    var userinfo = encodeURIComponent(username);
    if (password) userinfo += ':' + encodeURIComponent(password);
    return parsed.protocol + '//' + userinfo + '@' + parsed.host;
  } catch (_) {
    return url;
  }
}

function _proxyPoolToggleUrlVisibility(btn) {
  var row = btn && btn.closest ? btn.closest('.proxy-pool-row') : null;
  var input = row && row.querySelector('.pp-url');
  if (!row || !input) return;
  if (input.type === 'text') {
    input.type = 'password';
    btn.title = t('settings.proxyShowUrl') || '显示完整链接';
    btn.setAttribute('aria-label', btn.title);
    return;
  }
  var vault = (row.querySelector('.pp-credvault') || {}).value || '';
  if (_proxyPoolUrlHasCredential(input.value) || !vault) {
    input.type = 'text';
    btn.title = t('settings.proxyHideUrl') || '隐藏链接';
    btn.setAttribute('aria-label', btn.title);
    return;
  }
  var feedback = row.querySelector('.pp-url-help');
  if (typeof Api === 'undefined' || !Api.credentials || !Api.credentials.reveal) {
    if (feedback) feedback.textContent = t('settings.proxyRevealFail') || '暂时无法读取完整链接';
    return;
  }
  btn.disabled = true;
  if (feedback) feedback.textContent = t('settings.proxyRevealing') || '正在读取完整链接…';
  Api.credentials.reveal(vault).then(function (res) {
    btn.disabled = false;
    input.value = _proxyPoolAttachCredential(input.value, (res && res.value) || '');
    input.type = 'text';
    btn.title = t('settings.proxyHideUrl') || '隐藏链接';
    btn.setAttribute('aria-label', btn.title);
    if (feedback) feedback.textContent = t('settings.proxyUrlRevealed') || '完整链接已显示，30 秒后自动隐藏。';
    setTimeout(function () {
      if (input && input.isConnected) {
        input.type = 'password';
        btn.title = t('settings.proxyShowUrl') || '显示完整链接';
        btn.setAttribute('aria-label', btn.title);
        if (feedback) feedback.textContent = t('settings.proxyUrlHelp') || '复制完整链接后直接粘贴，账号密码会自动安全保存。';
      }
    }, 30000);
  }).catch(function (err) {
    btn.disabled = false;
    if (feedback) feedback.textContent = (err && err.message) ||
      (t('settings.proxyRevealFail') || '读取失败');
  });
}

function _proxyPoolSyncMeta(control) {
  var row = control && control.closest ? control.closest('.proxy-pool-row') : null;
  if (!row) return;
  var name = ((row.querySelector('.pp-name') || {}).value || '').trim();
  var order = (row.querySelector('.pp-order') || {}).textContent || '01';
  var display = row.querySelector('.pp-display-name');
  var displayName = name || ((t('settings.proxyCardTitle') || '代理') + ' ' + order);
  if (display) { display.textContent = displayName; display.title = displayName; }
  var scope = (row.querySelector('.pp-scope') || {}).value || 'subscription';
  var scopeLabel = scope === 'global'
    ? (t('settings.proxyScopeGlobal') || '全部出站流量')
    : (t('settings.proxyScopeSub') || '仅订阅流量');
  var badge = row.querySelector('.pp-scope-badge');
  if (badge) badge.textContent = scopeLabel;
}

function _proxyPoolSetEditor(row, open) {
  if (!row) return;
  var detail = row.querySelector('.proxy-pool-detail');
  var toggle = row.querySelector('.pp-edit-toggle');
  if (!detail || !toggle) return;
  row.classList.toggle('is-editing', !!open);
  detail.hidden = !open;
  toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
  toggle.title = open
    ? (t('settings.proxyCloseEditor') || '收起编辑')
    : (t('settings.proxyEdit') || '编辑代理');
  toggle.setAttribute('aria-label', toggle.title);
  if (!open) {
    var urlInput = row.querySelector('.pp-url');
    if (urlInput && (_proxyPoolUrlHasCredential(urlInput.value) ||
        row.getAttribute('data-has-credential') === '1')) {
      urlInput.type = 'password';
    }
  }
}

function _proxyPoolToggleEditor(btn) {
  var row = btn && btn.closest ? btn.closest('.proxy-pool-row') : null;
  if (!row) return;
  var willOpen = !row.classList.contains('is-editing');
  var rows = document.querySelectorAll('#proxyPoolList .proxy-pool-row');
  for (var i = 0; i < rows.length; i++) {
    if (rows[i] !== row) _proxyPoolSetEditor(rows[i], false);
  }
  _proxyPoolSetEditor(row, willOpen);
}

function _proxyPoolAppendRow() {
  var list = document.getElementById('proxyPoolList');
  if (!list) return;
  var empty = list.querySelector('.proxy-pool-empty');
  if (empty) empty.remove();
  var count = list.querySelectorAll('.proxy-pool-row').length;
  list.insertAdjacentHTML('beforeend', _proxyPoolRowHtml({ enabled: true }, count));
  _proxyPoolRefreshOrder();
  var rows = list.querySelectorAll('.proxy-pool-row');
  var last = rows[rows.length - 1];
  var editToggle = last && last.querySelector('.pp-edit-toggle');
  if (editToggle) _proxyPoolToggleEditor(editToggle);
  var urlInput = last && last.querySelector('.pp-url');
  if (urlInput) urlInput.focus();
}

function _proxyPoolDelete(btn) {
  var row = btn && btn.closest ? btn.closest('.proxy-pool-row') : null;
  if (!row) return;
  var list = row.parentNode;
  row.remove();
  if (list && !list.querySelector('.proxy-pool-row')) {
    list.innerHTML = String(safeHtml`<div class="proxy-pool-empty">
      <strong>${t('settings.proxyPoolEmpty') || '还没有代理'}</strong>
      <span>${t('settings.proxyPoolEmptyHint') || '不添加时，将继续使用系统环境变量中的代理配置。'}</span>
    </div>`);
  }
  _proxyPoolRefreshOrder();
}

function _proxyPoolRefreshOrder() {
  var list = document.getElementById('proxyPoolList');
  if (!list) return;
  var rows = list.querySelectorAll('.proxy-pool-row');
  for (var i = 0; i < rows.length; i++) {
    var order = rows[i].querySelector('.pp-order');
    if (order) order.textContent = String(i + 1).padStart(2, '0');
    var up = rows[i].querySelector('.pp-move-up');
    var down = rows[i].querySelector('.pp-move-down');
    if (up) up.disabled = i === 0;
    if (down) down.disabled = i === rows.length - 1;
    _proxyPoolSyncMeta(rows[i].querySelector('.pp-name') || rows[i]);
  }
  var countEl = document.getElementById('proxyPoolCount');
  if (countEl) {
    countEl.textContent = (t('settings.proxyPoolCount') || '{count} 个代理')
      .replace('{count}', String(rows.length));
  }
}

function _proxyPoolMove(btn, delta) {
  var row = btn && btn.closest ? btn.closest('.proxy-pool-row') : null;
  if (!row || !row.parentNode) return;
  if (delta < 0 && row.previousElementSibling) {
    row.parentNode.insertBefore(row, row.previousElementSibling);
  } else if (delta > 0 && row.nextElementSibling) {
    row.parentNode.insertBefore(row.nextElementSibling, row);
  }
  _proxyPoolRefreshOrder();
}

function _proxyPoolRowPayload(row) {
  function val(sel) {
    var el = row.querySelector(sel);
    return (el && el.value || '').trim();
  }
  var enabledEl = row.querySelector('.pp-enabled');
  var url = val('.pp-url');
  var hadCredential = row.getAttribute('data-has-credential') === '1';
  var urlChanged = row.getAttribute('data-url-dirty') === '1';
  var clearCredential = hadCredential && urlChanged && !_proxyPoolUrlHasCredential(url);
  var clearEl = row.querySelector('.pp-clearcred');
  if (clearEl) clearEl.value = clearCredential ? '1' : '0';
  return {
    id: row.getAttribute('data-id') || '',
    name: val('.pp-name'),
    url: url,
    scope: (row.querySelector('.pp-scope') || {}).value || 'subscription',
    credential_vault: val('.pp-credvault'),
    clear_credential: clearCredential,
    enabled: enabledEl ? !!enabledEl.checked : true,
  };
}

function _proxyPoolTest(btn) {
  var row = btn && btn.closest ? btn.closest('.proxy-pool-row') : null;
  if (!row || typeof Api === 'undefined' || !Api.network) return;
  var result = row.querySelector('.pp-result');
  if (!result) return;
  var payload = _proxyPoolRowPayload(row);
  if (!payload.url) {
    result.style.display = '';
    result.className = 'pp-result err';
    result.textContent = t('settings.proxyTestNoUrl') || '先填写代理地址';
    return;
  }
  btn.disabled = true;
  result.style.display = '';
  result.className = 'pp-result';
  result.textContent = t('settings.proxyTesting') || '测试中…';
  Api.network.proxyTest(payload).then(function (data) {
    btn.disabled = false;
    var results = (data && data.results) || [];
    if (!results.length) {
      result.className = 'pp-result err';
      result.textContent = (data && data.error) || (t('settings.proxyTestFail') || '测试失败');
      return;
    }
    var ok = !!data.any_ok;
    result.className = 'pp-result ' + (ok ? 'ok' : 'err');
    result.textContent = results.map(function (r) {
      if (r.verdict === 'ok') {
        return (t('settings.proxyTestOkTpl') || '{label} 可达（HTTP {code} · {ms}ms）')
          .replace('{label}', r.label || r.target)
          .replace('{code}', String(r.status))
          .replace('{ms}', String(r.latency_ms));
      }
      if (r.verdict === 'geo_blocked') {
        return (t('settings.proxyTestBlockedTpl') || '{label} 被拦截（HTTP {code}）')
          .replace('{label}', r.label || r.target)
          .replace('{code}', String(r.status));
      }
      if (r.verdict === 'proxy_auth') {
        return (t('settings.proxyTestAuthTpl') || '{label} 代理认证失败（凭证失效或已过期）')
          .replace('{label}', r.label || r.target);
      }
      return (t('settings.proxyTestFailTpl') || '{label} 网络失败：{err}')
        .replace('{label}', r.label || r.target)
        .replace('{err}', r.error || 'timeout');
    }).join('　·　');
  }).catch(function (e) {
    btn.disabled = false;
    result.className = 'pp-result err';
    result.textContent = ((e && e.body && e.body.error) || (e && e.message) ||
      (t('settings.proxyTestFail') || '测试失败'));
  });
}

/** Collect the editor rows into the save payload. Returns null when the
 *  pool editor is absent from the DOM (legacy/other surfaces) so the caller
 *  leaves the server's proxy config untouched. */
function _collectProxyPool() {
  var list = document.getElementById('proxyPoolList');
  if (!list) return null;
  var out = [];
  var rows = list.querySelectorAll('.proxy-pool-row');
  for (var i = 0; i < rows.length; i++) {
    var p = _proxyPoolRowPayload(rows[i]);
    if (!p.url) continue;  // blank rows are dropped, never persisted
    out.push(p);
  }
  return out;
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
  _populateResponsesExperimentControls();
  _populateCostExperiment(cfg);
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
  /* Populate IndexedDB cache stats */
  _refreshCacheStatsUI();
  if (typeof _renderCredentialsVault === 'function') _renderCredentialsVault();
}

function _setSelectValue(id, value, fallback) {
  var el = /** @type {HTMLSelectElement|null} */ (document.getElementById(id));
  if (!el) return;
  var wanted = String(value || fallback || '');
  var exists = Array.prototype.some.call(el.options || [], function(opt) {
    return opt.value === wanted;
  });
  el.value = exists ? wanted : fallback;
}

function _populateResponsesExperimentControls() {
  var toolsCfg = (config && config.tools) || {};
  var responsesCfg = (config && config.responses) || {};
  var orchestrationCfg = (config && config.orchestration) || {};
  _setSelectValue('settingToolSearch', toolsCfg.toolSearch, 'auto');
  _setSelectValue('settingProgrammaticCalling', toolsCfg.programmaticCalling, 'on');
  _setSelectValue('settingResponsesPromptProfile', responsesCfg.promptProfile, 'auto');
  _setSelectValue('settingResponsesTransport', responsesCfg.transport, 'sse');
  _setSelectValue('settingResponsesReasoning', responsesCfg.reasoningMode, 'standard');
  _setSelectValue('settingResponsesVerbosity', responsesCfg.verbosity, 'medium');
  _setSelectValue('settingResponsesImageDetail', responsesCfg.imageDetail, 'auto');
  var multiAgentMode = Object.prototype.hasOwnProperty.call(orchestrationCfg, 'multiAgent')
    ? orchestrationCfg.multiAgent : responsesCfg.multiAgent;
  _setSelectValue('settingResponsesMultiAgent', multiAgentMode, 'auto');
  var maxEl = document.getElementById('settingResponsesMaxSubagents');
  var maxAgents = Object.prototype.hasOwnProperty.call(orchestrationCfg, 'maxConcurrentAgents')
    ? orchestrationCfg.maxConcurrentAgents : responsesCfg.maxConcurrentSubagents;
  if (maxEl) maxEl.value = Math.max(1, Math.min(8,
    parseInt(maxAgents, 10) || 3));
  _syncResponsesExperimentUi();
}

function _syncResponsesExperimentUi() {
  var mode = document.getElementById('settingResponsesMultiAgent');
  var maxEl = document.getElementById('settingResponsesMaxSubagents');
  if (maxEl) maxEl.disabled = !mode || mode.value === 'off';
}

function _collectResponsesExperimentControls() {
  var value = function(id, fallback) {
    var el = document.getElementById(id);
    return (el && el.value) || fallback;
  };
  var maxEl = document.getElementById('settingResponsesMaxSubagents');
  var maxSubagents = Math.max(1, Math.min(8,
    parseInt(maxEl && maxEl.value, 10) || 3));
  config.tools = Object.assign({}, config.tools || {}, {
    toolSearch: value('settingToolSearch', 'auto'),
    // Composer switches shape model-visible schemas; the browser UI never
    // turns those switches into an execution allow-list. ``selected_only``
    // remains a headless/API compatibility input only.
    executionScope: 'available',
    programmaticCalling: value('settingProgrammaticCalling', 'on'),
  });
  config.responses = Object.assign({}, config.responses || {}, {
    transport: value('settingResponsesTransport', 'sse'),
    reasoningMode: value('settingResponsesReasoning', 'standard'),
    verbosity: value('settingResponsesVerbosity', 'medium'),
    imageDetail: value('settingResponsesImageDetail', 'auto'),
    promptProfile: value('settingResponsesPromptProfile', 'auto'),
  });
  // Provider-neutral owner. Old saved configs are read above, then migrated
  // on the next save so a Responses-only label can no longer imply that
  // other models lose multi-agent orchestration.
  config.orchestration = Object.assign({}, config.orchestration || {}, {
    multiAgent: value('settingResponsesMultiAgent', 'auto'),
    maxConcurrentAgents: maxSubagents,
  });
  delete config.responses.multiAgent;
  delete config.responses.maxConcurrentSubagents;
}

function _costExperimentInt(id, fallback, min, max) {
  var el = document.getElementById(id);
  var value = parseInt(el && el.value, 10);
  if (!Number.isFinite(value)) value = fallback;
  return Math.max(min, Math.min(max, value));
}

function _populateCostExperiment(cfg) {
  var exp = (cfg && cfg.cost_experiment) || {};
  var enabled = document.getElementById('settingCostExperimentEnabled');
  var id = document.getElementById('settingCostExperimentId');
  var traffic = document.getElementById('settingCostExperimentTraffic');
  var treatment = document.getElementById('settingCostExperimentTreatment');
  var minSample = document.getElementById('settingCostExperimentMinSample');
  if (!enabled) return;
  enabled.checked = exp.enabled === true;
  if (id) id.value = exp.experiment_id || 'context-cost-v1';
  if (traffic) traffic.value = exp.traffic_percent == null ? 10 : exp.traffic_percent;
  if (treatment) treatment.value = exp.treatment_percent == null ? 50 : exp.treatment_percent;
  if (minSample) minSample.value = exp.min_sample_size == null ? 20 : exp.min_sample_size;
  var trafficVal = document.getElementById('costExperimentTrafficVal');
  var treatmentVal = document.getElementById('costExperimentTreatmentVal');
  if (trafficVal) trafficVal.textContent = (traffic ? traffic.value : 10) + '%';
  if (treatmentVal) treatmentVal.textContent = (treatment ? treatment.value : 50) + '%';
  _syncCostExperimentUi();
  _refreshCostExperimentReport();
}

function _syncCostExperimentUi() {
  var enabled = document.getElementById('settingCostExperimentEnabled');
  var isOn = !!(enabled && enabled.checked);
  document.querySelectorAll('.cost-exp-fields input').forEach(function(el) {
    el.disabled = !isOn;
  });
  var controls = document.querySelector('.cost-exp-controls');
  if (controls) controls.classList.toggle('is-off', !isOn);
}

function _collectCostExperimentConfig() {
  var enabled = document.getElementById('settingCostExperimentEnabled');
  if (!enabled) return null;
  var id = document.getElementById('settingCostExperimentId');
  return {
    enabled: !!(enabled && enabled.checked),
    experiment_id: ((id && id.value) || 'context-cost-v1').trim(),
    traffic_percent: _costExperimentInt('settingCostExperimentTraffic', 10, 0, 100),
    treatment_percent: _costExperimentInt('settingCostExperimentTreatment', 50, 1, 99),
    min_sample_size: _costExperimentInt('settingCostExperimentMinSample', 20, 2, 10000),
  };
}

function _costExperimentPct(value) {
  return value == null ? '—' : (Number(value) * 100).toFixed(1) + '%';
}

function _costExperimentMoney(value) {
  return value == null ? '—' : '$' + Number(value).toFixed(4);
}

async function _refreshCostExperimentReport() {
  var el = document.getElementById('costExperimentReport');
  if (!el || typeof Api === 'undefined' || !Api.costExperiments) return;
  el.innerHTML = '<div class="cost-exp-empty">' + escapeHtml(t('settings.costExperimentLoading')) + '</div>';
  try {
    var report = await Api.costExperiments.report(14);
    var arms = (report && report.arms) || {};
    var control = arms.control || {};
    var optimized = arms.optimized || {};
    var decision = (report && report.decision) || {};
    var delta = report && report.comparison && report.comparison.costPerConversationDeltaPct;
    var deltaText = delta == null ? '—' : (delta > 0 ? '+' : '') + Number(delta).toFixed(1) + '%';
    var readiness;
    if (report && report.promotionEligible) {
      readiness = t('settings.costExperimentPromotable');
    } else if (decision.status === 'do_not_promote') {
      readiness = t('settings.costExperimentDoNotPromote');
    } else if (decision.dataValid === false) {
      readiness = t('settings.costExperimentInvalidDecision');
    } else if (report && report.sampleReady) {
      readiness = t('settings.costExperimentGuardrailsPending');
    } else {
      readiness = t('settings.costExperimentCollecting', { n: (report && report.minSampleSize) || 20 });
    }
    var warnings = [];
    if (report && report.truncated) {
      warnings.push(t('settings.costExperimentTruncated', { n: report.rowCap }));
    }
    if (report && report.configurationError) {
      warnings.push(t('settings.costExperimentConfigurationError'));
    }
    var blockers = Array.isArray(decision.blockers) ? decision.blockers : [];
    if (blockers.length) {
      warnings.push(t('settings.costExperimentBlockers', {
        reasons: blockers.map(function(code) {
          var translated = t('settings.costExperimentBlocker.' + code);
          return translated === 'settings.costExperimentBlocker.' + code ? code : translated;
        }).join(' · '),
      }));
    }
    var warning = warnings.map(function(message) {
      return '<div class="cost-exp-warning">' + escapeHtml(message) + '</div>';
    }).join('');
    function armCard(name, arm, cls) {
      return '<div class="cost-exp-result ' + cls + '">' +
        '<strong>' + escapeHtml(name) + '</strong>' +
        '<span>' + escapeHtml(t('settings.costExperimentConversations', { n: arm.conversations || 0 })) + '</span>' +
        '<span>' + escapeHtml(t('settings.costExperimentFullyPriced', { n: arm.fullyPricedAssignmentUnits || 0 })) + '</span>' +
        '<b>' + escapeHtml(_costExperimentMoney(arm.analysisCostPerFullyPricedAssignmentUnitUsd)) + '</b>' +
        '<small>' + escapeHtml(t('settings.costExperimentPerConversation')) + '</small>' +
        '<span>' + escapeHtml(t('settings.costExperimentCoverage')) + ' ' + escapeHtml(_costExperimentPct(arm.assignmentUnitPricingCoverage)) + '</span>' +
        '</div>';
    }
    var digest = String((report && report.specDigest) || '');
    var identity = '';
    if (digest) {
      identity += '<div class="settings-toggle-desc">' + escapeHtml(t('settings.costExperimentSpec', { digest: digest.slice(0, 12) })) + '</div>';
    }
    if (report && report.maximumAssignmentUnits) {
      identity += '<div class="settings-toggle-desc">' + escapeHtml(t('settings.costExperimentHorizon', {
        observed: report.observedAssignmentUnits || 0,
        maximum: report.maximumAssignmentUnits,
      })) + '</div>';
    }
    el.innerHTML = warning + '<div class="cost-exp-summary">' +
      armCard(t('settings.costExperimentArmControl'), control, 'control') +
      '<div class="cost-exp-delta"><span>' + escapeHtml(t('settings.costExperimentDelta')) + '</span><strong>' + escapeHtml(deltaText) + '</strong><small>' + escapeHtml(readiness) + '</small></div>' +
      armCard(t('settings.costExperimentArmOptimized'), optimized, 'optimized') +
      '</div><div class="cost-exp-table-wrap"><table class="cost-exp-table"><thead><tr>' +
      '<th>' + escapeHtml(t('settings.costExperimentMetric')) + '</th><th>' + escapeHtml(t('settings.costExperimentArmControl')) + '</th><th>' + escapeHtml(t('settings.costExperimentArmOptimized')) + '</th>' +
      '</tr></thead><tbody>' +
      '<tr><td>' + escapeHtml(t('settings.costExperimentPromptPerTurn')) + '</td><td>' + escapeHtml(String(control.promptTokensPerTurn == null ? '—' : control.promptTokensPerTurn)) + '</td><td>' + escapeHtml(String(optimized.promptTokensPerTurn == null ? '—' : optimized.promptTokensPerTurn)) + '</td></tr>' +
      '<tr><td>' + escapeHtml(t('settings.costExperimentCacheReadRatio')) + '</td><td>' + escapeHtml(_costExperimentPct(control.cacheReadRatio)) + '</td><td>' + escapeHtml(_costExperimentPct(optimized.cacheReadRatio)) + '</td></tr>' +
      '<tr><td>' + escapeHtml(t('settings.costExperimentLatencyAvg')) + '</td><td>' + escapeHtml(control.latencyAvgMs == null ? '—' : Math.round(control.latencyAvgMs) + ' ms') + '</td><td>' + escapeHtml(optimized.latencyAvgMs == null ? '—' : Math.round(optimized.latencyAvgMs) + ' ms') + '</td></tr>' +
      '<tr><td>' + escapeHtml(t('settings.costExperimentLatencyP90')) + '</td><td>' + escapeHtml(control.latencyP90Ms == null ? '—' : Math.round(control.latencyP90Ms) + ' ms') + '</td><td>' + escapeHtml(optimized.latencyP90Ms == null ? '—' : Math.round(optimized.latencyP90Ms) + ' ms') + '</td></tr>' +
      '<tr><td>' + escapeHtml(t('settings.costExperimentOraclePass')) + '</td><td>' + escapeHtml(_costExperimentPct(control.oraclePassRate)) + '</td><td>' + escapeHtml(_costExperimentPct(optimized.oraclePassRate)) + '</td></tr>' +
      '<tr><td>' + escapeHtml(t('settings.costExperimentErrorFree')) + '</td><td>' + escapeHtml(_costExperimentPct(control.terminalWithoutErrorRate)) + '</td><td>' + escapeHtml(_costExperimentPct(optimized.terminalWithoutErrorRate)) + '</td></tr>' +
      '</tbody></table></div>' + identity;
  } catch (e) {
    el.innerHTML = '<div class="cost-exp-warning">' + escapeHtml(t('settings.costExperimentLoadFailed')) + ': ' + escapeHtml(e && e.message || '') + '</div>';
  }
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
    // Force all in-memory conversations to _turnSnapshotRequired so next click refetches
    conversations.forEach(function(c) {
      if (c.id !== activeConvId) c._turnSnapshotRequired = true;
    });
    if (typeof showToast === 'function') showToast(t('settings.cacheCleared'));
  });
}
