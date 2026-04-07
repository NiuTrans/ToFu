"""lib/oauth/manager.py — OAuth flow manager with browser-centric callback relay.

Architecture (all browser-driven):
  1. Frontend calls /api/oauth/login → server generates PKCE + auth_url
  2. Server starts a tiny relay HTTP server on the registered callback port
  3. Frontend opens auth_url in a popup (window.open)
  4. User authenticates → OAuth redirects to localhost:PORT/callback?code=XXX
  5. Relay server serves a HTML page that uses postMessage() to send code to opener
  6. Frontend receives the code via message event listener
  7. Frontend POSTs the code to /api/oauth/callback → server exchanges for tokens

The relay server is ultra-lightweight — it just serves one HTML page and exits.
No webbrowser.open() — the browser handles everything.
"""

import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from lib.log import get_logger, audit_log

logger = get_logger(__name__)

__all__ = [
    'start_oauth_flow',
    'get_oauth_status',
    'get_all_oauth_status',
    'exchange_code',
    'logout_oauth',
]

# ── Active flow state ──
# provider → {state, pkce, status, auth_url, error, started_at}
_active_flows: dict[str, dict] = {}
_flows_lock = threading.Lock()

# Track running relay servers so we can shut them down on re-login
_active_servers: dict[str, HTTPServer] = {}
_servers_lock = threading.Lock()


# ══════════════════════════════════════════════════════════
#  Relay HTML — served by the callback server
#  This page uses postMessage() to relay the code back to
#  the opener window (our main app), then auto-closes.
# ══════════════════════════════════════════════════════════

_RELAY_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>OAuth Callback</title>
<style>
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       display: flex; justify-content: center; align-items: center;
       min-height: 100vh; margin: 0; background: #1a1a2e; color: #eee; }
