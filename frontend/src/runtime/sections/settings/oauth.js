/* ===== migrated source: settings/oauth.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   settings/oauth — extracted from settings.js (split 2026-05-28)

   OAuth flows: status/login/logout/manual-callback for Claude/Codex providers.

   This file is concatenated by Vite's module graph — symbols share
   the same window scope as every other frontend/src/runtime/*.js file. No
   exports / imports needed.
   ═══════════════════════════════════════════════════════════════════ */

// ══════════════════════════════════════════════════════
//  OAuth Subscription Login — Browser-Centric Flow
//
//  Flow:
//    1. User clicks "登录" → fetch /api/oauth/login → get auth_url
//    2. Open auth_url in popup window (window.open)
//    3. User authenticates in popup
//    4. OAuth redirect → local relay when browser/server are colocated;
//       remote Codex deployments copy the complete localhost callback URL
//    5. Relay page postMessages the code, or the user pastes that URL
//    6. We receive the code via 'message' event listener
//    7. Send code to /api/oauth/callback → server exchanges for tokens
//
//  All browser-driven. Server only does: PKCE generation + token exchange.
// ══════════════════════════════════════════════════════

// ── Pending-flow registry + callback message gate ──
// ANY window can postMessage us and any same-origin page can broadcast, so a
// bare `type: 'oauth_callback'` check accepts injected codes/states from
// unrelated pages. The relay page is served by OUR loopback relay on the
// flow's callback port and echoes the flow's server-minted state, so a
// legitimate callback is provable on two axes: sender origin and per-flow
// state nonce. Both are recorded here when a login starts.
var _oauthPendingFlows = {};

var _OAUTH_RELAY_DEFAULT_PORTS = { claude: 54545, codex: 1455 };

function _oauthRelayOrigins(provider, port) {
  var p = Number(port) || _OAUTH_RELAY_DEFAULT_PORTS[provider] || 0;
  if (!p) return [];
  // The relay binds 127.0.0.1 but the registered redirect may say
  // `localhost` — the popup's final origin can be either spelling.
  return ['http://127.0.0.1:' + p, 'http://localhost:' + p];
}

function _oauthRecordPendingFlow(provider, port, state) {
  _oauthPendingFlows[provider] = {
    state: state || '',
    origins: _oauthRelayOrigins(provider, port),
  };
}

function _oauthClearPendingFlow(provider) {
  delete _oauthPendingFlows[provider];
}

// origin === null marks the BroadcastChannel path: it is same-origin by
// construction, so there is no sender origin to verify and the pending-flow
// state check is the whole gate.
function _oauthCallbackMessageAllowed(provider, state, origin) {
  var pending = provider && _oauthPendingFlows[provider];
  if (!pending) {
    console.warn('[OAuth] Ignoring callback for %s — no pending flow', provider);
    return false;
  }
  if (origin !== null && pending.origins.length &&
      pending.origins.indexOf(origin) < 0) {
    console.warn('[OAuth] Rejecting %s callback from unexpected origin: %s',
      provider, origin);
    return false;
  }
  if (pending.state && state !== pending.state) {
    console.warn('[OAuth] Rejecting %s callback — state mismatch', provider);
    return false;
  }
  return true;
}

// ── Global postMessage listener for OAuth callbacks ──
// The relay page (served by the server's lightweight HTTP relay) sends
// the authorization code back to us via postMessage or BroadcastChannel.
(function _initOAuthMessageListener() {
  // postMessage from popup's relay page
  window.addEventListener('message', function(event) {
    var data = event.data;
    if (!data || data.type !== 'oauth_callback') return;
    if (!_oauthCallbackMessageAllowed(data.provider, data.state, event.origin || '')) return;
    console.log('[OAuth] Received code via postMessage from relay page for:', data.provider);
    _handleOAuthCode(data.provider, data.code, data.state);
  });

  // BroadcastChannel fallback (works even if popup loses window.opener ref)
  try {
    var bc = new BroadcastChannel('oauth_callback');
    bc.onmessage = function(event) {
      var data = event.data;
      if (!data || data.type !== 'oauth_callback') return;
      if (!_oauthCallbackMessageAllowed(data.provider, data.state, null)) return;
      console.log('[OAuth] Received code via BroadcastChannel for:', data.provider);
      _handleOAuthCode(data.provider, data.code, data.state);
    };
  } catch(e) {
    // BroadcastChannel not supported — postMessage still works
  }
})();

// Browser-side exchange params per provider, captured from the login response.
var _oauthExchangeParams = {};

// ── Browser-side token exchange (B1 geo-block workaround) ──
// Exchanges the auth code against the provider's token endpoint FROM THE
// BROWSER (using the user's VPN/proxy), then hands the resulting token to
// the server to persist. Returns a Promise that resolves to the parsed
// token JSON on success, or rejects (so the caller falls back to the
// server-side exchange). Anthropic/OpenAI token endpoints are CORS-open for
// the public OAuth client, but if not, the fetch rejects and we fall back.
function _browserExchange(provider, code, state) {
  var ex = _oauthExchangeParams[provider];
  if (!ex || !ex.token_url || !ex.code_verifier) return Promise.reject(new Error('no-exchange-params'));

  var headers, bodyData;
  if (ex.style === 'form') {
    headers = { 'Content-Type': 'application/x-www-form-urlencoded' };
    var p = new URLSearchParams();
    p.set('grant_type', 'authorization_code');
    p.set('code', code);
    p.set('redirect_uri', ex.redirect_uri);
    p.set('client_id', ex.client_id);
    p.set('code_verifier', ex.code_verifier);
    bodyData = p.toString();
  } else {
    headers = { 'Content-Type': 'application/json' };
    bodyData = JSON.stringify({
      grant_type: 'authorization_code',
      code: code,
      state: state || ex.state || '',
      redirect_uri: ex.redirect_uri,
      client_id: ex.client_id,
      code_verifier: ex.code_verifier,
    });
  }

  // Direct cross-origin fetch to the provider token endpoint, via the
  // browser's own network. No credentials — this is a public OAuth client.
  return fetch(ex.token_url, { method: 'POST', headers: headers, body: bodyData, mode: 'cors' })
    .then(function(r) {
      return r.text().then(function(txt) {
        var json; try { json = JSON.parse(txt); } catch (e) { json = null; }
        if (!r.ok || !json || !json.access_token) {
          var msg = (json && (json.error_description || (json.error && json.error.message) || json.error)) || ('HTTP ' + r.status);
          var err = new Error('exchange-failed: ' + msg);
          err._upstreamStatus = r.status;
          throw err;
        }
        return json;
      });
    });
}

