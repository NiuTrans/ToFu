"""lib/browser/queue.py — Command queue infrastructure for Chrome Extension.

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
"""

import asyncio
import threading
import time
import uuid

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'mark_poll', 'get_connected_clients', 'send_browser_command',
    'get_pending_commands', 'wait_for_commands', 'wait_for_commands_async',
    'resolve_command', 'resolve_batch', 'is_extension_connected',
    '_set_active_client', '_get_active_client',
    '_last_poll_time', '_commands', '_commands_lock',
]

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
import os as _os
try:
    POLL_WAIT_TIMEOUT = float(_os.environ.get('TOFU_BROWSER_POLL_WAIT', '8'))
except (ValueError, TypeError):
    POLL_WAIT_TIMEOUT = 8.0

# Per-client tracking: client_id → {last_poll, first_seen, name}
_clients = {}           # client_id → metadata dict
_clients_lock = threading.Lock()

# Legacy global poll time (kept for backward compat with is_extension_connected)
_last_poll_time = 0


def mark_poll(client_id=None, chrome_major=0):
    """Record a poll from a client (or anonymous legacy client).

    Args:
        client_id: Stable per-device extension id, or None for a legacy client.
        chrome_major: Chromium major version reported by the extension (0 if
            unknown). Stored so the UI can surface Chrome 142+ Local Network
            Access prompt guidance for the browser actually running the bridge.
    """
    global _last_poll_time
    now = time.time()
    _last_poll_time = now
    if client_id:
        with _clients_lock:
            if client_id not in _clients:
                _clients[client_id] = {'first_seen': now, 'last_poll': now, 'name': '',
                                       'poll_count': 1, 'chrome_major': chrome_major or 0}
                logger.info('[Browser] New client registered: %s (total clients: %d)',
                            client_id[:12], len(_clients))
            else:
                _clients[client_id]['last_poll'] = now
                _clients[client_id]['poll_count'] = _clients[client_id].get('poll_count', 0) + 1
                if chrome_major:
                    _clients[client_id]['chrome_major'] = chrome_major


def get_connected_clients():
    """Return list of currently connected client dicts."""
    now = time.time()
    with _clients_lock:
        return [
            {'client_id': cid, 'last_poll': info['last_poll'],
             'seconds_ago': round(now - info['last_poll'], 1),
             'name': info.get('name', ''),
             'poll_count': info.get('poll_count', 0),
             'chrome_major': info.get('chrome_major', 0),
             'first_seen': info.get('first_seen', 0)}
            for cid, info in _clients.items()
            if now - info['last_poll'] < 15
        ]


