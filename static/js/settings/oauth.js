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

// ── Server-side token exchange (primary path, S2) ──
// POSTs the raw code to /api/oauth/callback so the SERVER does the exchange.
// The server auto-routes direct OR through an egress-capable desktop agent,
// so this path works even when the server's own egress is geo-blocked.
// Rejection Error carries `_statusCode` from the server's error body
// (403 geo-block / 0 network-or-egress-unavailable / 400-401 auth rejection)
// so _completeLogin can classify whether a browser retry makes sense.
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
        var err = new Error((j && j.error) || t.slice(0, 200));
        if (j && typeof j.status_code !== 'undefined') err._statusCode = j.status_code;
        throw err;
      });
      return r.json();
    });
}

// ── Complete a login given an auth code: server → browser → curl ──
// Order (owner 2026-07-31, desktop-egress era):
// 1. Server exchange — auto-routes direct OR through an egress-capable
//    desktop agent (S2), so it now works even when the server's own egress
//    is geo-blocked, and has no CORS exposure. A genuine auth rejection
//    (400/401: code expired/used) is surfaced as-is — the code is burned,
//    retrying it anywhere else just fails again.
// 2. Browser exchange (B1) — only when the server failed with a geo-block
//    (403) / network error / egress-unavailable (status_code 0), i.e. the
//    code is provably still unconsumed.
// 3. curl helper (B2) — the user's own terminal as the last network.
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

  function _tryBrowser(reason) {
    console.warn('[OAuth] Server exchange unavailable (%s) — trying browser exchange', reason);
    _browserExchange(provider, code, state)
      .then(function(tokenJson) {
        console.log('[OAuth] Browser-side exchange succeeded for', provider);
        return _storeBrowserToken(provider, tokenJson).then(function(data) {
          if (!data || data.error) {
            _showCurlHelper(provider, code, state, (data && data.error) || 'store failed');
            return;
          }
          _onSuccess(data);
        });
      })
      .catch(function(e2) { _showCurlHelper(provider, code, state, (e2 && e2.message) || ''); });
  }

  _serverExchange(provider, code, state)
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

// ── curl-assisted manual exchange (B2: both browser AND server are blocked) ──
// Anthropic/OpenAI token endpoints don't send CORS headers, so a browser
// fetch is preflight-blocked; and the server's egress is geo-blocked. The
// one network that CAN reach them is the user's own terminal (with VPN), so
// we hand them the exact curl and accept the token JSON they paste back.
// The command is rendered for a CHOSEN shell, and all renderings are offered
// rather than one being sniffed. The reason is not that sniffing is fragile
// (though it is — navigator.platform is deprecated and userAgentData is
// Chromium-only): it is that the browser's platform is not evidence of the
// TARGET shell. This path exists because neither the browser nor the server
// can reach the token endpoint, so the terminal the user pastes into is
// routinely on a different machine than this page (self-hosted server +
// remote browser, VS Code tunnel, WSL). A sniff would hand those users a
// command that cannot run, with no way to switch. The sniff below therefore
// only decides which variant is shown FIRST.
var _CURL_SHELLS = ['bash', 'powershell', 'cmd'];
var _CURL_SHELL_LABELS = { bash: 'bash / zsh', powershell: 'PowerShell', cmd: 'CMD' };

function _curlDefaultShell() {
  var ua = '';
  try { ua = (navigator && navigator.userAgent) || ''; } catch (e) { ua = ''; }
  return /Windows/i.test(ua) ? 'powershell' : 'bash';
}

