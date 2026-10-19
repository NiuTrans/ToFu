"""lib/scheduler/timer/_lease.py — per-timer watcher lease (crash-reclaim).

Exactly one replica runs the live poll loop for a given timer. Ownership is a
first-writer-wins claim (``set_value_nx``) in the runtime state store —
inproc in personal mode (where the claim always succeeds inside the single
process, so behavior is unchanged), redis when distributed. The claim carries
a per-owner token so a watcher can detect that another replica took over; a
crashed replica's claim expires by TTL and the scheduler's periodic
``resume_active_timers`` sweep adopts the orphan.
"""

from __future__ import annotations

import os
import uuid

from lib.log import get_logger

logger = get_logger(__name__)

WATCHER_LEASE_KIND = 'timer-watcher'
WATCHER_LEASE_TTL = 120.0


def _store():
    from lib.runtime.runtime_state_store import get_store
    return get_store()


def claim_watcher_lease(timer_id: str, *,
                        ttl: float = WATCHER_LEASE_TTL) -> str | None:
    """Claim the live-watcher lease for ``timer_id``; return the owner token.

    Returns None when another replica holds a live claim or the claim's
    outcome is unknown (store fault) — an unknown claim must be retried by
    the caller, never assumed won.
    """
    token = f'{os.getpid()}:{uuid.uuid4().hex}'
    try:
        if _store().set_value_nx(WATCHER_LEASE_KIND, str(timer_id), token, ttl):
            return token
    except Exception as exc:
        logger.warning('[Timer:%s] watcher lease claim failed: %s',
                       timer_id, exc)
    return None


def renew_watcher_lease(timer_id: str, token: str, *,
                        ttl: float = WATCHER_LEASE_TTL) -> bool:
    """Extend the lease iff ``token`` still owns it; False means takeover.

    A transient store error keeps the watcher alive (fail-open for one beat):
    the next beat re-checks ownership, and the TTL bounds any duplication
    window to a crash-style reclaim.
    """
    try:
        store = _store()
        if store.get_value(WATCHER_LEASE_KIND, str(timer_id)) != token:
            return False
        store.set_value(WATCHER_LEASE_KIND, str(timer_id), token, ttl)
        return True
    except Exception as exc:
        logger.warning('[Timer:%s] watcher lease renew error: %s',
                       timer_id, exc)
        return True


def release_watcher_lease(timer_id: str, token: str) -> None:
    """Best-effort release so a standby replica can adopt within ~1s.

    Only the current owner may release; after a takeover this is a no-op.
    The short tombstone covers the inproc backend (values expire by TTL),
    while ``release_lease`` deletes the key outright on the redis backend.
    """
    try:
        store = _store()
        if store.get_value(WATCHER_LEASE_KIND, str(timer_id)) != token:
            return
        store.set_value(WATCHER_LEASE_KIND, str(timer_id), '', 0.2)
        store.release_lease(WATCHER_LEASE_KIND, str(timer_id))
    except Exception as exc:
        logger.debug('[Timer:%s] watcher lease release failed: %s',
                     timer_id, exc)