def send_browser_command(cmd_type, params=None, timeout=30, client_id=None):
    """Send a command to a specific browser extension client and block until result.

    Args:
        cmd_type: Command type string.
        params: Command parameters dict.
        timeout: Max seconds to wait for result.
        client_id: Target client ID. If None, falls back to thread-local active
                   client, then to any connected client.
    """
    # Auto-resolve client_id from thread-local if not explicitly provided
    if not client_id:
        client_id = _get_active_client()
    logger.info('[Browser] Sending command %s (timeout=%ds, target_client=%s)',
                cmd_type, timeout, (client_id or 'any')[:12])

    # Check if the target client (or any client) is connected
    if client_id:
        with _clients_lock:
            info = _clients.get(client_id)
        if not info or time.time() - info['last_poll'] > 30:
            logger.warning('[Browser] Target client %s not connected', client_id[:12])
            return None, (f"Browser extension client {client_id[:8]} is not connected. "
                          "Check that the extension is running on the correct device.")
    else:
        if time.time() - _last_poll_time > 30:
            logger.warning('[Browser] No extension connected (last poll %.0fs ago)',
                           time.time() - _last_poll_time)
            return None, ("Browser extension is not connected. "
                          "Install the extension and enable it.")

    _cleanup_stale()

    cmd_id = str(uuid.uuid4())
    event = threading.Event()
    cmd = {
        'id': cmd_id,
        'type': cmd_type,
        'params': params or {},
        'event': event,
        'result': None,
        'error': None,
        'created_at': time.time(),
        'picked_up': False,
        'target_client': client_id,   # None = any client can pick it up
        'timeout': timeout,           # caller's wait budget; delivery cutoff
        'cancelled': False,           # set when the caller gives up (see below)
    }
    with _commands_lock:
        _commands[cmd_id] = cmd
    _notify.set()
    _wake_async_waiters(client_id)

    if not event.wait(timeout=timeout):
        # Caller gave up: mark cancelled (so an in-flight get_pending_commands
        # that raced to pick it up won't hand it to the extension) and remove it.
        with _commands_lock:
            stale = _commands.get(cmd_id)
            if stale is not None:
                stale['cancelled'] = True
            timed_out_cmd = _commands.pop(cmd_id, None)
        picked = timed_out_cmd.get('picked_up', False) if timed_out_cmd else False
        url_hint = ''
        if timed_out_cmd:
            p = timed_out_cmd.get('params') or {}
            url_hint = p.get('url', '')[:80]
        with _commands_lock:
            pending_count = sum(1 for c in _commands.values() if not c.get('picked_up'))
            total_count = len(_commands)
        # 2026-05-05 noise-reduction: command-level timeout is routinely
        # triggered by slow pages / idle extensions; the CALLER (e.g.
        # try_browser_fetch) already logs its own WARNING / INFO on the
        # final giveup path. Log at INFO so error.log isn't flooded with
        # duplicate timeout notices (114+114/day under normal load).
        logger.info('[Browser] Command %s timed out after %ds (client=%s, picked_up=%s, '
                    'pending_queue=%d, total_inflight=%d, url=%s) '
                    '— extension may be overloaded or disconnected',
                    cmd_type, timeout, (client_id or 'any')[:12], picked,
                    pending_count, total_count, url_hint)
        return None, f"Browser command '{cmd_type}' timed out after {timeout}s. The extension may be busy or disconnected."

    with _commands_lock:
        cmd = _commands.pop(cmd_id, cmd)

    if cmd.get('error'):
        logger.warning('[Browser] Command %s returned error: %s', cmd_type, str(cmd['error'])[:200])
        return None, cmd['error']
    return cmd['result'], None


def get_pending_commands(client_id=None):
    """Return list of commands for a specific client (or unrouted commands).

    A command is eligible for a client if:
      - target_client is None (unrouted — any client can pick it up), OR
      - target_client matches the requesting client_id
    """
    now = time.time()
    with _commands_lock:
        pending = []
        for cmd_id, cmd in list(_commands.items()):
            if cmd.get('picked_up') or cmd.get('cancelled'):
                continue
            # Never deliver a command the caller has already given up on: the
            # delivery cutoff is the caller's OWN timeout, not a magic 60s. A
            # command picked up after this would fire a stray click/navigate
            # 30-60s after the model moved on, with its result silently dropped.
            if now - cmd['created_at'] > cmd.get('timeout', 30):
                continue
            # Per-client routing: only deliver commands targeted at this client
            target = cmd.get('target_client')
            if target and client_id and target != client_id:
                continue   # This command is for a different client
            cmd['picked_up'] = True
            pending.append({
                'id': cmd['id'],
                'type': cmd['type'],
                'params': cmd['params'],
            })
    return pending


def wait_for_commands(timeout=8, client_id=None):
    """Block until commands are available for this client, or timeout."""
    global _last_poll_time
    _last_poll_time = time.time()
    mark_poll(client_id)
    _cleanup_stale()

    deadline = time.time() + timeout
    while time.time() < deadline:
        pending = get_pending_commands(client_id=client_id)
        if pending:
            return pending
        _notify.clear()
        remaining = deadline - time.time()
        if remaining > 0:
            _notify.wait(timeout=min(remaining, 1.0))
    return []


