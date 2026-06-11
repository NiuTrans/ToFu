/* ═══════════════════════════════════════════════════════════════════
   settings/auth_sources.js — Login-walled fetch sources (Xiaohongshu, …)

   Renders the "需要登录的来源" section in Settings → Search.

   Connect flow (works for BOTH local and remote deployments):
     1. User clicks "连接" → a guided panel expands.
     2. "在浏览器中打开登录页" opens the site's login page in the USER's
        OWN browser tab (window.open) — NOT the server's browser. This is
        the key correctness fix: the server may be remote/headless.
     3. User logs in there, copies the Cookie request header from devtools,
        pastes it back here, and saves.

   Why not auto-capture? A page on Tofu's origin cannot read another site's
   cookies (cross-origin + HttpOnly web_session). Without a browser
   extension, a guided copy-paste is the only universal path. The
   server-side headful-login route still exists (debug/local convenience)
   but is intentionally NOT the primary UI affordance.

   Concatenated by lib/js_bundler.py — shared window scope, no imports.
   ═══════════════════════════════════════════════════════════════════ */

// Per-domain login landing pages for the "open login page" button.
var _AUTH_SRC_LOGIN_URLS = {
  'xiaohongshu.com': 'https://www.xiaohongshu.com/explore',
};

// Per-domain "which cookie actually carries the login session" hint. The
// store keeps the whole Cookie header, but flagging the critical one helps
// users confirm they grabbed the right thing. Shown in the connect panel.
var _AUTH_SRC_KEY_COOKIE = {
  'xiaohongshu.com': 'web_session',
};

function _renderAuthSources() {
  var box = document.getElementById('authSourcesList');
  if (!box) return;
  box.innerHTML = safeHtml`<div class="auth-src-loading">${t('common.loading') || '加载中…'}</div>`;
  Api.authSources.list().then(function (data) {
    var sources = (data && data.sources) || [];
    if (!sources.length) {
      box.innerHTML = safeHtml`<div class="auth-src-empty">${t('settings.authSourcesEmpty') || '暂无可登录的来源。'}</div>`;
      return;
    }
    box.innerHTML = sources.map(_authSourceCardHtml).join('');
  }).catch(function (e) {
    console.warn('[AuthSrc] list failed', e);
    box.innerHTML = safeHtml`<div class="auth-src-empty">${t('settings.authSourcesLoadFail') || '加载失败'}</div>`;
  });
}

function _authSourceCardHtml(src) {
  var connected = !!src.has_cookies;
  var enabled = !!src.enabled;
  var dom = src.domain || '';
  var id = _domId(dom);

  var stateClass, stateText;
  if (connected && enabled) {
    stateClass = 'on';
    stateText = (t('settings.authSrcConnected') || '已连接') + ' · ' + src.cookie_count + ' cookies' +
      (src.has_proxy ? (' · proxy ' + (src.proxy_hint || '')) : '');
  } else if (connected && !enabled) {
    stateClass = 'paused';
    stateText = t('settings.authSrcDisabled') || '已连接（已停用）';
  } else {
    stateClass = 'off';
    stateText = t('settings.authSrcNotConnected') || '未连接';
  }

  var primaryBtn = connected
    ? safeHtml`<button class="auth-src-btn" onclick="_authSourceTogglePanel('${raw(dom)}')">${t('settings.authSrcReconnect') || '重新连接'}</button>`
    : safeHtml`<button class="auth-src-btn primary" onclick="_authSourceTogglePanel('${raw(dom)}')">${t('settings.authSrcConnect') || '连接'}</button>`;

  var disconnectBtn = connected
    ? safeHtml`<button class="auth-src-btn ghost danger" onclick="_authSourceDisconnect('${raw(dom)}')">${t('settings.authSrcDisconnectBtn') || '断开'}</button>`
    : '';

  return safeHtml`
    <div class="auth-src-card ${raw(stateClass)}" data-domain="${dom}">
      <div class="auth-src-row">
        <span class="auth-src-state-dot ${raw(stateClass)}"></span>
        <div class="auth-src-meta">
          <div class="auth-src-name">${src.label || dom}<span class="auth-src-domain">${dom}</span></div>
          <div class="auth-src-state-text">${stateText}</div>
        </div>
        <label class="auth-src-switch" title="${t('settings.authSrcToggle') || '启用 / 停用'}">
          <input type="checkbox" ${raw(enabled ? 'checked' : '')} ${raw(connected ? '' : 'disabled')}
                 onchange="_authSourceToggle('${raw(dom)}', this.checked)">
          <span class="auth-src-switch-track"><span class="auth-src-switch-thumb"></span></span>
        </label>
      </div>
      <div class="auth-src-actions">
        ${raw(primaryBtn)}
        ${raw(disconnectBtn)}
      </div>
      ${raw(_authSourceConnectPanel(dom, id))}
      <div class="auth-src-msg" id="authSrcMsg_${raw(id)}"></div>
    </div>`;
}

