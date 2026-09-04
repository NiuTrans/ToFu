"""Quart lifespan owner for serving-loop executors and recovery jobs."""

from __future__ import annotations

import asyncio
import logging
import threading
import time as _time
from collections.abc import Mapping
from typing import Any

from lib.app_lifecycle import add_shutdown_handler, add_startup_handler


_ATTEMPT_DISPATCH_RECOVERY_INTERVAL_SECONDS = 2.0
_ATTEMPT_DISPATCH_RECOVERY_GRACE_MS = 1_500
_ATTEMPT_DISPATCH_RECOVERY_BATCH = 8


def _create_debug_guard(loop: asyncio.AbstractEventLoop, *, logger, environ):
    from lib.server_loop_debug import LoopDebugGuard

    return LoopDebugGuard(loop, logger=logger, environ=environ).start()


def _create_loop_runtime(
    loop: asyncio.AbstractEventLoop,
    shutdown_requested: Any,
    *,
    logger,
    environ,
):
    from lib.server_loop_runtime import ServingLoopRuntime

    return ServingLoopRuntime(
        loop, shutdown_requested, logger=logger, environ=environ).start()


def _create_watchdog(
    loop: asyncio.AbstractEventLoop,
    shutdown_requested: Any,
    *,
    host: str,
    port: int,
    hooks: Any,
    fault_shm_log: Any,
    fault_log: Any,
    logger,
    ready_event: Any,
    environ,
):
    from lib.server_loop_watchdog import LoopWatchdog

    return LoopWatchdog(
        loop,
        shutdown_requested,
        host=host,
        port=port,
        hooks=hooks,
        fault_shm_log=fault_shm_log,
        fault_log=fault_log,
        logger=logger,
        ready_event=ready_event,
        environ=environ,
    ).start()


def _load_write_freshness_snapshot() -> None:
    from lib import write_freshness

    write_freshness.load_snapshot()


def _start_auto_restart(shutdown_requested: Any) -> bool:
    from lib.auto_restart import maybe_start_auto_restart_watch

    return bool(maybe_start_auto_restart_watch(
        shutdown_requested=shutdown_requested))


def _stop_auto_restart() -> None:
    from lib.auto_restart import stop_auto_restart_watch

    stop_auto_restart_watch(timeout=2.0)


def _redispatch_orphaned_queue() -> Any:
    from lib.message_queue import redispatch_orphaned_queue_on_startup

    return redispatch_orphaned_queue_on_startup()


def _recover_dispatchable_attempts(created_before_ms: int) -> dict[str, int]:
    """Delegate one owner-aware, bounded recovery batch to the app service."""
    from lib.conversation_sync.runtime import conversation_turn_commands

    return conversation_turn_commands.recover_dispatchable_attempts(
        created_before_ms=created_before_ms,
        limit=_ATTEMPT_DISPATCH_RECOVERY_BATCH,
    )


def _turn_recovery_backstop_body(
        stop_event: Any, gate_open_ms: int, log: logging.Logger) -> None:
    """Post-serving safety net for restart settlement (2026-08-19 incident).

    Boot recovery (_init_database) is the ONLY other place orphaned
    'running' attempts get settled, and it runs while the sidecar is still
    warming up — one transient deadline failure used to leave turns
    'running' FOREVER, which the frontend renders as a sidebar full of
    "回答中" plus a topbar badge stuck on "重连中". Re-run the settlement
    once, shortly after serving starts, with HARD liveness guards so a
    genuinely-live attempt can never be swept:
      • created_before_ms — only attempts older than the serving gate;
      • exclude_task_ids — skip attempts whose task is in the live registry.
    Idempotent: when boot recovery already did its job this finds nothing.
    """
    for _ in range(45):  # let the serving loop settle; abortable on shutdown
        if stop_event.is_set():
            return
        _time.sleep(1)
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    live_task_ids = set(chat_task_runtime.task_ids())
    from lib.turn_lifecycle import recover_running_attempts
    settled = recover_running_attempts(
        created_before_ms=gate_open_ms, exclude_task_ids=live_task_ids)
    if settled:
        log.warning(
            '[Server] post-serving turn-recovery backstop settled %d '
            'orphaned attempt(s) that boot recovery missed', settled)