async def wait_for_commands_async(timeout=None, client_id=None):
    """Async-native variant of wait_for_commands for ``async def`` poll routes.

    Awaits on an asyncio.Event instead of blocking a thread on the
    threading.Event, so the Hypercorn worker thread is RELEASED for the
    entire (up-to-``timeout``) wait. Commands enqueued from sync tool threads
    wake this via ``_wake_async_waiters`` → ``loop.call_soon_threadsafe``.

    Preserves the exact semantics of the sync path: per-client routing and
    the §3 TTL delivery cutoff both live in ``get_pending_commands``, which
    this calls unchanged. Returns a list of command dicts (possibly empty).
    """
    if timeout is None:
        timeout = POLL_WAIT_TIMEOUT
    mark_poll(client_id)
    _cleanup_stale()

    # Fast path: something is already queued for us.
    pending = get_pending_commands(client_id=client_id)
    if pending:
        return pending

    loop = asyncio.get_running_loop()
    event = asyncio.Event()
    waiter = {'loop': loop, 'event': event, 'client_id': client_id}
    with _async_waiters_lock:
        _async_waiters.append(waiter)
    try:
        deadline = time.time() + timeout
        while time.time() < deadline:
            event.clear()
            pending = get_pending_commands(client_id=client_id)
            if pending:
                return pending
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            # Cap each await slice so a missed wake (e.g. command enqueued in
            # the tiny window between get_pending_commands and event.clear())
            # still gets re-checked promptly, mirroring the sync 1.0s cap.
            try:
                await asyncio.wait_for(event.wait(), timeout=min(remaining, 1.0))
            except asyncio.TimeoutError:
                pass  # slice elapsed — loop re-checks the queue
        # Final check after the loop in case a command landed at the deadline.
        return get_pending_commands(client_id=client_id)
    finally:
        # ALWAYS deregister — covers timeout, success, and CancelledError
        # (client disconnected mid-wait). Without this the registry leaks a
        # dead loop/event on every disconnect.
        with _async_waiters_lock:
            try:
                _async_waiters.remove(waiter)
            except ValueError:
                pass


def resolve_command(cmd_id, result=None, error=None):
    """Resolve a command result, unblocking the waiting send_browser_command."""
    with _commands_lock:
        cmd = _commands.get(cmd_id)
    if not cmd:
        return False
    cmd['result'] = result
    cmd['error'] = error
    cmd['event'].set()
    return True


def resolve_batch(results):
    """Resolve multiple command results at once. Returns count of resolved."""
    resolved = 0
    for r in (results or []):
        cmd_id = r.get('id', '')
        if not cmd_id:
            continue
        if resolve_command(cmd_id, result=r.get('result'), error=r.get('error')):
            resolved += 1
    return resolved


def is_extension_connected(client_id=None):
    """Check if any extension (or a specific client) is connected."""
    if client_id:
        with _clients_lock:
            info = _clients.get(client_id)
        if not info:
            return False
        return time.time() - info['last_poll'] < 15
    return time.time() - _last_poll_time < 15


# ── Thread-local active client for per-device routing ──
_active_client = threading.local()

def _set_active_client(client_id):
    """Set the active browser client ID for the current thread."""
    _active_client.client_id = client_id

def _get_active_client():
    """Get the active browser client ID for the current thread, or None."""
    return getattr(_active_client, 'client_id', None)


def _cleanup_stale():
    """Remove expired commands and stale clients."""
    now = time.time()
    with _commands_lock:
        stale = [cid for cid, cmd in _commands.items()
                 if now - cmd['created_at'] > cmd.get('timeout', 30) + _STALE_GRACE]
        for cid in stale:
            cmd = _commands.pop(cid, None)
            if cmd and cmd.get('event') and not cmd['event'].is_set():
                cmd['error'] = 'Command expired (stale cleanup)'
                cmd['event'].set()
    # Also clean up clients that haven't polled in > 5 minutes
    with _clients_lock:
        stale_clients = [cid for cid, info in _clients.items()
                         if now - info['last_poll'] > 300]
        for cid in stale_clients:
            info = _clients.pop(cid, {})
            logger.info('[Browser] Cleaned up stale client %s (polls=%d, last_poll=%.0fs ago)',
                        cid[:12], info.get('poll_count', 0), now - info.get('last_poll', now))
