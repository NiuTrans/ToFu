"""lib/browser/queue/_state.py — Process-wide shared state for the command queue.

This module is the single home for every mutable object the browser command
queue relies on: the command dict, locks/events, async-waiter registry, and
per-client registry. Every other module reads and
mutates THESE objects (by reference / via this module) so the process has
exactly one queue — a divergent copy would drop in-flight browser commands or
lose client registration.
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
#   {'loop':, 'event':, 'client_id':, 'owner_user_id':}
# and is responsible for removing ITSELF in a finally block (covers the
# timeout, success, AND CancelledError/disconnect paths) so nothing leaks.
_async_waiters = []           # list[dict]
_async_waiters_lock = threading.Lock()


def _wake_async_waiters(client_id, owner_user_id):
    """Wake async poll waiters after a command was enqueued (called sync).

    Only the exact owner/device poller is eligible for the command.
    """
    with _async_waiters_lock:
        waiters = list(_async_waiters)
    for w in waiters:
        if (w.get('client_id') != client_id
                or w.get('owner_user_id') != owner_user_id):
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
_locked_out = {}        # (owner_user_id, client_id) → recovery metadata
_locked_out_lock = threading.Lock()

# ── Incompatible fleet registry ───────────────────────────────────────
# An authenticated poll can still fail the protocol/capability handshake.
# Such a device must never enter the command registry, but it is materially
# different from "not installed": the owner needs an upgrade action. Keep a
# separate bounded recovery note so status/UI can report that truth without
# weakening protocol negotiation or retaining unbounded request history.
_incompatible_clients = {}  # (owner_user_id, client_id) → recovery metadata
_incompatible_clients_lock = threading.Lock()
