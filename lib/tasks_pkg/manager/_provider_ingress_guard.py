"""Protect provider-stream ingestion from synchronous observer side effects.

``stream_llm_response`` owns the lifetime of this guard.  While a provider
dispatch is active, event persistence, browser/WebSocket fan-out and recovery
checkpoints are observers: they may lag, but they must never hold the thread
that is draining the upstream model stream.  The drain thread only mints the
event sequence (single sequence authority) and hands a persist→push closure
to a task-local FIFO; one demand-started worker drains it without coupling
socket consumption to a slow Sidecar or push listener. Both root fallback and
child queues retain every accepted closure until delivery or an explicit
publication fence. Observer lag never evicts unique events.

The guard is task-local, retaining one bounded diagnostic receipt per task.
Child delivery starts on demand and retains FIFO events without a capacity
refusal. Closing a child revokes publication and drops its uncommitted queued
closures; an already-entered sink retains its own thread until actual exit,
without blocking unrelated children.
"""

from __future__ import annotations

import queue

from contextvars import copy_context
import threading
import time
from typing import Any, Callable

from lib.log import get_logger
from lib.agent_core.storage_errors import describe_storage_failure

logger = get_logger(__name__)


_LOCK_KEY = '_providerIngressIsolationLock'
_STATE_KEY = '_providerIngressIsolation'
_MAX_EVENT_TYPE_RECEIPT = 16
_MAX_RECEIPT_COUNT = (1 << 31) - 1
# Root observers may disclose an incomplete flush. Child evidence is required
# settlement and instead follows the invocation's cancellation/liveness policy.
_FLUSH_JOIN_TIMEOUT_SEC = 30.0

# Bounded redelivery for a transient persist failure. A supervised sidecar
# recycle fences storage for ~10 s; dropping the closure on its first
# rejection (the pre-2026-09-24 behavior) permanently lost the frame even
# though the authority returned seconds later. A failed closure is retried
# inline a bounded number of times with a short back-off, so the delivery
# worker rides the fence window without ever blocking the provider drain
# thread (deliveries run on the task-local worker, not the drain thread).
# The bound keeps a real outage fail-fast: past the cap the closure drops
# exactly as before and the frame is reported as a delivery failure.
_DELIVERY_MAX_ATTEMPTS = 8
_DELIVERY_RETRY_BASE_DELAY_S = 0.1
_DELIVERY_RETRY_MAX_DELAY_S = 1.0

# Child evidence cannot be repaired by a later parent projection. Closing
# revokes publication, while an entered callback owns its thread until exit.
_CHILD_FLUSH_TIMEOUT_SEC = 0.25


def _fail_child_delivery(task, message):
    task.setdefault('_invocationDeliveryError', RuntimeError(message))
    task['aborted'] = True
    abort = task.get('abort_event')
    if abort is not None:
        abort.set()
    fence = task.get('_invocationPublicationFence')
    if fence is not None:
        fence.close()


def detach_child_ingress(task):
    """Drop retained closures, not the admission lease of an in-flight worker."""
    if not task.get('_invocationPublicationFence'):
        return
    lock = _state_lock(task)
    with lock:
        state = task.get(_STATE_KEY)
        if not isinstance(state, dict):
            return
        state['active'] = False
        state['detached'] = True
        state['stop'].set()
        delivery_queue = state['queue']
        while True:
            try:
                item = delivery_queue.get_nowait()
            except queue.Empty:
                break
            delivery_queue.task_done()
            if item is not _STOP:
                state['droppedEvents'] = _bounded_count(state.get('droppedEvents'))
        try:
            delivery_queue.put_nowait(_STOP)
        except queue.Full:
            pass


