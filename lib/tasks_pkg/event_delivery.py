"""Ordered task-event delivery outside executor threads.

Entry points: delivery_lane_for, flush_task_events, shutdown_event_delivery.
Dependencies: launch-derived budgets, typed storage failures, task identity.
The selected storage authority still commits every public frame before push.
This module owns pending work through settlement, never a second durable store or cursor.
"""
from __future__ import annotations

from collections import deque
from contextvars import copy_context
from dataclasses import dataclass, field
import os
import sys
import threading
import time
import weakref
from typing import Callable, TYPE_CHECKING

from lib.agent_core.generated_task_event_delivery_contract import (
    PROGRESS_BATCH_MAX_FRAMES, PROGRESS_BATCH_MAX_WIRE_BYTES, DELIVERY_PROFILE_ENV,
)
from runtime_guards import resolve_resource_budget
from lib.log import get_logger
from lib.agent_core.storage_errors import is_transient_storage_error

from lib.agent_core.publication import accepted_publication_context
from lib.storage.storage_event_policy import resolve_storage_event_budget

if TYPE_CHECKING:
    from lib.tasks_pkg.event_delivery_profile import DeliverySample

logger = get_logger(__name__)


class EventDeliveryRevoked(RuntimeError):
    """Storage proved this executor no longer owns publication authority."""


class EventDeliveryError(RuntimeError):
    """Local event durability/worker startup failed; never a provider failure."""


class EventDeliveryCancelled(EventDeliveryError):
    """The owner cancelled a producer before event acceptance."""


def retained_snapshot_bytes(*values) -> int:
    """Charge detached Python graphs, without serializing a second wire copy."""
    pending = list(values)
    seen = set()
    total = 1024
    while pending:
        value = pending.pop()
        identity = id(value)
        if identity in seen:
            continue
        seen.add(identity)
        total += sys.getsizeof(value)
        if isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value, (list, tuple)):
            pending.extend(value)
    return total


@dataclass(eq=False)
class DeliveryLane:
    task: dict
    producer_lock: threading.RLock = field(default_factory=threading.RLock)
    pending: deque = field(default_factory=deque)
    scheduled: bool = False
    running: bool = False
    error: BaseException | None = None
    revoked: bool = False
    accepted: int = 0
    completed: int = 0
    projection_source: dict | None = None
    projection_bytes: int = 0
    projection_identity: object | None = None


@dataclass
class ProgressFrame:
    sequence: int
    event: dict
    publish: Callable[[], None]
    context: object = field(default_factory=copy_context)


@dataclass
class _Delivery:
    sequence: int
    retained_bytes: int
    persist: Callable[[], None]
    publish: Callable[[], None]
    context: object = field(default_factory=copy_context)
    accepted_at: float = field(default_factory=time.monotonic)
    projection_identity: object | None = None
    progress_frames: list[ProgressFrame] | None = None
    persist_progress: Callable[[list[ProgressFrame]], None] | None = None
    progress_wire_bytes: int = 0
    progress_payload_bytes: int = 0
    persistence_started: bool = False
    profile: DeliverySample | None = None

    @property
    def event_count(self):
        return len(self.progress_frames) if self.progress_frames is not None else 1


