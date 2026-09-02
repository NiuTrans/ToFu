"""Chat worker submission onto the serving loop and agent executor.

This module is the single owner of the process-level executor and serving-loop
references.  ``spawn_task`` emits the submitted phase before scheduling work,
then prefers the tracked serving loop and bounded agent executor.  A daemon
thread is reserved for CLI/test contexts with no serving loop.
"""

from __future__ import annotations

import asyncio
import threading

from lib.log import get_logger

logger = get_logger(__name__)

_agent_executor = None
_serving_loop = None


def set_agent_executor(executor) -> None:
    """Install, or during shutdown clear, the dedicated agent executor."""
    global _agent_executor
    _agent_executor = executor


def set_serving_loop(loop) -> None:
    """Install, or during shutdown clear, the server's main asyncio loop."""
    global _serving_loop
    _serving_loop = loop


def _finalize_rejected_submission(task: dict, error: Exception) -> None:
    """Settle a bound task when the executor rejected it before entry."""
    from lib.error_envelope import make_envelope
    from lib.tasks_pkg.manager import finalize_chat_task_error

    config = task.get('config') or {}
    envelope = make_envelope(
        'task_start_failed',
        detail='The agent worker rejected the task before execution began.',
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


def _executor_runner(loop, run_task, task):
    """Return the coroutine that runs one task on the configured executor.

    ``run_task`` owns all failures after entry. This wrapper owns only a
    rejected submission, before the worker callable begins; that distinction
    prevents both ghost-running tasks and duplicate terminal settlements.
    """
    worker_started = threading.Event()

    def _run_owned_task():
        worker_started.set()
        return run_task(task)

    async def _async_wrapper():
        try:
            if _agent_executor is not None:
                await loop.run_in_executor(_agent_executor, _run_owned_task)
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

    return _async_wrapper


def spawn_task(task: dict) -> None:
    """Submit one registered chat task through the authoritative worker lane."""
    from lib.tasks_pkg.orchestrator.api import run_task

    try:
        from lib.agent_core.events import Phase, emit_phase
        emit_phase(
            task,
            Phase.WORKING,
            detail='Submitted to the agent worker…',
            detailKey='stream.phase.submittedToWorker',
        )
    except Exception as error:
        logger.debug(
            '[Spawn] initial phase emit failed task=%s: %s',
            task.get('id', '?')[:8],
            error,
        )

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError as error:
        logger.debug('[Spawn] no running asyncio loop, using fallback: %s', error)
        loop = None

    if loop and loop.is_running():
        asyncio.ensure_future(_executor_runner(loop, run_task, task)())
        return

    serving_loop = _serving_loop
    if serving_loop is not None:
        try:
            if serving_loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    _executor_runner(serving_loop, run_task, task)(),
                    serving_loop,
                )
                return
        except RuntimeError as error:
            logger.warning(
                '[Spawn] serving-loop hop failed (%s) — thread fallback for task %s',
                error,
                task.get('id', '?')[:8],
            )

    threading.Thread(
        target=run_task,
        args=(task,),
        name=f'run_task-{task.get("id", "?")[:8]}',
        daemon=True,
    ).start()


__all__ = ['set_agent_executor', 'set_serving_loop', 'spawn_task']