.card { text-align: center; padding: 40px; border-radius: 16px;
        background: #16213e; box-shadow: 0 8px 32px rgba(0,0,0,0.3); max-width: 420px; }
.icon { font-size: 64px; margin-bottom: 16px; }
h1 { margin: 0 0 12px; font-size: 24px; color: #4ecca3; }
p { color: #aaa; margin: 8px 0; }
.countdown { color: #888; font-size: 14px; margin-top: 20px; }
.code-box { font-family: monospace; background: #1a1a2e; padding: 8px 12px;
            border-radius: 6px; margin: 12px 0; word-break: break-all; font-size: 13px;
            color: #4ecca3; border: 1px solid #333; }
.fallback { display: none; margin-top: 16px; }
.fallback p { font-size: 13px; color: #999; }
</style>
<script>
(function() {
  var code = "CODE_PLACEHOLDER";
  var state = "STATE_PLACEHOLDER";
  var provider = "PROVIDER_PLACEHOLDER";
  var error = "ERROR_PLACEHOLDER";

  if (error && error !== "") {
    document.addEventListener('DOMContentLoaded', function() {
      document.getElementById('icon').textContent = '❌';
      document.getElementById('title').textContent = 'Authorization Failed';
      document.getElementById('title').style.color = '#e74c3c';
      document.getElementById('desc').textContent = error;
      document.getElementById('countdown-area').style.display = 'none';
    });
    return;
  }

  // Try to send the code back to the opener via postMessage
  var sent = false;
  if (window.opener) {
    try {
      window.opener.postMessage({
        type: 'oauth_callback',
        provider: provider,
        code: code,
        state: state
      }, '*');
      sent = true;
    } catch(e) {
      console.error('postMessage failed:', e);
    }
  }

  // Also try BroadcastChannel as fallback (works when popup loses opener ref)
  try {
    var bc = new BroadcastChannel('oauth_callback');
    bc.postMessage({ type: 'oauth_callback', provider: provider, code: code, state: state });
    sent = true;
    setTimeout(function() { bc.close(); }, 1000);
  } catch(e) {}

  document.addEventListener('DOMContentLoaded', function() {
    if (sent) {
      // Auto-close after 3 seconds
      var t = 3;
      setInterval(function() {
        if (--t <= 0) window.close();
        var el = document.getElementById('cd');
        if (el) el.textContent = t;
      }, 1000);
    } else {
      // Can't relay — show the code for manual copy
      document.getElementById('fallback').style.display = 'block';
      document.getElementById('manual-code').textContent = code;
      document.getElementById('countdown-area').style.display = 'none';
      document.getElementById('desc').textContent = 'Please copy the code below and paste it in the Tofu settings.';
    }
  });
})();
</script>
</head><body>
<div class="card">
  <div class="icon" id="icon">✅</div>
  <h1 id="title">Authorization Successful</h1>
  <p id="desc">Sending authorization code back to Tofu…</p>
  <p class="countdown" id="countdown-area">This window will close in <span id="cd">3</span> seconds…</p>
  <div class="fallback" id="fallback">
    <p>Could not automatically relay the code. Please copy it and paste in the Tofu settings:</p>
    <div class="code-box" id="manual-code"></div>
  </div>
</div>
</body></html>"""


# ══════════════════════════════════════════════════════════
#  Relay HTTP Server — serves callback page, then exits
# ══════════════════════════════════════════════════════════

class _RelayHandler(BaseHTTPRequestHandler):
    """Ultra-lightweight callback handler — serves relay HTML and exits."""

    # Set by server factory
    provider = ''
    expected_state = ''
    on_served = None  # callback when page is served (to signal shutdown)

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        error = params.get('error', [None])[0]
        code = params.get('code', [''])[0]
        state = params.get('state', [''])[0]

        if error:
            desc = params.get('error_description', [error])[0]
            logger.warning('[OAuth Relay] Error from %s: %s — %s',
                           self.provider, error, desc)
            html = _RELAY_HTML.replace('CODE_PLACEHOLDER', '') \
                              .replace('STATE_PLACEHOLDER', '') \
                              .replace('PROVIDER_PLACEHOLDER', self.provider) \
                              .replace('ERROR_PLACEHOLDER', f'{error}: {desc}')
        elif state and state != self.expected_state:
            logger.warning('[OAuth Relay] State mismatch for %s: expected=%s got=%s',
                           self.provider, self.expected_state[:8], state[:8])
            html = _RELAY_HTML.replace('CODE_PLACEHOLDER', '') \
                              .replace('STATE_PLACEHOLDER', '') \
                              .replace('PROVIDER_PLACEHOLDER', self.provider) \
                              .replace('ERROR_PLACEHOLDER',
                                       'State parameter mismatch (CSRF protection)')
        else:
            logger.info('[OAuth Relay] Received code from %s (len=%d), serving relay page',
                         self.provider, len(code))
            # Escape for JS string embedding
            safe_code = code.replace('\\', '\\\\').replace('"', '\\"')
            safe_state = state.replace('\\', '\\\\').replace('"', '\\"')
            html = _RELAY_HTML.replace('CODE_PLACEHOLDER', safe_code) \
                              .replace('STATE_PLACEHOLDER', safe_state) \
                              .replace('PROVIDER_PLACEHOLDER', self.provider) \
                              .replace('ERROR_PLACEHOLDER', '')

        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        # Allow cross-origin access for postMessage relay
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(html.encode('utf-8'))

        # Signal that we've served the relay page
        if self.on_served:
            self.on_served()

    def log_message(self, format, *args):
        """Suppress default HTTP logging."""
        pass


def _run_relay_server(provider: str, port: int, state: str, timeout: int = 300):
    """Run relay HTTP server on the registered callback port.

    This server has ONE job: serve the relay HTML page when the OAuth
    redirect arrives. The relay page uses postMessage() to send the
    code to the opener window. The server exits after serving one request.

    Args:
        provider: 'claude' or 'codex'.
        port: Registered callback port (54545 for Claude, 1455 for Codex).
        state: Expected OAuth state parameter.
        timeout: Max seconds to wait.
    """
    served = threading.Event()

    handler_class = type('Handler', (_RelayHandler,), {
        'provider': provider,
        'expected_state': state,
        'on_served': staticmethod(lambda: served.set()),
    })

    # Shut down any previous relay server for this provider
    with _servers_lock:
        old = _active_servers.pop(provider, None)
    if old:
        try:
            old.server_close()
            logger.info('[OAuth Relay] Closed previous %s relay server', provider)
        except Exception as e:
            logger.debug('[OAuth Relay] Error closing old server: %s', e)
        time.sleep(0.3)

    try:
        server = HTTPServer(('127.0.0.1', port), handler_class)
        server.timeout = 2  # poll interval

        with _servers_lock:
            _active_servers[provider] = server

        logger.info('[OAuth Relay] Listening on :%d for %s callback (timeout=%ds)',
                     port, provider, timeout)

        with _flows_lock:
            if provider in _active_flows:
                _active_flows[provider]['status'] = 'waiting_callback'

        deadline = time.time() + timeout
        while time.time() < deadline and not served.is_set():
            server.handle_request()

        server.server_close()
        with _servers_lock:
            _active_servers.pop(provider, None)

        if not served.is_set():
            logger.warning('[OAuth Relay] Timeout waiting for %s callback', provider)
            with _flows_lock:
                if provider in _active_flows:
                    _active_flows[provider]['status'] = 'timeout'
                    _active_flows[provider]['error'] = 'Timeout — no callback received'

    except OSError as e:
        logger.error('[OAuth Relay] Failed to bind :%d: %s', port, e)
        with _servers_lock:
            _active_servers.pop(provider, None)
        with _flows_lock:
            if provider in _active_flows:
                _active_flows[provider]['status'] = 'error'
                _active_flows[provider]['error'] = f'Port {port} already in use. Try again in a few seconds.'


# ══════════════════════════════════════════════════════════
#  Public API
# ══════════════════════════════════════════════════════════

def start_oauth_flow(provider: str) -> dict:
    """Start an OAuth login flow.

    Generates PKCE codes and auth URL, starts relay server on
    the registered callback port. The frontend opens the auth URL
    in a popup and listens for postMessage with the code.

    Args:
        provider: 'claude' or 'codex'.

    Returns:
        dict with 'auth_url', 'status', 'provider', 'callback_port'.
    """
    if provider == 'claude':
        from lib.oauth.claude import claude_build_auth_url
        flow = claude_build_auth_url()
    elif provider == 'codex':
        from lib.oauth.codex import codex_build_auth_url
        flow = codex_build_auth_url()
    else:
        return {'error': f'Unknown provider: {provider}'}

    # Store flow state
    with _flows_lock:
        _active_flows[provider] = {
            'status': 'started',
            'auth_url': flow['auth_url'],
            'state': flow['state'],
            'pkce': flow['pkce'],
            'started_at': time.time(),
            'error': None,
            'email': None,
        }

    # Start relay server in background thread (only for providers that
    # redirect to localhost — Claude redirects to console.anthropic.com,
    # so the user must manually copy the code#state back)
    if provider != 'claude':
        thread = threading.Thread(
            target=_run_relay_server,
            args=(provider, flow['callback_port'], flow['state']),
            daemon=True,
            name=f'oauth-relay-{provider}',
        )
        thread.start()
        logger.info('[OAuth] Started %s flow — relay on :%d, auth URL ready',
                     provider, flow['callback_port'])
    else:
        logger.info('[OAuth] Started %s flow — auth URL ready (manual code paste required)',
                     provider)
    return {
        'auth_url': flow['auth_url'],
        'status': 'started',
        'provider': provider,
        'callback_port': flow['callback_port'],
    }


_FLOW_TIMEOUT = 300  # 5 minutes — auto-expire stale OAuth flows


def get_oauth_status(provider: str) -> dict:
    """Get current OAuth status for a provider."""
    from lib.oauth.token_store import load_token

    with _flows_lock:
        flow = _active_flows.get(provider, {})
        # Auto-expire stale flows that have been waiting too long
        if flow and flow.get('status') in ('started', 'waiting_callback'):
            started_at = flow.get('started_at', 0)
            if started_at and (time.time() - started_at) > _FLOW_TIMEOUT:
                logger.info('[OAuth] Auto-expiring stale %s flow (started %.0fs ago)',
                            provider, time.time() - started_at)
                _active_flows.pop(provider, None)
                flow = {}

    stored = load_token(provider)
    authenticated = bool(stored and stored.get('access_token'))

    return {
        'provider': provider,
        'status': flow.get('status', 'not_started'),
        'error': flow.get('error'),
        'email': flow.get('email') or (stored.get('email', '') if stored else ''),
        'authenticated': authenticated,
        'expire': stored.get('expire') if stored else None,
    }


def get_all_oauth_status() -> dict:
    """Get OAuth status for all supported providers."""
    return {
        'claude': get_oauth_status('claude'),
        'codex': get_oauth_status('codex'),
    }


def exchange_code(provider: str, code: str, state: str = '') -> dict:
    """Exchange an authorization code for tokens.

    Called by the frontend after receiving the code via postMessage
    from the relay page, or via manual paste.

    Args:
        provider: 'claude' or 'codex'.
        code: Authorization code from OAuth callback.
        state: OAuth state parameter for CSRF validation.

    Returns:
        dict with status info.
    """
    if not code:
        return {'error': 'No authorization code provided'}

    # Get PKCE verifier from the active flow
    with _flows_lock:
        flow = _active_flows.get(provider, {})
    pkce = flow.get('pkce', {})
    pkce_verifier = pkce.get('code_verifier', '')
    flow_state = flow.get('state', '')

    if not pkce_verifier:
        return {'error': 'No active OAuth flow found. Please start a new login first.'}

    # Use the state from the active flow if not explicitly provided
    if not state:
        state = flow_state

    logger.info('[OAuth] Exchanging code for %s tokens (code_len=%d)', provider, len(code))

    with _flows_lock:
        if provider in _active_flows:
            _active_flows[provider]['status'] = 'exchanging'

    if provider == 'claude':
        from lib.oauth.claude import claude_exchange_code
        token = claude_exchange_code(code, pkce_verifier, state=state)
    elif provider == 'codex':
        from lib.oauth.codex import codex_exchange_code
        token = codex_exchange_code(code, pkce_verifier)
    else:
        token = None

    with _flows_lock:
        if token:
            _active_flows[provider]['status'] = 'success'
            _active_flows[provider]['email'] = token.get('email', '')
            audit_log('oauth_login', provider=provider, email=token.get('email', ''))
        else:
            _active_flows[provider]['status'] = 'error'
            _active_flows[provider]['error'] = 'Token exchange failed'
            return {'error': 'Token exchange failed. The code may have expired.'}

    return {
        'ok': True,
        'provider': provider,
        'email': token.get('email', ''),
        'status': 'success',
    }


def logout_oauth(provider: str) -> dict:
    """Logout from an OAuth provider (delete stored token)."""
    from lib.oauth.token_store import delete_token

    delete_token(provider)

    with _flows_lock:
        _active_flows.pop(provider, None)

    # Shut down any running relay server
    with _servers_lock:
        old = _active_servers.pop(provider, None)
    if old:
        try:
            old.server_close()
        except Exception as e:
            logger.debug('[OAuth] Error closing relay server for %s: %s', provider, e)

    audit_log('oauth_logout', provider=provider)
    logger.info('[OAuth] Logged out from %s', provider)
    return {'ok': True, 'provider': provider}
