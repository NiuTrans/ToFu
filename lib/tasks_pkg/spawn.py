"""Chat worker submission onto the serving loop and agent executor.

This module owns the process-level executor/serving-loop references and chat's
durable worker admission. TaskRuntime owns submission, isolated worker context
and activity observation. Accepted tasks stay pending until physical entry.
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
    from lib.error_envelope import make_envelope, task_start_failure_cause
    from lib.tasks_pkg.manager import finalize_chat_task_aborted, finalize_chat_task_error

    if task.get('_abort_reason') == 'server_shutdown' and task.get('aborted'):
        # A tearing-down executor refuses work this process already quiesced.
        # Settling it as a start failure would forfeit its restart resume.
        try:
            finalize_chat_task_aborted(task)
        finally:
            _settle_unscheduled_workspace(task)
        return

    config = task.get('config') or {}
    # The fixed title only says THAT the executor did not start; carry
    # the translated underlying cause so the user sees WHY (the frontend
    # renders detail, which would otherwise shadow `raw`).
    cause = task_start_failure_cause(error)
    envelope = make_envelope(
        'task_start_failed',
        detail=(
            'The agent worker could not begin task execution: '
            + cause['detail']
        ),
        model=str(config.get('model') or task.get('model') or ''),
        context='task-start',
        source='tasks.spawn',
        raw=str(error),
        hint=cause['hint'],
    )
    finalize_chat_task_error(
        task,
        envelope,
        flow_reason='executor_start_failed',
    )

    _settle_unscheduled_workspace(task)

def _mark_worker_started(
    task: dict,
    worker_started: threading.Event | None = None,
    scheduling_phase_lock: threading.Lock | None = None,
) -> bool:
    """Cross the durable pending→running boundary on the physical worker."""
    phase_lock = scheduling_phase_lock or threading.Lock()
    from lib.observability import measure_task_startup
    with measure_task_startup('worker_entry', identity={
            'task_id': task.get('id'), 'conversation_id': task.get('convId'),
            'turn_id': task.get('_turnId'), 'attempt_id': task.get('_attemptId'),
            'user_id': task.get('_userId')}), phase_lock:
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
    abort_event = task.get('abort_event')
    if task.get('_conversationWorkerContext') is not None:
        from lib.tasks_pkg.invocation import invocation_abort_requested
        invocation_abort_requested(task)
    if task.get('aborted') or (abort_event is not None and abort_event.is_set()):
        from lib.llm.llm_errors import AbortedError
        raise AbortedError('Task cancelled before worker admission')

    from lib.project_dispatch.admission import validate_task
    validate_task(task)

    attempt_id = str(task.get('_attemptId') or task.get('attemptId') or '')
    if attempt_id:
        from lib.tasks_pkg.manager._registry import task_user_id
        from lib.turn_lifecycle import mark_task_started
        from lib.tasks_pkg.worker_binding import publication_arguments

        started = mark_task_started(
            attempt_id,
            task_id,
            user_id=task_user_id(task),
            **({'automation_context': task['_automationStartContext']}
               if task.get('_automationStartContext') is not None else {}),
            **publication_arguments(task),
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
    worker_entry_observed_at = time.time()
    worker_started_at = (float(started['startedAt']) / 1000
                         if attempt_id and started.get('startedAt') is not None else worker_entry_observed_at)
    task['_workerStartedAt'] = worker_started_at
    # Queue residence is not execution silence. Reset both reaper baselines at
    # the physical-entry boundary so a task that legitimately waited longer
    # than the stuck threshold is not force-failed immediately after acquiring
    # a worker (the phase persistence below is best-effort and may itself fail).
    task['_t_last_event'] = worker_entry_observed_at
    task['_dispatch_heartbeat'] = worker_entry_observed_at
    # Worker ownership is already committed. Publish a start phase before
    # root admission. Only an actual isolation request emits workspacePreparing
    # inside the binder; direct and readonly roots do not imply a snapshot.
    # Tools still wait for binding, and binding errors retain the finalizer.
    project_path = str((task.get('config') or {}).get('projectPath') or '')
    has_workspace = bool(
        project_path or (task.get('config') or {}).get('projectPaths'))
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
    if has_workspace:
        # Binding is execution authority: errors must escape to the existing
        # rejected-start terminal lane, never continue on a different root.
        from lib.tasks_pkg.workspace_delivery import bind_worker_workspace
        from lib.observability import measure_task_startup
        with measure_task_startup('workspace_binding', identity={
                'task_id': task_id, 'conversation_id': task.get('convId'),
                'turn_id': task.get('_turnId'), 'attempt_id': attempt_id,
                'user_id': task.get('_userId')}):
            bind_worker_workspace(task)

        if task.pop('_workspacePreparing', False):
            try:
                emit_phase(
                    task, Phase.WORKING,
                    detail='Agent worker acquired; starting the task…',
                    detailKey='stream.phase.workerStarting',
                )
            except Exception as error:
                logger.debug('[Spawn] workspace-ready phase emit failed task=%s: %s',
                             task_id[:8] or '?', error)
        project_path = str((task.get('config') or {}).get('projectPath') or '')
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
    if worker_started is not None:
        worker_started.set()
    return True


def _settle_unscheduled_workspace(task):
    """Early errors/cancellation preserve selected work without publishing it."""
    try:
        from lib.tasks_pkg.workspace_delivery import persist_terminal_workspace_intent
        persist_terminal_workspace_intent(task)
    except Exception as error:
        logger.warning('[Spawn] workspace preservation deferred task=%s: %s',
                       task.get('id', '?')[:8], error, exc_info=True)


def _owned_task_callable(
    run_task,
    task,
    worker_started: threading.Event,
    scheduling_phase_lock: threading.Lock,
):
    def _run_owned_task():
        from lib.llm.llm_errors import AbortedError
        try:
            admitted = _mark_worker_started(
                task, worker_started, scheduling_phase_lock)
        except AbortedError:
            from lib.turn_verdict import TurnStatus, derive_task_verdict
            abort_event = task.get('abort_event')
            if not (task.get('aborted')
                    or (abort_event is not None and abort_event.is_set())):
                raise
            # Internal authority fences also interrupt physical work. They
            # retain the failure lane; only cooperative Stop is cancellation.
            verdict = derive_task_verdict(
                task, {'type': 'aborted', 'finishReason': 'aborted'})
            if verdict.status is not TurnStatus.INTERRUPTED:
                raise
            from lib.tasks_pkg.manager import finalize_chat_task_aborted
            try:
                finalize_chat_task_aborted(task)
            finally:
                _settle_unscheduled_workspace(task)
            return None
        if not admitted:
            _settle_unscheduled_workspace(task)
            return None
        native_invocation_owned = task.get('_nativePreparedInvocation') is not None
        try:
            # After worker entry/workspace binding, before any runner reads
            # the model view (root and Flow alike).
            from lib.tasks_pkg.input_translation import (
                apply_deferred_input_translation,
            )
            settled_input = apply_deferred_input_translation(task)
            if settled_input is not None:
                # A deferred translation verdict makes the accepted input
                # final only now; every other request bound at registration,
                # and the command pinned the replay proof before it.
                from lib.project_dispatch.request_budget import (
                    bind_source_account,
                )
                bind_source_account(task, expected_input=settled_input)
                from lib.tasks_pkg.model_replay import rebind_history_proof
                rebind_history_proof(task, settled_input)
            return run_task(task)
        except BaseException as error:
            committed_native_stop=(isinstance(error,asyncio.CancelledError)
                and native_invocation_owned
                and task.get('status')=='aborted'
                and not task.get('_terminalPersistencePending'))
            if not committed_native_stop:
                from lib.tasks_pkg.manager import finalize_chat_task_error
                finalize_chat_task_error(task, str(error))
            raise
        finally:
            _settle_unscheduled_workspace(task)

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


def _prepare_task_worker(task, runner):
    """One domain admission/error envelope for inline and submitted workers."""
    if runner is None:
        from lib.tasks_pkg.orchestrator.api import run_task
        runner = run_task

    worker_started = threading.Event()
    scheduling_phase_lock = threading.Lock()
    owned = _owned_task_callable(
        runner, task, worker_started, scheduling_phase_lock)

    def failed(error):
        if not worker_started.is_set():
            # A rejected scheduler cannot be used to schedule its own repair.
            _finalize_rejected_submission(task, error)
        else:
            logger.error('[Spawn] Task %s failed after worker entry: %s',
                         task.get('id', '?')[:8], error, exc_info=True)

    return owned, failed, worker_started, scheduling_phase_lock


def run_task_inline(task: dict, *, runner=None):
    """Keep the existing physical worker until invocation and cleanup exit."""
    from contextlib import nullcontext
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    owned, failed, _, _ = _prepare_task_worker(task, runner)
    control = task.get('_invocationAbortCheck')
    watch = (control.watch(task) if task.get('_conversationWorkerContext') is not None
             else nullcontext())
    try:
        with watch:
            return chat_task_runtime.run_owned_worker(task, owned)
    except BaseException as error:
        failed(error)
        raise


def spawn_task(task: dict, *, runner=None, executor=None):
    """Submit one registered chat task through the authoritative worker lane."""
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    owned, failed, worker_started, scheduling_phase_lock = _prepare_task_worker(task, runner)
    if executor is not None:
        # Embeddings retain their own executor and inspect its original Future.
        return chat_task_runtime.submit_owned_worker(
            task, executor.submit, owned, on_error=failed)
    on_queued = None
    if callable(getattr(_agent_executor, 'scheduling_snapshot', None)):
        on_queued = lambda: _report_executor_queue(
            task, worker_started, scheduling_phase_lock)
    chat_task_runtime.spawn_owned_worker(
        task, owned, executor=_agent_executor, serving_loop=_serving_loop,
        on_error=failed, on_queued=on_queued,
        thread_name=f'run_task-{task.get("id", "?")[:8]}')


__all__ = [
    'abandon_running_task',
    'agent_scheduling_snapshot',
    'cancel_queued_task',
    'set_agent_executor',
    'set_serving_loop',
    'run_task_inline',
    'spawn_task',
]
