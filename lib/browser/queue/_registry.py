"""lib/browser/queue/_clients.py — Client registry, poll tracking, stale cleanup.

Holds the connection-facing functions: ``mark_poll`` (records a poll and
rebinds the process-wide ``_last_poll_time``), ``get_connected_clients``,
``is_extension_connected``, and ``_cleanup_stale`` (evicts expired commands
AND stale clients). All shared state lives in ``_state`` and is touched
through that module so the process keeps a single queue/registry.
"""

import time

from lib.log import get_logger

from . import _state
from ._state import _clients, _clients_lock, _commands, _commands_lock, _STALE_GRACE

logger = get_logger(__name__)


def mark_poll(client_id=None, chrome_major=0):
    """Record a poll from a client (or anonymous legacy client).

    Args:
        client_id: Stable per-device extension id, or None for a legacy client.
        chrome_major: Chromium major version reported by the extension (0 if
            unknown). Stored so the UI can surface Chrome 142+ Local Network
            Access prompt guidance for the browser actually running the bridge.
    """
    now = time.time()
    _state._last_poll_time = now
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


def is_extension_connected(client_id=None):
    """Check if any extension (or a specific client) is connected."""
    if client_id:
        with _clients_lock:
            info = _clients.get(client_id)
        if not info:
            return False
        return time.time() - info['last_poll'] < 15
    return time.time() - _state._last_poll_time < 15


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