class EventDeliveryCoordinator:
    """One FIFO per task with independent lanes on the shared demand executor.

    Queued and in-flight snapshots are measured, never a count/byte quota.
    A failed head stays retained; no later frame can overtake it. Transient
    pressure retains the exact head under the shared agent wait owner. Model/
    tool/round barriers pause execution without silently skipping evidence.
    """
    def __init__(self, *, workers=None, capacity=None, byte_capacity=None,
                 retry_attempts=8, retry_delay=0.1, idle_timeout=1.0, profile_every=None):
        if profile_every is None:
            try:
                profile_every = max(0, int(os.environ.get(DELIVERY_PROFILE_ENV, '0')))
            except ValueError:
                profile_every = 0
        if isinstance(profile_every, bool) or not isinstance(profile_every, int) or profile_every < 0:
            raise ValueError('profile_every must be a nonnegative integer')
        self.profiler = None
        if profile_every:
            from lib.tasks_pkg.event_delivery_profile import DeliveryProfiler
            self.profiler = DeliveryProfiler(profile_every)
        budget = resolve_storage_event_budget()
        self.idle_reuse_workers = max(1, int(workers)) if workers is not None else resolve_resource_budget(
            'TOFU_AGENT_WORKERS', minimum=1, maximum=sys.maxsize)
        self.capacity = self.byte_capacity = 0  # Legacy hints grant no admission quota.
        self.progress_batch_limit = min(budget.batch_max_events, PROGRESS_BATCH_MAX_FRAMES)
        self.progress_batch_byte_limit = min(budget.batch_max_bytes, PROGRESS_BATCH_MAX_WIRE_BYTES)
        self.retry_attempts = max(1, retry_attempts)
        self.retry_delay = max(0.0, retry_delay)
        self.idle_timeout = idle_timeout
        self._state_lock = threading.RLock()
        self.condition = threading.Condition(self._state_lock)
        self._flush_conditions = weakref.WeakKeyDictionary()
        self.ready = deque()
        self.lanes = set()
        self.workers = set()
        self._pumps = set()
        self._executor = None
        self.count = self.bytes = self.active = self.pending_frames = 0
        self.closed = False
        self._shutdown_deadline = None
        self.projections = {}
        self.metrics = dict(accepted=0, committed=0, completed=0, retries=0, failures=0,
                            peak_events=0, peak_bytes=0, peak_workers=0,
                            admission_waits=0, revoked_events=0, max_lag_ms=0.0,
                            merged_progress=0, peak_batches=0)

    def _schedule(self, lane):
        if lane.pending and not lane.running and not lane.scheduled and lane.error is None:
            lane.scheduled = True
            self.ready.append(lane)
        desired = self.active + len(self.ready)
        if self._executor is None and self.ready:
            from lib.observability import InstrumentedThreadPoolExecutor
            self._executor = InstrumentedThreadPoolExecutor(
                max_workers=self.idle_reuse_workers, metric_pool='task-event-delivery',
                thread_name_prefix='task-event-delivery')
        while len(self._pumps) < desired and self.ready:
            future = self._executor.submit(self._run)
            self._pumps.add(future)
            future.add_done_callback(self._pump_finished)
            self.metrics['peak_workers'] = max(self.metrics['peak_workers'], len(self._pumps))
        self.condition.notify_all()

    def _pump_finished(self, future):
        with self.condition:
            self._pumps.discard(future)
            if self.ready:
                lane = self.ready[0]
                try:
                    self._schedule(lane)
                except BaseException as error:
                    # Failed publication is unstarted. Retain that lane's head;
                    # already-owned siblings keep their existing pump lifetimes.
                    lane.error = error
                    lane.task['_eventDeliveryError'] = type(error).__name__
                    self.metrics['failures'] += 1
                    self._notify_lane(lane)
            self.condition.notify_all()

    def submit(self, lane, sequence, retained_bytes, persist, publish, *, timeout=None,
               projection_source=None, projection_bytes=0, projection_identity=None,
               progress_frame=None, persist_progress=None, progress_wire_bytes=0,
               progress_payload_bytes=0):
        # Progress sources are shallow copies of one detached structural graph.
        # Charge that graph once until its last queued/in-flight owner releases it.
        if projection_source is not None and projection_identity is None:
            projection_identity = object()
        item = _Delivery(sequence, retained_bytes, persist, publish,
                         projection_identity=projection_identity,
                         progress_frames=None if progress_frame is None else [progress_frame],
                         persist_progress=persist_progress, progress_wire_bytes=progress_wire_bytes,
                         progress_payload_bytes=progress_payload_bytes)
        with self.condition, accepted_publication_context() as accepted_context:
            self._check_lane(lane)
            # Root acceptance is the child-to-parent ownership boundary, not
            # child disposal or callback return. Both merged and single frames
            # keep receiver identity without a revocable producer lifetime.
            item.context = accepted_context
            if progress_frame is not None:
                progress_frame.context = accepted_context.copy()
            abort_event = lane.task.get('abort_event')
            if lane.task.get('aborted') or (abort_event is not None and abort_event.is_set()):
                raise EventDeliveryCancelled('Task event submission cancelled by its owner')
            # Only a queued tail can absorb progress. An entered sink
            # owns immutable inputs; structural events and other scopes
            # split batches. No canonical frame/sequence is discarded.
            tail = lane.pending[-1] if lane.pending else None
            if (progress_frame is not None and tail is not None
                    and not tail.persistence_started
                    and tail.progress_frames is not None
                    and tail.projection_identity is projection_identity
                    and tail.sequence + 1 == sequence
                    and len(tail.progress_frames) < self.progress_batch_limit):
                merged_bytes = tail.progress_wire_bytes + retained_bytes
                extra_bytes = merged_bytes - tail.retained_bytes
                if tail.progress_payload_bytes + progress_payload_bytes <= self.progress_batch_byte_limit:
                    if tail.profile is not None:
                        self.profiler.observe('accept_frame', tail.profile, sequence)
                    tail.progress_frames.append(progress_frame)
                    tail.sequence = sequence
                    tail.persist = persist
                    tail.persist_progress = persist_progress
                    tail.context = item.context
                    tail.retained_bytes = merged_bytes
                    tail.progress_wire_bytes += progress_wire_bytes
                    tail.progress_payload_bytes += progress_payload_bytes
                    self.bytes += extra_bytes
                    lane.accepted += 1
                    lane.projection_source = projection_source
                    lane.projection_bytes = projection_bytes
                    self.metrics['accepted'] += 1
                    self.pending_frames += 1
                    self.metrics['peak_events'] = max(self.metrics['peak_events'], self.pending_frames)
                    self.metrics['merged_progress'] += 1
                    self.metrics['peak_bytes'] = max(self.metrics['peak_bytes'], self.bytes)
                    return
            charge = retained_bytes
            if projection_identity is not None and projection_identity not in self.projections:
                charge += projection_bytes
            if projection_identity is not None:
                residency = self.projections.setdefault(projection_identity, [projection_bytes, 0])
                residency[1] += 1
            if self.profiler is not None:
                item.profile = self.profiler.observe('accept', sequence)
            lane.pending.append(item)
            lane.accepted += 1
            self.lanes.add(lane)
            self.count += 1
            self.pending_frames += 1
            self.bytes += charge
            self.metrics['accepted'] += 1
            self.metrics['peak_events'] = max(self.metrics['peak_events'], self.pending_frames)
            self.metrics['peak_batches'] = max(self.metrics['peak_batches'], self.count)
            self.metrics['peak_bytes'] = max(self.metrics['peak_bytes'], self.bytes)
            lane.task['_eventDeliveryPending'] = True
            if projection_source is not None:
                lane.projection_source = projection_source
                lane.projection_bytes = projection_bytes
                lane.projection_identity = projection_identity
            try:
                self._schedule(lane)
            except BaseException as exc:
                lane.error = exc
                lane.task['_eventDeliveryError'] = type(exc).__name__
                self.metrics['failures'] += 1
                self._notify_lane(lane)
                raise EventDeliveryError('Task event delivery worker could not start') from exc

    def _release_item(self, item):
        """Release private bytes and the final reference to shared structure."""
        self.count -= 1
        self.pending_frames -= item.event_count
        self.bytes -= item.retained_bytes
        if item.projection_identity is not None:
            residency = self.projections[item.projection_identity]
            residency[1] -= 1
            if residency[1] == 0:
                self.bytes -= residency[0]
                del self.projections[item.projection_identity]

    def _check_lane(self, lane):
        if lane.revoked:
            raise EventDeliveryRevoked('Task event publication authority was revoked')
        if self.closed:
            raise EventDeliveryCancelled('Task event delivery is shutting down')
        if lane.error is not None and not is_transient_storage_error(lane.error):
            raise EventDeliveryError('Earlier task event is not durable') from lane.error

    def _lane_condition(self, lane):
        # State stays under the same coordinator lock; only waiter queues are
        # separate. A sibling commit must not wake every task's barrier.
        condition = self._flush_conditions.get(lane)
        if condition is None:
            condition = threading.Condition(self._state_lock)
            self._flush_conditions[lane] = condition
        return condition

    def _notify_lane(self, lane):
        condition = self._flush_conditions.get(lane)
        if condition is not None:
            condition.notify_all()

    def flush(self, lane, *, timeout=30.0, recover=True, cancel_check=None):
        if self.profiler is None:
            return self._flush(lane, timeout=timeout, recover=recover, cancel_check=cancel_check)
        start = self.profiler.observe('clock_ns')
        failed = True
        try:
            result = self._flush(lane, timeout=timeout, recover=recover, cancel_check=cancel_check)
            failed = False
            return result
        finally:
            self.profiler.observe('barrier', start, failed)

    def _flush(self, lane, *, timeout=30.0, recover=True, cancel_check=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        with self.condition:
            lane_condition = self._lane_condition(lane)
            target = lane.accepted
            if recover and lane.error is not None:
                lane.error = None
                self._schedule(lane)
            while lane.completed < target:
                if lane.error is not None:
                    raise EventDeliveryError('Task event delivery failed; pending evidence retained') from lane.error
                if cancel_check is not None and cancel_check():
                    from lib.llm.llm_errors import AbortedError
                    raise AbortedError('Agent stopped at event durability barrier')
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise EventDeliveryError('Task event durability barrier timed out; pending evidence retained')
                wait_for = remaining
                if cancel_check is not None:
                    wait_for = 0.1 if remaining is None else min(0.1, remaining)
                lane_condition.wait(wait_for)

    def _persist(self, item, lane):
        from lib.tasks_pkg.persistence_wait import wait_for_persistence

        def persist():
            start = self.profiler.observe('attempt_start', item.profile) if item.profile is not None else None
            failed = True
            outcome = 'interrupted'
            try:
                if item.progress_frames is not None:
                    result = item.context.run(item.persist_progress, item.progress_frames)
                else:
                    result = item.context.run(item.persist)
                failed = False
                outcome = 'ok'
                return result
            except EventDeliveryRevoked:
                outcome = 'authority_revoked'
                raise
            except Exception as error:
                if item.profile is not None:
                    outcome = self.profiler.observe('failure_outcome', error)
                lane.task.setdefault('_pushWithheldAt', time.time())
                lane.task['_pushWithheldCount'] = min((1 << 31) - 1,
                    int(lane.task.get('_pushWithheldCount') or 0) + 1)
                raise
            finally:
                if item.profile is not None:
                    self.profiler.observe('attempt_end', item.profile, start, failed, outcome)

        def retry():
            with self.condition:
                self.metrics['retries'] += 1

                self.condition.notify_all()

        # Accepted evidence must still commit after user Stop. Shutdown hands
        # the exact head to existing debt/recovery rather than losing ownership.
        wait_for_persistence(lane.task, persist, label='task event',
                             cancellable=False, stopping=lambda: (
                                 self._shutdown_deadline is not None
                                 and time.monotonic() >= self._shutdown_deadline),
                             on_retry=retry)

    def _run(self):
        current = threading.current_thread()
        with self.condition:
            self.workers.add(current)
        try:
            while True:
                with self.condition:
                    if not self.ready:
                        if self.closed or len(self._pumps) > self.idle_reuse_workers:
                            self.workers.discard(current)
                            self.condition.notify_all()
                            return
                        self.condition.wait(self.idle_timeout)
                        if not self.ready:
                            # Future completion reschedules ready lanes after
                            # this pump exits; no departing pump loses a handoff.
                            self.workers.discard(current)
                            self.condition.notify_all()
                            return
                    lane = self.ready.popleft()
                    lane.scheduled = False
                    if lane.error is not None or not lane.pending:
                        continue
                    lane.running = True
                    self.active += 1
                    item = lane.pending[0]
                    # An unacknowledged attempt may already be committed.
                    # Keep its exact batch through failure and explicit repair.
                    item.persistence_started = True
                if item.profile is not None:
                    self.profiler.observe('dispatch', item.profile)
                try:
                    self._persist(item, lane)
                    if item.profile is not None:
                        self.profiler.observe('acknowledge', item.profile)
                except EventDeliveryRevoked:
                    with self.condition:
                        lane.running = False
                        self.active -= 1
                        lane.revoked = True
                        lane.task['_eventDeliveryRevoked'] = True
                        self.metrics['revoked_events'] += sum(entry.event_count for entry in lane.pending)
                        for entry in lane.pending:
                            if entry.profile is not None:
                                self.profiler.observe('finish', entry.profile, 'revoked')
                            self._release_item(entry)
                        lane.pending.clear()
                        lane.completed = lane.accepted
                        lane.projection_source = None
                        lane.projection_bytes = 0
                        lane.projection_identity = None
                        self.lanes.discard(lane)
                        lane.task.pop('_eventDeliveryPending', None)
                        self._notify_lane(lane)
                        self.condition.notify_all()
                    del item, lane
                    continue
                except BaseException as exc:
                    if item.profile is not None:
                        self.profiler.observe('failed_round')
                    with self.condition:
                        lane.running = False
                        self.active -= 1
                        lane.error = exc
                        self.metrics['failures'] += 1
                        lane.task['_eventDeliveryError'] = type(exc).__name__
                        self._notify_lane(lane)
                        self.condition.notify_all()
                    logger.warning('[TaskEvents] retained failed delivery task=%s seq=%s error=%s',
                                   str(lane.task.get('id') or '?')[:8], item.sequence, type(exc).__name__)
                    continue
                # Publish/replay share one exclusive cursor. The memory
                # buffer may contain accepted future frames, but consumers
                # cannot observe them before the authority commit.
                lane.task.pop('_pushWithheldAt', None)
                lane.task.pop('_pushWithheldCount', None)
                try:
                    if item.progress_frames is not None:
                        for frame in item.progress_frames:
                            self._publish(item, lane, frame.sequence, frame.context, frame.publish)
                    else:
                        self._publish(item, lane, item.sequence, item.context, item.publish)
                except Exception as exc:
                    # Visibility hints cannot invalidate a committed event or
                    # cause repeated authority writes; reconnect repairs push.
                    logger.debug('[TaskEvents] push observer failed: %s', type(exc).__name__)
                finally:
                    if item.profile is not None:
                        self.profiler.observe('finish', item.profile, 'committed')
                    with self.condition:
                        lane.pending.popleft()
                        lane.running = False
                        self.active -= 1
                        lane.completed += item.event_count
                        self._release_item(item)
                        self.metrics['completed'] += item.event_count
                        self._notify_lane(lane)
                        if item.sequence >= 0:
                            self.metrics['committed'] += item.event_count
                        self.metrics['max_lag_ms'] = max(self.metrics['max_lag_ms'],
                            (time.monotonic() - item.accepted_at) * 1000)
                        lane.task.pop('_eventDeliveryError', None)
                        if not lane.pending:
                            self.lanes.discard(lane)
                            lane.projection_source = None
                            lane.projection_bytes = 0
                            lane.projection_identity = None
                            lane.task.pop('_eventDeliveryPending', None)
                        self._schedule(lane)
                        self.condition.notify_all()
                # Do not keep the last large projection/callback across idle.
                del item, lane
        finally:
            with self.condition:
                self.workers.discard(current)
                self.condition.notify_all()

    def _publish(self, item, lane, sequence, context, publish):
        profile = item.profile
        start = self.profiler.observe('clock_ns') if profile is not None else None
        callback_start = None
        failed = True
        try:
            if sequence >= 0:
                advance_event_visibility(lane.task, sequence)
            if profile is not None:
                callback_start = self.profiler.observe('clock_ns')
            try:
                context.run(publish)
                failed = False
            except Exception as exc:
                logger.debug('[TaskEvents] push observer failed: %s', type(exc).__name__)
        finally:
            if profile is not None:
                end = self.profiler.observe('clock_ns')
                self.profiler.observe('publication', profile, sequence, start, callback_start, end, failed)

    def snapshot(self):
        with self.condition:
            result = {**self.metrics, 'pending_events': self.pending_frames,
                    'pending_batches': self.count,
                    'pending_bytes': self.bytes, 'workers': len(self.workers),
                    'active_workers': self.active,
                    'worker_capacity': 0, 'idle_reuse_workers': self.idle_reuse_workers,
                    'event_capacity': self.capacity,
                    'progress_batch_capacity': self.progress_batch_limit,
                    'byte_capacity': self.byte_capacity, 'failed_lanes': sum(l.error is not None for l in self.lanes)}
        result['performance_profile'] = (self.profiler.observe('snapshot') or {'enabled': True, 'available': False}) if self.profiler is not None else {'enabled': False}
        return result

    def close(self, timeout=10.0):
        deadline = time.monotonic() + timeout
        with self.condition:
            self.closed = True

            self._shutdown_deadline = deadline
            self.condition.notify_all()
            for lane in tuple(self.lanes):
                if lane.error is not None:
                    lane.error = None
                    self._schedule(lane)
            while self.count or self._pumps:
                if any(l.error is not None for l in self.lanes):
                    return False
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self.condition.wait(remaining)
            self.condition.notify_all()
            workers = tuple(self.workers)
        for worker in workers:
            if worker is not threading.current_thread():
                worker.join(max(0.0, deadline - time.monotonic()))
        with self.condition:
            finished = not self.workers and not self._pumps
            executor = self._executor if finished else None
        if executor is not None:
            executor.shutdown(wait=True)
        return finished


_COORDINATOR = None
_COORDINATOR_LOCK = threading.Lock()


def coordinator():
    global _COORDINATOR
    with _COORDINATOR_LOCK:
        if _COORDINATOR is None:
            _COORDINATOR = EventDeliveryCoordinator()
        return _COORDINATOR


def delivery_lane_for(task):
    """Bind physical root executor work; birth/child/Flow ports keep their fences."""
    existing = task.get('_eventDeliveryLane')
    excluded = bool(task.get('_eventDeliveryDetached') or task.get('_transientRuntime')
        or task.get('_invocationPublicationFence') or callable(task.get('_invocationEventSink'))
        or task.get('_flowLiveTurn')
        or task.get('_flowRootAttemptId') or task.get('_flow_managed')
        or task.get('_suppressEvents'))
    if excluded:
        if existing is not None:
            detach_task_event_delivery(task)
        return None
    if existing is not None:
        return existing
    if not task.get('_workerStartedAt'):
        return None
    with _COORDINATOR_LOCK:
        task.setdefault('_eventVisibilityCursor', int(task.get('_eventNextSeq') or 0))
        return task.setdefault('_eventDeliveryLane', DeliveryLane(task))


def detach_task_event_delivery(task):
    """Drain before a composition retargets this carrier's Turn identity."""
    lane = task.get('_eventDeliveryLane')
    if lane is None:
        return
    with lane.producer_lock:
        flush_task_events(task)
        task['_eventDeliveryDetached'] = True
        task.pop('_eventDeliveryLane', None)
        task.pop('_eventVisibilityCursor', None)


def advance_event_visibility(task, sequence):
    lock = task.get('events_lock')
    from contextlib import nullcontext
    with lock if lock is not None else nullcontext():
        task['_eventVisibilityCursor'] = max(
            int(task.get('_eventVisibilityCursor') or 0), int(sequence) + 1)
        receipt = task.get('_eventDeliveryReceipt')
        if isinstance(receipt, dict):
            receipt['visibleCursor'] = task['_eventVisibilityCursor']


def flush_task_events(task, *, timeout=None, cancel_check=None):
    lane = task.get('_eventDeliveryLane')
    if lane is not None:
        coordinator().flush(lane, timeout=timeout, cancel_check=cancel_check)
        task['_eventDeliveryReceipt'] = {
            'contract': 'tofu.task-event-delivery/v1',
            'accepted': lane.accepted, 'completed': lane.completed,
            'revoked': lane.revoked,
            'visibleCursor': int(task.get('_eventVisibilityCursor') or 0),
        }


def event_delivery_metrics():
    """Read existing residency without constructing workers or probing hosts."""
    with _COORDINATOR_LOCK:
        owner = _COORDINATOR
    return {} if owner is None else owner.snapshot()


def shutdown_event_delivery(timeout=10.0):
    with _COORDINATOR_LOCK:
        owner = _COORDINATOR
    return True if owner is None else owner.close(timeout)
