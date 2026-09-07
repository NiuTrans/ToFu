"""Protect provider-stream ingestion from synchronous observer side effects.

``stream_llm_response`` owns the lifetime of this guard.  While a provider
dispatch is active, event persistence, browser/WebSocket fan-out and recovery
checkpoints are observers: they may lag, but they must never hold the thread
that is draining the upstream model stream.  The drain thread only mints the
event sequence (single sequence authority) and hands a persist→push closure
to a bounded per-task delivery queue; one task-local worker thread drains it
FIFO, so durable-before-visible ordering is preserved without coupling socket
consumption to a slow Sidecar or push listener.  If observers fall more than
``_DELIVERY_QUEUE_MAX`` events behind, the oldest queued event is dropped —
the first post-ingress authoritative event carries the cumulative projection
and converges delivery again, exactly as it does for any ingress gap.

The guard is task-local.  It creates no process-global queue or worker pool
and retains only one bounded diagnostic receipt per task.
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Any, Callable

from lib.log import get_logger

logger = get_logger(__name__)


_LOCK_KEY = '_providerIngressIsolationLock'
_STATE_KEY = '_providerIngressIsolation'
_MAX_EVENT_TYPE_RECEIPT = 16
_MAX_RECEIPT_COUNT = (1 << 31) - 1
# A full queue means observers are seconds-to-minutes behind; dropping the
# oldest event keeps the live tail fresh and bounds memory per task.
_DELIVERY_QUEUE_MAX = 256
# The boundary flush shares the drain thread with the first post-ingress
# authoritative event, which would perform the same persistence work
# synchronously anyway.  The timeout only caps a truly wedged observer.
_FLUSH_JOIN_TIMEOUT_SEC = 30.0
_STOP = object()
_INTERNAL_STATE_KEYS = frozenset({'queue', 'worker'})


def _state_lock(task: dict[str, Any]) -> threading.Lock:
    """Return the task-local isolation lock, created before provider callbacks."""
    lock = task.get(_LOCK_KEY)
    if lock is None:
        # begin_provider_ingress runs on the owning task thread before any
        # provider callback can observe the state.  setdefault is defensive
        # for test/adopter code that opens two spans incorrectly.
        lock = task.setdefault(_LOCK_KEY, threading.Lock())
    return lock


def _bounded_count(value: Any) -> int:
    return min(_MAX_RECEIPT_COUNT, max(0, int(value or 0)) + 1)


def _delivery_worker(
    task: dict[str, Any],
    state: dict[str, Any],
    delivery_queue: 'queue.Queue',
) -> None:
    """Drain queued persist→push closures FIFO until the stop sentinel."""
    fence_hit = False
    while True:
        item = delivery_queue.get()
        try:
            if item is _STOP:
                return
            if fence_hit:
                # The attempt was fenced as stale: every remaining closure
                # would hit the same rejection, so drop them quietly.
                state['droppedEvents'] = _bounded_count(
                    state.get('droppedEvents'))
                continue
            sequence, deliver = item
            try:
                deliver()
            except Exception as exc:
                state['deliveryFailures'] = _bounded_count(
                    state.get('deliveryFailures'))
                logger.warning(
                    '[ProviderIngress] async delivery failed task=%s seq=%s: %s',
                    str(task.get('id') or '?')[:8], sequence, exc)
                if task.get('aborted'):
                    fence_hit = True
            else:
                state['deliveredEvents'] = _bounded_count(
                    state.get('deliveredEvents'))
        finally:
            delivery_queue.task_done()


def begin_provider_ingress(task: dict[str, Any], *, span_id: str) -> str:
    """Open one provider-ingress isolation span and return its stable token."""
    token = str(span_id or task.get('id') or 'provider')[:200]
    lock = _state_lock(task)
    with lock:
        current = task.get(_STATE_KEY)
        if isinstance(current, dict) and current.get('active'):
            raise RuntimeError(
                'provider ingress isolation already active for this task')
        delivery_queue: queue.Queue = queue.Queue(maxsize=_DELIVERY_QUEUE_MAX)
        state = {
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
            'deliveredEvents': 0,
            'droppedEvents': 0,
            'deliveryFailures': 0,
            'queue': delivery_queue,
            'worker': None,
        }
        worker = threading.Thread(
            target=_delivery_worker,
            args=(task, state, delivery_queue),
            name=f'ingress-deliver-{str(task.get("id") or "?")[:8]}',
            daemon=True,
        )
        state['worker'] = worker
        task[_STATE_KEY] = state
        worker.start()
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
        _record_event_receipt(state, sequence=sequence, event_type=event_type)


def _record_event_receipt(
    state: dict[str, Any],
    *,
    sequence: int,
    event_type: str,
) -> None:
    state['deferredEvents'] = _bounded_count(state.get('deferredEvents'))
    if state.get('firstDeferredSeq') is None:
        state['firstDeferredSeq'] = int(sequence)
    state['lastDeferredSeq'] = int(sequence)
    normalized_type = str(event_type or 'unknown')[:80]
    event_types = state.setdefault('eventTypes', [])
    if (normalized_type not in event_types
            and len(event_types) < _MAX_EVENT_TYPE_RECEIPT):
        event_types.append(normalized_type)


def enqueue_ingress_delivery(
    task: dict[str, Any],
    *,
    token: str,
    sequence: int,
    event_type: str,
    deliver: Callable[[], None],
) -> bool:
    """Hand one persist→push closure to the bounded delivery worker.

    Never blocks the caller: a full queue drops the OLDEST undelivered event
    (the live tail is more valuable, and the post-ingress convergence event
    repairs any gap).  Returns False when the span is no longer active, in
    which case the caller leaves the event memory-local as before.
    """
    lock = task.get(_LOCK_KEY)
    if lock is None:
        return False
    with lock:
        state = task.get(_STATE_KEY)
        if (not isinstance(state, dict) or state.get('token') != token
                or not state.get('active')):
            return False
        delivery_queue = state.get('queue')
        if delivery_queue is None:
            return False
        item = (int(sequence), deliver)
        try:
            delivery_queue.put_nowait(item)
        except queue.Full:
            try:
                delivery_queue.get_nowait()
                delivery_queue.task_done()
            except queue.Empty:
                pass
            state['droppedEvents'] = _bounded_count(state.get('droppedEvents'))
            try:
                delivery_queue.put_nowait(item)
            except queue.Full:
                return False
        _record_event_receipt(state, sequence=sequence, event_type=event_type)
        return True


def defer_provider_ingress_checkpoint(task: dict[str, Any]) -> bool:
    """Return True and account when a DB checkpoint must stay off ingress."""
    lock = task.get(_LOCK_KEY)
    if lock is None:
        return False
    with lock:
        state = task.get(_STATE_KEY)
        if not isinstance(state, dict) or not state.get('active'):
            return False
        state['deferredCheckpoints'] = _bounded_count(
            state.get('deferredCheckpoints'))
        return True


def end_provider_ingress(task: dict[str, Any], *, token: str) -> dict[str, Any]:
    """Close the matching span, flush queued deliveries, return the receipt.

    The flush join keeps every queued event ahead of the first post-ingress
    authoritative frame (strict seq ordering for both storage and push).  It
    is bounded by ``_FLUSH_JOIN_TIMEOUT_SEC``; a wedged observer degrades to
    the pre-queue behavior where the convergence event restores durability.
    """
    lock = task.get(_LOCK_KEY)
    if lock is None:
        return {}
    with lock:
        state = task.get(_STATE_KEY)
        if not isinstance(state, dict) or state.get('token') != token:
            raise RuntimeError('provider ingress isolation token mismatch')
        state['active'] = False
        state['completedAt'] = time.time()
        delivery_queue = state.get('queue')
        worker = state.get('worker')
    if delivery_queue is not None and worker is not None:
        delivery_queue.put(_STOP)
        worker.join(timeout=_FLUSH_JOIN_TIMEOUT_SEC)
        if worker.is_alive():
            logger.warning(
                '[ProviderIngress] delivery flush timed out task=%s '
                'remaining~%d; post-ingress convergence will restore order',
                str(task.get('id') or '?')[:8], delivery_queue.qsize())
            state['flushIncomplete'] = True
    with lock:
        return {
            key: (list(value) if isinstance(value, list) else value)
            for key, value in state.items()
            if key not in _INTERNAL_STATE_KEYS
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
    'enqueue_ingress_delivery',
    'record_deferred_observer_event',
    'release_provider_ingress_guard',
]
