/* ═══════════════════════════════════════════════════════════════════
   settings/auth_sources.js — Login-walled fetch sources (Xiaohongshu, …)

   Renders the "需要登录的来源" section in Settings → Search.

   Connect flow (works for BOTH local and remote deployments):
     1. User clicks "连接" → a guided panel expands.
     2. "在浏览器中打开登录页" opens the site's login page in the USER's
        OWN browser tab (window.open) — NOT the server's browser. This is
        the key correctness fix: the server may be remote/headless.
     3. User logs in there, copies each required cookie's VALUE from
        devtools into its own labelled input, and saves.

   Why one input per cookie instead of one textarea? A single free-text
   "paste the whole Cookie header" box makes the user responsible for the
   `name=value; name=value` syntax, and a mistyped delimiter stored a
   garbage cookie set that the UI then reported as "已连接" — the failure
   only surfaced later as an unexplained empty fetch. The fields are
   declared server-side (lib/auth_sources.py DEFAULT_SOURCES) and arrive
   on each source row, so this file hardcodes no per-site knowledge.

   Why not auto-capture? A page on Tofu's origin cannot read another site's
   cookies (cross-origin + HttpOnly web_session). Without a browser
   extension, a guided copy-paste is the only universal path. The
   server-side headful-login route still exists (debug/local convenience)
   but is intentionally NOT the primary UI affordance.

   Concatenated by lib/js_bundler.py — shared window scope, no imports.
   ═══════════════════════════════════════════════════════════════════ */

function _renderAuthSources() {
  var box = document.getElementById('authSourcesList');
  if (!box) return;
  box.innerHTML = String(safeHtml`<div class="auth-src-loading">${t('common.loading') || '加载中…'}</div>`);
  Api.authSources.list().then(function (data) {
    var sources = (data && data.sources) || [];
    if (!sources.length) {
      box.innerHTML = String(safeHtml`<div class="auth-src-empty">${t('settings.authSourcesEmpty') || '暂无可登录的来源。'}</div>`);
      return;
    }
    box.innerHTML = sources.map(_authSourceCardHtml).join('');
    _authSourceProbeLiveSessions(sources);
  }).catch(function (e) {
    console.warn('[AuthSrc] list failed', e);
    box.innerHTML = String(safeHtml`<div class="auth-src-empty">${t('settings.authSourcesLoadFail') || '加载失败'}</div>`);
  });
}