// Persist a browser-exchanged token via the server. Returns the parsed
// JSON result (with .error on failure).
function _storeBrowserToken(provider, tokenJson) {
  return Api.oauth.storeToken(provider, tokenJson)
    .then(function(r) { return r.json(); });
}

// ── Server-side token exchange (primary path, S2) ──
// POSTs the raw code to /api/oauth/callback so the SERVER does the exchange.
// The server auto-routes direct OR through an egress-capable desktop agent,
// so this path works even when the server's own egress is geo-blocked.
// Rejection Error carries `_statusCode` from the server's error body
// (403 geo-block / 0 network-or-egress-unavailable / 400-401 auth rejection)
// so _completeLogin can classify whether a browser retry makes sense.
function _serverExchange(provider, code, state, manual) {
  var body = { provider: provider, code: code };
  if (state) body.state = state;
  if (manual) body.manual = true;
  function _req(useGet) {
    if (useGet) {
      var qs = 'provider=' + encodeURIComponent(provider) + '&code=' + encodeURIComponent(code);
      if (state) qs += '&state=' + encodeURIComponent(state);
      if (manual) qs += '&manual=1';
      return Api.oauth.callbackGet(qs);
    }
    return Api.oauth.callbackPost(body);
  }
  return _req(false)
    .then(function(r) { return (r.status === 404 || r.status === 405) ? _req(true) : r; })
    .then(function(r) {
      if (!r.ok) return r.text().then(function(t) {
        var j; try { j = JSON.parse(t); } catch (e) { j = null; }
        var err = new Error((j && j.error) || t.slice(0, 200));
        if (j && typeof j.status_code !== 'undefined') err._statusCode = j.status_code;
        throw err;
      });
      return r.json();
    });
}

// ── Complete a login given an auth code: server → browser recovery ──
// Tofu owns transport selection and recovery; the user only authorizes.
// Order:
// 1. Server exchange — auto-routes direct OR through an egress-capable
//    desktop agent (S2), so it now works even when the server's own egress
//    is geo-blocked, and has no CORS exposure. A genuine auth rejection
//    (400/401: code expired/used) is surfaced as-is — the code is burned,
//    retrying it anywhere else just fails again.
// 2. Browser exchange (B1) — only when the server failed with a geo-block
//    (403) / network error / egress-unavailable (status_code 0), i.e. the
//    code is provably still unconsumed.
function _completeLogin(provider, code, state, opts) {
  // manual: the user pasted the code/URL by hand — the only path allowed to
  // arrive without the flow's state (raw code paste has no state channel).
  var manual = !!(opts && opts.manual);
  var capProvider = provider === 'codex' ? 'Codex' : 'Claude';
  _updateOAuthCard(provider, { status: 'exchanging' });

  function _onSuccess(data) {
    _oauthClearPendingFlow(provider);
    // Exchange/store responses historically returned `{ok, email}` without
    // the status projection fields consumed by _updateOAuthCard. Passing that
    // object through made a successful login repaint as "not logged in".
    // Normalize the success fact at this boundary while preserving richer
    // provider/model metadata when the backend supplies it.
    var success = Object.assign({}, data || {}, {
      status: 'success', authenticated: true,
    });
    _updateOAuthCard(provider, success);
    _autoConfigureOAuthProvider(provider, success);
    var manualDiv = document.getElementById('oauth' + capProvider + 'Manual');
    if (manualDiv) manualDiv.style.display = 'none';
    var manualInput = document.getElementById('oauth' + capProvider + 'ManualUrl');
    if (manualInput) manualInput.value = '';
  }
  function _onError(msg) {
    _oauthClearPendingFlow(provider);
    _updateOAuthCard(provider, { status: 'error' });
    showAlert(t('settings.oauthAuthorizationFailed', { msg: msg }));
  }

  function _recoveryFailed(reason) {
    _oauthClearPendingFlow(provider);
    console.error('[OAuth] Automatic recovery exhausted for %s: %s', provider, reason || 'unknown');
    _updateOAuthCard(provider, { status: 'error' });
    showAlert(t('settings.oauthAutomaticRecoveryFailed'));
  }

  function _tryBrowser(reason) {
    console.warn('[OAuth] Server exchange unavailable (%s) — trying browser exchange', reason);
    _browserExchange(provider, code, state)
      .then(function(tokenJson) {
        console.log('[OAuth] Browser-side exchange succeeded for', provider);
        return _storeBrowserToken(provider, tokenJson).then(function(data) {
          if (!data || data.error) {
            _recoveryFailed((data && data.error) || 'store failed');
            return;
          }
          _onSuccess(data);
        });
      })
      .catch(function(e2) { _recoveryFailed((e2 && e2.message) || 'browser exchange failed'); });
  }

  _serverExchange(provider, code, state, manual)
    .then(function(data) {
      if (!data || data.error) { _tryBrowser((data && data.error) || 'empty result'); return; }
      _onSuccess(data);
    })
    .catch(function(e) {
      var sc = e && e._statusCode;
      if (sc === 400 || sc === 401) {
        // Genuine auth rejection — the code is consumed/expired; don't burn
        // it a second time from the browser.
        _onError(e.message);
        return;
      }
      // 403 geo-block / 0 network-or-egress-unavailable / unknown — the code
      // was rejected at the edge BEFORE grant processing, so it is still
      // redeemable from the browser's own network.
      _tryBrowser(e.message);
    });
}

