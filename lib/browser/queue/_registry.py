"""Owner/device browser client registry and stale cleanup.

Every registered client has a stable device ID, authenticated owner, current
protocol, and explicit capability set. Anonymous or downgraded polls never
enter the registry.
"""

import time

from lib.log import get_logger

from ._state import (
    _clients, _clients_lock, _commands, _commands_lock, _STALE_GRACE,
    _locked_out, _locked_out_lock,
    _incompatible_clients, _incompatible_clients_lock,
)
from ._limits import BrowserPollCapacityExceeded, client_registry_limits

logger = get_logger(__name__)

# Locked-out entry freshness window (seconds). Read at CALL time on purpose
# (never as a default arg — the monkeypatch-default-binding trap). The
# parked 5-minute probe of a stranded extension keeps its entry fresh; once
# it stops knocking, the note lingers one more grace span so a panel open
# right after still sees it, then disappears.
_LOCKED_OUT_TTL_S = 900
_LOCKED_OUT_MAX = 32
_INCOMPATIBLE_TTL_S = 900
_INCOMPATIBLE_MAX = 32
_CLIENT_REGISTRY_MAX, _CLIENT_REGISTRY_PER_OWNER = client_registry_limits()


def _evict_inactive_client_locked(*, owner_user_id: str | None, now: float) -> bool:
    """Evict one disconnected LRU row; never displace a live browser."""
    candidates = [
        (info.get('last_poll', 0.0), client_id)
        for client_id, info in _clients.items()
        if now - info.get('last_poll', 0.0) >= 15
        and (owner_user_id is None
             or info.get('owner_user_id') == owner_user_id)
    ]
    if not candidates:
        return False
    _last_poll, client_id = min(candidates)
    _clients.pop(client_id, None)
    return True


def _reserve_client_registry_slot_locked(
    client_id: str,
    *,
    owner_user_id: str,
    now: float,
) -> None:
    """Keep recent-client state bounded without harming connected devices."""
    existing = _clients.get(client_id)
    joining_owner = (
        existing is None
        or existing.get('owner_user_id') != owner_user_id
    )
    if joining_owner:
        owner_count = sum(
            1 for info in _clients.values()
            if info.get('owner_user_id') == owner_user_id
        )
        if owner_count >= _CLIENT_REGISTRY_PER_OWNER and not (
                _evict_inactive_client_locked(
                    owner_user_id=owner_user_id, now=now)):
            raise BrowserPollCapacityExceeded(
                'browser_client_owner_capacity',
                'This owner has too many concurrently active browser devices.',
            )
    if existing is None and len(_clients) >= _CLIENT_REGISTRY_MAX and not (
            _evict_inactive_client_locked(owner_user_id=None, now=now)):
        raise BrowserPollCapacityExceeded(
            'browser_client_registry_capacity',
            'The browser client registry is at its active-device capacity.',
        )


