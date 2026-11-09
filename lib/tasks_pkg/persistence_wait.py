"""Agent persistence backpressure, independent of creator and presentation.

Entries: wait_for_persistence; checkpoint_invocation (tool/ready/terminal
effect boundaries) and checkpoint_model_dispatch (the pre-request boundary,
which never waits for request diagnostics). Callers supply a replay-safe,
frozen operation (receipt ID/payload, an idempotent checkpoint or an
owner-scoped read). This
retries only that operation, never preparation, a model request, a tool, or an
entire agent. RPC deadlines remain bounded; transient pressure has no
agent-attempt ceiling. No worker, queue, durable journal or backend is created
here.
"""
from __future__ import annotations

import logging
import time

import threading
from collections.abc import Callable
from typing import TypeVar

from lib.agent_core.storage_errors import (
    describe_storage_failure, is_transient_storage_error,
)
from lib.log import get_logger, log_event
from lib.agent_core.storage_waits import storage_wait_owner

logger = get_logger(__name__)
T = TypeVar('T')
_BASE_DELAY_SECONDS = 0.1
_MAX_DELAY_SECONDS = 2.0
_CONTROL_INTERVAL_SECONDS = 0.1
# Announce, never shorten: the event durability barrier keeps waiting.
SLOW_EVENT_BARRIER_SECONDS = 1.0


def wait_for_persistence(task: dict, operation: Callable[[], T], *, label: str,
                         cancellable: bool = True,
                         stopping: Callable[[], bool] | None = None,
                         on_retry: Callable[[], None] | None = None,
                         transient: Callable[[BaseException], bool] = is_transient_storage_error,
                         effect_free: bool = False) -> T:
    """Retain one exact operation until ACK, explicit stop, or permanent refusal.

    Cancellation before an effect raises AbortedError, not a storage failure.
    Accepted event/terminal debt uses cancellable=False: Stop halts execution,
    not persistence of already-observed evidence. Shutdown returns the original
    failure to the existing recovery/debt owner. An ``effect_free`` read owes no
    debt, so Stop and shutdown both cancel it. ``transient`` names the failures
    waited out (transient storage codes by default); a caller may add its own
    typed local deadline, never a permanent refusal. Concurrent waits retain
    distinct tokens; none can clear a sibling's liveness witness or phase.
    A supervised storage client also waits inside ``operation`` while its
    authority lives; this owner presents that wait and ends it on Stop or
    shutdown exactly as for a raised failure.
    """
    from lib.tasks_pkg.invocation import invocation_abort_requested
    from lib.llm.llm_errors import AbortedError
    token = object()
    lock = task.setdefault('_storageWaitLock', threading.RLock())
    with lock:
        waits = task.setdefault('_storageWaits', {})
    phase = None
    attempt = 0
    last_error = None
    next_log = 0.0

    beacon = task.get('_invocationProgressBeacon')
    progress_key = task.get('_invocationProgressKey') or task.get('id')
    acknowledged = False
    waiting_beacon = None

    def check_control():
        if not effect_free and last_error is not None and (
                task.get('_abort_reason') == 'server_shutdown'
                or (stopping is not None and stopping())):
            raise last_error
        if (cancellable or effect_free) and invocation_abort_requested(task):
            raise AbortedError('Agent stopped while waiting for persistence')

    def present_wait():
        nonlocal phase, waiting_beacon
        if phase is not None:
            return
        phase = {'phase': 'retrying', 'detail': 'Waiting for storage',
                 'storageOperation': label, 'since': time.time()}
        with lock:
            if not waits:
                task['_storageWaitPreviousPhase'] = task.get('phase')
            waits[token] = phase
            task['phase'] = phase

        if callable(getattr(beacon, 'begin_persistence_wait', None)):
            beacon.begin_persistence_wait(progress_key, token)
            waiting_beacon = beacon
        # Pure presentation hint; it is neither a durability ACK
        # nor execution proof. A failed hint cannot poison work.
        sink = task.get('_invocationEventSink')
        if callable(sink):
            from lib.agent_core.events import build_phase, Phase
            try:
                sink(build_phase(Phase.RETRYING, detail='Waiting for storage'))
            except Exception as hint_error:
                logger.debug('Storage wait hint unavailable: %s', type(hint_error).__name__)

    def observe_in_call_wait(error):
        nonlocal last_error
        last_error = error
        check_control()
        present_wait()

    try:
        while True:
            check_control()
            try:
                with storage_wait_owner(observe_in_call_wait):
                    result = operation()
                last_error = None

                acknowledged = True
                check_control()
                if phase is not None:
                    logger.info('[AgentPersistence] task=%s operation=%s succeeded after '
                                '%d transient failures (%.1fs)', str(task.get('id') or ''),
                                label, attempt, time.time() - phase['since'])
                return result
            except Exception as error:
                if not transient(error):
                    raise
                last_error = error
                attempt += 1
                present_wait()
                phase['attempt'] = attempt
                if on_retry is not None:
                    on_retry()
                now = time.monotonic()
                if now >= next_log:
                    logger.warning('[AgentPersistence] task=%s operation=%s waiting attempt=%d '
                                   'after %s', str(task.get('id') or ''), label, attempt,
                                   describe_storage_failure(error))
                    next_log = now + 30.0
                # Per-operation bounded backoff, not a task failure deadline.
                until = now + min(_MAX_DELAY_SECONDS,
                                  _BASE_DELAY_SECONDS * 2 ** min(attempt - 1, 8))
                while time.monotonic() < until:
                    check_control()
                    time.sleep(min(_CONTROL_INTERVAL_SECONDS,
                                   max(0.0, until - time.monotonic())))
    finally:
        with lock:
            waits.pop(token, None)
            if phase is not None and task.get('phase') is phase:
                task['phase'] = next(iter(waits.values()), task.get('_storageWaitPreviousPhase'))
            if not waits:
                task.pop('_storageWaitPreviousPhase', None)
        if waiting_beacon is not None:
            waiting_beacon.end_persistence_wait(progress_key, token, acknowledged=acknowledged)
        # Keep the empty task-local mapping/lock: active siblings may hold them.
        # Residency follows concurrent owners, never retry count or payload size.