// ── Handle received OAuth code (from postMessage / relay) ──
function _handleOAuthCode(provider, code, state) {
  if (!provider || !code) return;
  _completeLogin(provider, code, state);
}

function _loadOAuthStatus(fromRepoll) {
  if (!fromRepoll) {
    _oauthStatusRepollAttempts = 0;
  }
  Api.oauth.status()
    .then(function(data) {
      if (!data) return;
      _updateOAuthCard('claude', data.claude);
      _updateOAuthCard('codex', data.codex);
      // Egress and earned-reset reads both warm asynchronously. One bounded
      // status re-poll chain observes either without duplicating timers.
      var probing = [data.claude, data.codex].some(function(s) {
        return s && s.egress && s.egress.state === 'unknown';
      });
      var resetRefreshing = !!(data.codex && data.codex.reset_offer &&
        data.codex.reset_offer.refreshing);
      if (resetRefreshing) _scheduleOAuthStatusRepoll();
      if (!probing && !resetRefreshing) _oauthStatusRepollAttempts = 0;
    })
    .catch(function(e) {
      console.warn('[OAuth] Failed to load status:', e);
    });
}

function _oauthQuotaPct(value) {
  var n = Number(value);
  if (!Number.isFinite(n)) return '';
  return (Math.round(n * 10) / 10).toFixed(1).replace(/\.0$/, '');
}

function _oauthQuotaWindowLabel(minutes) {
  var n = Number(minutes || 0);
  if (n === 300) return t('quota.window5h');
  if (n === 10080) return t('quota.window7d');
  if (n > 0 && n % 1440 === 0) return t('quota.windowDays', { n: n / 1440 });
  if (n > 0 && n % 60 === 0) return t('quota.windowHours', { n: n / 60 });
  if (n > 0) return t('quota.windowMinutes', { n: n });
  return t('quota.windowUnknown');
}

function _oauthQuotaResetLabel(timestamp) {
  var seconds = Number(timestamp || 0);
  if (!Number.isFinite(seconds) || seconds <= 0) return '';
  var when = new Date(seconds * 1000);
  if (!Number.isFinite(when.getTime())) return '';
  try {
    var locale = (typeof _i18nLang !== 'undefined' && _i18nLang === 'zh')
      ? 'zh-CN' : 'en-US';
    return new Intl.DateTimeFormat(locale, {
      month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
    }).format(when);
  } catch (_err) {
    return '';
  }
}

function _renderOAuthQuota(provider, quota, authenticated) {
  var capProvider = provider === 'codex' ? 'Codex' : 'Claude';
  var el = document.getElementById('oauth' + capProvider + 'Quota');
  if (!el) return;
  if (provider !== 'codex' || !authenticated) {
    el.style.display = 'none';
    el.innerHTML = '';
    return;
  }
  el.style.display = '';
  if (!quota || (!quota.primary && !quota.secondary)) {
    el.innerHTML = '<div class="oauth-quota-title">' +
      escapeHtml(t('settings.oauthQuotaTitle')) + '</div>' +
      '<div class="oauth-quota-pending">' +
      escapeHtml(t('settings.oauthQuotaPending')) + '</div>';
    return;
  }
  var rows = [];
  ['primary', 'secondary'].forEach(function(name) {
    var win = quota[name];
    if (!win || !Number.isFinite(Number(win.remaining_percent))) return;
    var remaining = Math.max(0, Math.min(100, Number(win.remaining_percent)));
    var label = _oauthQuotaWindowLabel(win.window_minutes);
    var resetTime = _oauthQuotaResetLabel(win.resets_at);
    var resetCopy = resetTime
      ? '<span class="oauth-quota-reset">' + escapeHtml(t(
        'settings.oauthQuotaResetsAt', { time: resetTime })) + '</span>'
      : '';
    rows.push('<div class="oauth-quota-row">' +
      '<div class="oauth-quota-row-head"><span class="oauth-quota-window">' +
      escapeHtml(label) + resetCopy + '</span>' +
      '<span>' + escapeHtml(t('settings.oauthQuotaRemaining', {
        remaining: _oauthQuotaPct(remaining) })) + '</span></div>' +
      '<div class="oauth-quota-track"><span style="width:' + remaining + '%"></span></div>' +
      '</div>');
  });
  el.innerHTML = '<div class="oauth-quota-title">' +
    escapeHtml(t('settings.oauthQuotaTitle')) + '</div>' + rows.join('') +
    '<div class="oauth-quota-source">' +
    escapeHtml(t('settings.oauthQuotaSource')) + '</div>';
}

function _oauthResetExpiryLabel(timestamp) {
  var seconds = Number(timestamp || 0);
  if (!Number.isFinite(seconds) || seconds <= 0) return '';
  try {
    var locale = (typeof _i18nLang !== 'undefined' && _i18nLang === 'zh')
      ? 'zh-CN' : 'en';
    return new Date(seconds * 1000).toLocaleString(locale, {
      dateStyle: 'medium', timeStyle: 'short',
    });
  } catch (_err) {
    return '';
  }
}

