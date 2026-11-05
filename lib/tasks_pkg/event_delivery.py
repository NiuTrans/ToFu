"""Ordered task-event delivery outside executor threads.

Entry points: delivery_lane_for, invocation_evidence_lane, flush_task_events,
task_event_backlog, task_event_ordering, shutdown_event_delivery, is_barrier_event.
Dependencies: launch-derived budgets, typed storage failures, task identity,
the event registry and task-replay terminal vocabulary.
The selected storage authority still commits every public frame before push.
This module owns pending work through settlement, never a second durable store or cursor.
"""
from __future__ import annotations

from collections import deque
from contextlib import nullcontext
from contextvars import copy_context
from dataclasses import dataclass, field
from itertools import islice
import os
import sys
import threading
import time
import weakref
from typing import Callable, NamedTuple, TYPE_CHECKING

from lib.agent_core.events import EventType, interaction_types
from lib.agent_core.generated_task_event_delivery_contract import (
    PROGRESS_BATCH_MAX_FRAMES, PROGRESS_BATCH_MAX_WIRE_BYTES, DELIVERY_PROFILE_ENV,
)
from runtime_guards import resolve_resource_budget
from lib.log import get_logger
from lib.agent_core.storage_errors import is_transient_storage_error

from lib.agent_core.publication import accepted_publication_context
from lib.storage.storage_event_policy import resolve_storage_event_budget
from lib.task_replay.task_replay import TASK_REPLAY_TERMINAL_EVENT_TYPES
from lib.utils import retained_graph_bytes

if TYPE_CHECKING:
    from lib.tasks_pkg.event_delivery_profile import DeliverySample

logger = get_logger(__name__)

# Contract ``barriers`` and ``structuralBatch.eligibility``: a frame that settles
# the stream, asks for a registered response, acknowledges a background-command
# receipt or fences a tool effect drains its lane and keeps its own Turn
# transaction. Every other frame may queue and share a structural batch.
BARRIER_EVENT_TYPES = (frozenset(TASK_REPLAY_TERMINAL_EVENT_TYPES) | interaction_types()
                       | {EventType.BACKGROUND_COMMAND_INJECT, EventType.TOOL_EFFECT})


def is_barrier_event(event) -> bool:
    """Whether *event* keeps its own transaction and barrier instead of queueing."""
    return event.get('type') in BARRIER_EVENT_TYPES


class EventDeliveryRevoked(RuntimeError):
    """Storage proved this executor no longer owns publication authority."""


class EventDeliveryError(RuntimeError):
    """Local event durability/worker startup failed; never a provider failure."""


class EventDeliveryCancelled(EventDeliveryError):
    """The owner cancelled a producer before event acceptance."""


class EventBacklog(NamedTuple):
    """Accepted-but-unacknowledged events and whether a failed head is retained."""
    pending: int
    failed: bool


class BarrierCost(NamedTuple):
    """Storage work of the events one barrier awaited, apart from local queuing.

    ``persist_calls`` counts every call of their persistence callbacks,
    transient retries included; a run persisted together counts once.
    ``persist_seconds`` runs from each batch's first storage dispatch to its
    acknowledgement (attempts and backoff); ``queued_seconds`` is each
    batch's wait at its FIFO head for a pump.
    """
    persist_calls: int
    persist_seconds: float
    queued_seconds: float


def retained_snapshot_bytes(*values) -> int:
    """Charge detached Python graphs, without serializing a second wire copy."""
    return retained_graph_bytes(*values)


class LaneProjection(NamedTuple):
    """A lane's newest detached projection inputs, replaced as one reference.

    Every write swaps the whole triple, so a producer reads its own lane's
    source, charge and identity together without the coordinator lock.
    """
    source: dict | None = None
    source_bytes: int = 0
    identity: object | None = None


