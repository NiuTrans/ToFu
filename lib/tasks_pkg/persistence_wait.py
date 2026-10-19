"""Agent persistence backpressure, independent of creator and presentation.

Entry: wait_for_persistence. Callers supply a replay-safe, frozen operation
(receipt ID/payload or an idempotent checkpoint). This retries only that write,
never preparation, a model request, a tool, or an entire agent. RPC deadlines
remain bounded; transient pressure has no agent-attempt ceiling. No worker,
queue, durable journal or backend is created here.
"""
from __future__ import annotations

import time

import threading
from collections.abc import Callable
from typing import TypeVar

from lib.log import get_logger
from lib.storage.retry import is_transient_storage_error

logger = get_logger(__name__)
T = TypeVar('T')
_BASE_DELAY_SECONDS = 0.1
_MAX_DELAY_SECONDS = 2.0
_CONTROL_INTERVAL_SECONDS = 0.1


def wait_for_persistence(task: dict, operation: Callable[[], T], *, label: str,
                         cancellable: bool = True,
                         stopping: Callable[[], bool] | None = None,
                         on_retry: Callable[[], None] | None = None) -> T:
    """Retain one exact operation until ACK, explicit stop, or permanent refusal.

    Cancellation before an effect raises AbortedError, not a storage failure.
    Accepted event/terminal debt uses cancellable=False: Stop halts execution,
    not persistence of already-observed evidence. Shutdown returns the original
    failure to the existing recovery/debt owner. Concurrent waits retain distinct
    tokens; none can clear a sibling's liveness witness or phase.
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
        if last_error is not None and (
                task.get('_abort_reason') == 'server_shutdown'
                or (stopping is not None and stopping())):
            raise last_error
        if cancellable and invocation_abort_requested(task):
            raise AbortedError('Agent stopped while waiting for persistence')

    try:
        while True:
            check_control()
            try:
                result = operation()
                last_error = None

                acknowledged = True
                check_control()
                return result
            except Exception as error:
                if not is_transient_storage_error(error):
                    raise
                last_error = error
                attempt += 1
                if phase is None:
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
                phase['attempt'] = attempt
                if on_retry is not None:
                    on_retry()
                now = time.monotonic()
                if now >= next_log:
                    logger.warning('[AgentPersistence] task=%s operation=%s waiting attempt=%d',
                                   str(task.get('id') or '')[:8], label, attempt)
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


def checkpoint_invocation(task: dict, messages: list, round_num: int, *, phase: str) -> None:
    """One effect-boundary checkpoint for all agent compositions.

    Presentation/domain ports own their schema; the invocation owns ordering.
    Ready receipts must ACK before another paid request or external effect.
    """
    from lib.tasks_pkg.event_delivery import flush_task_events
    from lib.tasks_pkg.invocation import invocation_abort_requested
    task['_invocationCheckpointPhase'] = phase
    sink = task.get('_invocationCheckpointSink')
    if callable(sink):
        wait_for_persistence(task, lambda: sink(task, messages, round_num),
                             label=f'checkpoint:{phase}')
    flush_task_events(task, timeout=None,
                      cancel_check=lambda: invocation_abort_requested(task))