function _renderOAuthResetOffer(provider, offer, authenticated) {
  var capProvider = provider === 'codex' ? 'Codex' : 'Claude';
  var el = document.getElementById('oauth' + capProvider + 'ResetOffer');
  if (!el) return;
  el.style.display = 'none';
  el.className = 'oauth-reset-offer';
  el.innerHTML = '';
  if (provider !== 'codex' || !authenticated || !offer) return;

  if (offer.state === 'unknown' && offer.refreshing) {
    el.className += ' is-checking';
    el.innerHTML = '<div class="oauth-reset-offer-title">' +
      escapeHtml(t('settings.oauthResetAvailableTitle')) + '</div>' +
      '<div class="oauth-reset-offer-copy">' +
      escapeHtml(t('settings.oauthResetChecking')) + '</div>';
    el.style.display = '';
    return;
  }
  var count = Number(offer.available_count || 0);
  if (offer.state !== 'available' || !Number.isInteger(count) || count <= 0) return;

  var detail = count === 1
    ? t('settings.oauthResetAvailableOne')
    : t('settings.oauthResetAvailableMany', { count: count });
  var meta = [];
  var expiry = _oauthResetExpiryLabel(offer.expires_at);
  if (expiry) meta.push(t('settings.oauthResetExpires', { time: expiry }));
  if (offer.stale) meta.push(t('settings.oauthResetStale'));
  el.className += offer.stale ? ' is-stale' : ' is-available';
  el.innerHTML = '<div class="oauth-reset-offer-title">' +
    escapeHtml(t('settings.oauthResetAvailableTitle')) + '</div>' +
    '<div class="oauth-reset-offer-copy">' + escapeHtml(detail) + '</div>' +
    (meta.length ? '<div class="oauth-reset-offer-meta">' +
      escapeHtml(meta.join(' · ')) + '</div>' : '') +
    '<div class="oauth-reset-offer-hint">' +
      escapeHtml(t('settings.oauthResetRedeemHint')) + '</div>';
  el.style.display = '';
}

// ── Asynchronous OAuth-status re-poll ──
// Egress reachability and Codex reset-credit detection both warm off-request.
// The first status read therefore may be `unknown+refreshing`. Re-poll only
// while Settings is open, with one timer and a hard attempt cap; ordinary
// steady-state reads pay no polling cost.
var _oauthStatusRepollTimer = null;
var _oauthStatusRepollAttempts = 0;
var _OAUTH_STATUS_REPOLL_MS = 2000;
var _OAUTH_STATUS_REPOLL_MAX = 8;  // usage + optional details are each <= 5s

function _scheduleOAuthStatusRepoll() {
  if (_oauthStatusRepollTimer) return;
  if (_oauthStatusRepollAttempts >= _OAUTH_STATUS_REPOLL_MAX) return;
  _oauthStatusRepollAttempts++;
  _oauthStatusRepollTimer = setTimeout(function() {
    _oauthStatusRepollTimer = null;
    var modal = document.getElementById('settingsModal');
    if (!modal || !modal.classList.contains('open')) {
      _oauthStatusRepollAttempts = 0;
      return;
    }
    _loadOAuthStatus(true);
  }, _OAUTH_STATUS_REPOLL_MS);
}

// ── Desktop-egress status line + pin selector (S4) ──
// Renders the server-computed egress state per card. NEVER probes inline —
// the server's status payload carries a cached verdict only.
function _renderEgressLine(provider, egress) {
  var capProvider = provider === 'codex' ? 'Codex' : 'Claude';
  var el = document.getElementById('oauth' + capProvider + 'Egress');
  if (!el) return;
  el.style.display = 'none';
  el.className = 'oauth-egress-line';
  el.textContent = '';
  el.innerHTML = '';
  if (!egress || !egress.state) return;

  var target = provider === 'codex' ? 'OpenAI' : 'Anthropic';
  var key = '';
  var vars = { provider: target };
  var visual = 'is-warning';
  if (egress.state === 'unknown') {
    key = 'settings.egressChecking';
    visual = 'is-checking';
    _scheduleOAuthStatusRepoll();
  } else if (egress.state === 'direct') {
    var routeId = egress.preferred_server_route || '';
    var routeMode = egress.preferred_server_route_mode ||
      ((routeId === 'env' || routeId.indexOf('pool:') === 0) ? 'proxy' : 'direct');
    if (routeMode === 'proxy' || routeMode === 'env') {
      key = 'settings.egressViaProxy';
      vars.route = egress.preferred_server_route_label || routeId ||
        t('settings.egressConfiguredProxy');
    } else {
      key = 'settings.egressDirect';
    }
    visual = 'is-ok';
  } else if (egress.state === 'agent') {
    key = 'settings.egressViaAgent';
    var agent = (egress.agents || [])[0] || {};
    vars.agent = agent.name || agent.agent_id || t('settings.egressDesktopAgent');
    visual = 'is-ok';
  } else if (egress.state === 'agent_no_capability') {
    key = 'settings.egressAgentNoCap';
  } else {
    key = 'settings.egressUnavailable';
    visual = 'is-error';
  }
  el.className = 'oauth-egress-line ' + visual;
  el.textContent = t(key, vars);
  el.style.display = '';
}

