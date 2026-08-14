"""Ownership boundary for resources attached to Hypercorn's serving loop."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Mapping
from typing import Any, Coroutine

from lib.observability import InstrumentedThreadPoolExecutor


def _worker_count(
    key: str,
    environ: Mapping[str, str],
    logger: logging.Logger,
) -> int:
    """Resolve a bounded pool size without letting malformed env block boot."""
    try:
        configured = int(environ.get(key, '') or '0')
    except (ValueError, TypeError, OverflowError) as exc:
        logger.debug('[Server] bad %s, auto-sizing: %s', key, exc)
        configured = 0
    if configured <= 0:
        return min(16, max(8, os.cpu_count() or 4))
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
        self.agent_executor: InstrumentedThreadPoolExecutor | None = None
        self.reaper_task: asyncio.Task[None] | None = None
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
            )
            self.loop.set_default_executor(self.sync_executor)
            default_installed = True
            self.logger.info(
                '[Server] Sync route executor sized to %d threads', sync_workers)

            agent_workers = _worker_count(
                'TOFU_AGENT_WORKERS', self.environ, self.logger)
            self.agent_executor = InstrumentedThreadPoolExecutor(
                max_workers=agent_workers,
                thread_name_prefix='tofu-agent',
                metric_pool='agent',
            )
            from lib.tasks_pkg import set_agent_executor, set_serving_loop
            set_agent_executor(self.agent_executor)
            set_serving_loop(self.loop)
            self.logger.info(
                '[Server] Agent-worker executor sized to %d threads',
                agent_workers)

            from lib.push import hub as push_hub
            push_hub.set_loop(self.loop)

            interval = _cleanup_interval(self.environ, self.logger)
            if interval > 0:
                self.reaper_task = self.loop.create_task(
                    self._task_reaper(interval),
                    name='tofu-finished-task-reaper',
                )
                self.logger.info(
                    '[Server] Finished-task reaper every %ds', interval)
        except BaseException:
            # ``start`` runs before Quart's startup callbacks, so no lifespan
            # rollback hook exists yet. Detach everything installed so far.
            try:
                from lib.tasks_pkg import set_agent_executor, set_serving_loop
                set_serving_loop(None)
                set_agent_executor(None)
            except Exception as exc:
                self.logger.debug(
                    '[Server] task runtime rollback detach failed: %s', exc)
            try:
                from lib.push import hub as push_hub
                push_hub.clear_loop(self.loop)
            except Exception as exc:
                self.logger.debug(
                    '[Server] push-loop rollback detach failed: %s', exc)
            if self.reaper_task is not None:
                self.reaper_task.cancel()
                self.reaper_task = None
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
        from lib.tasks_pkg import cleanup_old_tasks
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

        from lib.tasks_pkg import set_agent_executor, set_serving_loop
        set_serving_loop(None)
        set_agent_executor(None)

        from lib.push import hub as push_hub
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


__all__ = ['ServingLoopRuntime']