NO_LANE_PROJECTION = LaneProjection()


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
    # When the head last became dispatchable: a batch settled or was repaired.
    head_ready_at: float = 0.0
    # The lane holds one charge on its projection's structure until a new
    # structural source replaces it or the lane ends; see _retain_projection.
    projection: LaneProjection = NO_LANE_PROJECTION
    projection_finalizer: object | None = field(default=None, repr=False)


@dataclass(eq=False)
class _DeliveryTiming:
    """One batch's storage cost; a barrier keeps only this, never the batch."""
    attempts: int = 0
    queued: float = 0.0
    persisted: float = 0.0


@dataclass
class ProgressFrame:
    sequence: int
    event: dict
    publish: Callable[[], None]
    context: object = field(default_factory=copy_context)


_UNFROZEN = object()


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
    timing: _DeliveryTiming = field(default_factory=_DeliveryTiming)
    batch_scope: object | None = None
    persist_batch: Callable[[list], None] | None = None
    batch_entry: Callable[[list[ProgressFrame] | None], object] | None = None
    # Set once by the first attempt of a run; every retry persists this object.
    frozen_entry: object = _UNFROZEN
    # On a started head: how many items, itself included, its run froze.
    run_length: int = 1

    @property
    def event_count(self):
        return len(self.progress_frames) if self.progress_frames is not None else 1


