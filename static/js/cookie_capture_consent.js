/* ═══════════════════════════════════════════════════════════
   cookie_capture_consent.js — Login-wall cookie-capture consent banner

   When a fetch hits an SSO login wall, the backend (lib/browser/
   cookie_capture.py) pushes a 'cookie_capture' frame asking whether it may
   read THAT DOMAIN's cookies from the user's browser. This module renders
   the allow/deny banner, posts the decision back, and toasts when a session
   was captured (a retry will then succeed via auth-source replay).

   Channels:
     pushSubscribe('cookie_capture', 'consent')   — request/captured frames
     Api.authSources.cookieConsentPending()       — reload restore
     Api.authSources.cookieConsentResolve(id, ok) — user decision
   ═══════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  var _banners = {};   // consentId → DOM node

  function _t(key) {
    return (typeof t === 'function') ? t(key) : key;
  }

  function _removeBanner(id) {
    var node = _banners[id];
    if (node && node.parentNode) node.parentNode.removeChild(node);
    delete _banners[id];
  }

  function _container() {
    var c = document.getElementById('ccConsentStack');
    if (!c) {
      c = document.createElement('div');
      c.id = 'ccConsentStack';
      c.style.cssText = 'position:fixed;right:16px;bottom:16px;z-index:10000;' +
        'display:flex;flex-direction:column;gap:8px;max-width:380px;';
      document.body.appendChild(c);
    }
    return c;
  }

  function _showBanner(item) {
    if (!item || !item.id || _banners[item.id]) return;
    var card = document.createElement('div');
    card.className = 'cc-consent-card';
    card.setAttribute('data-consent-id', item.id);
    card.style.cssText = 'background:var(--bg-elevated,#1e1e24);color:var(--text-primary,#eee);' +
      'border:1px solid var(--border,#333);border-radius:10px;padding:12px 14px;' +
      'box-shadow:0 6px 24px rgba(0,0,0,.35);font-size:13px;line-height:1.5;';

    var title = document.createElement('div');
    title.style.cssText = 'font-weight:600;margin-bottom:4px;';
    title.textContent = _t('cc.banner.title').replace('{domain}', item.domain);
    card.appendChild(title);

    var body = document.createElement('div');
    body.style.cssText = 'opacity:.85;margin-bottom:10px;word-break:break-all;';
    body.textContent = _t('cc.banner.body').replace('{domain}', item.domain);
    card.appendChild(body);

    var row = document.createElement('div');
    row.style.cssText = 'display:flex;gap:8px;justify-content:flex-end;';

    var deny = document.createElement('button');
    deny.textContent = _t('cc.banner.deny');
    deny.className = 'cc-consent-deny';
    deny.style.cssText = 'padding:5px 12px;border-radius:6px;border:1px solid var(--border,#444);' +
      'background:transparent;color:inherit;cursor:pointer;';
    deny.onclick = function () { _resolve(item.id, false); };

    var allow = document.createElement('button');
    allow.textContent = _t('cc.banner.allow');
    allow.className = 'cc-consent-allow';
    allow.style.cssText = 'padding:5px 12px;border-radius:6px;border:1px solid var(--accent,#4f8cff);' +
      'background:var(--accent,#4f8cff);color:#fff;cursor:pointer;';
    allow.onclick = function () { _resolve(item.id, true); };

    row.appendChild(deny);
    row.appendChild(allow);
    card.appendChild(row);
    _container().appendChild(card);
    _banners[item.id] = card;
  }

  function _resolve(id, approved) {
    _removeBanner(id);
    if (typeof Api !== 'undefined' && Api && Api.authSources &&
        typeof Api.authSources.cookieConsentResolve === 'function') {
      Api.authSources.cookieConsentResolve(id, approved);
    }
  }

  function _handleFrame(frame) {
    if (!frame || !frame.type) return;
    if (frame.type === 'request') {
      _showBanner({ id: frame.id, domain: frame.domain, url: frame.url });
    } else if (frame.type === 'captured') {
      if (typeof showToast === 'function') {
        showToast(_t('cc.captured').replace('{domain}', frame.domain), 'success');
      }
    }
  }

  function refreshPending() {
    if (typeof Api === 'undefined' || !Api || !Api.authSources ||
        typeof Api.authSources.cookieConsentPending !== 'function') return;
    var p = Api.authSources.cookieConsentPending();
    if (!p || typeof p.then !== 'function') return;
    p.then(function (res) {
      var rows = res && res.data && res.data.pending;
      if (Array.isArray(rows)) rows.forEach(_showBanner);
    }).catch(function () { /* pending list is best-effort */ });
  }

  function initCookieCaptureConsent() {
    if (typeof pushSubscribe === 'function') {
      pushSubscribe('cookie_capture', 'consent', _handleFrame);
    }
    refreshPending();
  }

  window.CookieCaptureConsent = {
    init: initCookieCaptureConsent,
    refreshPending: refreshPending,
    // Exposed for the jsdom harness (drive a frame without a live socket).
    _handleFrame: _handleFrame,
    _showBanner: _showBanner,
    _removeBanner: _removeBanner,
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initCookieCaptureConsent);
  } else {
    initCookieCaptureConsent();
  }
})();
