"""lib/oauth/manager/_relay.py — browser-centric OAuth callback relay.

Serves a single HTML page (``_RELAY_HTML``) that uses ``postMessage()`` to
relay the authorization code back to the opener window, then exits. The
relay server handles + flow state are shared BY REFERENCE from
``._state`` — this module never rebinds ``_active_flows`` / ``_active_servers``.
"""

import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from lib.log import get_logger

from lib.oauth.manager._state import (
    _active_flows,
    _flows_lock,
    _active_servers,
    _servers_lock,
)

logger = get_logger(__name__)


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
