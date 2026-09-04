"""Chat worker submission onto the serving loop and agent executor.

This module is the single owner of the process-level executor and serving-loop
references. ``spawn_task`` leaves accepted work pending while it waits in the
bounded executor, then publishes worker entry before invoking the task runner.
A daemon thread is reserved for CLI/test contexts with no serving loop.
"""

from __future__ import annotations

import asyncio
import threading
import time

from lib.log import get_logger

logger = get_logger(__name__)

_agent_executor = None
_serving_loop = None
_QUEUE_STATUS_POLL_SECONDS = 10.0


def set_agent_executor(executor) -> None:
    """Install, or during shutdown clear, the dedicated agent executor."""
    global _agent_executor
    _agent_executor = executor


def set_serving_loop(loop) -> None:
    """Install, or during shutdown clear, the server's main asyncio loop."""
    global _serving_loop
    _serving_loop = loop


def _finalize_rejected_submission(task: dict, error: Exception) -> None:
    """Settle a bound task when scheduling or worker entry failed."""
    from lib.agent_core.worker_executor import AgentExecutorQueueFull
    from lib.error_envelope import make_envelope
    from lib.tasks_pkg.manager import finalize_chat_task_error

    config = task.get('config') or {}
    queue_full = isinstance(error, AgentExecutorQueueFull)
    envelope = make_envelope(
        'server_busy' if queue_full else 'task_start_failed',
        detail=(
            'The bounded server AI-task queue is full; retry after an active '
            'task finishes.'
            if queue_full else
            'The agent worker could not begin task execution.'
        ),
        model=str(config.get('model') or task.get('model') or ''),
        context='task-start',
        source='tasks.spawn',
        raw=str(error),
    )
    finalize_chat_task_error(
        task,
        envelope,
        flow_reason='executor_start_failed',
    )


def _mark_worker_started(
    task: dict,
    worker_started: threading.Event | None = None,
    scheduling_phase_lock: threading.Lock | None = None,
) -> bool:
    """Cross the durable pending→running boundary on the physical worker."""
    phase_lock = scheduling_phase_lock or threading.Lock()
    with phase_lock:
        return _mark_worker_started_locked(task, worker_started)


def _mark_worker_started_locked(
    task: dict,
    worker_started: threading.Event | None,
) -> bool:
    """Commit worker entry while excluding a stale queued-phase publisher."""
    task_id = str(task.get('id') or '')
    terminal = {'done', 'error', 'aborted', 'interrupted'}
    if str(task.get('status') or '') in terminal:
        return False

    attempt_id = str(task.get('_attemptId') or task.get('attemptId') or '')
    if attempt_id:
        from lib.tasks_pkg.manager._registry import task_user_id
        from lib.turn_lifecycle import mark_task_started

        started = mark_task_started(
            attempt_id,
            task_id,
            user_id=task_user_id(task),
        )
        if started is None or str(started.get('status') or '') != 'running':
            raise RuntimeError('bound attempt rejected physical worker entry')

    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    registered = chat_task_runtime.get(task_id) if task_id else None
    if registered is task:
        if not chat_task_runtime.mark_running(task_id):
            return False
    else:
        # CLI/tests may intentionally use an unregistered task carrier.
        task['status'] = 'running'
    worker_started_at = time.time()
    task['_workerStartedAt'] = worker_started_at
    # Queue residence is not execution silence. Reset both reaper baselines at
    # the physical-entry boundary so a task that legitimately waited longer
    # than the stuck threshold is not force-failed immediately after acquiring
    # a worker (the phase persistence below is best-effort and may itself fail).
    task['_t_last_event'] = worker_started_at
    task['_dispatch_heartbeat'] = worker_started_at
    project_path = str((task.get('config') or {}).get('projectPath') or '')
    if project_path:
        try:
            from lib.conversations.project_brain import (
                note_isolated_workspace_signal,
            )
            note_isolated_workspace_signal(task, project_path)
        except Exception as error:
            # Workspace detection is coordination, not task admission. A later
            # todo/file signal still creates the same deterministic work item.
            logger.debug(
                '[Spawn] isolated Project work signal failed task=%s: %s',
                task_id[:8] or '?', error,
            )
    try:
        from lib.agent_core.events import Phase, emit_phase
        emit_phase(
            task,
            Phase.WORKING,
            detail='Agent worker acquired; starting the task…',
            detailKey='stream.phase.workerStarting',
        )
    except Exception as error:
        logger.debug(
            '[Spawn] worker-entry phase emit failed task=%s: %s',
            task_id[:8] or '?', error,
        )
    if worker_started is not None:
        worker_started.set()
    return True


