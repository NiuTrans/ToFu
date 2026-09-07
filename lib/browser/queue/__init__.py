"""Owner/device-addressed command queue for the Chromium extension.

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

The process-wide queue/registry state (``_commands`` and ``_clients``) lives
in ``_state`` and is shared by reference across the
submodules — there is exactly one queue/registry in the process. The
single POST poll settles and claims commands for one authenticated owner/device.
"""

from lib.log import get_logger

logger = get_logger(__name__)

# ── Shared state (single home) ──
from ._state import (
    _commands, _commands_lock, _notify,
    _async_waiters, _async_waiters_lock, _wake_async_waiters,
    _clients, _clients_lock, _STALE_GRACE, POLL_WAIT_TIMEOUT,
)

# ── Client registry / poll tracking / stale cleanup ──
from ._registry import (
    mark_poll, get_connected_clients, is_extension_connected, _cleanup_stale,
    mark_locked_out, get_locked_out_clients, client_owner_user_id,
    mark_incompatible_client, get_incompatible_clients,
)

# ── Command dispatch & resolution (SYNC + ASYNC) ──
from ._dispatch import (
    send_browser_command, get_pending_commands,
    wait_for_commands, wait_for_commands_async,
    resolve_command, resolve_batch,
)
from ._limits import (
    BrowserPollCapacityExceeded,
    MAX_COMMANDS_PER_POLL,
    MAX_RESULTS_PER_POLL,
)

__all__ = [
    'mark_poll', 'get_connected_clients', 'send_browser_command',
    'get_pending_commands', 'wait_for_commands', 'wait_for_commands_async',
    'resolve_command', 'resolve_batch', 'is_extension_connected',
    'mark_locked_out', 'get_locked_out_clients',
    'mark_incompatible_client', 'get_incompatible_clients',
    'client_owner_user_id',
    'BrowserPollCapacityExceeded',
    'MAX_COMMANDS_PER_POLL', 'MAX_RESULTS_PER_POLL',
    '_commands', '_commands_lock',
]
