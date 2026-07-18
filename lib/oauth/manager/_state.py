"""lib/oauth/manager/_state.py — shared OAuth flow + relay-server state.

CRITICAL: this module is the SINGLE home for all mutable module-level OAuth
state. Every other submodule in this package (``_relay``, ``_flow``,
``_exchange``) imports these names BY REFERENCE and mutates the *contents*
of the dicts (never rebinds them), so there is exactly ONE ``_active_flows``
and ONE ``_active_servers`` per process. A divergent copy would strand a
running relay HTTPServer or lose a pending flow — do not shadow or reassign
these at import sites.
"""

import threading
from http.server import HTTPServer

from lib.log import get_logger

logger = get_logger(__name__)


# ── Active flow state ──
# provider → {state, pkce, status, auth_url, error, started_at}
_active_flows: dict[str, dict] = {}
_flows_lock = threading.Lock()

# Track running relay servers so we can shut them down on re-login
_active_servers: dict[str, HTTPServer] = {}
_servers_lock = threading.Lock()

_FLOW_TIMEOUT = 300  # 5 minutes — auto-expire stale OAuth flows