class EventDeliveryCoordinator:
    """One FIFO per task with independent lanes on the shared demand executor.

    Queued and in-flight snapshots are measured, never a count/byte quota.
    A failed head stays retained; no later frame can overtake it. Transient
    pressure retains the exact head under the shared agent wait owner. Model/
    tool/round barriers pause execution without silently skipping evidence.
    A pump that takes an unstarted head claims a contiguous prefix within the
    existing RPC row bound and persists it in one call (``_claim_run``).
    Each acknowledged prefix publishes before the next storage call.
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
        self.structural_batch_limit = budget.batch_max_events
        self.retry_attempts = max(1, retry_attempts)
        self.retry_delay = max(0.0, retry_delay)
        self.idle_timeout = idle_timeout
        self._state_lock = threading.RLock()
        self.condition = threading.Condition(self._state_lock)
        # Parked pumps wait apart from shutdown/observer waiters, so a ready
        # lane wakes exactly one of them and nothing else wakes them at all.
        self._idle = threading.Condition(self._state_lock)
        self._flush_conditions = weakref.WeakKeyDictionary()
        self.ready = deque()
        self.lanes = set()
        self.workers = set()
        self._pumps = set()
        self._pump_reservations = 0
        # Lanes holding a retained projection charge, and charges handed back
        # by lanes collected without an explicit release.
        self._retaining = weakref.WeakSet()
        self._abandoned_projections = deque()
        self._executor = None
        self.count = self.bytes = self.active = self.pending_frames = 0
        self.closed = False
        self._shutdown_deadline = None
        self.projections = {}
        self.metrics = dict(accepted=0, committed=0, completed=0, retries=0, failures=0,
                            peak_events=0, peak_bytes=0, peak_workers=0,
                            admission_waits=0, revoked_events=0, max_lag_ms=0.0,
                            merged_progress=0, peak_batches=0)

    def _ready(self, lane):
        """Queue a lane whose unstarted head may run; True when newly queued."""
        if lane.pending and not lane.running and not lane.scheduled and lane.error is None:
            lane.scheduled = True
            self.ready.append(lane)
            return True
        return False

    def _schedule(self, lane):
        """Ready ``lane`` and reserve the pumps demand lacks; returns how many.

        Runs under the state lock. A newly ready lane wakes exactly one parked
        pump. The caller starts the reserved pumps after releasing the lock
        (``_start_pumps``): no worker start ever runs under it.
        """
        if self._ready(lane):
            self._idle.notify()
        owed = self.active + len(self.ready) - len(self._pumps) - self._pump_reservations
        if owed <= 0:
            return 0
        if self._executor is None:
            from lib.observability import InstrumentedThreadPoolExecutor
            self._executor = InstrumentedThreadPoolExecutor(
                max_workers=self.idle_reuse_workers, metric_pool='task-event-delivery',
                thread_name_prefix='task-event-delivery')
        self._pump_reservations += owed
        return owed

    def _start_pumps(self, owed, lane):
        """Submit ``owed`` reserved pump jobs; call without the state lock.

        A refused start retains the unstarted head it left without a pump for
        explicit repair, preferring ``lane`` (the scheduling caller's), and
        returns the failure when that head is ``lane``'s. A refusal whose demand
        another pump already met changes nothing.
        """
        while owed:
            try:
                future = self._executor.submit(self._run)
            except BaseException as error:
                with self.condition:
                    self._pump_reservations -= owed
                    stranded = None
                    if self.ready and (self.active + len(self.ready)
                                       > len(self._pumps) + self._pump_reservations):
                        stranded = lane if lane.scheduled else self.ready[0]
                        self.ready.remove(stranded)
                        stranded.scheduled = False
                        stranded.error = error
                        stranded.task['_eventDeliveryError'] = type(error).__name__
                        self.metrics['failures'] += 1
                        self._notify_lane(stranded)
                    self.condition.notify_all()
                return error if stranded is lane else None
            owed -= 1
            with self.condition:
                self._pump_reservations -= 1
                self._pumps.add(future)
                self.metrics['peak_workers'] = max(self.metrics['peak_workers'], len(self._pumps))
            future.add_done_callback(self._pump_finished)
        return None

    def _pump_finished(self, future):
        # A retiring pump counted toward demand until now; hand off any lane
        # that became ready meanwhile.
        with self.condition:
            self._pumps.discard(future)
            lane = self.ready[0] if self.ready else None
            owed = 0 if lane is None else self._schedule(lane)
            self.condition.notify_all()
        if owed:
            # A refusal retains that lane's unstarted head; already-owned
            # siblings keep their existing pump lifetimes.
            self._start_pumps(owed, lane)

    def submit(self, lane, sequence, retained_bytes, persist, publish, *, timeout=None,
               projection_source=None, projection_bytes=0, projection_identity=None,
               progress_frame=None, persist_progress=None, progress_wire_bytes=0,
               progress_payload_bytes=0, accept_after_stop=False,
               batch_scope=None, persist_batch=None, batch_entry=None):
        # The batch port is optional: ``batch_entry(progress_frames)`` freezes
        # this item's entry and ``persist_batch(entries)`` persists a FIFO run
        # of entries of one ``batch_scope`` in one storage call.
        # Progress sources are shallow copies of one detached structural graph.
        # Charge that graph once until its last queued/in-flight owner releases it.
        if projection_source is not None and projection_identity is None:
            projection_identity = object()
        item = _Delivery(sequence, retained_bytes, persist, publish,
                         projection_identity=projection_identity,
                         progress_frames=None if progress_frame is None else [progress_frame],
                         persist_progress=persist_progress, progress_wire_bytes=progress_wire_bytes,
                         progress_payload_bytes=progress_payload_bytes,
                         batch_scope=batch_scope, persist_batch=persist_batch,
                         batch_entry=batch_entry)
        owed = 0
        with self.condition, accepted_publication_context() as accepted_context:
            self._settle_abandoned_projections()
            self._check_lane(lane)
            # Root acceptance is the child-to-parent ownership boundary, not
            # child disposal or callback return. Both merged and single frames
            # keep receiver identity without a revocable producer lifetime.
            item.context = accepted_context
            if progress_frame is not None:
                progress_frame.context = accepted_context.copy()
            abort_event = lane.task.get('abort_event')
            if not accept_after_stop and (lane.task.get('aborted') or (
                    abort_event is not None and abort_event.is_set())):
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
                    tail.batch_scope = batch_scope
                    tail.persist_batch = persist_batch
                    tail.batch_entry = batch_entry
                    tail.context = item.context
                    tail.retained_bytes = merged_bytes
                    tail.progress_wire_bytes += progress_wire_bytes
                    tail.progress_payload_bytes += progress_payload_bytes
                    self.bytes += extra_bytes
                    lane.accepted += 1
                    if projection_source is not None:
                        self._retain_projection(lane, LaneProjection(
                            projection_source, projection_bytes, projection_identity))
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
                self._retain_projection(
                    lane, LaneProjection(projection_source, projection_bytes, projection_identity))
            try:
                owed = self._schedule(lane)
            except BaseException as exc:
                lane.error = exc
                lane.task['_eventDeliveryError'] = type(exc).__name__
                self.metrics['failures'] += 1
                self._notify_lane(lane)
                raise EventDeliveryError('Task event delivery worker could not start') from exc
        if owed:
            # Accepted above; a refused start leaves this head retained for repair.
            error = self._start_pumps(owed, lane)
            if error is not None:
                raise EventDeliveryError('Task event delivery worker could not start') from error

    def _release_item(self, item):
        """Release private bytes and this item's reference to shared structure."""
        self.count -= 1
        self.pending_frames -= item.event_count
        self.bytes -= item.retained_bytes
        if item.projection_identity is not None:
            self._unref_projection(item.projection_identity)

    def _unref_projection(self, identity):
        """Drop one holder (an item or a lane); the last one frees the charge."""
        residency = self.projections[identity]
        residency[1] -= 1
        if residency[1] == 0:
            self.bytes -= residency[0]
            del self.projections[identity]

    def _retain_projection(self, lane, projection):
        """Publish the lane's newest projection; the lane holds each identity once.

        A drained lane keeps its structural source so later progress frames
        reuse it instead of copying the structure again. The lane's charge on
        an identity ends exactly once: when a new identity replaces it, or
        when the lane ends (``release_projection``, revocation, shutdown).
        """
        held = lane.projection.identity
        if projection.identity is not held:
            if projection.identity is not None:
                self.projections[projection.identity][1] += 1
            if held is not None:
                lane.projection_finalizer.detach()
                self._unref_projection(held)
            lane.projection_finalizer = None
            self._retaining.discard(lane)
            if projection.identity is not None:
                # A lane collected without release hands its charge back on the
                # next locked observation; the finalizer itself takes no lock.
                finalizer = weakref.finalize(
                    lane, self._abandoned_projections.append, projection.identity)
                finalizer.atexit = False
                lane.projection_finalizer = finalizer
                self._retaining.add(lane)
        lane.projection = projection

    def _settle_abandoned_projections(self):
        while self._abandoned_projections:
            self._unref_projection(self._abandoned_projections.popleft())

    def release_projection(self, lane):
        """A lane whose owner ended (terminal, handoff) frees its retained source."""
        with self.condition:
            self._retain_projection(lane, NO_LANE_PROJECTION)

    def _revoke_locked(self, lane):
        """Release every retained item of a lane whose publication authority ended."""
        lane.revoked = True
        lane.error = None
        lane.task['_eventDeliveryRevoked'] = True
        self.metrics['revoked_events'] += sum(entry.event_count for entry in lane.pending)
        for entry in lane.pending:
            if entry.profile is not None:
                self.profiler.observe('finish', entry.profile, 'revoked')
            self._release_item(entry)
        lane.pending.clear()
        lane.completed = lane.accepted
        self._retain_projection(lane, NO_LANE_PROJECTION)
        self.lanes.discard(lane)
        lane.task.pop('_eventDeliveryPending', None)
        self._notify_lane(lane)
        self.condition.notify_all()

    def revoke(self, lane):
        """An owner whose publication ended releases its never-acknowledged items.

        Returns False while a persistence attempt is entered: that pump keeps
        the head until it returns. Later submissions are refused as revoked.
        """
        with self.condition:
            if lane.running:
                return False
            self._revoke_locked(lane)
            return True

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

    def backlog(self, lane):
        """Observe one lane under the queue lock; never waits, repairs or schedules."""
        with self.condition:
            return EventBacklog(max(0, lane.accepted - lane.completed), lane.error is not None)

    def flush(self, lane, *, timeout=30.0, recover=True, cancel_check=None,
              slow_after=None, on_slow=None, on_release=None):
        if self.profiler is None:
            return self._flush(lane, timeout=timeout, recover=recover, cancel_check=cancel_check,
                               slow_after=slow_after, on_slow=on_slow, on_release=on_release)
        start = self.profiler.observe('clock_ns')
        failed = True
        try:
            result = self._flush(lane, timeout=timeout, recover=recover, cancel_check=cancel_check,
                                 slow_after=slow_after, on_slow=on_slow, on_release=on_release)
            failed = False
            return result
        finally:
            self.profiler.observe('barrier', start, failed)

    def _flush(self, lane, *, timeout=30.0, recover=True, cancel_check=None,
               slow_after=None, on_slow=None, on_release=None):
        """Wait until every event accepted before this call is committed.

        Returns how many of those events were still unacknowledged at entry.
        ``on_slow(pending)`` observes a barrier still waiting after
        ``slow_after`` seconds. ``on_release(BarrierCost)`` observes the
        storage work of the awaited events once the barrier returns or raises.
        Both run once, outside the coordinator lock; the target is fixed at
        entry and never grows.
        """
        started = time.monotonic()
        deadline = None if timeout is None else started + timeout
        owed = 0
        with self.condition:
            lane_condition = self._lane_condition(lane)
            target = lane.accepted
            awaited = max(0, target - lane.completed)
            # The awaited items are exactly those pending now; keep only their
            # timing records, never their callbacks or detached inputs.
            timings = None if on_release is None else [item.timing for item in lane.pending]
            if recover and lane.error is not None:
                lane.error = None
                lane.head_ready_at = time.monotonic()
                owed = self._schedule(lane)
        try:
            if owed:
                # A refused start retains the head; the wait below reports it.
                self._start_pumps(owed, lane)
            with self.condition:
                if on_slow is None or slow_after is None:
                    self._await_target(lane, lane_condition, target, deadline, cancel_check)
                    return awaited
                if self._await_target(lane, lane_condition, target, deadline, cancel_check,
                                      until=started + max(0.0, slow_after)):
                    return awaited
                pending = max(0, target - lane.completed)
            on_slow(pending)
            with self.condition:
                self._await_target(lane, lane_condition, target, deadline, cancel_check)
            return awaited
        finally:
            if timings is not None:
                # The owning pump writes these records outside the lock. They
                # are final once the wait succeeded; a refused wait reports the
                # partial cost it observed.
                on_release(BarrierCost(sum(timing.attempts for timing in timings),
                                       sum(timing.persisted for timing in timings),
                                       sum(timing.queued for timing in timings)))

    def _await_target(self, lane, lane_condition, target, deadline, cancel_check, until=None):
        """Barrier wait under the coordinator lock; False only when ``until`` passes first."""
        while lane.completed < target:
            if lane.error is not None:
                raise EventDeliveryError('Task event delivery failed; pending evidence retained') from lane.error
            if cancel_check is not None and cancel_check():
                from lib.llm.llm_errors import AbortedError
                raise AbortedError('Agent stopped at event durability barrier')
            now = time.monotonic()
            remaining = None if deadline is None else deadline - now
            if remaining is not None and remaining <= 0:
                raise EventDeliveryError('Task event durability barrier timed out; pending evidence retained')
            if until is not None and now >= until:
                return False
            wait_for = remaining
            if cancel_check is not None:
                wait_for = 0.1 if remaining is None else min(0.1, remaining)
            if until is not None:
                wait_for = until - now if wait_for is None else min(wait_for, until - now)
            lane_condition.wait(wait_for)
        return True

    def _claim_run(self, lane):
        """Under the lock: the items one storage call persists, head first.

        An unstarted head claims following items of its batch scope within
        the existing RPC row bound, never waiting for more, and each is
        marked started so none absorbs later progress. An unacknowledged
        attempt may already be committed: a started head keeps exactly the
        run it froze through failure and explicit repair. Items without a
        batch port end runs; a run of one persists through its own port.
        """
        head = lane.pending[0]
        if not head.persistence_started:
            length = 1
            rows = head.event_count
            if head.persist_batch is not None:
                for item in islice(lane.pending, 1, None):
                    if item.persist_batch is None or item.batch_scope != head.batch_scope:
                        break
                    if rows + item.event_count > self.structural_batch_limit:
                        break
                    length += 1
                    rows += item.event_count
            head.run_length = length
            for item in islice(lane.pending, length):
                item.persistence_started = True
        # The run is one batch: its head's timing record carries its cost.
        head.timing.queued += time.monotonic() - max(head.accepted_at, lane.head_ready_at)
        return list(islice(lane.pending, head.run_length))

    def _persist(self, run, lane):
        from lib.tasks_pkg.persistence_wait import wait_for_persistence
        head = run[0]
        profiled = [item for item in run if item.profile is not None]
        public = any(item.sequence >= 0 for item in run)

        def operation():
            if len(run) == 1:
                if head.progress_frames is not None:
                    return head.context.run(head.persist_progress, head.progress_frames)
                return head.context.run(head.persist)
            # Entries freeze once, in FIFO order: every retry persists the
            # same objects, so the storage owner reconciles exact rows.
            for item in run:
                if item.frozen_entry is _UNFROZEN:
                    item.frozen_entry = item.context.run(item.batch_entry, item.progress_frames)
            return head.context.run(head.persist_batch, [item.frozen_entry for item in run])

        def persist():
            head.timing.attempts += 1
            starts = [self.profiler.observe('attempt_start', item.profile) for item in profiled]
            failed = True
            outcome = 'interrupted'
            try:
                result = operation()
                failed = False
                outcome = 'ok'
                return result
            except EventDeliveryRevoked:
                outcome = 'authority_revoked'
                raise
            except Exception as error:
                if profiled:
                    outcome = self.profiler.observe('failure_outcome', error)
                if public:
                    # Only a public frame is withheld from push; FIFO work
                    # without a sequence (child evidence, checkpoints) is not.
                    lane.task.setdefault('_pushWithheldAt', time.time())
                    lane.task['_pushWithheldCount'] = min((1 << 31) - 1,
                        int(lane.task.get('_pushWithheldCount') or 0) + 1)
                raise
            finally:
                for item, start in zip(profiled, starts):
                    self.profiler.observe('attempt_end', item.profile, start, failed, outcome)

        def retry():
            with self.condition:
                self.metrics['retries'] += 1

                self.condition.notify_all()

        # Accepted evidence must still commit after user Stop. Shutdown hands
        # the exact head to existing debt/recovery rather than losing ownership.
        started = time.monotonic()
        try:
            wait_for_persistence(lane.task, persist, label='task event',
                                 cancellable=False, stopping=lambda: (
                                     self._shutdown_deadline is not None
                                     and time.monotonic() >= self._shutdown_deadline),
                                 on_retry=retry)
        finally:
            head.timing.persisted += time.monotonic() - started

    def _await_ready(self):
        """Park until a lane is ready; False once this pump should retire.

        A wake-up that finds nothing ready is spurious: the pump keeps its
        original idle deadline instead of retiring and being resubmitted.
        """
        deadline = None
        while not self.ready:
            if self.closed or len(self._pumps) > self.idle_reuse_workers:
                return False
            now = time.monotonic()
            if deadline is None:
                deadline = now + self.idle_timeout
            elif now >= deadline:
                # Future completion reschedules ready lanes after this pump
                # exits; no departing pump loses a handoff.
                return False
            self._idle.wait(deadline - now)
        return True

    def _run(self):
        current = threading.current_thread()
        with self.condition:
            self.workers.add(current)
        try:
            while True:
                with self.condition:
                    if not self._await_ready():
                        return
                    lane = self.ready.popleft()
                    lane.scheduled = False
                    if lane.error is not None or not lane.pending:
                        continue
                    lane.running = True
                    self.active += 1
                    run = self._claim_run(lane)
                for item in run:
                    if item.profile is not None:
                        self.profiler.observe('dispatch', item.profile)
                try:
                    self._persist(run, lane)
                    for item in run:
                        if item.profile is not None:
                            self.profiler.observe('acknowledge', item.profile)
                except EventDeliveryRevoked:
                    with self.condition:
                        lane.running = False
                        self.active -= 1
                        self._revoke_locked(lane)
                    del item, run, lane
                    continue
                except BaseException as exc:
                    if any(item.profile is not None for item in run):
                        self.profiler.observe('failed_round')
                    with self.condition:
                        lane.running = False
                        self.active -= 1
                        lane.error = exc
                        self.metrics['failures'] += 1
                        lane.task['_eventDeliveryError'] = type(exc).__name__
                        self._notify_lane(lane)
                        self.condition.notify_all()
                    logger.warning('[TaskEvents] retained failed delivery task=%s seq=%s run=%d error=%s',
                                   str(lane.task.get('id') or '?')[:8], run[0].sequence, len(run),
                                   type(exc).__name__)
                    continue
                # Publish/replay share one exclusive cursor. The memory
                # buffer may contain accepted future frames, but consumers
                # cannot observe them before the authority commit.
                lane.task.pop('_pushWithheldAt', None)
                lane.task.pop('_pushWithheldCount', None)
                try:
                    for item in run:
                        try:
                            if item.progress_frames is not None:
                                for frame in item.progress_frames:
                                    self._publish(item, lane, frame.sequence, frame.context,
                                                  frame.publish)
                            else:
                                self._publish(item, lane, item.sequence, item.context, item.publish)
                        except Exception as exc:
                            # Visibility hints cannot invalidate a committed event or
                            # cause repeated authority writes; reconnect repairs push.
                            logger.debug('[TaskEvents] push observer failed: %s', type(exc).__name__)
                finally:
                    for item in run:
                        if item.profile is not None:
                            self.profiler.observe('finish', item.profile, 'committed')
                    with self.condition:
                        settled = time.monotonic()
                        for item in run:
                            lane.pending.popleft()
                            lane.completed += item.event_count
                            self._release_item(item)
                            self.metrics['completed'] += item.event_count
                            if item.sequence >= 0:
                                self.metrics['committed'] += item.event_count
                            self.metrics['max_lag_ms'] = max(self.metrics['max_lag_ms'],
                                (settled - item.accepted_at) * 1000)
                        lane.running = False
                        self.active -= 1
                        lane.head_ready_at = settled
                        self._notify_lane(lane)
                        lane.task.pop('_eventDeliveryError', None)
                        if not lane.pending:
                            self.lanes.discard(lane)
                            lane.task.pop('_eventDeliveryPending', None)
                        # This pump claims the next ready lane itself: no wake-up.
                        self._ready(lane)
                # The pump keeps no batch callback or lane across idle; only
                # the lane itself retains its projection source.
                del item, run, lane
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
            self._settle_abandoned_projections()
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
        repairs = []
        with self.condition:
            self.closed = True

            self._shutdown_deadline = deadline
            self.condition.notify_all()
            self._idle.notify_all()
            for lane in tuple(self.lanes):
                if lane.error is not None:
                    lane.error = None
                    lane.head_ready_at = time.monotonic()
                    repairs.append((self._schedule(lane), lane))
        for owed, lane in repairs:
            if owed:
                # A refused start retains that head; the drain below reports it.
                self._start_pumps(owed, lane)
        with self.condition:
            while self.count or self._pumps or self._pump_reservations:
                if any(l.error is not None for l in self.lanes):
                    return False
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self.condition.wait(remaining)
            # Every lane ends with the coordinator: release retained sources.
            for lane in tuple(self._retaining):
                self._retain_projection(lane, NO_LANE_PROJECTION)
            self._settle_abandoned_projections()
            self.condition.notify_all()
            workers = tuple(self.workers)
        for worker in workers:
            if worker is not threading.current_thread():
                worker.join(max(0.0, deadline - time.monotonic()))
        with self.condition:
            finished = not self.workers and not self._pumps and not self._pump_reservations
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


def invocation_evidence_lane(task):
    """A child invocation's ordered lane for its Request Inspector appends.

    Children never bind a root lane: their required sink and publication fences
    keep event delivery. This lane owns only the durable appends of the child's
    Inspector stream, so the barriers below can await or repair them.
    """
    existing = task.get('_invocationEvidenceLane')
    if existing is not None:
        return existing
    with _COORDINATOR_LOCK:
        return task.setdefault('_invocationEvidenceLane', DeliveryLane(task))


def _ordered_lane(task):
    """The task's FIFO: a root delivery lane or a child's Inspector evidence lane."""
    return task.get('_eventDeliveryLane') or task.get('_invocationEvidenceLane')


def detach_task_event_delivery(task):
    """Drain before a composition retargets this carrier's Turn identity."""
    lane = task.get('_eventDeliveryLane')
    if lane is None:
        return
    with lane.producer_lock:
        flush_task_events(task)
        coordinator().release_projection(lane)
        task['_eventDeliveryDetached'] = True
        task.pop('_eventDeliveryLane', None)
        task.pop('_eventVisibilityCursor', None)


def task_event_ordering(task):
    """The lock ``append_event`` holds while it sequences this task's events.

    It binds the managed root lane exactly as ``append_event`` does. A caller
    that would otherwise wait for it while holding its own lock takes this one
    first. A task without a managed root lane orders events without it.
    """
    lane = delivery_lane_for(task)
    return nullcontext() if lane is None else lane.producer_lock


def advance_event_visibility(task, sequence):
    lock = task.get('events_lock')
    with lock if lock is not None else nullcontext():
        task['_eventVisibilityCursor'] = max(
            int(task.get('_eventVisibilityCursor') or 0), int(sequence) + 1)
        receipt = task.get('_eventDeliveryReceipt')
        if isinstance(receipt, dict):
            receipt['visibleCursor'] = task['_eventVisibilityCursor']


def flush_task_events(task, *, timeout=None, cancel_check=None, slow_after=None, on_slow=None,
                      on_release=None):
    """Barrier over this task's accepted events; returns how many it awaited.

    ``slow_after``/``on_slow`` only observe a slow barrier and ``on_release``
    its awaited events' ``BarrierCost`` (see ``EventDeliveryCoordinator._flush``);
    a task without a lane returns 0 and observes nothing.
    """
    lane = _ordered_lane(task)
    if lane is None:
        return 0
    awaited = coordinator().flush(lane, timeout=timeout, cancel_check=cancel_check,
                                  slow_after=slow_after, on_slow=on_slow, on_release=on_release)
    task['_eventDeliveryReceipt'] = {
        'contract': 'tofu.task-event-delivery/v1',
        'accepted': lane.accepted, 'completed': lane.completed,
        'revoked': lane.revoked,
        'visibleCursor': int(task.get('_eventVisibilityCursor') or 0),
    }
    return awaited


def task_event_backlog(task) -> EventBacklog:
    """This task's undurable accepted events; a task without a lane owes none."""
    lane = _ordered_lane(task)
    if lane is None:
        return EventBacklog(0, False)
    return coordinator().backlog(lane)


def event_delivery_metrics():
    """Read existing residency without constructing workers or probing hosts."""
    with _COORDINATOR_LOCK:
        owner = _COORDINATOR
    return {} if owner is None else owner.snapshot()


def shutdown_event_delivery(timeout=10.0):
    with _COORDINATOR_LOCK:
        owner = _COORDINATOR
    return True if owner is None else owner.close(timeout)