def mark_poll(client_id, *, owner_user_id, protocol_version, capabilities,
              chrome_major=0, ext_version='', profile=''):
    """Validate and record one authenticated current-protocol poll.

    Args:
        client_id: Stable per-device extension id.
        chrome_major: Chromium major version reported by the extension (0 if
            unknown). Stored so the UI can surface Chrome 142+ Local Network
            Access prompt guidance for the browser actually running the bridge.
        owner_user_id: The bridge caller this poll authenticated as. Mirrors the
            desktop bridge's per-user scoping (``lib/desktop/bridge.py``):
            without it a multi-tenant relay lets tenant A's extension collect
            tenant B's commands — and a browser command can read cookies and
            attach the debugger, so that is a session-takeover primitive.
            HTTP callers always provide a positive numeric owner string.
        ext_version: The extension's own manifest version (2026-08-04).
            Compared against the version the server would serve, this is how
            the panel tells an outdated-but-working install from a current
            one. A poll that SUCCEEDED also clears any locked-out note for
            the client — the cure (re-downloaded preseeded zip) arrived.
    """
    from lib.browser.protocol import (
        normalize_capabilities,
        normalize_protocol_version,
    )

    now = time.time()
    client_id = str(client_id or '').strip()
    owner_user_id = str(owner_user_id or '').strip()
    if not client_id or len(client_id) > 128:
        raise ValueError('client_id must be a non-empty stable device ID')
    if not owner_user_id.isdigit() or int(owner_user_id) < 1:
        raise ValueError('owner_user_id must be a positive integer')
    negotiated_version = normalize_protocol_version(protocol_version)
    negotiated_caps = sorted(normalize_capabilities(
        capabilities, protocol_version=negotiated_version))
    with _clients_lock:
        _reserve_client_registry_slot_locked(
            client_id, owner_user_id=owner_user_id, now=now)
        if client_id not in _clients:
            _clients[client_id] = {
                'first_seen': now,
                'last_poll': now,
                'name': '',
                'poll_count': 1,
                'chrome_major': chrome_major or 0,
                'owner_user_id': owner_user_id,
                'ext_version': str(ext_version or ''),
                'protocol_version': negotiated_version,
                'capabilities': negotiated_caps,
                'profile': str(profile or '')[:80],
            }
            logger.info('[Browser] New client registered: %s (total clients: %d)',
                        client_id[:12], len(_clients))
        else:
            row = _clients[client_id]
            row['last_poll'] = now
            row['poll_count'] = row.get('poll_count', 0) + 1
            if chrome_major:
                row['chrome_major'] = chrome_major
            if ext_version:
                row['ext_version'] = str(ext_version)
            row['protocol_version'] = negotiated_version
            row['capabilities'] = negotiated_caps
            row['profile'] = str(profile or '')[:80]
            # Re-pairing intentionally transfers this stable device address.
            # Existing commands remain owner-scoped and therefore cannot cross.
            row['owner_user_id'] = owner_user_id
    with _locked_out_lock:
        for recovery_key in [
            key for key in _locked_out if key[1] == client_id
        ]:
            _locked_out.pop(recovery_key, None)
    with _incompatible_clients_lock:
        _incompatible_clients.pop((owner_user_id, client_id), None)


def mark_locked_out(client_id, *, owner_user_id, ext_version=''):
    """Record a poll that DIED at the bridge-auth gate (2026-08-04).

    A 401 answered by Tofu's own gate (never by a proxy — those never reach
    this process) means an installed extension holding a stale/revoked
    credential. It cannot heal itself: side-loaded extensions have no update
    channel, and a parked 401 client cannot poll. This note is the stranded
    fleet's only distress signal — the panel turns it into a one-click
    re-download (the preseeded zip pairs with zero input). Anonymous
    (client_id-less) knockers cannot be attributed and are not recorded.
    """
    client_id = str(client_id or '').strip()
    owner_user_id = str(owner_user_id or '').strip()
    if not client_id:
        return
    if not owner_user_id.isdigit() or int(owner_user_id) < 1:
        raise ValueError('owner_user_id must be a positive integer')
    now = time.time()
    recovery_key = (owner_user_id, client_id)
    with _locked_out_lock:
        ent = _locked_out.get(recovery_key)
        if ent is None:
            if len(_locked_out) >= _LOCKED_OUT_MAX:
                oldest = min(_locked_out,
                             key=lambda k: _locked_out[k]['last_seen'])
                _locked_out.pop(oldest, None)
            _locked_out[recovery_key] = {
                'first_seen': now, 'last_seen': now,
                'ext_version': str(ext_version or ''), 'fail_count': 1}
            logger.info('[Browser] locked-out client recorded: %s '
                        '(ext %s)', client_id[:12], ext_version or '?')
        else:
            ent['last_seen'] = now
            ent['fail_count'] = ent.get('fail_count', 0) + 1
            if ext_version:
                ent['ext_version'] = str(ext_version)


def get_locked_out_clients(*, owner_user_id):
    """Return fresh recovery notes for exactly one authenticated owner."""
    owner_user_id = str(owner_user_id or '').strip()
    if not owner_user_id.isdigit() or int(owner_user_id) < 1:
        raise ValueError('owner_user_id must be a positive integer')
    now = time.time()
    with _locked_out_lock:
        rows = [
            {'client_id': cid,
             'ext_version': info.get('ext_version', ''),
             'fail_count': info.get('fail_count', 0),
             'seconds_ago': round(now - info['last_seen'], 1)}
            for (owner, cid), info in _locked_out.items()
            if owner == owner_user_id
            and now - info['last_seen'] < _LOCKED_OUT_TTL_S
        ]
    rows.sort(key=lambda r: r['seconds_ago'])
    return rows