def register_serving_loop_lifecycle(
    app: Any,
    *,
    shutdown_requested: Any | None = None,
    host: str = '0.0.0.0',
    port: int = 15000,
    hooks: Any = None,
    fault_shm_log: Any = None,
    fault_log: Any = None,
    logger: logging.Logger | None = None,
    environ: Mapping[str, str] | None = None,
    process_role: str = 'all',
) -> bool:
    """Register loop-bound owners once, before production startup handlers."""
    from lib.process_roles import (
        CAPABILITY_TASK_RECOVERY,
        CAPABILITY_TASK_WORKERS,
        normalize_process_role,
        process_role_has,
    )

    process_role = normalize_process_role(process_role)
    owns_task_recovery = process_role_has(
        process_role, CAPABILITY_TASK_RECOVERY)
    owns_task_workers = process_role_has(
        process_role, CAPABILITY_TASK_WORKERS)
    owns_attempt_dispatch_recovery = owns_task_workers
    marker = 'tofu_serving_loop_lifecycle_registered'
    if app.extensions.get(marker):
        return False

    log = logger or logging.getLogger('server')
    stop_event = (threading.Event()
                  if shutdown_requested is None else shutdown_requested)
    state = {
        'status': 'registered',
        'loop': None,
        'gate': None,
        'debug_guard': None,
        'runtime': None,
        'watchdog': None,
        'exception_handler': None,
        'previous_exception_handler': None,
        'auto_restart_started': False,
        'process_role': process_role,
        'owns_task_recovery': owns_task_recovery,
        'owns_task_workers': owns_task_workers,
        'owns_attempt_dispatch_recovery': owns_attempt_dispatch_recovery,
    }

    async def _startup() -> None:
        state['status'] = 'starting'
        loop = asyncio.get_running_loop()
        gate = asyncio.Event()
        state['loop'] = loop
        state['gate'] = gate
        app.extensions['tofu_production_startup_gate'] = gate

        previous_handler = loop.get_exception_handler()

        def _loop_exception_handler(_loop, context):
            message = context.get('message') \
                or 'Unhandled exception in event loop'
            exception = context.get('exception')
            log.error(
                '[asyncio] %s',
                message,
                exc_info=exception if exception else False,
            )

        state['previous_exception_handler'] = previous_handler
        state['exception_handler'] = _loop_exception_handler
        loop.set_exception_handler(_loop_exception_handler)

        state['debug_guard'] = _create_debug_guard(
            loop, logger=log, environ=environ)
        runtime = _create_loop_runtime(
            loop, stop_event, logger=log, environ=environ)
        state['runtime'] = runtime
        state['watchdog'] = _create_watchdog(
            loop,
            stop_event,
            host=host,
            port=port,
            hooks=hooks,
            fault_shm_log=fault_shm_log,
            fault_log=fault_log,
            logger=log,
            ready_event=gate,
            environ=environ,
        )

        try:
            await asyncio.to_thread(_load_write_freshness_snapshot)
        except Exception as exc:
            log.warning(
                '[Server] write-freshness snapshot replay failed: %s', exc)

        try:
            state['auto_restart_started'] = await asyncio.to_thread(
                _start_auto_restart, stop_event)
            if state['auto_restart_started']:
                log.info(
                    '[Server] Auto-restart watcher armed '
                    '(TOFU_AUTO_RESTART=1)')
        except Exception as exc:
            log.warning('[Server] Auto-restart watcher setup failed: %s', exc)

        async def _run_orphan_queue_redispatch() -> None:
            await gate.wait()
            if stop_event.is_set():
                return
            try:
                spawned = await asyncio.to_thread(_redispatch_orphaned_queue)
                if spawned:
                    log.info(
                        '[Server] orphaned-queue redispatch spawned %d '
                        'task(s) from stranded queue rows',
                        len(spawned),
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning(
                    '[Server] orphaned-queue redispatch failed: %s', exc)

        async def _run_turn_recovery_backstop() -> None:
            await gate.wait()
            if stop_event.is_set():
                return
            # Attempts created before the gate opened cannot belong to this
            # process's serving era; combined with the live-registry exclusion
            # inside the backstop body, a live turn can never be swept.
            gate_open_ms = int(_time.time() * 1000)
            try:
                await asyncio.to_thread(
                    _turn_recovery_backstop_body, stop_event, gate_open_ms, log)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning(
                    '[Server] turn-recovery backstop failed: %s', exc)

        async def _run_attempt_dispatch_recovery() -> None:
            """Continuously close commit-to-claim gaps without busy polling."""
            await gate.wait()
            transient_failures = 0
            while not stop_event.is_set():
                delay = _ATTEMPT_DISPATCH_RECOVERY_INTERVAL_SECONDS
                try:
                    result = await asyncio.to_thread(
                        _recover_dispatchable_attempts,
                        int(_time.time() * 1000)
                        - _ATTEMPT_DISPATCH_RECOVERY_GRACE_MS,
                    )
                    examined = int(result.get('examined') or 0)
                    recovered = int(result.get('recovered') or 0)
                    settled_failed = int(result.get('settledFailed') or 0)
                    if recovered or settled_failed:
                        log.warning(
                            '[Server] accepted-attempt recovery examined=%d '
                            'started=%d settled_failed=%d',
                            examined,
                            recovered,
                            settled_failed,
                        )
                    # Drain a real backlog promptly, but still yield to the
                    # event loop and the storage writer between bounded pages.
                    if examined >= _ATTEMPT_DISPATCH_RECOVERY_BATCH:
                        delay = 0.05
                    transient_failures = 0
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    transient_failures += 1
                    if transient_failures & (transient_failures - 1) == 0:
                        log.warning(
                            '[Server] accepted-attempt recovery transient '
                            'failure (attempt=%d): %s',
                            transient_failures,
                            exc,
                        )
                    delay = min(
                        30.0,
                        _ATTEMPT_DISPATCH_RECOVERY_INTERVAL_SECONDS
                        * (2 ** min(transient_failures, 4)),
                    )
                await asyncio.sleep(delay)

        if owns_task_workers:
            runtime.create_task(
                _run_orphan_queue_redispatch(),
                name='tofu-orphan-queue-redispatch',
            )
        if owns_task_recovery:
            runtime.create_task(
                _run_turn_recovery_backstop(),
                name='tofu-turn-recovery-backstop',
            )
        if owns_attempt_dispatch_recovery:
            runtime.create_task(
                _run_attempt_dispatch_recovery(),
                name='tofu-attempt-dispatch-recovery',
            )
        state['status'] = 'ready'

    async def _shutdown() -> None:
        state['status'] = 'stopping'
        stop_event.set()
        try:
            await asyncio.to_thread(_stop_auto_restart)
        except Exception as exc:
            log.warning('[Server] auto-restart watcher stop failed: %s', exc)

        watchdog = state.get('watchdog')
        if watchdog is not None:
            try:
                await watchdog.stop(timeout=2.0)
            except Exception as exc:
                log.warning('[Server] loop watchdog stop failed: %s', exc)
            state['watchdog'] = None

        runtime = state.get('runtime')
        if runtime is not None:
            try:
                await runtime.stop()
            except Exception as exc:
                log.warning('[Server] serving-loop stop failed: %s', exc)
            state['runtime'] = None

        debug_guard = state.get('debug_guard')
        if debug_guard is not None:
            try:
                debug_guard.stop()
            except Exception as exc:
                log.warning('[Server] loop debug guard stop failed: %s', exc)
            state['debug_guard'] = None

        loop = state.get('loop')
        handler = state.get('exception_handler')
        if loop is not None and loop.get_exception_handler() is handler:
            loop.set_exception_handler(state.get('previous_exception_handler'))
        state.update(
            status='stopped',
            loop=None,
            gate=None,
            exception_handler=None,
            previous_exception_handler=None,
        )

    app.extensions[marker] = True
    app.extensions['tofu_serving_loop_lifecycle'] = state
    app.extensions['tofu_shutdown_requested'] = stop_event
    add_startup_handler(app, _startup, name='tofu.serving-loop.startup')
    add_shutdown_handler(app, _shutdown, name='tofu.serving-loop.shutdown')
    return True


__all__ = ['register_serving_loop_lifecycle']
