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
from ._state import (
    _clients, _clients_lock, _commands, _commands_lock, _STALE_GRACE,
    _locked_out, _locked_out_lock,
)

logger = get_logger(__name__)

# Locked-out entry freshness window (seconds). Read at CALL time on purpose
# (never as a default arg — the monkeypatch-default-binding trap). The
# parked 5-minute probe of a stranded extension keeps its entry fresh; once
# it stops knocking, the note lingers one more grace span so a panel open
# right after still sees it, then disappears.
_LOCKED_OUT_TTL_S = 900
_LOCKED_OUT_MAX = 32


def mark_poll(client_id=None, chrome_major=0, user_id='', ext_version='',
              protocol_version=None, capabilities=None, profile=''):
    """Record a poll from a client (or anonymous legacy client).

    Args:
        client_id: Stable per-device extension id, or None for a legacy client.
        chrome_major: Chromium major version reported by the extension (0 if
            unknown). Stored so the UI can surface Chrome 142+ Local Network
            Access prompt guidance for the browser actually running the bridge.
        user_id: The bridge caller this poll authenticated as. Mirrors the
            desktop bridge's per-user scoping (``lib/desktop/bridge.py``):
            without it a multi-tenant relay lets tenant A's extension collect
            tenant B's commands — and a browser command can read cookies and
            attach the debugger, so that is a session-takeover primitive.
            ``''`` = unscoped (single-user deployment / legacy global secret).
        ext_version: The extension's own manifest version (2026-08-04).
            Compared against the version the server would serve, this is how
            the panel tells an outdated-but-working install from a current
            one. A poll that SUCCEEDED also clears any locked-out note for
            the client — the cure (re-downloaded preseeded zip) arrived.
    """
    from lib.browser.protocol import normalize_capabilities

    now = time.time()
    try:
        negotiated_version = max(1, int(protocol_version or 1))
    except (ValueError, TypeError) as exc:
        logger.debug('[Browser] invalid protocol version; using v1: %s', exc)
        negotiated_version = 1
    negotiated_caps = sorted(normalize_capabilities(
        capabilities, protocol_version=negotiated_version))
    _state._last_poll_time = now
    if client_id:
        with _clients_lock:
            if client_id not in _clients:
                _clients[client_id] = {'first_seen': now, 'last_poll': now, 'name': '',
                                       'poll_count': 1, 'chrome_major': chrome_major or 0,
                                       'user_id': str(user_id or ''),
                                       'ext_version': str(ext_version or ''),
                                       'protocol_version': negotiated_version,
                                       'capabilities': negotiated_caps,
                                       'profile': str(profile or '')[:80]}
                logger.info('[Browser] New client registered: %s (total clients: %d)',
                            client_id[:12], len(_clients))
            else:
                _clients[client_id]['last_poll'] = now
                _clients[client_id]['poll_count'] = _clients[client_id].get('poll_count', 0) + 1
                if chrome_major:
                    _clients[client_id]['chrome_major'] = chrome_major
                if ext_version:
                    _clients[client_id]['ext_version'] = str(ext_version)
                # wait_for_commands() records the same poll a second time
                # without protocol fields. Preserve the capability handshake
                # from the HTTP entry instead of downgrading it to v1.
                if protocol_version is not None or capabilities is not None:
                    _clients[client_id]['protocol_version'] = negotiated_version
                    _clients[client_id]['capabilities'] = negotiated_caps
                if profile:
                    _clients[client_id]['profile'] = str(profile)[:80]
                # Re-registration may arrive on a different credential; the
                # latest authenticated identity wins (same as desktop).
                _clients[client_id]['user_id'] = str(user_id or '')
        with _locked_out_lock:
            _locked_out.pop(client_id, None)


def mark_locked_out(client_id, ext_version=''):
    """Record a poll that DIED at the bridge-auth gate (2026-08-04).

    A 401 answered by Tofu's own gate (never by a proxy — those never reach
    this process) means an installed extension holding a stale/revoked
    credential. It cannot heal itself: side-loaded extensions have no update
    channel, and a parked 401 client cannot poll. This note is the stranded
    fleet's only distress signal — the panel turns it into a one-click
    re-download (the preseeded zip pairs with zero input). Anonymous
    (client_id-less) knockers cannot be attributed and are not recorded.
    """
    if not client_id:
        return
    now = time.time()
    with _locked_out_lock:
        ent = _locked_out.get(client_id)
        if ent is None:
            if len(_locked_out) >= _LOCKED_OUT_MAX:
                oldest = min(_locked_out,
                             key=lambda k: _locked_out[k]['last_seen'])
                _locked_out.pop(oldest, None)
            _locked_out[client_id] = {
                'first_seen': now, 'last_seen': now,
                'ext_version': str(ext_version or ''), 'fail_count': 1}
            logger.info('[Browser] locked-out client recorded: %s '
                        '(ext %s)', client_id[:12], ext_version or '?')
        else:
            ent['last_seen'] = now
            ent['fail_count'] = ent.get('fail_count', 0) + 1
            if ext_version:
                ent['ext_version'] = str(ext_version)


def get_locked_out_clients():
    """Fresh locked-out notes, newest first. TTL-filtered at READ time."""
    now = time.time()
    with _locked_out_lock:
        rows = [
            {'client_id': cid,
             'ext_version': info.get('ext_version', ''),
             'fail_count': info.get('fail_count', 0),
             'seconds_ago': round(now - info['last_seen'], 1)}
            for cid, info in _locked_out.items()
            if now - info['last_seen'] < _LOCKED_OUT_TTL_S
        ]
    rows.sort(key=lambda r: r['seconds_ago'])
    return rows


def client_user_id(client_id):
    """Return the bridge user a registered client authenticated as ('' if none)."""
    if not client_id:
        return ''
    with _clients_lock:
        info = _clients.get(client_id)
    return str((info or {}).get('user_id') or '')


def get_connected_clients(user_id=None):
    """Return list of currently connected client dicts.

    ``user_id`` (B0): when given, only clients registered by that bridge
    caller are returned — a tenant must never see another tenant's browsers.
    ``None`` = unfiltered operator view.
    """
    now = time.time()
    with _clients_lock:
        out = [
            {'client_id': cid, 'last_poll': info['last_poll'],
             'seconds_ago': round(now - info['last_poll'], 1),
             'name': info.get('name', ''),
             'poll_count': info.get('poll_count', 0),
             'chrome_major': info.get('chrome_major', 0),
             'first_seen': info.get('first_seen', 0),
             'user_id': info.get('user_id', ''),
             'ext_version': info.get('ext_version', ''),
             'protocol_version': int(info.get('protocol_version') or 1),
             'capabilities': list(info.get('capabilities') or []),
             'profile': info.get('profile', '')}
            for cid, info in _clients.items()
            if now - info['last_poll'] < 15
        ]
    if user_id is not None:
        out = [c for c in out if (c.get('user_id') or '') == (user_id or '')]
    return out


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
