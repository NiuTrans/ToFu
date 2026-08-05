"""lib/browser/queue/ — Command queue infrastructure for Chrome Extension.

Façade-preserving package. This ``__init__`` re-exports EVERY symbol the
original ``lib/browser/queue.py`` module exposed, so all existing imports
(``from lib.browser.queue import X`` and ``from .queue import *`` in
``lib/browser/__init__.py``) keep working byte-identically.

Architecture (single-endpoint, proxy-safe):
  LLM tool_call  →  send_browser_command() [blocks with timeout]
                          ↓ (added to queue)
  Extension polls  →  POST /api/browser/poll  { results: [...] }
                          ↓
  Server:  1) resolves any results from the body
           2) returns new pending commands in the response
                          ↓
  Extension executes  →  stashes results  →  sends with next poll
                          ↓
  send_browser_command() unblocks and returns

v4: single POST endpoint eliminates separate result POST that VSCode proxy drops.

CRITICAL: the process-wide singleton state (``_commands``, ``_clients``,
``_active_client`` …) lives in ``_state`` and is shared BY REFERENCE across the
submodules — there is exactly one queue/registry in the process. The
submodules are deliberately named ``_registry`` / ``_dispatch`` (NOT
``_clients`` / ``_commands``) so they never shadow the ``_clients`` /
``_commands`` dict attributes that consumers reach for as ``queue._commands``.
"""

from lib.log import get_logger

logger = get_logger(__name__)

# ── Shared state (single home) ──
from ._state import (
    _commands, _commands_lock, _notify,
    _async_waiters, _async_waiters_lock, _wake_async_waiters,
    _clients, _clients_lock, _last_poll_time, _STALE_GRACE, POLL_WAIT_TIMEOUT,
    _active_client, _set_active_client, _get_active_client,
)

# ── Client registry / poll tracking / stale cleanup ──
from ._registry import (
    mark_poll, get_connected_clients, is_extension_connected, _cleanup_stale,
    mark_locked_out, get_locked_out_clients,
)

# ── Command dispatch & resolution (SYNC + ASYNC) ──
from ._dispatch import (
    send_browser_command, get_pending_commands,
    wait_for_commands, wait_for_commands_async,
    resolve_command, resolve_batch,
)

__all__ = [
    'mark_poll', 'get_connected_clients', 'send_browser_command',
    'get_pending_commands', 'wait_for_commands', 'wait_for_commands_async',
    'resolve_command', 'resolve_batch', 'is_extension_connected',
    'mark_locked_out', 'get_locked_out_clients',
    '_set_active_client', '_get_active_client',
    '_last_poll_time', '_commands', '_commands_lock',
]
