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

// ── Handle received OAuth code ──
function _handleOAuthCode(provider, code, state) {
  if (!provider || !code) return;

  var capProvider = provider === 'codex' ? 'Codex' : 'Claude';
  _updateOAuthCard(provider, { status: 'exchanging' });

  // Send code to server for token exchange
  // Try POST first; if proxy returns 405, fall back to GET with query params
  var body = { provider: provider, code: code };
  if (state) body.state = state;
  function _doCallbackRequest(useGet) {
    if (useGet) {
      console.warn('[OAuth] POST got 405, retrying as GET for /api/oauth/callback');
      var qs = 'provider=' + encodeURIComponent(provider) + '&code=' + encodeURIComponent(code);
      if (state) qs += '&state=' + encodeURIComponent(state);
      return Api.oauth.callbackGet(qs);
    }
    return Api.oauth.callbackPost(body);
  }
  _doCallbackRequest(false)
    .then(function(r) {
      if (r.status === 404 || r.status === 405) return _doCallbackRequest(true);
      return r;
    })
    .then(function(r) {
      if (!r.ok) return r.text().then(function(t) { throw new Error(t.slice(0, 200)); });
      return r.json();
    })
    .then(function(data) {
      if (data.error) {
        _updateOAuthCard(provider, { status: 'error' });
        alert('Token 交换失败: ' + data.error);
        return;
      }
      // Success!
      _updateOAuthCard(provider, { status: 'success', authenticated: true, email: data.email || '' });
      _autoConfigureOAuthProvider(provider, { email: data.email });

      // Hide manual fallback
      var manualDiv = document.getElementById('oauth' + capProvider + 'Manual');
      if (manualDiv) manualDiv.style.display = 'none';
    })
    .catch(function(e) {
      console.error('[OAuth] Token exchange error:', e);
      _updateOAuthCard(provider, { status: 'error' });
      alert('Token 交换失败: ' + e.message);
    });
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
    badge.textContent = '已登录';
    badge.className = 'oauth-status-badge authenticated';
    if (info) { info.style.display = ''; }
    if (email) { email.textContent = status.email || '(unknown)'; }
    if (loginBtn) { loginBtn.style.display = 'none'; }
    if (logoutBtn) { logoutBtn.style.display = ''; }
  } else if (status.status === 'started' || status.status === 'waiting_callback' || status.status === 'exchanging') {
    badge.textContent = status.status === 'exchanging' ? '正在获取 Token…' : '等待授权…';
    badge.className = 'oauth-status-badge pending';
    if (info) { info.style.display = 'none'; }
    // Show a cancel/retry button so users aren't stuck forever
    if (loginBtn) {
      loginBtn.disabled = false;
      loginBtn.textContent = '取消 / 重试';
      loginBtn.onclick = function() { _oauthCancelAndRetry(provider); };
    }
    if (logoutBtn) { logoutBtn.style.display = 'none'; }
  } else if (status.status === 'error') {
    badge.textContent = '错误';
    badge.className = 'oauth-status-badge error';
    if (info) { info.style.display = 'none'; }
    if (loginBtn) { loginBtn.disabled = false; loginBtn.textContent = provider === 'codex' ? '登录 ChatGPT' : '登录 Claude'; loginBtn.onclick = function() { _oauthLogin(provider); }; }
    if (logoutBtn) { logoutBtn.style.display = 'none'; }
  } else {
    badge.textContent = '未登录';
    badge.className = 'oauth-status-badge';
    if (info) { info.style.display = 'none'; }
    if (loginBtn) { loginBtn.disabled = false; loginBtn.textContent = provider === 'codex' ? '登录 ChatGPT' : '登录 Claude'; loginBtn.style.display = ''; loginBtn.onclick = function() { _oauthLogin(provider); }; }
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
  if (loginBtn) { loginBtn.disabled = true; loginBtn.textContent = '正在准备…'; }

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
        alert('OAuth 登录失败: ' + data.error);
        if (loginBtn) { loginBtn.disabled = false; loginBtn.textContent = provider === 'codex' ? '登录 ChatGPT' : '登录 Claude'; }
        return;
      }

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
            if (badge && (badge.textContent.indexOf('等待') >= 0 || badge.textContent.indexOf('授权') >= 0)) {
              // Don't reset — just update button to allow retry
              var loginBtn2 = document.getElementById('oauth' + capProvider + 'LoginBtn');
              if (loginBtn2) {
                loginBtn2.disabled = false;
                loginBtn2.textContent = '重新打开弹窗';
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
      alert('OAuth 登录请求失败: ' + e.message);
      if (loginBtn) { loginBtn.disabled = false; loginBtn.textContent = provider === 'codex' ? '登录 ChatGPT' : '登录 Claude'; }
    });
}

