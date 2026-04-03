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

import threading
import time
import uuid

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'mark_poll', 'get_connected_clients', 'send_browser_command',
    'get_pending_commands', 'wait_for_commands', 'resolve_command',
    'resolve_batch', 'is_extension_connected',
    '_set_active_client', '_get_active_client',
    '_last_poll_time', '_commands', '_commands_lock',
]

# ══════════════════════════════════════════
#  Command Queue — Per-Client Routing
# ══════════════════════════════════════════

_commands = {}          # cmd_id → {id, type, params, event, result, error, created_at, picked_up, target_client}
_commands_lock = threading.Lock()
_notify = threading.Event()   # Signaled when a new command is added

# Per-client tracking: client_id → {last_poll, first_seen, name}
_clients = {}           # client_id → metadata dict
_clients_lock = threading.Lock()

# Legacy global poll time (kept for backward compat with is_extension_connected)
_last_poll_time = 0


def mark_poll(client_id=None):
    """Record a poll from a client (or anonymous legacy client)."""
    global _last_poll_time
    now = time.time()
    _last_poll_time = now
    if client_id:
        with _clients_lock:
            if client_id not in _clients:
                _clients[client_id] = {'first_seen': now, 'last_poll': now, 'name': '', 'poll_count': 1}
                logger.info('[Browser] New client registered: %s (total clients: %d)',
                            client_id[:12], len(_clients))
            else:
                _clients[client_id]['last_poll'] = now
                _clients[client_id]['poll_count'] = _clients[client_id].get('poll_count', 0) + 1


def get_connected_clients():
    """Return list of currently connected client dicts."""
    now = time.time()
    with _clients_lock:
        return [
            {'client_id': cid, 'last_poll': info['last_poll'],
             'seconds_ago': round(now - info['last_poll'], 1),
             'name': info.get('name', ''),
             'poll_count': info.get('poll_count', 0),
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
    }
    with _commands_lock:
        _commands[cmd_id] = cmd
    _notify.set()

    if not event.wait(timeout=timeout):
        with _commands_lock:
            timed_out_cmd = _commands.pop(cmd_id, None)
        picked = timed_out_cmd.get('picked_up', False) if timed_out_cmd else False
        url_hint = ''
        if timed_out_cmd:
            p = timed_out_cmd.get('params') or {}
            url_hint = p.get('url', '')[:80]
        with _commands_lock:
            pending_count = sum(1 for c in _commands.values() if not c.get('picked_up'))
            total_count = len(_commands)
        logger.warning('[Browser] Command %s timed out after %ds (client=%s, picked_up=%s, '
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
            if cmd.get('picked_up'):
                continue
            if now - cmd['created_at'] > 60:
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
        stale = [cid for cid, cmd in _commands.items() if now - cmd['created_at'] > 90]
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