// Render one curl invocation with the quoting + continuation rules of `shell`.
function _renderCurl(shell, url, contentType, body) {
  if (shell === 'cmd') {
    // CMD groups arguments with double quotes ONLY, and has no line
    // continuation that survives a quoted payload — hence one long line.
    // Inner characters follow the MSVCRT rules curl.exe itself parses with,
    // and those rules are precise about backslashes: a backslash is LITERAL
    // unless it sits in a run immediately before a quote. So doubling every
    // backslash is WRONG (a payload `\` would arrive as `\\`) — only a run
    // that precedes a quote, or the end of the payload (the wrapper quote
    // follows it), may double. Quotes themselves escape as `\"`.
    var cmdBody = body.replace(/\\+(?="|$)/g, function (m) { return m + m; })
                      .replace(/"/g, '\\"');
    return 'curl "' + url + '" -H "Content-Type: ' + contentType + '" --data-raw "' + cmdBody + '"';
  }
  if (shell === 'powershell') {
    // `curl` is an ALIAS for Invoke-WebRequest in PowerShell, which does not
    // accept -H / --data-raw — the real binary must be named explicitly.
    // Backtick is the continuation character; single-quoted strings are
    // literal (no interpolation), with `'` escaped by doubling.
    var psBody = body.replace(/'/g, "''");
    return "curl.exe '" + url + "' `\n  -H 'Content-Type: " + contentType + "' `\n  --data-raw '" + psBody + "'";
  }
  // POSIX shells: single-quoted literal, closed/re-opened around any quote.
  var shBody = body.replace(/'/g, "'\\''");
  return "curl '" + url + "' \\\n  -H 'Content-Type: " + contentType + "' \\\n  --data-raw '" + shBody + "'";
}

function _buildCurlCommand(provider, code, state, shell) {
  var ex = _oauthExchangeParams[provider];
  if (!ex || !ex.token_url || !ex.code_verifier) return '';
  var contentType, body;
  if (ex.style === 'form') {
    var p = new URLSearchParams();
    p.set('grant_type', 'authorization_code');
    p.set('code', code);
    p.set('redirect_uri', ex.redirect_uri);
    p.set('client_id', ex.client_id);
    p.set('code_verifier', ex.code_verifier);
    contentType = 'application/x-www-form-urlencoded';
    body = p.toString();
  } else {
    contentType = 'application/json';
    body = JSON.stringify({
      grant_type: 'authorization_code', code: code, state: state || ex.state || '',
      redirect_uri: ex.redirect_uri, client_id: ex.client_id, code_verifier: ex.code_verifier,
    });
  }
  var use = _CURL_SHELLS.indexOf(shell) >= 0 ? shell : _curlDefaultShell();
  return _renderCurl(use, ex.token_url, contentType, body);
}

function _showCurlHelper(provider, code, state, reason) {
  var capP = provider === 'codex' ? 'Codex' : 'Claude';
  var shell = _curlDefaultShell();
  var curl = _buildCurlCommand(provider, code, state, shell);
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
  var tabs = _CURL_SHELLS.map(function(s) {
    return '<button class="btn-small oauth-curl-shell' + (s === shell ? ' active' : '') +
      '" data-shell="' + s + '" style="margin-right:4px">' +
      escapeHtml(_CURL_SHELL_LABELS[s]) + '</button>';
  }).join('');
  helper.innerHTML =
    '<p class="oauth-manual-hint" style="color:#e0a030">' +
    t('settings.oauthCurlHelp') + '</p>' +
    '<div style="margin-bottom:6px">' + tabs + '</div>' +
    '<textarea readonly class="oauth-manual-input" id="oauth' + capP + 'Curl" ' +
    'style="width:100%;height:104px;font-family:monospace;font-size:11px;white-space:pre"></textarea>' +
    '<button class="btn-small" id="oauth' + capP + 'CurlCopy" style="margin-top:6px">' + escapeHtml(t('settings.oauthCopyCmd')) + '</button>';
  var ta = document.getElementById('oauth' + capP + 'Curl');
  if (ta) ta.value = curl;
  // Re-render into the SAME textarea on switch, so the copy button below
  // always carries whatever variant is currently displayed.
  helper.querySelectorAll('.oauth-curl-shell').forEach(function(btn) {
    btn.onclick = function() {
      var s = this.getAttribute('data-shell');
      if (ta) ta.value = _buildCurlCommand(provider, code, state, s);
      helper.querySelectorAll('.oauth-curl-shell').forEach(function(b) {
        b.classList.toggle('active', b === btn);
      });
    };
  });
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

function _loadOAuthStatus(fromRepoll) {
  if (!fromRepoll) {
    _egressRepollAttempts = 0;  // fresh load → fresh budget
    _adapterStartPolling();     // settings just opened — start the adapter card chain
  }
  Api.oauth.status()
    .then(function(data) {
      if (!data) return;
      _updateOAuthCard('claude', data.claude);
      _updateOAuthCard('codex', data.codex);
      // Resolved verdicts free the re-poll budget for the next cold cache.
      var probing = [data.claude, data.codex].some(function(s) {
        return s && s.egress && s.egress.state === 'unknown';
      });
      if (!probing) _egressRepollAttempts = 0;
    })
    .catch(function(e) {
      console.warn('[OAuth] Failed to load status:', e);
    });
}

// ── 'unknown' re-poll ──
// The status endpoint NEVER probes inline: a cold probe cache answers
// 'unknown' and warms the verdict on a background thread (~1s observed;
// the cache TTL is 300s and probes are fired only BY status polls, so
// virtually every settings-open starts cold). Without a re-poll the
// 出口检测中 label is TERMINAL — the verdict lands in the server cache a
// second later but the open panel never re-fetches it. Re-poll on a short
// cadence, bounded, and only while the settings modal is open; each poll
// is a cache read server-side, so the cadence costs nothing.
var _egressRepollTimer = null;
var _egressRepollAttempts = 0;
var _EGRESS_REPOLL_MS = 2000;
var _EGRESS_REPOLL_MAX = 5;   // probe worst case: 5s connect timeout + slack

function _scheduleEgressRepoll() {
  if (_egressRepollTimer) return;  // one chain at a time
  if (_egressRepollAttempts >= _EGRESS_REPOLL_MAX) return;
  _egressRepollAttempts++;
  _egressRepollTimer = setTimeout(function() {
    _egressRepollTimer = null;
    var modal = document.getElementById('settingsModal');
    if (!modal || !modal.classList.contains('open')) {
      _egressRepollAttempts = 0;  // modal closed mid-chain — drop it
      return;
    }
    _loadOAuthStatus(true);
  }, _EGRESS_REPOLL_MS);
}

// ── Desktop-egress status line + pin selector (S4) ──
// Renders the server-computed egress state per card. NEVER probes inline —
// the server's status payload carries a cached verdict only.
function _renderEgressLine(provider, egress) {
  var capProvider = provider === 'codex' ? 'Codex' : 'Claude';
  var el = document.getElementById('oauth' + capProvider + 'Egress');
  if (!el) return;
  if (!egress || !egress.state) { el.style.display = 'none'; el.innerHTML = ''; return; }
  el.style.display = '';
  var agents = egress.agents || [];
  var html = '';
  var cls = 'oauth-egress-line';
  switch (egress.state) {
    case 'direct':
      html = '<span class="oauth-egress-ok">' + t('settings.egressDirect') + '</span>';
      break;
    case 'agent':
      html = '<span class="oauth-egress-ok">' + t('settings.egressViaAgent', { name: (agents[0] && agents[0].name) || agents[0].agent_id }) + '</span>';
      break;
    case 'agent_no_capability':
      cls += ' oauth-egress-warn';
      html = '<span class="oauth-egress-warn">' + t('settings.egressAgentNoCap') + '</span>';
      break;
    case 'unavailable':
      cls += ' oauth-egress-bad';
      /* The ONLY way out of this state is installing the desktop agent — so
       * the prompt LEADS with that action (diagnosis demoted to the sub-line)
       * and renders the ONE next step as a prominent button deep-linking to
       * the Local Control modal (the single install surface: backend-chosen
       * download links + bridge-token connect line) instead of growing a
       * second install guide here. */
      html = '<div class="oauth-egress-callout">' +
               '<div class="oauth-egress-callout-text">' +
                 '<span class="oauth-egress-callout-title">' + t('settings.egressUnavailable') + '</span>' +
                 '<span class="oauth-egress-callout-sub">' + t('settings.egressUnavailSub') + '</span>' +
               '</div>' +
               '<button type="button" class="btn-small oauth-egress-agent-btn" id="oauth' + capProvider + 'EgressAgentBtn"' +
               ' title="' + escapeHtml(t('settings.egressGetAgentTitle')) + '">' +
               escapeHtml(t('settings.egressGetAgent')) + '</button>' +
             '</div>';
      break;
    default: // unknown — 探测已在后台触发
      html = '<span class="oauth-egress-pending">' + t('settings.egressProbing') + '</span>';
      _scheduleEgressRepoll();
  }
  // Pin selector when several egress-capable agents are online.
  if (agents.length > 1 && egress.state === 'agent') {
    html += ' <select class="oauth-egress-pin" id="oauth' + capProvider + 'EgressPin">';
    agents.forEach(function(a) {
      html += '<option value="' + escapeHtml(a.agent_id) + '">' +
              escapeHtml(a.name || a.agent_id) + '</option>';
    });
    html += '</select>';
  }
  el.className = cls;
  el.innerHTML = html;
  var agentBtn = document.getElementById('oauth' + capProvider + 'EgressAgentBtn');
  if (agentBtn) {
    agentBtn.onclick = function() { _oauthOpenAgentSetup(); };
  }
  var sel = document.getElementById('oauth' + capProvider + 'EgressPin');
  if (sel) {
    sel.onchange = function() {
      Api.oauth.egressAgentSet(this.value).then(function() {
        _loadOAuthStatus();
      });
    };
    // Pre-select the currently pinned agent.
    Api.oauth.egressAgentGet().then(function(d) {
      if (d && d.pinned) sel.value = d.pinned;
    });
  }
}

// ── Egress unavailable → hand the user the desktop-agent installer ──
// The Local Control modal is the ONE install surface (its download links are
// chosen by the backend's setup_state, and it mints the bridge-token connect
// line) — the egress line deep-links to it rather than re-authoring any of
// that guidance. While the modal is open we re-poll the (cached) OAuth status
// so this line flips to "via agent" the moment the agent connects, with one
// final refresh on close. The status endpoint NEVER probes inline, so the
// 3 s cadence costs a cache read only.
var _oauthAgentSetupPoll = null;

function _oauthOpenAgentSetup() {
  if (typeof openLocalControlModal === 'function') openLocalControlModal();
  if (_oauthAgentSetupPoll) { clearInterval(_oauthAgentSetupPoll); _oauthAgentSetupPoll = null; }
  _oauthAgentSetupPoll = setInterval(function() {
    var m = document.getElementById('localControlModal');
    if (!m || !m.classList.contains('open')) {
      clearInterval(_oauthAgentSetupPoll);
      _oauthAgentSetupPoll = null;
    }
    _loadOAuthStatus();
  }, 3000);
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
    // A page reload mid-flow lands HERE — not in _oauthLogin's callback —
    // re-rendered from the status projection alone. Restore the manual box,
    // its truthful instructions, and the escape hatch from that projection,
    // or the reloaded page silently offers nothing but a retry that re-runs
    // the same (possibly broken) callback decision — the exact loop the
    // hatch exists to break. Synthetic waiting states (exchange in flight,
    // curl helper) carry no redirect_mode and are left untouched.
    if (status.redirect_mode && status.status !== 'exchanging') {
      var flowManual = document.getElementById('oauth' + capProvider + 'Manual');
      if (flowManual) {
        flowManual.style.display = '';
        var flowUrl = document.getElementById('oauth' + capProvider + 'AuthUrl');
        if (flowUrl && status.auth_url) flowUrl.value = status.auth_url;
      }
      _oauthApplyRedirectMode(provider, status.redirect_mode);
    }
    // Exchange params are the OTHER piece of login-response state a reload
    // destroys. On the desktop build the SERVER is the user's machine, so a
    // geo-blocked server exchange leaves the BROWSER exchange as the only
    // path — and without these params it rejects with no-exchange-params
    // while the curl helper cannot even build its command. Restoring them
    // from the status projection keeps both backstops alive after a reload.
    if (status.exchange) {
      _oauthExchangeParams[provider] = status.exchange;
    }
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
           *   for every Connect click that never closed its popup (pt_3cd6cd48). */
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


// ══════════════════════════════════════════════════════
//  订阅适配器 (CLIProxyAPI) — subscription-adapter card
//
//  The backend manages a CLIProxyAPI sidecar on the user's desktop agent
//  and projects it as a provider (`订阅适配器 · <agent name>`, id
//  `adapter_<agent8>`) once bring-up reaches 'ready'. This card is the
//  settings-surface for that lifecycle: it renders BELOW the OAuth cards
//  (built in JS because the oauth.html panel ships before the bundler —
//  touching the panel file is outside this module's ownership).
//
//  Polling follows the same contract as the egress re-poll above: a
//  single setTimeout chain, alive ONLY while the settings modal is open
//  AND the OAuth tab is active; the cadence tightens while any agent is
//  mid-bring-up (first run downloads ~20MB, minutes) and relaxes once
//  every task is settled. Stop = the chain simply never re-arms.
// ══════════════════════════════════════════════════════

var _adapterPollTimer = null;
var _adapterCardBuilt = false;
var _ADAPTER_POLL_IDLE_MS = 5000;   // steady state
var _ADAPTER_POLL_BUSY_MS = 2000;   // while any ensure task is in flight
var _adapterLastBusy = false;       // exposed for tests / diagnostics

function _adapterPanelVisible() {
  var modal = document.getElementById('settingsModal');
  var panel = document.getElementById('settingsTab_oauth');
  return !!(modal && modal.classList.contains('open') &&
            panel && panel.classList.contains('active'));
}

// Build the card DOM once, appended after the OAuth provider cards.
function _adapterEnsureCard() {
  if (_adapterCardBuilt) return;
  var panel = document.getElementById('settingsTab_oauth');
  if (!panel) return;
  if (document.getElementById('adapterCard')) { _adapterCardBuilt = true; return; }
  var card = document.createElement('div');
  card.className = 'oauth-provider-card';
  card.id = 'adapterCard';
  card.innerHTML =
    '<div class="oauth-provider-header">' +
      '<span class="oauth-provider-name">' + escapeHtml(t('settings.adapterTitle')) + '</span>' +
    '</div>' +
    '<p class="oauth-provider-desc">' + escapeHtml(t('settings.adapterDesc')) + '</p>' +
    '<div id="adapterRows"></div>' +
    '<div class="adapter-empty" id="adapterEmpty" style="display:none">' +
      escapeHtml(t('settings.adapterEmpty')) +
    '</div>' +
    '<p class="adapter-info-line">' + escapeHtml(t('settings.adapterInfoLine')) + '</p>';
  panel.appendChild(card);
  _adapterCardBuilt = true;
}

// One status payload → full re-render of the per-agent rows.
function _renderAdapterRows(data) {
  _adapterEnsureCard();
  var rows = document.getElementById('adapterRows');
  var empty = document.getElementById('adapterEmpty');
  if (!rows || !empty) return;
  var agents = (data && Array.isArray(data.agents)) ? data.agents : [];
  var tasks = (data && data.ensure_tasks) || {};
  var online = [];
  for (var i = 0; i < agents.length; i++) {
    if (agents[i] && agents[i].online) online.push(agents[i]);
  }
  if (!online.length) {
    rows.innerHTML = '';
    empty.style.display = '';
    _adapterLastBusy = false;
    return;
  }
  empty.style.display = 'none';
  var html = '';
  var busy = false;
  for (var j = 0; j < online.length; j++) {
    var a = online[j];
    var task = tasks[a.agent_id] || null;
    var ad = a.adapter || {};
    var state;   // 'ensuring' | 'running' | 'error' | 'installed' | 'not_installed'
    if (task && task.state === 'ensuring') state = 'ensuring';
    else if ((task && task.state === 'error') || ad.error) state = 'error';
    else if (ad.running) state = 'running';
    else if (ad.installed) state = 'installed';
    else state = 'not_installed';
    if (state === 'ensuring') busy = true;

    var badgeCls = 'oauth-status-badge';
    var badgeTxt;
    if (state === 'ensuring') {
      badgeCls += ' pending'; badgeTxt = t('settings.adapterBadgeInstalling');
    } else if (state === 'running') {
      badgeCls += ' authenticated';
      badgeTxt = t('settings.adapterBadgeRunning', {
        version: ad.version || '?', port: ad.port || (a.policy && a.policy.port) || 8317 });
    } else if (state === 'error') {
      badgeCls += ' error'; badgeTxt = t('settings.adapterBadgeError');
    } else if (state === 'installed') {
      badgeTxt = t('settings.adapterBadgeInstalled');
    } else {
      badgeTxt = t('settings.adapterBadgeNotInstalled');
    }

    html += '<div class="adapter-agent-row" data-agent="' + escapeHtml(a.agent_id) + '">' +
      '<div class="adapter-agent-head">' +
        '<span class="adapter-agent-name">' + escapeHtml(a.name || a.agent_id) + '</span>' +
        '<span class="' + badgeCls + '">' + escapeHtml(badgeTxt) + '</span>' +
      '</div>';

    var n = Array.isArray(ad.accounts) ? ad.accounts.length
          : (typeof ad.accounts === 'number' ? ad.accounts : 0);
    html += '<div class="adapter-agent-meta">' +
      escapeHtml(t('settings.adapterAccounts', { n: n })) + '</div>';

    if (state === 'ensuring') {
      html += '<div class="adapter-progress">' +
        '<span class="adapter-spinner"></span>' +
        escapeHtml(t('settings.adapterEnsuring')) + '</div>';
    } else if (task && task.state === 'ready') {
      html += '<div class="adapter-ready-line">' +
        escapeHtml(t('settings.adapterReady', { name: a.name || a.agent_id })) + '</div>';
    } else if (state === 'error') {
      var detail = (task && task.detail) || ad.error || '';
      html += '<div class="adapter-error-line">' + escapeHtml(detail) + '</div>';
    }

    html += '<div class="oauth-provider-actions">';
    if (state === 'running') {
      html += '<button class="btn-small btn-danger adapter-stop-btn" data-agent="' +
        escapeHtml(a.agent_id) + '">' + escapeHtml(t('settings.adapterStop')) + '</button>';
    } else if (state === 'ensuring') {
      html += '<button class="btn-small btn-primary" disabled>' +
        escapeHtml(t('settings.adapterStart')) + '</button>';
    } else if (state === 'error') {
      html += '<button class="btn-small btn-primary adapter-start-btn" data-agent="' +
        escapeHtml(a.agent_id) + '">' + escapeHtml(t('settings.adapterRetry')) + '</button>';
    } else {
      html += '<button class="btn-small btn-primary adapter-start-btn" data-agent="' +
        escapeHtml(a.agent_id) + '">' + escapeHtml(t('settings.adapterStart')) + '</button>';
    }
    html += '</div></div>';
  }
  rows.innerHTML = html;
  _adapterLastBusy = busy;

  rows.querySelectorAll('.adapter-start-btn').forEach(function(btn) {
    btn.onclick = function() { _adapterEnsure(this.getAttribute('data-agent')); };
  });
  rows.querySelectorAll('.adapter-stop-btn').forEach(function(btn) {
    btn.onclick = function() { _adapterStop(this.getAttribute('data-agent')); };
  });
}

function _adapterEnsure(agentId) {
  if (!agentId) return;
  Api.post('/api/v1/adapter/ensure', { agent_id: agentId }, { onError: 'null' })
    .then(function() { _adapterTick(true); });
}

function _adapterStop(agentId) {
  if (!agentId) return;
  Api.post('/api/v1/adapter/stop', { agent_id: agentId }, { onError: 'null' })
    .then(function() { _adapterTick(true); });
}

function _adapterTick(force) {
  if (_adapterPollTimer) { clearTimeout(_adapterPollTimer); _adapterPollTimer = null; }
  if (!force && !_adapterPanelVisible()) return;
  Api.get('/api/v1/adapter/status', { onError: 'null' })
    .then(function(data) { if (data) _renderAdapterRows(data); })
    .catch(function() {})
    .then(function() {
      if (!_adapterPanelVisible()) return;   // hidden → chain dies here
      _adapterPollTimer = setTimeout(function() {
        _adapterPollTimer = null;
        _adapterTick(false);
      }, _adapterLastBusy ? _ADAPTER_POLL_BUSY_MS : _ADAPTER_POLL_IDLE_MS);
    });
}

// Kick the chain (idempotent). Wired into _loadOAuthStatus, which
// openSettings() calls on every settings open.
function _adapterStartPolling() {
  _adapterEnsureCard();
  if (_adapterPollTimer) return;
  _adapterTick(false);
}


