"""Browser tab leases bound to one authenticated owner and one device."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

from lib.log import get_logger
from runtime_guards import resolve_resource_budget

logger = get_logger(__name__)


class BrowserSessionMode(str, Enum):
    EPHEMERAL = 'ephemeral'
    PERSISTENT = 'persistent'


class BrowserSessionCapacityError(RuntimeError):
    """The process-wide owner/device lease budget is exhausted."""


@dataclass
class BrowserSessionLease:
    lease_id: str
    owner_user_id: str
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
            'owner_user_id': self.owner_user_id,
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
_leases_lock = threading.RLock()
_leases_changed = threading.Condition(_leases_lock)
_lease_sweeper_thread: threading.Thread | None = None


def _lease_capacity() -> int:
    return resolve_resource_budget(
        'TOFU_BROWSER_SESSION_LEASE_CAPACITY', minimum=1, maximum=8192)


def _lease_sweeper_loop() -> None:
    """Expire every timed lease from one process-wide lifecycle thread."""
    while True:
        with _leases_changed:
            deadlines = [
                lease.expires_at
                for lease in _leases.values()
                if lease.active and lease.expires_at
            ]
            if not deadlines:
                # Once created, the sole daemon stays parked on the condition.
                # This removes the empty→new-lease exit race without ever
                # multiplying threads or waking on a polling cadence.
                _leases_changed.wait()
                continue
            delay = max(0.05, min(deadlines) - time.time())
            _leases_changed.wait(timeout=delay)
        try:
            cleanup_expired_leases()
        except Exception as exc:
            # A lifecycle thread must not disappear and strand every later
            # lease because one cleanup path had a programmer bug.
            logger.error(
                '[Browser] lease sweep failed: %s', exc, exc_info=True)
            with _leases_changed:
                _leases_changed.wait(timeout=1.0)


def _ensure_lease_sweeper_locked() -> None:
    """Start the sole sweeper; caller holds ``_leases_lock``."""
    global _lease_sweeper_thread
    if (_lease_sweeper_thread is not None
            and _lease_sweeper_thread.is_alive()):
        _leases_changed.notify_all()
        return
    thread = threading.Thread(
        target=_lease_sweeper_loop,
        name='browser-lease-sweeper',
        daemon=True,
    )
    _lease_sweeper_thread = thread
    try:
        thread.start()
    except Exception:
        _lease_sweeper_thread = None
        raise


def _normalize_owner(owner_user_id) -> str:
    owner = str(owner_user_id or '').strip()
    if not owner.isdigit() or int(owner) < 1:
        raise ValueError('owner_user_id must be a positive integer')
    return owner


def _choose_client(owner_user_id: str, client_id: str | None) -> tuple[str, str]:
    from .queue import get_connected_clients

    clients = get_connected_clients(owner_user_id=owner_user_id)
    cid = str(client_id or '')
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


def acquire_browser_lease(*, owner_user_id: str, client_id: str | None = None,
                          profile: str = '', task_id: str = '',
                          session: str | BrowserSessionMode = 'ephemeral',
                          tab_id: int | None = None, timeout: float = 120) \
        -> BrowserSessionLease:
    cleanup_expired_leases()
    owner = _normalize_owner(owner_user_id)
    mode = session if isinstance(session, BrowserSessionMode) \
        else BrowserSessionMode(str(session))
    cid, detected_profile = _choose_client(owner, client_id)
    now = time.time()
    lease = BrowserSessionLease(
        lease_id=str(uuid.uuid4()), owner_user_id=owner, client_id=cid,
        profile=str(profile or detected_profile), task_id=str(task_id or ''),
        mode=mode, tab_id=int(tab_id) if tab_id is not None else None,
        created_at=now, expires_at=(now + max(1.0, float(timeout))) if timeout else 0,
    )
    with _leases_changed:
        capacity = _lease_capacity()
        if len(_leases) >= capacity:
            raise BrowserSessionCapacityError(
                f'Browser session lease capacity reached ({capacity})')
        _leases[lease.lease_id] = lease
        if lease.expires_at:
            try:
                _ensure_lease_sweeper_locked()
            except Exception:
                _leases.pop(lease.lease_id, None)
                raise
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
    with _leases_changed:
        if not lease.active:
            return
        captures = list(lease.network_captures)
        tab_id = lease.tab_id
        should_close = lease.mode is BrowserSessionMode.EPHEMERAL and tab_id is not None
        lease.network_captures.clear()
        lease.released_at = time.time()
        _leases.pop(lease.lease_id, None)
        _leases_changed.notify_all()
    for capture_id in captures:
        try:
            send('network_capture_stop', {'captureId': capture_id}, timeout=5,
                 client_id=lease.client_id,
                 owner_user_id=lease.owner_user_id)
        except Exception as exc:
            logger.warning(
                '[Browser] capture cleanup failed for lease %s (%s): %s',
                lease.lease_id[:12], capture_id, exc)
    if should_close:
        try:
            send('close_tab', {'tabId': int(tab_id)}, timeout=5,
                 client_id=lease.client_id,
                 owner_user_id=lease.owner_user_id)
        except Exception as exc:
            logger.warning(
                '[Browser] tab cleanup failed for lease %s (tab %s): %s',
                lease.lease_id[:12], tab_id, exc)


def cleanup_expired_leases(*, sender=None) -> int:
    now = time.time()
    with _leases_lock:
        expired = [lease for lease in _leases.values()
                   if lease.active and lease.expires_at and lease.expires_at <= now]
    for lease in expired:
        release_browser_lease(lease, reason='timeout', sender=sender)
    return len(expired)


def lease_status(*, owner_user_id: str, client_id: str | None = None) -> list[dict]:
    owner = _normalize_owner(owner_user_id)
    cleanup_expired_leases()
    with _leases_lock:
        leases = list(_leases.values())
    return [lease.public_dict() for lease in leases
            if lease.active
            and lease.owner_user_id == owner
            and (client_id is None or lease.client_id == client_id)]


def lease_runtime_snapshot() -> dict:
    """Small resource-budget projection for diagnostics and tests."""
    with _leases_lock:
        return {
            'active': len(_leases),
            'capacity': _lease_capacity(),
            'expiring': sum(
                1 for lease in _leases.values() if lease.expires_at),
            'sweeperAlive': bool(
                _lease_sweeper_thread
                and _lease_sweeper_thread.is_alive()),
        }


__all__ = [
    'BrowserSessionMode', 'BrowserSessionLease',
    'BrowserSessionCapacityError', 'acquire_browser_lease',
    'get_browser_lease', 'bind_lease_tab', 'release_browser_lease',
    'cleanup_expired_leases', 'lease_status', 'lease_runtime_snapshot',
]
