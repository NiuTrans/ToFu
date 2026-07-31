/* ═══════════════════════════════════════════════════════════════════
   settings/private_hosts.js — Internal-host SSRF allowlist

   Renders the "内网主机放行" section in Settings → Search.

   By default the fetch pipeline refuses any URL whose host resolves to a
   private / loopback / reserved address (SSRF guard). An entry here is the
   explicit "I do mean to fetch this internal host".

   TWO SEPARATE GATES — do not merge them:
     • THIS list grants REACHABILITY and no credentials.
     • auth_sources.js above grants CREDENTIALS and no SSRF exemption.
   Connecting an account must never silently widen the network boundary,
   and allowlisting a host must never imply a login.

   Matching is by NAME (exact or parent-suffix: example-corp.com covers
   your-llm-gateway.example.com), never by resolved IP — an internal load balancer
   rotates its address between lookups, so an IP entry rots silently. The
   server rejects a bare IP with 400; we surface that message verbatim
   rather than pre-validating a second copy of the rule here.

   Concatenated by lib/js_bundler.py — shared window scope, no imports.
   ═══════════════════════════════════════════════════════════════════ */

function _renderPrivateHosts() {
  var box = document.getElementById('privateHostsList');
  if (!box) return;
  box.innerHTML = String(safeHtml`<div class="priv-host-loading">${t('common.loading') || '加载中…'}</div>`);
  Api.privateHosts.list().then(function (data) {
    var hosts = (data && data.hosts) || [];
    box.innerHTML = _privateHostsHtml(hosts);
  }).catch(function (e) {
    console.warn('[PrivHosts] list failed', e);
    box.innerHTML = String(safeHtml`<div class="priv-host-empty">${t('settings.privateHostsLoadFail') || '加载失败'}</div>`);
  });
}

function _privateHostsHtml(hosts) {
  var rows = hosts.length
    ? hosts.map(_privateHostRowHtml).join('')
    : String(safeHtml`<div class="priv-host-empty">${t('settings.privateHostsEmpty') || '尚未放行任何内网主机。'}</div>`);
  return rows + _privateHostAddHtml();
}

function _privateHostRowHtml(row) {
  var host = row.host || '';
  var id = _privHostDomId(host);
  var enabled = !!row.enabled;
  var stateClass = enabled ? 'on' : 'off';
  var stateText = enabled
    ? (t('settings.privHostAllowed') || '已放行')
    : (t('settings.privHostPaused') || '已停用');
  var toggleLabel = enabled
    ? (t('settings.privHostDisable') || '停用')
    : (t('settings.privHostEnable') || '启用');

  return String(safeHtml`
    <div class="priv-host-row" id="privHostRow_${raw(id)}">
      <div class="priv-host-main">
        <span class="priv-host-dot ${raw(stateClass)}"></span>
        <span class="priv-host-name">${host}</span>
        <span class="priv-host-state ${raw(stateClass)}">${stateText}</span>
      </div>
      <div class="priv-host-actions">
        <button class="priv-host-btn" onclick="_privateHostToggle('${raw(host)}', ${raw(enabled ? 'false' : 'true')})">${toggleLabel}</button>
        <button class="priv-host-btn danger" onclick="_privateHostRemove('${raw(host)}')">${t('settings.privHostRemove') || '移除'}</button>
      </div>
    </div>`);
}

function _privateHostAddHtml() {
  return String(safeHtml`
    <div class="priv-host-add">
      <input type="text" id="privHostInput" class="priv-host-input"
             placeholder="${t('settings.privHostPlaceholder') || 'your-llm-gateway.example.com'}"
             onkeydown="if(event.key==='Enter'){event.preventDefault();_privateHostAdd();}">
      <button class="priv-host-btn primary" onclick="_privateHostAdd()">${t('settings.privHostAdd') || '添加'}</button>
      <div id="privHostMsg" class="priv-host-msg"></div>
    </div>`);
}

function _privHostDomId(v) {
  return String(v).replace(/[^a-zA-Z0-9]/g, '_');
}

function _privHostSetMsg(text, cls) {
  var el = document.getElementById('privHostMsg');
  if (el) {
    el.textContent = text || '';
    el.className = 'priv-host-msg' + (cls ? ' ' + cls : '');
  }
}

function _privateHostAdd() {
  var input = document.getElementById('privHostInput');
  if (!input) return;
  var host = (input.value || '').trim();
  if (!host) {
    _privHostSetMsg(t('settings.privHostNeedHost') || '请输入主机名。', 'err');
    return;
  }
  _privHostSetMsg(t('common.saving') || '保存中…', '');
  Api.privateHosts.upsert({ host: host }).then(function (res) {
    // The server normalizes and validates (bare IPs are refused). Its
    // message is the single source of truth — echo it rather than keeping a
    // second copy of the rule in the frontend.
    if (res && res.error) {
      _privHostSetMsg(res.error.message || String(res.error), 'err');
      return;
    }
    input.value = '';
    _privHostSetMsg('', '');
    _renderPrivateHosts();
  }).catch(function (e) {
    console.warn('[PrivHosts] upsert failed', e);
    var msg = (e && e.message) || (t('settings.privHostSaveFail') || '保存失败');
    _privHostSetMsg(msg, 'err');
  });
}

function _privateHostToggle(host, enable) {
  Api.privateHosts.toggle(host, enable).then(function () {
    _renderPrivateHosts();
  }).catch(function (e) {
    console.warn('[PrivHosts] toggle failed', e);
    _privHostSetMsg(t('settings.privHostSaveFail') || '保存失败', 'err');
  });
}

function _privateHostRemove(host) {
  Api.privateHosts.remove(host).then(function () {
    _renderPrivateHosts();
  }).catch(function (e) {
    console.warn('[PrivHosts] remove failed', e);
    _privHostSetMsg(t('settings.privHostSaveFail') || '保存失败', 'err');
  });
}

window._renderPrivateHosts = _renderPrivateHosts;
window._privateHostAdd = _privateHostAdd;
window._privateHostToggle = _privateHostToggle;
window._privateHostRemove = _privateHostRemove;