function _updateOAuthCard(provider, status) {
  if (!status) return;
  var capProvider = provider === 'codex' ? 'Codex' : 'Claude';
  _renderEgressLine(provider, status.egress);
  var badge = document.getElementById('oauth' + capProvider + 'Status');
  var info = document.getElementById('oauth' + capProvider + 'Info');
  var email = document.getElementById('oauth' + capProvider + 'Email');
  var loginBtn = document.getElementById('oauth' + capProvider + 'LoginBtn');
  var logoutBtn = document.getElementById('oauth' + capProvider + 'LogoutBtn');

  if (!badge) return;
  badge.title = '';
  _renderOAuthQuota(provider, status.quota, Boolean(status.authenticated));
  _renderOAuthResetOffer(
    provider, status.reset_offer, Boolean(status.authenticated));

  // Device-authorization flow (codex): show the code panel while a device
  // flow waits, hide it in every terminal state, and keep the entry button
  // in sync with the login button's visibility rules.
  if (provider === 'codex') {
    var deviceBtn = document.getElementById('oauthCodexDeviceBtn');
    var devWaiting = !status.authenticated &&
      (status.status === 'started' || status.status === 'waiting_callback');
    if (devWaiting && status.device) {
      _showDevicePanel(status.device.user_code, status.device.verification_url);
    } else if (status.authenticated || !devWaiting) {
      _hideDevicePanel();
      _stopDeviceStatusPoll();
    }
    if (deviceBtn) {
      deviceBtn.style.display = status.authenticated ? 'none' : '';
      if (!status.authenticated && !devWaiting) {
        deviceBtn.disabled = false;
        deviceBtn.textContent = t('settings.oauthDeviceLogin');
      }
    }
  }

  if (status.authenticated) {
    var ready = status.provider_ready !== false;
    badge.textContent = ready
      ? t('settings.oauthModelsReady', { n: status.model_count || 0 })
      : t('settings.oauthAutomaticRecovery');
    badge.className = 'oauth-status-badge ' + (ready ? 'authenticated' : 'pending');
    if (info) { info.style.display = ''; }
    if (email) {
      email.textContent = (status.email || t('settings.oauthConnectedAccount')) + (ready ? '' :
        ' · ' + t('settings.oauthProviderRepairing'));
    }
    if (loginBtn) { loginBtn.style.display = 'none'; }
    if (logoutBtn) { logoutBtn.style.display = ''; }
  } else if (status.status === 'started' || status.status === 'waiting_callback' || status.status === 'exchanging') {
    badge.textContent = status.status === 'exchanging' ? t('settings.oauthGettingToken') : t('settings.oauthWaitingAuth');
    badge.className = 'oauth-status-badge pending';
    if (info) { info.style.display = 'none'; }
    // Show a cancel/retry button so users aren't stuck forever
    if (loginBtn) {
      loginBtn.disabled = false;
      loginBtn.textContent = t('settings.oauthCancelRetry');
      loginBtn.onclick = function() { _oauthCancelAndRetry(provider); };
    }
    if (logoutBtn) { logoutBtn.style.display = 'none'; }
    // A page reload mid-flow lands HERE — not in _oauthLogin's callback —
    // re-rendered from the status projection alone. Restore the manual box,
    // its truthful instructions, and the escape hatch from that projection,
    // or the reloaded page silently offers nothing but a retry that re-runs
    // the same (possibly broken) callback decision — the exact loop the
    // hatch exists to break. Synthetic waiting states (exchange in flight,
    // curl helper) carry no redirect_mode and are left untouched.
    if (status.redirect_mode && status.redirect_mode !== 'device' &&
        status.status !== 'exchanging') {
      var flowManual = document.getElementById('oauth' + capProvider + 'Manual');
      if (flowManual) {
        flowManual.style.display = '';
        var flowUrl = document.getElementById('oauth' + capProvider + 'AuthUrl');
        if (flowUrl && status.auth_url) flowUrl.value = status.auth_url;
      }
      _oauthApplyRedirectMode(provider, status.redirect_mode);
    }
    // Restore browser-recovery parameters after a reload so Tofu can keep
    // handling the exchange without asking the user for infrastructure work.
    if (status.exchange) {
      _oauthExchangeParams[provider] = status.exchange;
      // Re-arm the callback gate too: the login response is gone after a
      // reload, but the status projection still carries the flow's state
      // nonce (the port falls back to the provider's registered default).
      if (!_oauthPendingFlows[provider]) {
        _oauthRecordPendingFlow(provider, 0, status.exchange.state || '');
      }
    }
  } else if (status.status === 'error') {
    badge.textContent = t('settings.oauthError');
    badge.className = 'oauth-status-badge error';
    badge.title = errorEnvelopeMessage(status.error);
    if (info) { info.style.display = 'none'; }
    if (loginBtn) { loginBtn.disabled = false; loginBtn.textContent = provider === 'codex' ? t('settings.oauthLoginChatGPT') : t('settings.oauthLoginClaude'); loginBtn.onclick = function() { _oauthLogin(provider); }; }
    if (logoutBtn) { logoutBtn.style.display = 'none'; }
  } else {
    badge.textContent = t('settings.oauthNotLoggedIn');
    badge.className = 'oauth-status-badge';
    if (info) { info.style.display = 'none'; }
    if (loginBtn) { loginBtn.disabled = false; loginBtn.textContent = provider === 'codex' ? t('settings.oauthLoginChatGPT') : t('settings.oauthLoginClaude'); loginBtn.style.display = ''; loginBtn.onclick = function() { _oauthLogin(provider); }; }
    if (logoutBtn) { logoutBtn.style.display = 'none'; }
  }
}

function _oauthCancelAndRetry(provider) {
  var capProvider = provider === 'codex' ? 'Codex' : 'Claude';
  _oauthClearPendingFlow(provider);
  // Call logout to reset the server-side flow state
  Api.oauth.logoutPost(provider).catch(function() {});
  // Reset UI immediately
  _updateOAuthCard(provider, { status: 'not_started', authenticated: false });
  // Restore normal onclick
  var loginBtn = document.getElementById('oauth' + capProvider + 'LoginBtn');
  if (loginBtn) {
    loginBtn.onclick = function() { _oauthLogin(provider); };
  }
  // Hide manual paste box
  var manualDiv = document.getElementById('oauth' + capProvider + 'Manual');
  if (manualDiv) manualDiv.style.display = 'none';
  // Hide device panel + stop its status poll (codex)
  if (provider === 'codex') {
    _stopDeviceStatusPoll();
    _hideDevicePanel();
    var deviceBtn = document.getElementById('oauthCodexDeviceBtn');
    if (deviceBtn) {
      deviceBtn.disabled = false;
      deviceBtn.textContent = t('settings.oauthDeviceLogin');
    }
  }
}

