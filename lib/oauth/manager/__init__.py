"""lib/oauth/manager/ — OAuth flow manager with browser-centric callback relay.

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

This is a pure re-export facade — every implementation lives in the
sub-modules below. ``__all__`` is preserved verbatim from the original
single-file module so ``from lib.oauth.manager import X`` (and the package
facade ``from lib.oauth import *``, which does ``from .manager import *``)
keeps working byte-identically.

CRITICAL SHARED STATE: ``_active_flows`` / ``_flows_lock`` / ``_active_servers``
/ ``_servers_lock`` / ``_FLOW_TIMEOUT`` all live in ``._state`` and are
re-exported here BY REFERENCE — there is exactly ONE of each per process.
"""

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'start_oauth_flow',
    'start_device_flow',
    'stop_device_flow',
    'get_oauth_status',
    'get_all_oauth_status',
    'exchange_code',
    'store_token',
    'logout_oauth',
]


# ═══════════════════════════════════════════════════════════════════════════════
#  Shared state (single home — re-exported BY REFERENCE from ._state)
# ═══════════════════════════════════════════════════════════════════════════════

from lib.oauth.manager._state import (  # noqa: E402,F401
    _active_flows,
    _flows_lock,
    _active_servers,
    _servers_lock,
    _FLOW_TIMEOUT,
)


# ═══════════════════════════════════════════════════════════════════════════════
#  Relay HTML + relay HTTP server
# ═══════════════════════════════════════════════════════════════════════════════

from lib.oauth.manager._relay import (  # noqa: E402,F401
    _RELAY_HTML,
    _RelayHandler,
    _run_relay_server,
)


# ═══════════════════════════════════════════════════════════════════════════════
#  Flow lifecycle — start + status
# ═══════════════════════════════════════════════════════════════════════════════

from lib.oauth.manager._flow import (  # noqa: E402,F401
    start_oauth_flow,
    get_oauth_status,
    get_all_oauth_status,
)


# ═══════════════════════════════════════════════════════════════════════════════
#  Device-authorization flow (Codex — no localhost redirect involved)
# ═══════════════════════════════════════════════════════════════════════════════

from lib.oauth.manager._device import (  # noqa: E402,F401
    start_device_flow,
    stop_device_flow,
)


# ═══════════════════════════════════════════════════════════════════════════════
#  Token exchange / store / logout
# ═══════════════════════════════════════════════════════════════════════════════

from lib.oauth.manager._exchange import (  # noqa: E402,F401
    exchange_code,
    store_token,
    logout_oauth,
)