def mark_incompatible_client(
    client_id,
    *,
    owner_user_id,
    ext_version='',
    protocol_version=0,
    reason='',
):
    """Record an authenticated device rejected by the strict handshake.

    Returns ``True`` only for the first sighting (or a changed rejection
    reason), allowing the HTTP boundary to emit one actionable warning rather
    than one INFO record per retry. The registry is owner-scoped, TTL-filtered
    and capacity-capped; rejected devices never become command authorities.
    """
    client_id = str(client_id or '').strip()
    owner_user_id = str(owner_user_id or '').strip()
    if not client_id or len(client_id) > 128:
        return False
    if not owner_user_id.isdigit() or int(owner_user_id) < 1:
        raise ValueError('owner_user_id must be a positive integer')
    try:
        reported_protocol = int(protocol_version or 0)
    except (TypeError, ValueError):
        reported_protocol = 0
    bounded_reason = str(reason or 'incompatible browser protocol')[:240]
    recovery_key = (owner_user_id, client_id)
    now = time.time()
    with _incompatible_clients_lock:
        entry = _incompatible_clients.get(recovery_key)
        should_log = entry is None or entry.get('reason') != bounded_reason
        if entry is None:
            if len(_incompatible_clients) >= _INCOMPATIBLE_MAX:
                oldest = min(
                    _incompatible_clients,
                    key=lambda key: _incompatible_clients[key]['last_seen'],
                )
                _incompatible_clients.pop(oldest, None)
            entry = {
                'first_seen': now,
                'last_seen': now,
                'ext_version': str(ext_version or '')[:32],
                'protocol_version': reported_protocol,
                'reason': bounded_reason,
                'fail_count': 1,
            }
            _incompatible_clients[recovery_key] = entry
        else:
            entry['last_seen'] = now
            entry['fail_count'] = entry.get('fail_count', 0) + 1
            entry['protocol_version'] = reported_protocol
            entry['reason'] = bounded_reason
            if ext_version:
                entry['ext_version'] = str(ext_version)[:32]
    return should_log


def get_incompatible_clients(*, owner_user_id):
    """Return fresh handshake-rejection notes for one authenticated owner."""
    owner_user_id = str(owner_user_id or '').strip()
    if not owner_user_id.isdigit() or int(owner_user_id) < 1:
        raise ValueError('owner_user_id must be a positive integer')
    now = time.time()
    with _incompatible_clients_lock:
        rows = [
            {
                'client_id': client_id,
                'ext_version': info.get('ext_version', ''),
                'protocol_version': int(info.get('protocol_version') or 0),
                'reason': info.get('reason', ''),
                'fail_count': info.get('fail_count', 0),
                'seconds_ago': round(now - info['last_seen'], 1),
            }
            for (owner, client_id), info in _incompatible_clients.items()
            if owner == owner_user_id
            and now - info['last_seen'] < _INCOMPATIBLE_TTL_S
        ]
    rows.sort(key=lambda row: row['seconds_ago'])
    return rows


def client_owner_user_id(client_id):
    """Return the authenticated owner for one registered client."""
    if not client_id:
        return ''
    with _clients_lock:
        info = _clients.get(client_id)
    return str((info or {}).get('owner_user_id') or '')


def get_connected_clients(*, owner_user_id: str | None):
    """Return list of currently connected client dicts.

    ``owner_user_id``: when given, only clients registered by that bridge
    caller are returned — a tenant must never see another tenant's browsers.
    Callers needing the unfiltered operator view must pass ``None`` explicitly;
    omitting identity is never an implicit authority fallback.
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
             'owner_user_id': info.get('owner_user_id', ''),
             'ext_version': info.get('ext_version', ''),
             'protocol_version': int(info.get('protocol_version') or 0),
             'capabilities': list(info.get('capabilities') or []),
             'profile': info.get('profile', '')}
            for cid, info in _clients.items()
            if now - info['last_poll'] < 15
        ]
    if owner_user_id is not None:
        wanted_owner = str(owner_user_id or '')
        out = [
            client for client in out
            if (client.get('owner_user_id') or '') == wanted_owner
        ]
    return out


def is_extension_connected(client_id, *, owner_user_id):
    """Whether this exact owner/device pair has polled recently."""
    client_id = str(client_id or '')
    owner_user_id = str(owner_user_id or '')
    if not client_id or not owner_user_id:
        return False
    with _clients_lock:
        info = dict(_clients.get(client_id) or {})
    return bool(
        info
        and info.get('owner_user_id') == owner_user_id
        and time.time() - info.get('last_poll', 0) < 15
    )


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