_STOP = object()
_INTERNAL_STATE_KEYS = frozenset({'queue', 'worker', 'stop'})


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
        if state['stop'].is_set():
            try:
                item = delivery_queue.get_nowait()
            except queue.Empty:
                return
        else:
            item = delivery_queue.get()
        try:
            if item is _STOP:
                return
            if fence_hit or state.get('detached'):
                # The attempt was fenced as stale: every remaining closure
                # would hit the same rejection, so drop them quietly.
                state['droppedEvents'] = _bounded_count(
                    state.get('droppedEvents'))
                continue
            sequence, deliver = item
            last_exc: Exception | None = None
            delivered = False
            for delivery_attempt in range(_DELIVERY_MAX_ATTEMPTS):
                try:
                    deliver()
                    delivered = True
                    break
                except Exception as exc:
                    last_exc = exc
                    # A genuinely fenced task can never succeed on replay:
                    # stop burning attempts on the same stale rejection. The
                    # stop flag is NOT an abort here — end_provider_ingress
                    # sets it to flush the queue, and the flush is exactly
                    # when riding out a transient fence window matters. Only
                    # a stale-attempt fence, an explicit abort, or a child
                    # detach abandons the closure early.
                    if (task.get('_invocationPublicationFence')
                            or task.get('aborted')
                            or state.get('detached')):
                        break
                    if delivery_attempt + 1 >= _DELIVERY_MAX_ATTEMPTS:
                        break
                    delay = min(
                        _DELIVERY_RETRY_BASE_DELAY_S * (2 ** delivery_attempt),
                        _DELIVERY_RETRY_MAX_DELAY_S)
                    logger.warning(
                        '[ProviderIngress] async delivery attempt %d/%d failed '
                        'task=%s seq=%s: %s; retrying in %.2fs',
                        delivery_attempt + 1, _DELIVERY_MAX_ATTEMPTS,
                        str(task.get('id') or '?')[:8], sequence,
                        describe_storage_failure(exc), delay)
                    # Plain bounded sleep: stop must NOT interrupt the retry,
                    # otherwise the flush window (stop is set) would defeat
                    # the redelivery the fence-outage path depends on.
                    time.sleep(delay)
            if not delivered:
                exc = last_exc if last_exc is not None else RuntimeError(
                    'delivery aborted')
                if task.get('_invocationPublicationFence'):
                    _fail_child_delivery(task, 'Invocation evidence delivery failed')
                    fence_hit = True
                state['deliveryFailures'] = _bounded_count(
                    state.get('deliveryFailures'))
                logger.warning(
                    '[ProviderIngress] async delivery failed task=%s seq=%s: %s',
                    str(task.get('id') or '?')[:8], sequence,
                    describe_storage_failure(exc))
                if task.get('aborted'):
                    fence_hit = True
            else:
                state['deliveredEvents'] = _bounded_count(
                    state.get('deliveredEvents'))
                beacon = task.get('_invocationProgressBeacon')
                if beacon is not None:
                    beacon.touch(task.get('_invocationProgressKey') or task['id'],
                                 'evidence_delivered')
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
        child = bool(task.get('_invocationPublicationFence'))
        if child:
            if task.get('_invocationDeliveryError') is not None:
                raise RuntimeError('Child ingress already failed')
        from lib.tasks_pkg.event_delivery import delivery_lane_for
        task_delivery = delivery_lane_for(task) is not None
        delivery_queue = None if task_delivery else queue.Queue()
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

            'flushIncomplete': False,
            'stop': threading.Event(),
            'queue': delivery_queue,
            'worker': None,
        }
        task[_STATE_KEY] = state
        if task_delivery:
            # Root delivery crosses provider boundaries without an EOF join.
            return token
        worker = threading.Thread(
            target=_delivery_worker,
            args=(task, state, delivery_queue),
            name=f'ingress-deliver-{str(task.get("id") or "?")[:8]}',
            daemon=True,
        )
        state['worker'] = worker
        task[_STATE_KEY] = state
        try:
            worker.start()
        except BaseException:
            state['active'] = False
            raise
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
    (retaining the live tail without prioritizing diagnostics over content).
    Cumulative content may converge later; unique diagnostic gaps remain.  Returns False when the span is no longer active, in
    which case the caller leaves the event memory-local as before.
    """
    lock = task.get(_LOCK_KEY)
    if lock is None:
        return False
    with lock:
        state = task.get(_STATE_KEY)
        if (not isinstance(state, dict) or state.get('token') != token
                or not state.get('active')
                or (task.get('_invocationPublicationFence')
                    and task.get('_invocationDeliveryError') is not None)):
            return False
        delivery_queue = state.get('queue')
        if delivery_queue is None:
            return False
        context = copy_context()
        item = (int(sequence), lambda: context.run(deliver))
        delivery_queue.put_nowait(item)
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


def end_provider_ingress(task: dict[str, Any], *, token: str,
                         disposing: bool = False) -> dict[str, Any]:
    """Close the matching span, flush queued deliveries, return the receipt.

    The flush join keeps every queued event ahead of the first post-ingress
    authoritative frame (strict seq ordering for both storage and push).  It
    gives root observers ``_FLUSH_JOIN_TIMEOUT_SEC``. Required child evidence
    waits under the existing invocation cancellation, deadline and progress
    policy; storage latency alone cannot revoke an unsettled child. Disposal
    or an invocation stop retains the short revocation window. An incomplete
    flush cannot establish ordering or repair unique diagnostics.
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
        state['stop'].set()
        try:
            # Wake an idle worker; a full queue already wakes it. The stop
            # flag lets it exit after FIFO drain without reserving a slot or
            # evicting an event merely to shut down.
            delivery_queue.put_nowait(_STOP)
        except queue.Full:
            pass
        if task.get('_invocationPublicationFence'):
            from lib.tasks_pkg.invocation import invocation_abort_requested
            # Never classify an entered required sink as failed merely because
            # the root observer grace period elapsed. Join in control-check
            # intervals so inherited Stop, deadlines and genuine silence still
            # revoke publication without waiting for blocked storage to return.
            while worker.is_alive():
                if disposing or invocation_abort_requested(task):
                    worker.join(timeout=_CHILD_FLUSH_TIMEOUT_SEC)
                    break
                worker.join(timeout=_CHILD_FLUSH_TIMEOUT_SEC)
        else:
            worker.join(timeout=_FLUSH_JOIN_TIMEOUT_SEC)
        if worker.is_alive():
            logger.warning(
                '[ProviderIngress] delivery flush timed out task=%s '
                'remaining~%d; observer gap and in-flight ordering unresolved',
                str(task.get('id') or '?')[:8], delivery_queue.qsize())
            state['flushIncomplete'] = True
    if task.get('_invocationPublicationFence') and (
            state['flushIncomplete'] or state['deliveryFailures'] or state['droppedEvents']):
        _fail_child_delivery(task, 'Invocation evidence delivery incomplete')
        detach_child_ingress(task)
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