// ── Which callback is this flow actually walking, and how to get out ──
// Whether Anthropic accepts the loopback redirect for our client is an
// EXTERNAL fact we cannot verify locally. If it ever refuses, a desktop user
// lands on an authorization error with NOTHING to paste (the console page is
// what renders the code, and a loopback flow never reaches it) — and the
// cancel/retry button re-runs the SAME decision, so the user would loop
// through the identical broken flow forever. The way out therefore has to be
// a first-class control in the product, not the TOFU_OAUTH_LOOPBACK env var:
// a packaged .exe user has nowhere to set one.
function _oauthApplyRedirectMode(provider, mode) {
  if (provider !== 'claude') return;   // codex has exactly one registered redirect
  var loopback = mode === 'loopback';
  var pasteHint = document.getElementById('oauthClaudeCodeHint');
  var pasteRow = document.getElementById('oauthClaudePasteRow');
  var lbNote = document.getElementById('oauthClaudeLoopbackNote');
  var fbRow = document.getElementById('oauthClaudeConsoleFallbackRow');
  // The paste instructions are only TRUE on the console flow.
  if (pasteHint) pasteHint.style.display = loopback ? 'none' : '';
  if (pasteRow) pasteRow.style.display = loopback ? 'none' : '';
  // The note + escape hatch are only MEANINGFUL on the loopback flow.
  if (lbNote) lbNote.style.display = loopback ? '' : 'none';
  if (fbRow) fbRow.style.display = loopback ? '' : 'none';
  var btn = document.getElementById('oauthClaudeConsoleFallbackBtn');
  if (btn) btn.onclick = function() { _oauthUseConsoleFallback('claude'); };
}

// Restart the flow pinned to the console callback (manual code paste).
// A fresh flow is required rather than reusing the pending one: the
// redirect_uri is baked into the authorize URL AND must be echoed at
// exchange time, so the old flow's PKCE/state pair cannot be reused with a
// different redirect.
function _oauthUseConsoleFallback(provider) {
  var capP = provider === 'codex' ? 'Codex' : 'Claude';
  // Drop the pending flow so its relay releases the port and its state is
  // not mistaken for the new one.
  Api.oauth.logoutPost(provider).catch(function() {});
  var input = document.getElementById('oauth' + capP + 'ManualUrl');
  if (input) input.value = '';
  _oauthLogin(provider, true);
}

function _oauthLogin(provider, preferConsole) {
  var capProvider = provider === 'codex' ? 'Codex' : 'Claude';
  var loginBtn = document.getElementById('oauth' + capProvider + 'LoginBtn');
  if (loginBtn) { loginBtn.disabled = true; loginBtn.textContent = t('settings.oauthPreparing'); }

  // Step 1: Ask server to generate PKCE + auth URL + start relay server
  // Try POST first; if proxy returns 404/405, fall back to GET with query params
  // (VSCode tunnel proxies may not forward POST to unknown paths)
  function _doLoginRequest(useGet) {
    if (useGet) {
      console.warn('[OAuth] POST failed, retrying as GET for /api/oauth/login');
      return Api.oauth.loginGet(provider, preferConsole);
    }
    return Api.oauth.loginPost(provider, preferConsole);
  }
  _doLoginRequest(false)
    .then(function(r) {
      if (r.status === 404 || r.status === 405) return _doLoginRequest(true);
      return r;
    })
    .then(function(r) {
      if (!r.ok) {
        return r.text().then(function(t) { throw new Error('HTTP ' + r.status + ': ' + t.slice(0, 200)); });
      }
      return r.json();
    })
    .then(function(data) {
      if (data.error) {
        showAlert(t('settings.oauthLoginFailed', {
          error: errorEnvelopeMessage(data.error) || String(data.error),
        }));
        if (loginBtn) { loginBtn.disabled = false; loginBtn.textContent = provider === 'codex' ? t('settings.oauthLoginChatGPT') : t('settings.oauthLoginClaude'); }
        return;
      }

      // Stash browser-side exchange params (B1): when the server's egress is
      // geo-blocked from the provider token endpoint, the browser (with the
      // user's VPN) does the exchange itself. code_verifier is OUR PKCE
      // secret, so it's fine to keep it client-side for the duration.
      _oauthExchangeParams[provider] = data.exchange || null;
      // Arm the callback gate for THIS flow before the popup can navigate
      // back: only our relay origin echoing this flow's state gets through.
      _oauthRecordPendingFlow(
        provider, data.callback_port,
        (data.exchange && data.exchange.state) || '');

      // Step 2: Open the auth URL in a popup window
      // For Claude: redirects to console.anthropic.com which shows code#state
      // For Codex: local desktop flows auto-relay; remote flows stop on the
      // fixed localhost callback and the complete address-bar URL is pasted.
      var popup = null;
      if (data.auth_url) {
        var w = 600, h = 700;
        var left = (screen.width - w) / 2, top = (screen.height - h) / 2;
        popup = window.open(data.auth_url, 'oauth_' + provider,
          'width=' + w + ',height=' + h + ',left=' + left + ',top=' + top +
          ',menubar=no,toolbar=no,status=no,scrollbars=yes');

        if (!popup || popup.closed) {
          // Popup blocked — fall back to new tab
          popup = null;
          window.open(data.auth_url, '_blank');
        }
      }

      // Update UI to waiting state
      _updateOAuthCard(provider, { status: 'waiting_callback' });

      // Keep recovery out of the happy path. The user should see it only
      // when the browser blocked the popup or the provider explicitly uses a
      // console flow that requires pasting an authorization result.
      var manualDiv = document.getElementById('oauth' + capProvider + 'Manual');
      if (manualDiv) {
        var manualNeeded = !popup || data.redirect_mode === 'console' ||
          data.redirect_mode === 'manual';
        manualDiv.style.display = manualNeeded ? '' : 'none';
        var authUrlInput = document.getElementById('oauth' + capProvider + 'AuthUrl');
        if (authUrlInput && data.auth_url) authUrlInput.value = data.auth_url;
      }
      // Describe the flow the user is ACTUALLY about to walk, and expose the
      // way out of it. During a loopback flow the paste instructions are
      // FALSE (the provider redirects to localhost and never renders a
      // code), so showing them unchanged would hand the user a task that
      // cannot be completed.
      _oauthApplyRedirectMode(provider, data.redirect_mode);

      // ── Detect popup closed → auto-reset ONLY if manual box not used ──
      if (popup) {
        var popupCheckInterval = setInterval(function() {
          /* Self-terminate once the login resolves (success / error / cancel):
           *   the old code only stopped on popup.closed, leaking a 1s interval
           *   for every Connect click that never closed its popup (). */
          var badgeNow = document.getElementById('oauth' + capProvider + 'Status');
          if (badgeNow && !badgeNow.classList.contains('pending')) {
            clearInterval(popupCheckInterval);
            return;
          }
          if (!popup || popup.closed) {
            clearInterval(popupCheckInterval);
            // Don't reset if manual paste box is visible (user may be pasting code)
            var manualInput = document.getElementById('oauth' + capProvider + 'ManualUrl');
            if (manualInput && manualInput.value.trim()) return;  // user is typing
            // Only reset if still in waiting state (not already succeeded)
            var badge = document.getElementById('oauth' + capProvider + 'Status');
            if (badge && badge.classList.contains('pending')) {
              // The automatic route did not finish. Reveal recovery only now,
              // after the user has closed the authorization window.
              if (manualDiv) manualDiv.style.display = '';
              _oauthApplyRedirectMode(provider, data.redirect_mode);
              // Don't reset — just update button to allow retry
              var loginBtn2 = document.getElementById('oauth' + capProvider + 'LoginBtn');
              if (loginBtn2) {
                loginBtn2.disabled = false;
                loginBtn2.textContent = t('settings.oauthReopenPopup');
                loginBtn2.onclick = function() {
                  // Re-open popup with same auth URL, don't create new flow
                  var w2 = 600, h2 = 700;
                  var left2 = (screen.width - w2) / 2, top2 = (screen.height - h2) / 2;
                  window.open(data.auth_url, 'oauth_' + provider,
                    'width=' + w2 + ',height=' + h2 + ',left=' + left2 + ',top=' + top2 +
                    ',menubar=no,toolbar=no,status=no,scrollbars=yes');
                };
              }
            }
          }
        }, 1000);
      }
    })
    .catch(function(e) {
      console.error('[OAuth] Login error:', e);
      showAlert(t('settings.oauthLoginReqFailed', { error: e.message }));
      if (loginBtn) { loginBtn.disabled = false; loginBtn.textContent = provider === 'codex' ? t('settings.oauthLoginChatGPT') : t('settings.oauthLoginClaude'); }
    });
}