def _owned_task_callable(
    run_task,
    task,
    worker_started: threading.Event,
    scheduling_phase_lock: threading.Lock,
):
    def _run_owned_task():
        if not _mark_worker_started(
                task, worker_started, scheduling_phase_lock):
            return None
        return run_task(task)

    _run_owned_task._tofu_task_id = str(task.get('id') or '')  # type: ignore[attr-defined]
    return _run_owned_task


def _queue_wait_bucket(wait_seconds: int) -> int:
    """Sparse heartbeat buckets: 20s initially, then once per minute."""
    if wait_seconds < 60:
        return wait_seconds // 20
    return 2 + (wait_seconds // 60)


def _queued_phase_candidate(task: dict) -> tuple | None:
    """Read only in-memory evidence; persistence is scheduled only on change."""
    snapshot = agent_scheduling_snapshot(str(task.get('id') or ''))
    if snapshot.get('taskState') != 'queued':
        return None
    try:
        position = max(1, int(snapshot.get('queuePosition') or 1))
        active = max(0, int(snapshot.get('active') or 0))
        capacity = max(1, int(snapshot.get('capacity') or 1))
        wait_seconds = max(
            0, int(float(snapshot.get('queuedForSeconds') or 0)))
    except (TypeError, ValueError, OverflowError):
        return ()
    # Total queue depth is payload evidence but not an invalidation key: a new
    # arrival behind this task must not repaint every existing queue resident.
    return (
        position,
        active,
        capacity,
        _queue_wait_bucket(wait_seconds),
    )


def _publish_executor_queue_phase(
    task: dict,
    worker_started: threading.Event,
    scheduling_phase_lock: threading.Lock,
    previous_signature: tuple | None,
) -> tuple[bool, tuple | None]:
    """Publish one truthful queue snapshot, ordered before worker entry."""
    with scheduling_phase_lock:
        if worker_started.is_set() or str(task.get('status') or '') in {
                'done', 'error', 'aborted', 'interrupted'}:
            return False, previous_signature
        snapshot = agent_scheduling_snapshot(str(task.get('id') or ''))
        if snapshot.get('taskState') != 'queued':
            return False, previous_signature
        try:
            position = max(1, int(snapshot.get('queuePosition') or 1))
            queued = max(1, int(snapshot.get('queued') or 1))
            active = max(0, int(snapshot.get('active') or 0))
            capacity = max(1, int(snapshot.get('capacity') or 1))
            wait_seconds = max(
                0, int(float(snapshot.get('queuedForSeconds') or 0)))
        except (TypeError, ValueError, OverflowError):
            return True, previous_signature
        signature = (
            position,
            active,
            capacity,
            _queue_wait_bucket(wait_seconds),
        )
        if signature == previous_signature:
            return True, signature
        try:
            from lib.agent_core.events import Phase, emit_phase
            emit_phase(
                task,
                Phase.EXECUTOR_QUEUED,
                detail=(
                    f'Waiting in the server AI-task queue at position '
                    f'{position}; {active}/{capacity} slots are active; '
                    f'waited {wait_seconds}s (not model/API quota).'),
                detailKey='stream.phase.executorQueuedWithMetrics',
                detailArgs={
                    'position': position,
                    'queued': queued,
                    'active': active,
                    'capacity': capacity,
                    'waitSeconds': wait_seconds,
                },
                queuePosition=position,
                queued=queued,
                active=active,
                capacity=capacity,
                waitSeconds=wait_seconds,
            )
        except Exception as error:
            logger.debug(
                '[Spawn] queued phase emit failed task=%s: %s',
                str(task.get('id') or '?')[:8], error,
            )
        return True, signature


async def _report_executor_queue(
    task: dict,
    worker_started: threading.Event,
    scheduling_phase_lock: threading.Lock,
) -> None:
    """Keep queue position and elapsed wait current without holding a thread."""
    signature = None
    while not worker_started.is_set():
        candidate = _queued_phase_candidate(task)
        if candidate is None:
            return
        if candidate != signature:
            keep_waiting, signature = await asyncio.to_thread(
                _publish_executor_queue_phase,
                task,
                worker_started,
                scheduling_phase_lock,
                signature,
            )
            if not keep_waiting:
                return
        await asyncio.sleep(_QUEUE_STATUS_POLL_SECONDS)


def _executor_runner(loop, run_task, task):
    """Return the coroutine that runs one task on the configured executor.

    ``run_task`` owns all failures after entry. This wrapper owns only a
    rejected submission, before the worker callable begins; that distinction
    prevents both ghost-running tasks and duplicate terminal settlements.
    """
    worker_started = threading.Event()
    scheduling_phase_lock = threading.Lock()

    _run_owned_task = _owned_task_callable(
        run_task, task, worker_started, scheduling_phase_lock)

    async def _async_wrapper():
        queue_reporter = None
        try:
            if _agent_executor is not None:
                execution = loop.run_in_executor(
                    _agent_executor, _run_owned_task)
                if callable(getattr(
                        _agent_executor, 'scheduling_snapshot', None)):
                    queue_reporter = asyncio.create_task(
                        _report_executor_queue(
                            task, worker_started, scheduling_phase_lock))
                await execution
            else:
                await asyncio.to_thread(_run_owned_task)
        except Exception as error:
            if not worker_started.is_set():
                logger.error(
                    '[Spawn] Executor rejected task %s before worker entry: %s',
                    task.get('id', '?')[:8],
                    error,
                    exc_info=True,
                )
                # This is an exceptional, fail-closed path. Run the canonical
                # settlement synchronously so a shutdown/rejected executor
                # cannot reject the recovery operation as well.
                _finalize_rejected_submission(task, error)
                return
            logger.error(
                '[Spawn] Task %s failed after worker entry: %s',
                task.get('id', '?')[:8],
                error,
                exc_info=True,
            )
        finally:
            if queue_reporter is not None:
                queue_reporter.cancel()
                try:
                    await queue_reporter
                except asyncio.CancelledError:
                    pass
                except Exception as error:
                    logger.debug(
                        '[Spawn] queue reporter stopped task=%s: %s',
                        str(task.get('id') or '?')[:8], error,
                    )

    return _async_wrapper


def cancel_queued_task(task_id: str) -> bool:
    """Cancel a task only if it has not acquired an agent worker yet."""
    executor = _agent_executor
    cancel = getattr(executor, 'cancel_task', None)
    return bool(callable(cancel) and cancel(str(task_id or '')))


def abandon_running_task(task_id: str) -> bool:
    """Restore logical capacity after the reaper proves a worker wedged."""
    executor = _agent_executor
    abandon = getattr(executor, 'abandon_task', None)
    return bool(callable(abandon) and abandon(str(task_id or '')))


def agent_scheduling_snapshot(task_id: str | None = None) -> dict:
    """Return current bounded-queue evidence without exposing task payloads."""
    executor = _agent_executor
    snapshot = getattr(executor, 'scheduling_snapshot', None)
    if not callable(snapshot):
        return {}
    return dict(snapshot(task_id))


def spawn_task(task: dict, *, runner=None) -> None:
    """Submit one registered chat task through the authoritative worker lane."""
    if runner is None:
        from lib.tasks_pkg.orchestrator.api import run_task
        runner = run_task

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError as error:
        logger.debug('[Spawn] no running asyncio loop, using fallback: %s', error)
        loop = None

    if loop and loop.is_running():
        asyncio.ensure_future(_executor_runner(loop, runner, task)())
        return

    serving_loop = _serving_loop
    if serving_loop is not None:
        try:
            if serving_loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    _executor_runner(serving_loop, runner, task)(),
                    serving_loop,
                )
                return
        except RuntimeError as error:
            logger.warning(
                '[Spawn] serving-loop hop failed (%s) — thread fallback for task %s',
                error,
                task.get('id', '?')[:8],
            )

    worker_started = threading.Event()
    owned = _owned_task_callable(
        runner, task, worker_started, threading.Lock())

    def _fallback_runner():
        try:
            owned()
        except Exception as error:
            if not worker_started.is_set():
                _finalize_rejected_submission(task, error)
            else:
                logger.error(
                    '[Spawn] fallback task %s failed after worker entry: %s',
                    task.get('id', '?')[:8], error, exc_info=True,
                )

    threading.Thread(
        target=_fallback_runner,
        name=f'run_task-{task.get("id", "?")[:8]}',
        daemon=True,
    ).start()


__all__ = [
    'abandon_running_task',
    'agent_scheduling_snapshot',
    'cancel_queued_task',
    'set_agent_executor',
    'set_serving_loop',
    'spawn_task',
]