function _authSourceCardHtml(src) {
  var connected = !!src.has_cookies;
  var enabled = !!src.enabled;
  var dom = src.domain || '';
  var id = _domId(dom);
  var strategy = src.access_strategy || 'browser_first';
  // The toggle's credential gate: replay needs stored cookies, but a
  // browser_first / public row's credential is the user's LIVE browser
  // session — no cookie paste required (OpenCLI parity).
  var toggleAllowed = connected || strategy !== 'cookies_replay';

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
          ${raw(_authSourceRegistryBadges(src))}
          ${raw(_authSourceRiskNote(src))}
        </div>
        <label class="auth-src-switch" title="${t('settings.authSrcToggle') || '启用 / 停用'}">
          <input type="checkbox" ${raw(enabled ? 'checked' : '')} ${raw(toggleAllowed ? '' : 'disabled')}
                 onchange="_authSourceToggle('${raw(dom)}', this.checked)">
          <span class="auth-src-switch-track"><span class="auth-src-switch-thumb"></span></span>
        </label>
      </div>
      <div class="auth-src-actions">
        ${raw(primaryBtn)}
        ${raw(disconnectBtn)}
      </div>
      ${raw(_authSourceConnectPanel(src, dom, id))}
      <div class="auth-src-msg" id="authSrcMsg_${raw(id)}"></div>
    </div>`;
}

/** One labelled input per declared cookie. Falls back to a single generic
 *  field when the server declares none (an unknown//custom domain). */
function _authSourceFieldRows(src, id) {
  var fields = (src && src.fields) || [];
  if (!fields.length) {
    fields = [{ name: 'cookie', importance: 'required' }];
  }
  return fields.map(function (f, i) {
    var imp = f.importance || 'optional';
    var badge = imp === 'required'
      ? (t('settings.authSrcRequired') || '必填')
      : (imp === 'recommended' ? (t('settings.authSrcRecommended') || '建议填写')
                               : (t('settings.authSrcOptional') || '可选'));
    return safeHtml`
      <div class="auth-src-field">
        <label class="auth-src-field-label" for="authSrcField_${raw(id)}_${raw(String(i))}">
          <code>${f.name}</code>
          <span class="auth-src-field-badge ${raw(imp)}">${badge}</span>
        </label>
        <input type="text" class="auth-src-field-input" spellcheck="false" autocomplete="off"
               id="authSrcField_${raw(id)}_${raw(String(i))}"
               data-cookie-name="${f.name}" data-importance="${imp}"
               placeholder="${(t('settings.authSrcFieldPh') || '粘贴 {name} 的值').replace('{name}', f.name)}">
      </div>`;
  }).join('');
}

function _authSourceConnectPanel(src, dom, id) {
  var loginUrl = (src && src.login_url) || '';
  var step1 = loginUrl
    ? safeHtml`
      <li>
        <span class="auth-src-step-txt">${t('settings.authSrcStep1') || '在你自己的浏览器中打开该站点并登录'}</span>
        <button class="auth-src-btn sm" onclick="_authSourceOpenLogin('${raw(dom)}', '${raw(loginUrl)}')">
          ${t('settings.authSrcOpenLogin') || '打开登录页 ↗'}
        </button>
      </li>`
    : safeHtml`<li><span class="auth-src-step-txt">${t('settings.authSrcStep1Generic') || '在你自己的浏览器中登录该站点'}</span></li>`;

  var isBrowserFirst = ((src && src.access_strategy) || 'browser_first') === 'browser_first';
  var browserFirstHint = isBrowserFirst
    ? safeHtml`<div class="auth-src-live-hint">${t('settings.authSrcBrowserFirstHint') || '通常无需粘贴 Cookie：在你自己的浏览器里登录该站即可——检测到浏览器会话后直接启用，搜索与抓取就走你的活会话。下面粘贴 Cookie 只是浏览器不在线时的离线兜底。'}</div>`
    : '';
  var fieldsSteps = isBrowserFirst
    ? safeHtml`<li>${t('settings.authSrcStep2FieldsFallback') || '（离线兜底，可选）浏览器不在线时才需要：F12 → Application → Cookies，逐个复制 Cookie 值粘贴到下面'}</li>`
    : safeHtml`<li>${t('settings.authSrcStep2Fields') || '打开开发者工具 (F12) → Application → Cookies，找到下面每个 Cookie，逐个复制它的 Value'}</li>
           <li>${t('settings.authSrcStep3Fields') || '分别粘贴到对应输入框并保存（只填值，不要带名字或分号）'}</li>`;

  return safeHtml`
    <div class="auth-src-panel" id="authSrcPanel_${raw(id)}" style="display:none">
      ${raw(_authSourceRiskNote(src))}
      ${raw(browserFirstHint)}
      <ol class="auth-src-steps">
        ${raw(step1)}
        ${raw(fieldsSteps)}
      </ol>
      <div class="auth-src-fields">
        ${raw(_authSourceFieldRows(src, id))}
      </div>
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

/** Registry badges (Site Knowledge Layer P2): the access STRATEGY (who
 *  opens the door — browser_first / cookies_replay / public) and the
 *  KNOWLEDGE state (a site-doctor-pinned extractor = 已内化 vN, else
 *  仅凭据). Both are projected server-side on each row; the JS hardcodes
 *  no per-site facts — internalizing a site is appending a registry row. */
function _authSourceRegistryBadges(src) {
  var strategy = (src && src.access_strategy) || 'browser_first';
  var strategyKey = {
    browser_first: 'settings.authSrcStrategyBrowserFirst',
    cookies_replay: 'settings.authSrcStrategyCookiesReplay',
    'public': 'settings.authSrcStrategyPublic'
  }[strategy] || 'settings.authSrcStrategyBrowserFirst';
  var k = (src && src.knowledge) || {};
  var knowledgeHtml = k.pinned
    ? safeHtml`<span class="auth-src-meta-badge knowledge">${(t('settings.authSrcKnowledgePinned') || '已内化 v{v}').replace('{v}', String(k.version || '?'))}</span>`
    : (src.has_cookies
      ? safeHtml`<span class="auth-src-meta-badge">${t('settings.authSrcKnowledgeCredentials') || '仅凭据'}</span>`
      : '');
  return String(safeHtml`<div class="auth-src-badges">
    <span class="auth-src-meta-badge strategy">${t(strategyKey)}</span>${knowledgeHtml}<span id="authSrcLive_${raw(_domId((src && src.domain) || ''))}"></span>
  </div>`);
}

/** Lazy live-session probe for browser_first rows: the credential is the
 *  user's own browser login, so SAY whether it is there instead of asking
 *  for a cookie paste blindly. */
function _authSourceProbeLiveSessions(sources) {
  (sources || []).forEach(function (src) {
    if ((src.access_strategy || 'browser_first') !== 'browser_first') return;
    var dom = src.domain || '';
    Api.authSources.liveSession(dom).then(function (st) {
      var el = document.getElementById('authSrcLive_' + _domId(dom));
      if (!el || !st) return;
      var html;
      if (!st.extension) {
        html = safeHtml`<span class="auth-src-meta-badge live off">${t('settings.authSrcLiveOffline') || '扩展离线'}</span>`;
      } else if (st.live_session) {
        html = safeHtml`<span class="auth-src-meta-badge live on">${t('settings.authSrcLiveOn') || '浏览器会话已检测'}</span>`;
      } else {
        html = safeHtml`<span class="auth-src-meta-badge live">${t('settings.authSrcLiveNone') || '未检测到浏览器登录'}</span>`;
      }
      el.innerHTML = String(html);
    });
  });
}

/** Per-site account-risk note (e.g. XHS 风控). The SITE KNOWLEDGE lives
 *  server-side (``risk_note_key`` on the catalog row, projected by
 *  lib/auth_sources.py); this renders whatever i18n key the server names —
 *  the JS still hardcodes no per-site text of its own. Shown on the card
 *  (always visible) AND atop the connect panel, because the moment that
 *  matters is BEFORE the user logs in with their main account. */
function _authSourceRiskNote(src) {
  var key = (src && src.risk_note_key) || '';
  if (!key) return '';
  return String(safeHtml`<div class="auth-src-risk-note">${t(key)}</div>`);
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

function _authSourceOpenLogin(dom, url) {
  window.open(url || ('https://' + dom + '/'), '_blank', 'noopener');
}

function _authSourceToggle(dom, on) {
  Api.authSources.toggle(dom, on).then(_renderAuthSources);
}

/** Collect the per-cookie inputs of one card into a {name: value} map.
 *  A pasted `name=value` (or a whole `a=1; b=2` header) in a single field is
 *  unwrapped rather than stored verbatim: it is unambiguously not a raw value,
 *  and silently keeping it would reproduce the very bug the fields removed. */
function _authSourceCollectFields(id) {
  var out = {};
  var missing = [];
  var inputs = document.querySelectorAll('#authSrcPanel_' + id + ' .auth-src-field-input');
  for (var i = 0; i < inputs.length; i++) {
    var el = inputs[i];
    var name = el.getAttribute('data-cookie-name') || '';
    var val = (el.value || '').trim();
    if (val.indexOf('=') !== -1) {
      var pairs = val.split(';');
      for (var j = 0; j < pairs.length; j++) {
        var pair = pairs[j].trim();
        if (!pair) continue;
        var eq = pair.indexOf('=');
        if (eq <= 0) continue;
        out[pair.slice(0, eq).trim()] = pair.slice(eq + 1).trim();
      }
    } else if (val) {
      out[name] = val;
    }
    if (!val && el.getAttribute('data-importance') === 'required') missing.push(name);
  }
  return { values: out, missing: missing };
}

function _authSourceSavePaste(dom) {
  var id = _domId(dom);
  var collected = _authSourceCollectFields(id);
  var proxy = (document.getElementById('authSrcProxy_' + id) || {}).value || '';

  if (collected.missing.length) {
    _authSrcSetMsg(dom, (t('settings.authSrcFieldMissing') || '请填写必填 Cookie：') +
      collected.missing.join(', '), 'err');
    return;
  }
  _authSrcSetMsg(dom, t('common.saving') || '保存中…');
  Api.authSources.upsert({ domain: dom, cookie_fields: collected.values, proxy: proxy, enabled: true })
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