// ── Device-authorization login (Codex) ──
// The loopback callback (localhost:1455) only resolves when the browser and
// the Tofu server share a machine. The device flow never touches a localhost
// redirect: the server mints a user code, the user enters it at the
// verification URL in ANY browser (phone included), and the server's poll
// thread completes the exchange — we just watch the status projection.
var _oauthDevicePollTimer = null;

function _oauthDeviceLogin(provider) {
  if (provider !== 'codex') return;
  var deviceBtn = document.getElementById('oauthCodexDeviceBtn');
  if (deviceBtn) { deviceBtn.disabled = true; deviceBtn.textContent = t('settings.oauthPreparing'); }

  function _doDeviceRequest(useGet) {
    if (useGet) {
      console.warn('[OAuth] POST failed, retrying as GET for /api/v1/oauth/device-login');
      return Api.oauth.deviceLoginGet(provider);
    }
    return Api.oauth.deviceLoginPost(provider);
  }
  _doDeviceRequest(false)
    .then(function(r) {
      if (r.status === 404 || r.status === 405) return _doDeviceRequest(true);
      return r;
    })
    .then(function(r) {
      if (!r.ok) {
        return r.text().then(function(tx) {
          var body; try { body = JSON.parse(tx); } catch (parseError) { body = null; }
          var detail = body &&
            (errorEnvelopeMessage(body.error) || body.detail);
          var err = new Error('HTTP ' + r.status + ': ' +
            String(detail || tx).slice(0, 200));
          err._httpStatus = r.status;
          if (body && typeof body.status_code !== 'undefined') {
            err._statusCode = body.status_code;
          }
          throw err;
        });
      }
      return r.json();
    })
    .then(function(data) {
      if (deviceBtn) { deviceBtn.disabled = false; deviceBtn.textContent = t('settings.oauthDeviceLogin'); }
      if (!data || data.error) {
        showAlert(t('settings.oauthLoginFailed', {
          error: (data && errorEnvelopeMessage(data.error)) || 'unknown',
        }));
        return;
      }
      _showDevicePanel(data.user_code, data.verification_url);
      _updateOAuthCard(provider, {
        status: 'waiting_callback',
        device: { user_code: data.user_code, verification_url: data.verification_url },
      });
      _startDeviceStatusPoll(provider);
    })
    .catch(function(e) {
      console.error('[OAuth] Device login error:', e);
      if (deviceBtn) { deviceBtn.disabled = false; deviceBtn.textContent = t('settings.oauthDeviceLogin'); }
      if (e && (e._httpStatus === 503 || e._statusCode === 0)) {
        // Deviceauth must be minted by the server. When every server/agent
        // route is down, fall back to the browser-network PKCE flow instead
        // of ending on a raw HTTP 400/503. The remote callback-copy box makes
        // the fixed localhost redirect usable without any local listener.
        Promise.resolve(showAlert(t('settings.oauthDeviceFallback'))).then(
          function() { _oauthLogin(provider); },
          function() { _oauthLogin(provider); });
        return;
      }
      showAlert(t('settings.oauthLoginReqFailed', { error: e.message }));
    });
}