function _oauthLogout(provider) {
  if (!confirm('确定要退出 ' + (provider === 'codex' ? 'ChatGPT' : 'Claude') + ' 订阅登录吗？')) return;

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
      alert('退出失败: ' + e.message);
    });
}

function _oauthManualSubmit(provider) {
  var capP = provider === 'codex' ? 'Codex' : 'Claude';
  var input = document.getElementById('oauth' + capP + 'ManualUrl');
  if (!input || !input.value.trim()) {
    alert('请粘贴授权码或回调 URL');
    return;
  }
  var val = input.value.trim();

  // Support multiple formats:
  // 1. Full callback URL: http://localhost:PORT/callback?code=XXX&state=YYY
  // 2. code#state format (shown by Anthropic console after auth)
  // 3. Raw authorization code
  var body = { provider: provider };
  if (val.indexOf('http') === 0) {
    body.callback_url = val;
  } else if (val.indexOf('#') > 0) {
    // code#state format from Anthropic console
    var parts = val.split('#');
    body.code = parts[0];
    body.state = parts[1] || '';
  } else {
    body.code = val;
  }

  // Try POST first; if proxy returns 405, fall back to GET with query params
  function _doManualCallbackRequest(useGet) {
    if (useGet) {
      console.warn('[OAuth] POST got 405, retrying as GET for /api/oauth/callback (manual)');
      var qs = 'provider=' + encodeURIComponent(body.provider);
      if (body.code) qs += '&code=' + encodeURIComponent(body.code);
      if (body.state) qs += '&state=' + encodeURIComponent(body.state);
      if (body.callback_url) qs += '&callback_url=' + encodeURIComponent(body.callback_url);
      return Api.oauth.callbackGet(qs);
    }
    return Api.oauth.callbackPost(body);
  }
  _doManualCallbackRequest(false)
    .then(function(r) {
      if (r.status === 404 || r.status === 405) return _doManualCallbackRequest(true);
      return r;
    })
    .then(function(r) {
      if (!r.ok) return r.text().then(function(t) { throw new Error(t.slice(0, 200)); });
      return r.json();
    })
    .then(function(data) {
      if (data.error) {
        alert('授权失败: ' + data.error);
      } else {
        _updateOAuthCard(provider, { status: 'success', authenticated: true, email: data.email || '' });
        var manualDiv = document.getElementById('oauth' + capP + 'Manual');
        if (manualDiv) manualDiv.style.display = 'none';
        input.value = '';
        _autoConfigureOAuthProvider(provider, { email: data.email });
      }
    })
    .catch(function(e) {
      alert('提交失败: ' + e.message);
    });
}

function _autoConfigureOAuthProvider(provider, status) {
  var name = provider === 'codex' ? 'ChatGPT Plus' : 'Claude Pro';
  var el = document.getElementById('settingsStatusHint');
  if (el) {
    el.textContent = '✅ ' + name + ' 登录成功！请在「服务商」标签页添加对应模型。';
    el.style.color = '#28a745';
  }
}


