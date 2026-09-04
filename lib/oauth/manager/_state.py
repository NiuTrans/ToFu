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
from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)


# ── Active flow state ──
# provider → {flow_id, owner_user_id, state, pkce, status, ...}
_active_flows: dict[str, dict] = {}
_flows_lock = threading.Lock()


def _flow_is_current(provider: str, flow_id: str) -> bool:
    """Whether an async callback still belongs to the active generation."""
    with _flows_lock:
        return bool(
            flow_id
            and (_active_flows.get(provider) or {}).get('flow_id') == flow_id
        )


def _update_active_flow(
    provider: str,
    flow_id: str,
    **updates: Any,
) -> bool:
    """Update one flow only if it has not been replaced by a newer login.

    Relay and device workers outlive request threads. Provider-only mutation
    lets a timed-out old worker corrupt the status of a newly started flow;
    the opaque generation makes every delayed callback harmless.
    """
    with _flows_lock:
        flow = _active_flows.get(provider)
        if not flow_id or not flow or flow.get('flow_id') != flow_id:
            return False
        flow.update(updates)
        return True

# Track running relay servers so we can shut them down on re-login
_active_servers: dict[str, HTTPServer] = {}
_servers_lock = threading.Lock()

_FLOW_TIMEOUT = 300  # 5 minutes — auto-expire stale OAuth flows
