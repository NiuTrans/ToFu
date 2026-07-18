/* ═══════════════════════════════════════════════════════════════════
   settings/oauth — extracted from settings.js (split 2026-05-28)

   OAuth flows: status/login/logout/manual-callback for Claude/Codex providers.

   This file is concatenated by lib/js_bundler.py — symbols share
   the same window scope as every other static/js/*.js file. No
   exports / imports needed.
   ═══════════════════════════════════════════════════════════════════ */

// ══════════════════════════════════════════════════════
//  OAuth Subscription Login — Browser-Centric Flow
//
//  Flow:
//    1. User clicks "登录" → fetch /api/oauth/login → get auth_url
//    2. Open auth_url in popup window (window.open)
//    3. User authenticates in popup
//    4. OAuth redirect → localhost:PORT → relay server serves HTML page
//    5. Relay page uses postMessage() to send code back to this window
//    6. We receive the code via 'message' event listener
//    7. Send code to /api/oauth/callback → server exchanges for tokens
//
//  All browser-driven. Server only does: PKCE generation + token exchange.
// ══════════════════════════════════════════════════════

// ── Global postMessage listener for OAuth callbacks ──
// The relay page (served by the server's lightweight HTTP relay) sends
// the authorization code back to us via postMessage or BroadcastChannel.
(function _initOAuthMessageListener() {
  // postMessage from popup's relay page
  window.addEventListener('message', function(event) {
    var data = event.data;
    if (!data || data.type !== 'oauth_callback') return;
    console.log('[OAuth] Received code via postMessage from relay page for:', data.provider);
    _handleOAuthCode(data.provider, data.code);
  });

  // BroadcastChannel fallback (works even if popup loses window.opener ref)
  try {
    var bc = new BroadcastChannel('oauth_callback');
    bc.onmessage = function(event) {
      var data = event.data;
      if (!data || data.type !== 'oauth_callback') return;
      console.log('[OAuth] Received code via BroadcastChannel for:', data.provider);
      _handleOAuthCode(data.provider, data.code);
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

// ── Server-side token exchange (fallback path) ──
// POSTs the raw code to /api/oauth/callback so the SERVER does the exchange.
// Used when browser-side exchange isn't possible or fails for a non-auth
// reason (e.g. the server's egress isn't geo-blocked). Returns parsed JSON.
function _serverExchange(provider, code, state) {
  var body = { provider: provider, code: code };
  if (state) body.state = state;
  function _req(useGet) {
    if (useGet) {
      var qs = 'provider=' + encodeURIComponent(provider) + '&code=' + encodeURIComponent(code);
      if (state) qs += '&state=' + encodeURIComponent(state);
      return Api.oauth.callbackGet(qs);
    }
    return Api.oauth.callbackPost(body);
  }
  return _req(false)
    .then(function(r) { return (r.status === 404 || r.status === 405) ? _req(true) : r; })
    .then(function(r) {
      if (!r.ok) return r.text().then(function(t) {
        var j; try { j = JSON.parse(t); } catch (e) { j = null; }
        throw new Error((j && j.error) || t.slice(0, 200));
      });
      return r.json();
    });
}

// ── Complete a login given an auth code: browser-first, server fallback ──
// 1. Try the browser-side exchange (uses the user's VPN — bypasses the
//    server's geo-blocked egress). On success, store via the server.
// 2. If browser exchange can't run (no params) or fails with a NETWORK/CORS
//    error (not a real auth rejection), fall back to the server exchange.
//    A genuine auth rejection (4xx with an error body) is reported as-is.
function _completeLogin(provider, code, state) {
  var capProvider = provider === 'codex' ? 'Codex' : 'Claude';
  _updateOAuthCard(provider, { status: 'exchanging' });

  function _onSuccess(data) {
    _updateOAuthCard(provider, { status: 'success', authenticated: true, email: data.email || '' });
    _autoConfigureOAuthProvider(provider, { email: data.email });
    var manualDiv = document.getElementById('oauth' + capProvider + 'Manual');
    if (manualDiv) manualDiv.style.display = 'none';
    var manualInput = document.getElementById('oauth' + capProvider + 'ManualUrl');
    if (manualInput) manualInput.value = '';
  }
  function _onError(msg) {
    _updateOAuthCard(provider, { status: 'error' });
    showAlert(t('settings.oauthTokenExchangeFailed', { msg: msg }));
  }

  // Step 1: browser-side exchange → store via server.
  _browserExchange(provider, code, state)
    .then(function(tokenJson) {
      console.log('[OAuth] Browser-side exchange succeeded for', provider);
      return _storeBrowserToken(provider, tokenJson).then(function(data) {
        if (!data || data.error) { _onError((data && data.error) || 'store failed'); return; }
        _onSuccess(data);
      });
    })
    .catch(function(e) {
      // Browser exchange failed. If it was a genuine auth rejection from the
      // provider (4xx with a body), surface it — retrying server-side won't
      // help and would just hit the geo-block. Otherwise (CORS/network/no
      // params), fall back to the server-side exchange.
      var st = e && e._upstreamStatus;
      if (st === 400 || st === 401) {
        _onError(e.message.replace(/^exchange-failed: /, ''));
        return;
      }
      console.warn('[OAuth] Browser exchange unavailable (%s) — falling back to server', e && e.message);
      _serverExchange(provider, code, state)
        .then(function(data) {
          if (!data || data.error) {
            // Both browser (CORS) and server (geo-block) failed → curl helper.
            _showCurlHelper(provider, code, state, (data && data.error) || '');
            return;
          }
          _onSuccess(data);
        })
        .catch(function(e2) { _showCurlHelper(provider, code, state, e2.message); });
    });
}

// ── curl-assisted manual exchange (B2: both browser AND server are blocked) ──
// Anthropic/OpenAI token endpoints don't send CORS headers, so a browser
// fetch is preflight-blocked; and the server's egress is geo-blocked. The
// one network that CAN reach them is the user's own terminal (with VPN), so
// we hand them the exact curl and accept the token JSON they paste back.
function _buildCurlCommand(provider, code, state) {
  var ex = _oauthExchangeParams[provider];
  if (!ex || !ex.token_url || !ex.code_verifier) return '';
  if (ex.style === 'form') {
    var p = new URLSearchParams();
    p.set('grant_type', 'authorization_code');
    p.set('code', code);
    p.set('redirect_uri', ex.redirect_uri);
    p.set('client_id', ex.client_id);
    p.set('code_verifier', ex.code_verifier);
    return "curl '" + ex.token_url + "' \\\n  -H 'Content-Type: application/x-www-form-urlencoded' \\\n  --data-raw '" + p.toString() + "'";
  }
  var body = JSON.stringify({
    grant_type: 'authorization_code', code: code, state: state || ex.state || '',
    redirect_uri: ex.redirect_uri, client_id: ex.client_id, code_verifier: ex.code_verifier,
  });
  return "curl '" + ex.token_url + "' \\\n  -H 'Content-Type: application/json' \\\n  --data-raw '" + body + "'";
}

function _showCurlHelper(provider, code, state, reason) {
  var capP = provider === 'codex' ? 'Codex' : 'Claude';
  var curl = _buildCurlCommand(provider, code, state);
  if (!curl) {
    _updateOAuthCard(provider, { status: 'error' });
    showAlert(t('settings.oauthTokenExchangeNoCmd', { reason: (reason || '') }));
    return;
  }
  // Keep the card in a 'pending' state — the user has a clear next action.
  _updateOAuthCard(provider, { status: 'waiting_callback' });
  var manualDiv = document.getElementById('oauth' + capP + 'Manual');
  if (manualDiv) manualDiv.style.display = '';

  var helper = document.getElementById('oauth' + capP + 'CurlHelper');
  if (!helper) {
    helper = document.createElement('div');
    helper.id = 'oauth' + capP + 'CurlHelper';
    helper.className = 'oauth-curl-helper';
    helper.style.marginTop = '10px';
    if (manualDiv) manualDiv.appendChild(helper);
  }
  helper.innerHTML =
    '<p class="oauth-manual-hint" style="color:#e0a030">' +
    t('settings.oauthCurlHelp') + '</p>' +
    '<textarea readonly class="oauth-manual-input" id="oauth' + capP + 'Curl" ' +
    'style="width:100%;height:104px;font-family:monospace;font-size:11px;white-space:pre"></textarea>' +
    '<button class="btn-small" id="oauth' + capP + 'CurlCopy" style="margin-top:6px">' + escapeHtml(t('settings.oauthCopyCmd')) + '</button>';
  var ta = document.getElementById('oauth' + capP + 'Curl');
  if (ta) ta.value = curl;
  var copyBtn = document.getElementById('oauth' + capP + 'CurlCopy');
  if (copyBtn) {
    copyBtn.onclick = function() {
      var b = this;
      _safeClipboardWrite(ta.value).then(function() {
        b.textContent = t('settings.oauthCopied');
        setTimeout(function() { b.textContent = t('settings.oauthCopyCmd'); }, 1500);
      }).catch(function(e) { debugLog('[OAuth] copy failed: ' + e.message, 'warn'); });
    };
  }
  // Repurpose the paste input for the JSON result.
  var input = document.getElementById('oauth' + capP + 'ManualUrl');
  if (input) { input.value = ''; input.placeholder = t('settings.oauthPasteJsonPlaceholder'); }
}

// ── Handle received OAuth code (from postMessage / relay) ──
function _handleOAuthCode(provider, code, state) {
  if (!provider || !code) return;
  _completeLogin(provider, code, state);
}

function _loadOAuthStatus() {
  Api.oauth.status()
    .then(function(data) {
      if (!data) return;
      _updateOAuthCard('claude', data.claude);
      _updateOAuthCard('codex', data.codex);
    })
    .catch(function(e) {
      console.warn('[OAuth] Failed to load status:', e);
    });
}

function _updateOAuthCard(provider, status) {
  if (!status) return;
  var capProvider = provider === 'codex' ? 'Codex' : 'Claude';
  var badge = document.getElementById('oauth' + capProvider + 'Status');
  var info = document.getElementById('oauth' + capProvider + 'Info');
  var email = document.getElementById('oauth' + capProvider + 'Email');
  var loginBtn = document.getElementById('oauth' + capProvider + 'LoginBtn');
  var logoutBtn = document.getElementById('oauth' + capProvider + 'LogoutBtn');

  if (!badge) return;

  if (status.authenticated) {
    badge.textContent = t('settings.oauthLoggedIn');
    badge.className = 'oauth-status-badge authenticated';
    if (info) { info.style.display = ''; }
    if (email) { email.textContent = status.email || '(unknown)'; }
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
  } else if (status.status === 'error') {
    badge.textContent = t('settings.oauthError');
    badge.className = 'oauth-status-badge error';
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
}

function _oauthLogin(provider) {
  var capProvider = provider === 'codex' ? 'Codex' : 'Claude';
  var loginBtn = document.getElementById('oauth' + capProvider + 'LoginBtn');
  if (loginBtn) { loginBtn.disabled = true; loginBtn.textContent = t('settings.oauthPreparing'); }

  // Step 1: Ask server to generate PKCE + auth URL + start relay server
  // Try POST first; if proxy returns 404/405, fall back to GET with query params
  // (VSCode tunnel proxies may not forward POST to unknown paths)
  function _doLoginRequest(useGet) {
    if (useGet) {
      console.warn('[OAuth] POST failed, retrying as GET for /api/oauth/login');
      return Api.oauth.loginGet(provider);
    }
    return Api.oauth.loginPost(provider);
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
        showAlert(t('settings.oauthLoginFailed', { error: data.error }));
        if (loginBtn) { loginBtn.disabled = false; loginBtn.textContent = provider === 'codex' ? t('settings.oauthLoginChatGPT') : t('settings.oauthLoginClaude'); }
        return;
      }

      // Stash browser-side exchange params (B1): when the server's egress is
      // geo-blocked from the provider token endpoint, the browser (with the
      // user's VPN) does the exchange itself. code_verifier is OUR PKCE
      // secret, so it's fine to keep it client-side for the duration.
      _oauthExchangeParams[provider] = data.exchange || null;

      // Step 2: Open the auth URL in a popup window
      // For Claude: redirects to console.anthropic.com which shows code#state
      // For Codex: redirects to localhost relay which auto-sends via postMessage
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

      // Show manual paste box immediately with auth URL for copy
      // (Chinese users need to copy the URL to a proxied browser)
      var manualDiv = document.getElementById('oauth' + capProvider + 'Manual');
      if (manualDiv) {
        manualDiv.style.display = '';
        var authUrlInput = document.getElementById('oauth' + capProvider + 'AuthUrl');
        if (authUrlInput && data.auth_url) authUrlInput.value = data.auth_url;
      }

      // ── Detect popup closed → auto-reset ONLY if manual box not used ──
      if (popup) {
        var popupCheckInterval = setInterval(function() {
          if (!popup || popup.closed) {
            clearInterval(popupCheckInterval);
            // Don't reset if manual paste box is visible (user may be pasting code)
            var manualInput = document.getElementById('oauth' + capProvider + 'ManualUrl');
            if (manualInput && manualInput.value.trim()) return;  // user is typing
            // Only reset if still in waiting state (not already succeeded)
            var badge = document.getElementById('oauth' + capProvider + 'Status');
            if (badge && badge.classList.contains('pending')) {
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
      _updateOAuthCard(provider, { status: 'not_started', authenticated: false });
    })
    .catch(function(e) {
      showAlert(t('settings.oauthLogoutFailed', { error: e.message }));
    });
}

function _oauthManualSubmit(provider) {
  var capP = provider === 'codex' ? 'Codex' : 'Claude';
  var input = document.getElementById('oauth' + capP + 'ManualUrl');
  if (!input || !input.value.trim()) {
    showAlert(t('settings.oauthPasteCodePrompt'));
    return;
  }
  var val = input.value.trim();

  // Format 0: token JSON pasted from the curl helper (B2 dead-end path).
  // If it parses to an object with access_token, store it directly — no
  // exchange needed (the user's terminal already did it).
  if (val.charAt(0) === '{') {
    var tok = null;
    try { tok = JSON.parse(val); } catch (e) { tok = null; }
    if (tok && tok.access_token) {
      _updateOAuthCard(provider, { status: 'exchanging' });
      _storeBrowserToken(provider, tok)
        .then(function(data) {
          if (!data || data.error) {
            _updateOAuthCard(provider, { status: 'error' });
            showAlert(t('settings.oauthSaveFailed', { error: ((data && data.error) || 'unknown') }));
            return;
          }
          _updateOAuthCard(provider, { status: 'success', authenticated: true, email: data.email || '' });
          var md = document.getElementById('oauth' + capP + 'Manual');
          if (md) md.style.display = 'none';
          input.value = '';
          _autoConfigureOAuthProvider(provider, { email: data.email });
        })
        .catch(function(e) {
          _updateOAuthCard(provider, { status: 'error' });
          showAlert(t('settings.oauthSaveFailed', { error: e.message }));
        });
      return;
    }
    showAlert(t('settings.oauthNoAccessToken'));
    return;
  }

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
  _completeLogin(provider, code, state);
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
  if (typeof _loadServerConfig === 'function') {
    try { _loadServerConfig(); } catch (e) { /* best-effort UI refresh */ }
  }
}