function _showDevicePanel(userCode, verificationUrl) {
  var panel = document.getElementById('oauthCodexDevice');
  if (!panel) return;
  panel.style.display = '';
  var codeEl = document.getElementById('oauthCodexDeviceCode');
  if (codeEl) codeEl.textContent = userCode || '';
  var link = document.getElementById('oauthCodexDeviceLink');
  if (link && verificationUrl) link.href = verificationUrl;
  // The loopback manual box is FALSE during a device flow — the provider
  // never redirects anywhere, it renders a code-entry page.
  var manual = document.getElementById('oauthCodexManual');
  if (manual) manual.style.display = 'none';
}

function _hideDevicePanel() {
  var panel = document.getElementById('oauthCodexDevice');
  if (panel) panel.style.display = 'none';
}

function _oauthCopyDeviceCode(button) {
  var codeEl = document.getElementById('oauthCodexDeviceCode');
  if (!codeEl || !codeEl.textContent) return;
  function _copied() {
    button.textContent = t('settings.oauthCopied');
    setTimeout(function() { button.textContent = t('settings.oauthCopyCode'); }, 1500);
  }
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(codeEl.textContent).then(_copied, function() {});
  } else {
    var tmp = document.createElement('textarea');
    tmp.value = codeEl.textContent;
    document.body.appendChild(tmp);
    tmp.select();
    try { document.execCommand('copy'); _copied(); } catch (e) {}
    document.body.removeChild(tmp);
  }
}

function _startDeviceStatusPoll(provider) {
  _stopDeviceStatusPoll();
  _oauthDevicePollTimer = setInterval(function() {
    Api.oauth.status()
      .then(function(data) {
        if (!data) return;
        var s = data[provider];
        _updateOAuthCard(provider, s);
        var terminal = !s || s.authenticated ||
          ['error', 'timeout', 'not_started'].indexOf(s.status) >= 0 ||
          !s.device;
        if (terminal) {
          _stopDeviceStatusPoll();
          if (s && s.authenticated) _autoConfigureOAuthProvider(provider, s);
        }
      })
      .catch(function() {});
  }, 3000);
}

function _stopDeviceStatusPoll() {
  if (_oauthDevicePollTimer) {
    clearInterval(_oauthDevicePollTimer);
    _oauthDevicePollTimer = null;
  }
}

async function _oauthLogout(provider) {
  if (!await showConfirm(t('settings.oauthLogoutConfirm', { provider: (provider === 'codex' ? 'ChatGPT' : 'Claude') }))) return;

  // Try POST first; if proxy returns 405, fall back to GET with query params
  function _doLogoutRequest(useGet) {
    if (useGet) {
      console.warn('[OAuth] POST failed, retrying as GET for /api/oauth/logout');
      return Api.oauth.logoutGet(provider);
    }
    return Api.oauth.logoutPost(provider);
  }
  _doLogoutRequest(false)
    .then(function(r) {
      if (r.status === 404 || r.status === 405) return _doLogoutRequest(true);
      return r;
    })
    .then(function(r) { return r.json(); })
    .then(function() {
      _oauthClearPendingFlow(provider);
      _updateOAuthCard(provider, { status: 'not_started', authenticated: false });
      if (typeof _refreshSubscriptionModelCatalog === 'function') {
        return _refreshSubscriptionModelCatalog();
      }
      return null;
    })
    .catch(function(e) {
      showAlert(t('settings.oauthLogoutFailed', { error: e.message }));
    });
}

function _oauthCopyAuthLink(button, provider) {
  const capProvider = provider === 'codex' ? 'Codex' : 'Claude';
  const input = document.getElementById('oauth' + capProvider + 'AuthUrl');
  if (!input) return;
  input.select();
  document.execCommand('copy');
  button.textContent = t('settings.oauthCopied');
  setTimeout(function() {
    button.textContent = t('settings.oauthCopyLink');
  }, 1500);
}

function _oauthManualSubmit(provider) {
  var capP = provider === 'codex' ? 'Codex' : 'Claude';
  var input = document.getElementById('oauth' + capP + 'ManualUrl');
  if (!input || !input.value.trim()) {
    showAlert(t('settings.oauthPasteCodePrompt'));
    return;
  }
  var val = input.value.trim();

  // Support multiple formats:
  // 1. Full callback URL: http://localhost:PORT/callback?code=XXX&state=YYY
  // 2. code#state format (shown by Anthropic console after auth)
  // 3. Raw authorization code
  var code = '', state = '';
  if (val.indexOf('http') === 0) {
    try {
      var u = new URL(val);
      code = u.searchParams.get('code') || '';
      state = u.searchParams.get('state') || '';
    } catch (e) { code = ''; }
    if (!code) { showAlert(t('settings.oauthNoCodeInUrl')); return; }
  } else if (val.indexOf('#') > 0) {
    // code#state format from Anthropic console
    var parts = val.split('#');
    code = parts[0];
    state = parts[1] || '';
  } else {
    code = val;
  }

  // Browser-first exchange (bypasses the server's geo-block), server fallback.
  _completeLogin(provider, code, state, { manual: true });
}

function _autoConfigureOAuthProvider(provider, status) {
  var name = provider === 'codex' ? 'ChatGPT Plus' : 'Claude Pro';
  var el = document.getElementById('settingsStatusHint');
  if (el) {
    el.textContent = t('settings.oauthAutoConfigured', { name: name });
    el.style.color = '#28a745';
  }
  // The backend auto-provisions a managed provider on login; refresh the
  // providers list so the new models appear without a manual reload.
  if (typeof _refreshSubscriptionModelCatalog === 'function') {
    _refreshSubscriptionModelCatalog().then(function(cfg) {
      if (!cfg) return;
      _loadOAuthStatus();
    }).catch(function(e) {
      console.warn('[OAuth] subscription catalogue refresh failed:', e);
      showAlert(t('settings.oauthCatalogRepairFailed'));
    });
  }
}
