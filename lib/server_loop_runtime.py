"""Ownership boundary for resources attached to Hypercorn's serving loop."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Mapping
from typing import Any, Coroutine

from lib.agent_core.worker_executor import RecoverableAgentExecutor
from lib.observability import InstrumentedThreadPoolExecutor
from runtime_guards import deployment_resource_default


def _worker_count(
    key: str,
    environ: Mapping[str, str],
    logger: logging.Logger,
) -> int:
    """Resolve a bounded pool size without letting malformed env block boot."""
    try:
        default_ceiling = deployment_resource_default(key, environ)
    except KeyError:
        # Keep the helper useful for independently named bounded executors;
        # only the two production-owned pools have profile-specific budgets.
        default_ceiling = 16
    try:
        configured = int(environ.get(key, '') or '0')
    except (ValueError, TypeError, OverflowError) as exc:
        logger.debug('[Server] bad %s, auto-sizing: %s', key, exc)
        configured = 0
    if configured <= 0:
        minimum = max(1, default_ceiling // 2)
        return min(default_ceiling, max(minimum, os.cpu_count() or 4))
    if configured > 512:
        logger.warning('[Server] %s=%d is unsafe; clamping to 512',
                       key, configured)
        return 512
    return configured


def _cleanup_interval(
    environ: Mapping[str, str],
    logger: logging.Logger,
) -> int:
    try:
        return max(
            0,
            int(environ.get('TOFU_TASK_CLEANUP_INTERVAL', '') or '60'),
        )
    except (ValueError, TypeError, OverflowError) as exc:
        logger.debug(
            '[Server] bad TOFU_TASK_CLEANUP_INTERVAL, using 60: %s', exc)
        return 60


def _agent_queue_capacity(
    workers: int,
    environ: Mapping[str, str],
    logger: logging.Logger,
) -> int:
    """Resolve a finite local wait queue from the probed worker budget."""
    default = max(8, min(512, int(workers) * 8))
    try:
        configured = int(
            environ.get('TOFU_AGENT_QUEUE_CAPACITY', '') or default)
    except (ValueError, TypeError, OverflowError) as exc:
        logger.debug(
            '[Server] bad TOFU_AGENT_QUEUE_CAPACITY, using %d: %s',
            default, exc,
        )
        configured = default
    if configured <= 0:
        configured = default
    return max(1, min(4096, configured))


def _agent_stuck_replacements(
    workers: int,
    environ: Mapping[str, str],
    logger: logging.Logger,
) -> int:
    """Bound physical threads retained while proven-wedged calls unwind."""
    default = max(1, min(4, (int(workers) + 3) // 4))
    try:
        configured = int(
            environ.get('TOFU_AGENT_STUCK_REPLACEMENTS', '') or default)
    except (ValueError, TypeError, OverflowError) as exc:
        logger.debug(
            '[Server] bad TOFU_AGENT_STUCK_REPLACEMENTS, using %d: %s',
            default, exc,
        )
        configured = default
    if configured <= 0:
        configured = default
    return max(1, min(16, int(workers), configured))


def _executor_idle_seconds(
    environ: Mapping[str, str],
    logger: logging.Logger,
) -> int:
    """Resolve the executor retirement window; explicit zero disables it."""
    default = deployment_resource_default(
        'TOFU_EXECUTOR_IDLE_SECONDS', environ)
    raw = str(environ.get('TOFU_EXECUTOR_IDLE_SECONDS', '') or '').strip()
    if raw == '0':
        return 0
    try:
        configured = int(raw) if raw else default
    except (ValueError, TypeError, OverflowError) as exc:
        logger.debug(
            '[Server] bad TOFU_EXECUTOR_IDLE_SECONDS, using %d: %s',
            default, exc)
        configured = default
    if configured < 0:
        configured = default
    return max(60, min(24 * 60 * 60, configured))


class ServingLoopRuntime:
    """Own executors and periodic jobs installed on one serving loop.

    The loop owns its default executor and closes it as part of
    ``asyncio.run`` teardown. This object owns the separate agent executor and
    every task it creates, so they can be detached before that final teardown.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        shutdown_requested: Any,
        *,
        environ: Mapping[str, str] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.loop = loop
        self.shutdown_requested = shutdown_requested
        self.environ = os.environ if environ is None else environ
        self.logger = logger or logging.getLogger(__name__)
        self.sync_executor: InstrumentedThreadPoolExecutor | None = None
        self.agent_executor: RecoverableAgentExecutor | None = None
        self.reaper_task: asyncio.Task[None] | None = None
        self.executor_reaper_task: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._started = False
        self._stopped = False

    def start(self) -> 'ServingLoopRuntime':
        """Attach bounded pools, task spawning and maintenance to the loop."""
        if self._started:
            return self
        if self._stopped:
            raise RuntimeError('serving-loop runtime cannot restart after stop')

        default_installed = False
        try:
            sync_workers = _worker_count(
                'TOFU_SYNC_WORKERS', self.environ, self.logger)
            self.sync_executor = InstrumentedThreadPoolExecutor(
                max_workers=sync_workers,
                thread_name_prefix='tofu-sync',
                metric_pool='sync',
                idle_retain_threads=min(2, sync_workers),
            )
            self.loop.set_default_executor(self.sync_executor)
            default_installed = True
            self.logger.info(
                '[Server] Sync route executor sized to %d threads', sync_workers)

            agent_workers = _worker_count(
                'TOFU_AGENT_WORKERS', self.environ, self.logger)
            agent_queue_capacity = _agent_queue_capacity(
                agent_workers, self.environ, self.logger)
            agent_stuck_replacements = _agent_stuck_replacements(
                agent_workers, self.environ, self.logger)
            self.agent_executor = RecoverableAgentExecutor(
                max_workers=agent_workers,
                queue_capacity=agent_queue_capacity,
                max_abandoned_workers=agent_stuck_replacements,
                thread_name_prefix='tofu-agent',
                metric_pool='agent',
                idle_retain_threads=0,
            )
            from lib.tasks_pkg.spawn import set_agent_executor, set_serving_loop
            set_agent_executor(self.agent_executor)
            set_serving_loop(self.loop)
            self.logger.info(
                '[Server] Agent-worker executor capacity=%d queue=%d '
                'stuck_replacements=%d',
                agent_workers, agent_queue_capacity,
                agent_stuck_replacements)

            from lib.agent_core.push import hub as push_hub
            push_hub.set_loop(self.loop)

            interval = _cleanup_interval(self.environ, self.logger)
            if interval > 0:
                self.reaper_task = self.loop.create_task(
                    self._task_reaper(interval),
                    name='tofu-finished-task-reaper',
                )
                self.logger.info(
                    '[Server] Finished-task reaper every %ds', interval)

            executor_idle_seconds = _executor_idle_seconds(
                self.environ, self.logger)
            if executor_idle_seconds > 0:
                sweep_interval = min(60, executor_idle_seconds)
                self.executor_reaper_task = self.loop.create_task(
                    self._executor_reaper(
                        sweep_interval, executor_idle_seconds),
                    name='tofu-idle-executor-reaper',
                )
                self.logger.info(
                    '[Server] Idle executor retirement after %ds '
                    '(sweep=%ds)',
                    executor_idle_seconds, sweep_interval)
        except BaseException:
            # ``start`` runs before Quart's startup callbacks, so no lifespan
            # rollback hook exists yet. Detach everything installed so far.
            try:
                from lib.tasks_pkg.spawn import set_agent_executor, set_serving_loop
                set_serving_loop(None)
                set_agent_executor(None)
            except Exception as exc:
                self.logger.debug(
                    '[Server] task runtime rollback detach failed: %s', exc)
            try:
                from lib.agent_core.push import hub as push_hub
                push_hub.clear_loop(self.loop)
            except Exception as exc:
                self.logger.debug(
                    '[Server] push-loop rollback detach failed: %s', exc)
            if self.reaper_task is not None:
                self.reaper_task.cancel()
                self.reaper_task = None
            if self.executor_reaper_task is not None:
                self.executor_reaper_task.cancel()
                self.executor_reaper_task = None
            if self.agent_executor is not None:
                self.agent_executor.shutdown(wait=False, cancel_futures=True)
                self.agent_executor = None
            # Once installed, the sync pool belongs to the loop and will be
            # closed by asyncio teardown. If installation itself failed, no
            # owner adopted it, so close it here.
            if not default_installed and self.sync_executor is not None:
                self.sync_executor.shutdown(wait=False, cancel_futures=True)
                self.sync_executor = None
            raise

        self._started = True
        return self

    def create_task(
        self,
        awaitable: Coroutine[Any, Any, Any],
        *,
        name: str,
    ) -> asyncio.Task[Any]:
        """Create and strongly own one serving-loop background task."""
        if not self._started or self._stopped:
            awaitable.close()
            raise RuntimeError('serving-loop runtime is not accepting tasks')
        task = self.loop.create_task(awaitable, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _task_reaper(self, interval: int) -> None:
        from lib.tasks_pkg.manager import cleanup_old_tasks
        while not self.shutdown_requested.is_set():
            await asyncio.sleep(interval)
            if self.shutdown_requested.is_set():
                break
            try:
                await asyncio.to_thread(cleanup_old_tasks)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.logger.warning(
                    '[Server] task reaper sweep failed: %s', exc)

    async def _executor_reaper(
        self,
        sweep_interval: int,
        idle_seconds: int,
    ) -> None:
        """Retire burst-grown worker generations at quiescent loop points."""
        while not self.shutdown_requested.is_set():
            await asyncio.sleep(sweep_interval)
            if self.shutdown_requested.is_set():
                break
            try:
                self._retire_idle_executors(idle_seconds)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.logger.warning(
                    '[Server] idle executor retirement failed: %s', exc,
                    exc_info=True)

    def _retire_idle_executors(self, idle_seconds: int) -> dict[str, int]:
        """Publish lazy replacement pools, then drain idle old generations.

        This method runs on the serving-loop thread and contains no await, so
        the default-executor pointer and the task spawn executor are replaced
        atomically with respect to normal submissions. A rare caller holding
        an old direct reference remains safe: ``shutdown(wait=False)`` lets
        already accepted work finish and rejects only later stale submissions.
        """
        retired: dict[str, int] = {}

        old_sync = self.sync_executor
        if old_sync is not None:
            sync_state = old_sync.idle_retirement_snapshot(idle_seconds)
            if sync_state['due']:
                replacement_sync = InstrumentedThreadPoolExecutor(
                    max_workers=old_sync._max_workers,
                    thread_name_prefix='tofu-sync',
                    metric_pool='sync',
                    idle_retain_threads=min(2, old_sync._max_workers),
                )
                try:
                    self.loop.set_default_executor(replacement_sync)
                except BaseException:
                    replacement_sync.shutdown(
                        wait=False, cancel_futures=True)
                    raise
                self.sync_executor = replacement_sync
                sync_threads = int(sync_state['resident_threads'])
                old_sync.record_idle_retirement(sync_threads)
                old_sync.shutdown(wait=False, cancel_futures=False)
                retired['sync'] = sync_threads

        old_agent = self.agent_executor
        if old_agent is not None:
            agent_state = old_agent.idle_retirement_snapshot(idle_seconds)
            if agent_state['due']:
                replacement_agent = RecoverableAgentExecutor(
                    max_workers=old_agent._max_workers,
                    queue_capacity=old_agent.queue_capacity,
                    max_abandoned_workers=old_agent.max_abandoned_workers,
                    thread_name_prefix='tofu-agent',
                    metric_pool='agent',
                    idle_retain_threads=0,
                )
                from lib.tasks_pkg.spawn import set_agent_executor
                try:
                    set_agent_executor(replacement_agent)
                except BaseException:
                    replacement_agent.shutdown(
                        wait=False, cancel_futures=True)
                    raise
                self.agent_executor = replacement_agent
                agent_threads = int(agent_state['resident_threads'])
                old_agent.record_idle_retirement(agent_threads)
                old_agent.shutdown(wait=False, cancel_futures=False)
                retired['agent'] = agent_threads

        if retired:
            self.logger.info(
                '[Server] Retired idle executor workers: %s '
                '(capacity preserved lazily)', retired)
        return retired

    async def stop(self) -> None:
        """Cancel owned jobs and reject new work on the dedicated agent pool."""
        if self._stopped:
            return
        self._stopped = True

        task = self.reaper_task
        self.reaper_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                self.logger.warning('[Server] task reaper stop failed: %s', exc)

        executor_reaper = self.executor_reaper_task
        self.executor_reaper_task = None
        if executor_reaper is not None and not executor_reaper.done():
            executor_reaper.cancel()
            try:
                await executor_reaper
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                self.logger.warning(
                    '[Server] executor reaper stop failed: %s', exc)

        owned = tuple(self._tasks)
        self._tasks.clear()
        for background in owned:
            if not background.done():
                background.cancel()
        if owned:
            results = await asyncio.gather(*owned, return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException) and not isinstance(
                        result, asyncio.CancelledError):
                    self.logger.warning(
                        '[Server] serving-loop background stop failed: %s',
                        result)

        from lib.tasks_pkg.spawn import set_agent_executor, set_serving_loop
        set_serving_loop(None)
        set_agent_executor(None)

        from lib.agent_core.push import hub as push_hub
        clear_loop = getattr(push_hub, 'clear_loop', None)
        if callable(clear_loop):
            clear_loop(self.loop)

        executor = self.agent_executor
        self.agent_executor = None
        if executor is not None:
            # Agent calls can be unbounded third-party work. Cancel queued
            # futures and return promptly; the process hard deadline remains
            # the backstop for an already-running provider call.
            executor.shutdown(wait=False, cancel_futures=True)


__all__ = [
    'ServingLoopRuntime',
    '_agent_queue_capacity',
    '_agent_stuck_replacements',
    '_executor_idle_seconds',
]
