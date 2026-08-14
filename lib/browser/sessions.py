"""Browser tab leases bound to a user, client/profile, and task."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

from lib.log import get_logger

logger = get_logger(__name__)


class BrowserSessionMode(str, Enum):
    EPHEMERAL = 'ephemeral'
    PERSISTENT = 'persistent'


@dataclass
class BrowserSessionLease:
    lease_id: str
    user_id: str
    client_id: str
    profile: str = ''
    task_id: str = ''
    mode: BrowserSessionMode = BrowserSessionMode.EPHEMERAL
    tab_id: int | None = None
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    network_captures: set[str] = field(default_factory=set)
    released_at: float = 0.0

    @property
    def active(self) -> bool:
        return not self.released_at

    def public_dict(self) -> dict:
        return {
            'lease_id': self.lease_id,
            'user_id': self.user_id,
            'client_id': self.client_id,
            'profile': self.profile,
            'task_id': self.task_id,
            'session': self.mode.value,
            'tab_id': self.tab_id,
            'created_at': self.created_at,
            'expires_at': self.expires_at or None,
            'active': self.active,
        }


_leases: dict[str, BrowserSessionLease] = {}
_lease_timers: dict[str, threading.Timer] = {}
_leases_lock = threading.RLock()


def _expire_lease(lease_id: str) -> None:
    with _leases_lock:
        lease = _leases.get(lease_id)
        if not lease or not lease.active:
            _lease_timers.pop(lease_id, None)
            return
        remaining = lease.expires_at - time.time() if lease.expires_at else 0
        if remaining > 0.05:
            timer = threading.Timer(remaining, _expire_lease, args=(lease_id,))
            timer.daemon = True
            _lease_timers[lease_id] = timer
            timer.start()
            return
    release_browser_lease(lease, reason='timeout')


def _choose_client(user_id: str | None, client_id: str | None) -> tuple[str, str]:
    from .queue import _get_active_client, get_connected_clients

    clients = get_connected_clients(user_id=user_id) if user_id is not None \
        else get_connected_clients()
    cid = str(client_id or '')
    # The queue's active-client pointer is process-global legacy state.  It is
    # safe only for an unscoped operator call; a user-scoped lease selects
    # strictly from that user's connected clients below.
    if not cid and user_id is None:
        cid = str(_get_active_client() or '')
    if cid:
        match = next((c for c in clients if c.get('client_id') == cid), None)
        if match is None:
            raise RuntimeError(f'Browser client {cid[:8]} is not connected for this user')
    else:
        if not clients:
            raise RuntimeError('Browser extension is not connected')
        match = max(clients, key=lambda c: c.get('last_poll', 0))
        cid = str(match.get('client_id') or '')
    return cid, str((match or {}).get('profile') or '')


def acquire_browser_lease(*, user_id: str | None = None, client_id: str | None = None,
                          profile: str = '', task_id: str = '',
                          session: str | BrowserSessionMode = 'ephemeral',
                          tab_id: int | None = None, timeout: float = 120) \
        -> BrowserSessionLease:
    cleanup_expired_leases()
    mode = session if isinstance(session, BrowserSessionMode) \
        else BrowserSessionMode(str(session))
    cid, detected_profile = _choose_client(user_id, client_id)
    now = time.time()
    lease = BrowserSessionLease(
        lease_id=str(uuid.uuid4()), user_id=str(user_id or ''), client_id=cid,
        profile=str(profile or detected_profile), task_id=str(task_id or ''),
        mode=mode, tab_id=int(tab_id) if tab_id is not None else None,
        created_at=now, expires_at=(now + max(1.0, float(timeout))) if timeout else 0,
    )
    with _leases_lock:
        _leases[lease.lease_id] = lease
        if lease.expires_at:
            timer = threading.Timer(
                max(0.05, lease.expires_at - now), _expire_lease,
                args=(lease.lease_id,))
            timer.daemon = True
            _lease_timers[lease.lease_id] = timer
            timer.start()
    return lease


def get_browser_lease(lease_id: str) -> BrowserSessionLease | None:
    with _leases_lock:
        return _leases.get(str(lease_id or ''))


def bind_lease_tab(lease: BrowserSessionLease, tab_id: int | None) -> None:
    with _leases_lock:
        if not lease.active:
            raise RuntimeError('Browser lease has already been released')
        lease.tab_id = int(tab_id) if tab_id is not None else None


def release_browser_lease(lease: BrowserSessionLease, *, reason: str = 'complete',
                          sender=None) -> None:
    """Release listeners and, for an ephemeral lease, its owned tab."""
    if not lease:
        return
    from .queue import send_browser_command

    send = sender or send_browser_command
    with _leases_lock:
        if not lease.active:
            return
        timer = _lease_timers.pop(lease.lease_id, None)
        if timer is not None:
            timer.cancel()
        captures = list(lease.network_captures)
        tab_id = lease.tab_id
        should_close = lease.mode is BrowserSessionMode.EPHEMERAL and tab_id is not None
        lease.network_captures.clear()
        lease.released_at = time.time()
    for capture_id in captures:
        try:
            send('network_capture_stop', {'captureId': capture_id}, timeout=5,
                 client_id=lease.client_id)
        except Exception as exc:
            logger.warning(
                '[Browser] capture cleanup failed for lease %s (%s): %s',
                lease.lease_id[:12], capture_id, exc)
    if should_close:
        try:
            send('close_tab', {'tabId': int(tab_id)}, timeout=5,
                 client_id=lease.client_id)
        except Exception as exc:
            logger.warning(
                '[Browser] tab cleanup failed for lease %s (tab %s): %s',
                lease.lease_id[:12], tab_id, exc)
    with _leases_lock:
        _leases.pop(lease.lease_id, None)


def cleanup_expired_leases(*, sender=None) -> int:
    now = time.time()
    with _leases_lock:
        expired = [lease for lease in _leases.values()
                   if lease.active and lease.expires_at and lease.expires_at <= now]
    for lease in expired:
        release_browser_lease(lease, reason='timeout', sender=sender)
    return len(expired)


def lease_status(*, user_id: str | None = None, client_id: str | None = None) -> list[dict]:
    cleanup_expired_leases()
    with _leases_lock:
        leases = list(_leases.values())
    return [lease.public_dict() for lease in leases
            if lease.active
            and (user_id is None or lease.user_id == str(user_id or ''))
            and (client_id is None or lease.client_id == client_id)]


__all__ = [
    'BrowserSessionMode', 'BrowserSessionLease', 'acquire_browser_lease',
    'get_browser_lease', 'bind_lease_tab', 'release_browser_lease',
    'cleanup_expired_leases', 'lease_status',
]
