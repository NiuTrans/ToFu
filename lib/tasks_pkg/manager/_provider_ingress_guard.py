"""Protect provider-stream ingestion from synchronous observer side effects.

``stream_llm_response`` owns the lifetime of this guard.  While a provider
dispatch is active, event persistence, browser/WebSocket fan-out and recovery
checkpoints are observers: they may lag, but they must never hold the thread
that is draining the upstream model stream.  The task's bounded in-memory
event log and cumulative content remain the live projection; the first event
after the provider boundary performs the ordinary authoritative convergence.

The guard is task-local.  It creates no process-global queue or worker pool and
retains only one bounded diagnostic receipt per task.
"""

from __future__ import annotations

import threading
import time
from typing import Any


_LOCK_KEY = '_providerIngressIsolationLock'
_STATE_KEY = '_providerIngressIsolation'
_MAX_EVENT_TYPE_RECEIPT = 16
_MAX_RECEIPT_COUNT = (1 << 31) - 1


def _state_lock(task: dict[str, Any]) -> threading.Lock:
    """Return the task-local isolation lock, created before provider callbacks."""
    lock = task.get(_LOCK_KEY)
    if lock is None:
        # begin_provider_ingress runs on the owning task thread before any
        # provider callback can observe the state.  setdefault is defensive
        # for test/adopter code that opens two spans incorrectly.
        lock = task.setdefault(_LOCK_KEY, threading.Lock())
    return lock


def begin_provider_ingress(task: dict[str, Any], *, span_id: str) -> str:
    """Open one provider-ingress isolation span and return its stable token."""
    token = str(span_id or task.get('id') or 'provider')[:200]
    lock = _state_lock(task)
    with lock:
        current = task.get(_STATE_KEY)
        if isinstance(current, dict) and current.get('active'):
            raise RuntimeError(
                'provider ingress isolation already active for this task')
        task[_STATE_KEY] = {
            'contract': 'tofu.provider-ingress-isolation/v1',
            'token': token,
            'active': True,
            'startedAt': time.time(),
            'completedAt': None,
            'deferredEvents': 0,
            'deferredCheckpoints': 0,
            'firstDeferredSeq': None,
            'lastDeferredSeq': None,
            'eventTypes': [],
        }
    return token


def active_provider_ingress_token(task: dict[str, Any]) -> str | None:
    """Return the active isolation token without exposing mutable state."""
    lock = task.get(_LOCK_KEY)
    if lock is None:
        return None
    with lock:
        state = task.get(_STATE_KEY)
        if not isinstance(state, dict) or not state.get('active'):
            return None
        return str(state.get('token') or '') or None


def record_deferred_observer_event(
    task: dict[str, Any],
    *,
    token: str,
    sequence: int,
    event_type: str,
) -> None:
    """Record that one event stayed memory-local while ingress was active."""
    lock = task.get(_LOCK_KEY)
    if lock is None:
        return
    with lock:
        state = task.get(_STATE_KEY)
        if not isinstance(state, dict) or state.get('token') != token:
            return
        count = min(
            _MAX_RECEIPT_COUNT,
            max(0, int(state.get('deferredEvents') or 0)) + 1,
        )
        state['deferredEvents'] = count
        if state.get('firstDeferredSeq') is None:
            state['firstDeferredSeq'] = int(sequence)
        state['lastDeferredSeq'] = int(sequence)
        normalized_type = str(event_type or 'unknown')[:80]
        event_types = state.setdefault('eventTypes', [])
        if (normalized_type not in event_types
                and len(event_types) < _MAX_EVENT_TYPE_RECEIPT):
            event_types.append(normalized_type)


def defer_provider_ingress_checkpoint(task: dict[str, Any]) -> bool:
    """Return True and account when a DB checkpoint must stay off ingress."""
    lock = task.get(_LOCK_KEY)
    if lock is None:
        return False
    with lock:
        state = task.get(_STATE_KEY)
        if not isinstance(state, dict) or not state.get('active'):
            return False
        state['deferredCheckpoints'] = min(
            _MAX_RECEIPT_COUNT,
            max(0, int(state.get('deferredCheckpoints') or 0)) + 1,
        )
        return True


def end_provider_ingress(task: dict[str, Any], *, token: str) -> dict[str, Any]:
    """Close the matching span and return an immutable diagnostic receipt."""
    lock = task.get(_LOCK_KEY)
    if lock is None:
        return {}
    with lock:
        state = task.get(_STATE_KEY)
        if not isinstance(state, dict) or state.get('token') != token:
            raise RuntimeError('provider ingress isolation token mismatch')
        state['active'] = False
        state['completedAt'] = time.time()
        return {
            key: (list(value) if isinstance(value, list) else value)
            for key, value in state.items()
        }


def release_provider_ingress_guard(task: dict[str, Any]) -> None:
    """Release reconstructible guard state after model-request completion."""
    lock = task.get(_LOCK_KEY)
    if lock is None:
        return
    with lock:
        state = task.get(_STATE_KEY)
        if isinstance(state, dict) and state.get('active'):
            raise RuntimeError('cannot release an active provider ingress guard')
        task.pop(_STATE_KEY, None)
        task.pop(_LOCK_KEY, None)


__all__ = [
    'active_provider_ingress_token',
    'begin_provider_ingress',
    'defer_provider_ingress_checkpoint',
    'end_provider_ingress',
    'record_deferred_observer_event',
    'release_provider_ingress_guard',
]
