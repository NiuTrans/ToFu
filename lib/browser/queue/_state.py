"""lib/browser/queue/_state.py — Process-wide shared state for the command queue.

This module is the SINGLE HOME for every mutable module-level object the
browser command queue relies on: the command dict, its lock, the SYNC notify
Event, the async-waiter registry, the per-client registry, the legacy global
poll time, and the thread-local active client. Every other submodule reads and
mutates THESE objects (by reference / via this module) so the process has
exactly one queue — a divergent copy would drop in-flight browser commands or
lose client registration.

``_last_poll_time`` is REBOUND (not mutated in place) by ``mark_poll`` and
``wait_for_commands``. To preserve a single home for it those functions rebind
it as ``_state._last_poll_time = now`` (module attribute) rather than via a
per-file ``global`` — so there is exactly one binding in the process.
"""

import os as _os
import threading

from lib.log import get_logger

logger = get_logger(__name__)

# ══════════════════════════════════════════
#  Command Queue — Per-Client Routing
# ══════════════════════════════════════════

_commands = {}          # cmd_id → {id, type, params, event, result, error, created_at, picked_up, target_client, timeout, cancelled}
_commands_lock = threading.Lock()
_notify = threading.Event()   # Signaled when a new command is added (SYNC waiters)

# ── Async-waiter registry (for async def poll routes) ──────────────────
# An async poll handler runs ON the event loop, so it cannot block on the
# threading.Event without pinning a worker thread (the whole point of the
# async route). Instead each async waiter registers an asyncio.Event here;
# the SYNC enqueue path (send_browser_command, on a tool thread) wakes them
# via loop.call_soon_threadsafe — the only thread-safe way to touch an
# asyncio.Event from outside its loop. Each waiter is
#   {'loop':, 'event':, 'client_id':}
# and is responsible for removing ITSELF in a finally block (covers the
# timeout, success, AND CancelledError/disconnect paths) so nothing leaks.
_async_waiters = []           # list[dict]
_async_waiters_lock = threading.Lock()


def _wake_async_waiters(client_id=None):
    """Wake async poll waiters after a command was enqueued (called sync).

    Wakes a waiter when the new command could be FOR it: unrouted commands
    (client_id is None) wake everyone; a client-targeted command wakes only
    the matching waiter and the anonymous (client_id-less) waiters. The
    waiter re-checks get_pending_commands after waking, so an over-broad
    wake is merely a harmless spurious loop, never a mis-delivery.
    """
    with _async_waiters_lock:
        waiters = list(_async_waiters)
    for w in waiters:
        wcid = w.get('client_id')
        if client_id and wcid and wcid != client_id:
            continue
        loop, event = w.get('loop'), w.get('event')
        if loop is None or event is None:
            continue
        try:
            loop.call_soon_threadsafe(event.set)
        except RuntimeError as e:
            # Loop already closed (handler torn down between snapshot and
            # wake). The waiter's finally has/will deregister it; ignore.
            logger.debug('[Browser] async waiter wake skipped (loop closed): %s', e)

# Grace period (seconds) a command lingers in the queue PAST its caller's
# timeout before _cleanup_stale forcibly evicts it. The caller has already
# given up by then; the grace only lets a near-miss result resolve without a
# KeyError. Delivery itself is cut off at exactly the caller's timeout (see
# get_pending_commands) so a command never executes after the model moved on.
_STALE_GRACE = 15

# Long-poll wait window (seconds) the async poll route blocks for before
# returning empty so the extension re-polls. Env-overridable (e.g. tests set a
# small value); MUST stay < the extension's FETCH_TIMEOUT (12s) so the server
# replies before the client aborts.
try:
    POLL_WAIT_TIMEOUT = float(_os.environ.get('TOFU_BROWSER_POLL_WAIT', '8'))
except (ValueError, TypeError) as _e:
    logger.debug('[Browser] bad TOFU_BROWSER_POLL_WAIT %r (%s) — using 8.0s default',
                 _os.environ.get('TOFU_BROWSER_POLL_WAIT'), _e)
    POLL_WAIT_TIMEOUT = 8.0

# Per-client tracking: client_id → {last_poll, first_seen, name}
_clients = {}           # client_id → metadata dict
_clients_lock = threading.Lock()

# ── Locked-out fleet registry (2026-08-04, stranded-extension fix) ──
# A poll that dies at the bridge-auth gate carries a stale/revoked
# credential — an INSTALLED extension that can never heal itself (a
# side-loaded extension has no update channel and a 401-parked one cannot
# poll). Recording who knocked here lets the panel tell "installed but
# locked out" from "never installed" and offer the one-click re-download.
# Entries carry last_seen + ext_version + fail_count; reads filter by TTL
# (the parked 5-min probe keeps a live stranded client fresh), writes are
# capacity-capped.
_locked_out = {}        # client_id → {first_seen, last_seen, ext_version, fail_count}
_locked_out_lock = threading.Lock()

# Legacy global poll time (kept for backward compat with is_extension_connected).
# REBOUND by mark_poll / wait_for_commands via ``_state._last_poll_time = ...``.
_last_poll_time = 0


# ── Thread-local active client for per-device routing ──
_active_client = threading.local()

def _set_active_client(client_id):
    """Set the active browser client ID for the current thread."""
    _active_client.client_id = client_id

def _get_active_client():
    """Get the active browser client ID for the current thread, or None."""
    return getattr(_active_client, 'client_id', None)