def _announce_slow_event_barrier(task: dict, *, checkpoint: str, round_num: int,
                                 elapsed: float, pending: int) -> None:
    """Log only: an emitted event would queue behind the FIFO being awaited."""
    task_id = str(task.get('id') or '')
    elapsed_ms = int(elapsed * 1000)
    try:
        log_event(logger, logging.WARNING, 'event_barrier_slow',
                  '[EventBarrier] slow task=%s conv=%s checkpoint=%s checkpointRound=%s '
                  'elapsedMs=%d pendingEvents=%d',
                  task_id, task.get('convId') or '', checkpoint, round_num, elapsed_ms, pending,
                  task_id=task_id, checkpoint=checkpoint, elapsed_ms=elapsed_ms,
                  pending_events=pending)
    except Exception as error:
        logger.debug('[EventBarrier] slow record dropped: %s', type(error).__name__)


def checkpoint_invocation(task: dict, messages: list, round_num: int, *, phase: str,
                          await_events: bool = True) -> int:
    """One effect-boundary checkpoint for all agent compositions.

    Presentation/domain ports own their schema; the invocation owns ordering.
    The domain sink (a Swarm/Evolution write-ahead record) always ACKs first.
    Ready receipts and tool intent must ACK before another paid request or
    external effect. ``await_events=False`` is the pre-request boundary: accepted
    request diagnostics stay in the ordered FIFO for the next awaited checkpoint,
    while a lane already holding a failed head still blocks for repair/refusal.
    A child's lane holds only its Request Inspector snapshots, so its ``ready``
    boundary (before the next paid request) behaves the same way; its tool
    intent and terminal settle still drain them.
    Returns how many accepted task events the durability barrier awaited.
    """
    from lib.tasks_pkg.event_delivery import flush_task_events, task_event_backlog
    from lib.tasks_pkg.invocation import invocation_abort_requested, is_child_invocation
    from lib.tasks_pkg.round_prep_timing import round_prep_step
    task['_invocationCheckpointPhase'] = phase
    sink = task.get('_invocationCheckpointSink')
    if callable(sink):
        with round_prep_step(task, 'barrier.sink'):
            wait_for_persistence(task, lambda: sink(task, messages, round_num),
                                 label=f'checkpoint:{phase}')
    if phase == 'ready' and is_child_invocation(task):
        await_events = False
    if not await_events and not task_event_backlog(task).failed:
        return 0
    started = time.monotonic()
    announced = []
    costs = []

    def announce(pending):
        announced.append(pending)
        _announce_slow_event_barrier(task, checkpoint=phase, round_num=round_num,
                                     elapsed=time.monotonic() - started, pending=pending)

    def released(outcome, awaited=None):
        if announced:
            # Storage calls/time of the awaited events beside their wait for a
            # delivery pump: slow storage and local queuing read differently.
            cost = costs[0] if costs else None
            logger.info('[EventBarrier] released task=%s conv=%s checkpoint=%s '
                        'checkpointRound=%s elapsedMs=%d awaitedEvents=%s persistCalls=%s '
                        'persistMs=%s queuedMs=%s outcome=%s',
                        str(task.get('id') or ''), task.get('convId') or '', phase, round_num,
                        int((time.monotonic() - started) * 1000),
                        '-' if awaited is None else awaited,
                        '-' if cost is None else cost.persist_calls,
                        '-' if cost is None else int(cost.persist_seconds * 1000),
                        '-' if cost is None else int(cost.queued_seconds * 1000), outcome)

    try:
        with round_prep_step(task, 'barrier.events'):
            awaited = flush_task_events(task, timeout=None,
                                        cancel_check=lambda: invocation_abort_requested(task),
                                        slow_after=SLOW_EVENT_BARRIER_SECONDS, on_slow=announce,
                                        on_release=costs.append)
    except BaseException as error:
        released(type(error).__name__)
        raise
    released('ok', awaited)
    return awaited


def checkpoint_model_dispatch(task: dict, messages: list, round_num: int) -> int:
    """The pre-request boundary: the domain write-ahead ACKs, diagnostics do not.

    Owner decision 2026-10-28: saving request diagnostics never blocks a paid
    model call. Accepted events (the Request Inspector snapshot among them)
    persist behind the request in the same ordered FIFO; the next
    tools_in_flight, ready or terminal barrier awaits them (a child's evidence
    lane: tools_in_flight or its terminal settle). RoundPrepTiming
    names the sink and any repair wait (``barrierMs``) and records the events
    still undurable at dispatch and those awaited. Returns the awaited count.
    """
    from lib.tasks_pkg.event_delivery import task_event_backlog
    from lib.tasks_pkg.round_prep_timing import note_round_prep, round_prep_step
    with round_prep_step(task, 'barrier'):
        awaited = checkpoint_invocation(task, messages, round_num, phase='model_in_flight',
                                        await_events=False)
    note_round_prep(task, barrierAwaitedEvents=awaited,
                    barrierPendingEvents=task_event_backlog(task).pending)
    return awaited