function _authSourceConnectPanel(dom, id) {
  var hasLoginUrl = !!_AUTH_SRC_LOGIN_URLS[dom];
  var step1 = hasLoginUrl
    ? safeHtml`
      <li>
        <span class="auth-src-step-txt">${t('settings.authSrcStep1') || '在你自己的浏览器中打开该站点并登录'}</span>
        <button class="auth-src-btn sm" onclick="_authSourceOpenLogin('${raw(dom)}')">
          ${t('settings.authSrcOpenLogin') || '打开登录页 ↗'}
        </button>
      </li>`
    : safeHtml`<li><span class="auth-src-step-txt">${t('settings.authSrcStep1Generic') || '在你自己的浏览器中登录该站点'}</span></li>`;

  var keyCookieHint = _AUTH_SRC_KEY_COOKIE[dom]
    ? safeHtml`<div class="auth-src-key-cookie">${raw(t('settings.authSrcKeyCookie') || '关键 Cookie：登录态由 <code>web_session</code> 携带，请确保它在内。')}</div>`
    : '';

  return safeHtml`
    <div class="auth-src-panel" id="authSrcPanel_${raw(id)}" style="display:none">
      <ol class="auth-src-steps">
        ${raw(step1)}
        <li>${t('settings.authSrcStep2') || '打开开发者工具 (F12) → Network，点任一请求，复制 Request Headers 里完整的 Cookie'}</li>
        <li>${t('settings.authSrcStep3') || '粘贴到下方并保存'}</li>
      </ol>
      ${raw(keyCookieHint)}
      <textarea class="auth-src-cookie" id="authSrcCookie_${raw(id)}" rows="3"
                placeholder="${t('settings.authSrcCookiePh') || 'web_session=...; a1=...'}"></textarea>
      <input type="text" class="auth-src-proxy" id="authSrcProxy_${raw(id)}"
             placeholder="${t('settings.authSrcProxyPh') || '可选代理，例如 http://host:port'}">
      <div class="auth-src-panel-actions">
        <button class="auth-src-btn primary" onclick="_authSourceSavePaste('${raw(dom)}')">
          ${t('settings.authSrcSaveConnect') || '保存并连接'}
        </button>
        <button class="auth-src-btn ghost" onclick="_authSourceTogglePanel('${raw(dom)}')">
          ${t('common.cancel') || '取消'}
        </button>
      </div>
    </div>`;
}

/** DOM-id-safe version of a domain (non-alnum → underscore). */
function _domId(dom) {
  return String(dom).replace(/[^a-zA-Z0-9]/g, '_');
}

function _authSrcSetMsg(dom, text, kind) {
  var el = document.getElementById('authSrcMsg_' + _domId(dom));
  if (!el) return;
  el.textContent = text || '';
  el.className = 'auth-src-msg' + (kind ? ' ' + kind : '');
}

function _authSourceTogglePanel(dom) {
  var el = document.getElementById('authSrcPanel_' + _domId(dom));
  if (el) el.style.display = el.style.display === 'none' ? '' : 'none';
}

function _authSourceOpenLogin(dom) {
  var url = _AUTH_SRC_LOGIN_URLS[dom] || ('https://' + dom + '/');
  window.open(url, '_blank', 'noopener');
}

function _authSourceToggle(dom, on) {
  Api.authSources.toggle(dom, on).then(_renderAuthSources);
}

function _authSourceSavePaste(dom) {
  var id = _domId(dom);
  var cookie = (document.getElementById('authSrcCookie_' + id) || {}).value || '';
  var proxy = (document.getElementById('authSrcProxy_' + id) || {}).value || '';
  if (!cookie.trim()) {
    _authSrcSetMsg(dom, t('settings.authSrcCookieEmpty') || '请粘贴 Cookie', 'err');
    return;
  }
  _authSrcSetMsg(dom, t('common.saving') || '保存中…');
  Api.authSources.upsert({ domain: dom, cookie_header: cookie, proxy: proxy, enabled: true })
    .then(function () {
      _authSrcSetMsg(dom, t('settings.authSrcSaved') || '已连接', 'ok');
      _renderAuthSources();
    }).catch(function (e) {
      _authSrcSetMsg(dom, (t('settings.authSrcSaveFail') || '保存失败: ') +
        ((e && e.body && e.body.error) || (e && e.message) || ''), 'err');
    });
}

function _authSourceDisconnect(dom) {
  if (!confirm(t('settings.authSrcDisconnectConfirm') || '断开并清除该来源的 Cookie？')) return;
  Api.authSources.remove(dom).then(_renderAuthSources);
}
