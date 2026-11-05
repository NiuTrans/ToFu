"""Bounded production cleanup run from Quart's native shutdown lifespan."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import threading
import time
from typing import Any

from lib.log import get_logger


_MODULE_LOG = get_logger(__name__)


def _shutdown_loaded_module(module_name: str, close_name: str, **kwargs: Any) -> None:
    """Join an optional owner's in-flight import on the shutdown worker."""
    if module_name not in sys.modules:
        return
    import importlib
    # sys.modules publishes a module before its body finishes. Normal import
    # acquires the existing import lock; direct attribute access does not.
    module = importlib.import_module(module_name)
    getattr(module, close_name)(**kwargs)


async def _stop_independent_producers(log: logging.Logger) -> None:
    """Join independent producer loops together, before retiring consumers.

    Each owner retains its existing stop deadline and error policy. Optional
    owners stay unloaded; a partial import joins through the normal import lock.
    Storage, event delivery, HTTP and automation remain live until all finish.
    """
    owners = (
        ('lib.swarm.integration', 'stop_swarm_cleanup_timer', 'swarm cleanup timer', 2.0),
        ('lib.swarm.integration', 'stop_swarm_output_cleanup', 'swarm output cleanup', 2.0),
        ('lib.netpath', 'stop_prober', 'netpath prober', None),
        ('lib.server.server_background_services', 'stop_lan_discovery_responder', 'LAN discovery responder', 2.0),
        ('lib.llm_dispatch.health_local', 'stop_local_health_checker', 'local-health checker', 2.0),
        ('lib.fs_keepalive', 'stop_fs_keepalive', 'filesystem keepalive', 2.0),
        ('lib.tasks_pkg.manager._presence_keepalive', 'stop_keepalive', 'presence keepalive', 2.0),
        ('lib.presence', 'stop_sweeper', 'presence sweeper', 2.0),
        ('lib.integration_control', 'stop_worker', 'integration worker', 2.0),
    )

    async def stop(module, function, label, timeout):
        try:
            options = {} if timeout is None else {'timeout': timeout}
            await asyncio.to_thread(_shutdown_loaded_module, module, function, **options)
        except Exception as exc:
            log.warning('[Server] %s stop failed: %s', label, exc)

    # Cancelling to_thread only abandons its awaiter; the physical cleanup
    # keeps running. Preserve its join before cancellation can retire the loop.
    joining = asyncio.gather(*(stop(*owner) for owner in owners), return_exceptions=True)
    interrupted = False
    while not joining.done():
        try:
            await asyncio.shield(joining)
        except asyncio.CancelledError:
            interrupted = True
    outcomes = joining.result()
    if interrupted:
        raise asyncio.CancelledError
    for outcome in outcomes:
        if isinstance(outcome, BaseException):
            raise outcome


def graceful_shutdown_signals() -> list[int]:
    """Return the platform signals routed through the graceful-drain path."""
    names = ('SIGTERM', 'SIGINT', 'SIGHUP')
    return [
        registered_signal
        for name in names
        if (registered_signal := getattr(signal, name, None)) is not None
    ]


def shutdown_hard_deadline_seconds() -> float:
    """Return the bounded wall-time budget for signal-driven shutdown."""
    raw = os.environ.get('TOFU_SHUTDOWN_HARD_SECS', '') or '30'
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        _MODULE_LOG.debug('[Server] invalid shutdown hard deadline %r: %s',
                          raw, exc)
        value = 30.0
    return max(5.0, min(300.0, value))


def http_keep_alive_timeout_seconds() -> float:
    """Return bounded idle HTTP retention; active SSE is unaffected."""
    raw = os.environ.get('TOFU_HTTP_KEEP_ALIVE_SECS', '') or '15'
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        _MODULE_LOG.debug('[Server] invalid HTTP keep-alive timeout %r: %s',
                          raw, exc)
        value = 15.0
    return max(1.0, min(120.0, value))


def start_shutdown_hard_deadline(
    shutdown_requested: Any,
    *,
    timeout: float | None = None,
    exit_fn: Any = None,
    timer_factory: Any = None,
    logger: logging.Logger | None = None,
) -> Any:
    """Arm a process-scoped daemon backstop for graceful shutdown."""
    delay = (
        shutdown_hard_deadline_seconds()
        if timeout is None
        else max(0.0, float(timeout))
    )
    exit_now = os._exit if exit_fn is None else exit_fn
    make_timer = threading.Timer if timer_factory is None else timer_factory
    log = logger or logging.getLogger('server')

    def enforce_deadline() -> None:
        if not shutdown_requested.is_set():
            return
        # The async logging queue may already be blocked or closed. Preserve
        # the failure in the manager-captured console before the forced exit.
        _shutdown_timing_to_stderr(
            'graceful shutdown exceeded %.1fs hard deadline; forcing exit'
            % delay)
        log.critical(
            '[Server] Graceful shutdown exceeded %.1fs hard deadline; '
            'forcing process exit so the supervisor can restore service.',
            delay,
        )
        exit_now(1)

    timer = make_timer(delay, enforce_deadline)
    timer.daemon = True
    timer.start()
    return timer


def request_graceful_shutdown(
    shutdown_requested: Any,
    *,
    timer_starter: Any = None,
    mark_clean_fn: Any = None,
    logger: logging.Logger | None = None,
    reason: str = 'signal',
) -> Any:
    """Request shutdown and arm recovery before best-effort marker I/O."""
    log = logger or logging.getLogger('server')
    start_timer = (
        start_shutdown_hard_deadline
        if timer_starter is None
        else timer_starter
    )

    shutdown_requested.set()
    timer = start_timer(shutdown_requested, logger=log)

    try:
        if mark_clean_fn is None:
            from lib.shutdown_marker import mark_clean as mark_clean_fn
        mark_clean_fn(reason)
    except Exception as exc:
        log.warning('[Server] mark_clean(%s) failed: %s', reason, exc)
    return timer


def _shutdown_timing_to_stderr(line: str) -> None:
    """Mirror one shutdown-timing line to the terminal/captured console.

    The nonblocking logging pipeline is already winding down when these lines
    emit, so app.log silently loses them on every drain; stderr reaches the
    manager-captured console log (and the foreground terminal) before exit.
    A re-exec may inherit a broken stderr pipe — never let diagnostics kill.
    """
    try:
        stamp = time.strftime('%H:%M:%S')
        sys.stderr.write(f'\033[36m[Server {stamp}] {line}\033[0m\n')
        sys.stderr.flush()
    except Exception as _e:
        _MODULE_LOG.debug('emit shutdown timing failed: %s', _e)
        pass


async def wait_for_shutdown_trigger(
    shutdown_requested: Any,
    *,
    logger: logging.Logger | None = None,
    close_push_clients: Any = None,
) -> None:
    """Wake push sockets before Hypercorn begins its connection drain."""
    while not shutdown_requested.is_set():
        await asyncio.sleep(0.05)
    _shutdown_timing_to_stderr('shutdown trigger observed by serving loop')
    log = logger or logging.getLogger('server')
    try:
        if close_push_clients is None:
            from lib.agent_core.push import hub
            close_push_clients = hub.close_clients_for_shutdown
        closed = close_push_clients()
        if closed:
            log.info(
                '[Server] Closed %d push connection(s) before Hypercorn drain',
                closed)
    except Exception:
        log.exception('[Server] Failed to close push connections before drain')


async def _stop_storage_boundary_for_shutdown(log: logging.Logger) -> bool:
    """Release storage and certify that a pending re-exec may proceed.

    Storage remains the last production owner stopped.  A failure is logged and
    left uncertified: ordinary process exit can still finish, while the re-exec
    gate fails closed and lets the outer lifecycle manager recover with a new
    PID instead of carrying a child authority across ``execv``.
    """
    try:
        from lib.storage import stop_storage
        started = time.monotonic()
        # Native storage checkpoints both authority and search WALs on EOF.
        # A fixed five-second kill window repeatedly interrupts that work and
        # moves its cost into the next startup. Reserve half the existing
        # process deadline for it; the process backstop still bounds all cleanup.
        storage_timeout = shutdown_hard_deadline_seconds() / 2.0
        await asyncio.to_thread(stop_storage, timeout=storage_timeout)
        elapsed = time.monotonic() - started
        log.info('[Server] storage boundary stop took %.1fs', elapsed)
        _shutdown_timing_to_stderr(f'storage boundary stop took {elapsed:.1f}s')
    except Exception as exc:
        log.warning('[Server] storage sidecar shutdown failed: %s', exc)
        return False

    from lib.server.server_reexec import (
        confirm_server_reexec_storage_boundary_released,
    )
    if confirm_server_reexec_storage_boundary_released():
        log.info(
            '[Restart] Storage boundary released; in-place re-exec certified')
    return True


async def shutdown_production_runtime(
    shutdown_requested: Any,
    *,
    app: Any = None,
    logger: logging.Logger | None = None,
) -> None:
    """Quiesce tasks and close loop-owned resources before executor teardown."""
    log = logger or logging.getLogger(__name__)
    shutdown_started = time.monotonic()
    _shutdown_timing_to_stderr('production runtime shutdown begin')
    try:
        from lib.tasks_pkg.manager import quiesce_running_tasks
        # Closing a native client can publish its terminal immediately. Stamp
        # the shared shutdown cause first, without overwriting an earlier Stop.
        quiesced = quiesce_running_tasks(reason='server_shutdown')
    except Exception as exc:
        log.warning('[Server] task quiesce failed: %s', exc)
        quiesced = 0
    native_host=getattr(app,'extensions',{}).get('tofu_native_host')
    if native_host is not None:
        # Native terminal/accounting callbacks must join while their dependencies live.
        # A failed join fences storage shutdown; the application lifecycle reports it.
        await native_host.close()
    try:
        from lib.scheduler.timer import shutdown_timer_watchers
        await asyncio.to_thread(shutdown_timer_watchers)
    except Exception as exc:
        log.warning('[Server] timer watcher shutdown failed: %s', exc)
        # Keep dependencies live if timer ownership could not be settled.
        raise
    try:
        drain_seconds = float(
            os.environ.get('TOFU_SHUTDOWN_DRAIN_SECS', '') or '3')
    except (ValueError, TypeError, OverflowError) as exc:
        log.debug('[Server] bad TOFU_SHUTDOWN_DRAIN_SECS, using 3.0: %s', exc)
        drain_seconds = 3.0
    if quiesced and drain_seconds > 0:
        log.info('[Server] Draining %d aborted task(s) up to %.0fs before PG '
                 'stop…', quiesced, drain_seconds)
        try:
            sys.stderr.write(
                '\033[33m[Server] Waiting up to %.0fs for %d running task(s) '
                'to stop (Ctrl+C to skip)…\033[0m\n'
                % (drain_seconds, quiesced))
            sys.stderr.flush()
        except Exception as exc:
            log.debug('[Server] shutdown console notice failed: %s', exc)
        deadline = time.monotonic() + drain_seconds
        try:
            from lib.tasks_pkg.manager.runtime import chat_task_runtime
            while time.monotonic() < deadline:
                still_running = sum(
                    1 for task in chat_task_runtime.snapshot()
                    if task.get('status') == 'running'
                )
                if still_running == 0:
                    break
                await asyncio.sleep(0.25)
        except Exception as exc:
            log.warning('[Server] shutdown drain wait failed: %s', exc)

    # Translation callbacks borrow push, provider and storage dependencies.
    # Join their actual exit before retiring any of those service owners; an
    # unused translation module stays unloaded during shutdown.
    try:
        await asyncio.to_thread(_shutdown_loaded_module,
            'lib.translate.incremental', 'shutdown_incremental_translations')
    except Exception as exc:
        log.warning('[Server] incremental translation shutdown failed: %s', exc)
        raise

    # The remaining synchronous close/join operations are bounded, but they
    # still belong off the serving loop so async client pools can close cleanly.
    try:
        paper_builds = sys.modules.get('lib.paper.builds.runtime')
        if paper_builds is not None:
            await asyncio.to_thread(paper_builds.shutdown_builds)
    except Exception as exc:
        log.warning('[Server] paper build shutdown failed: %s', exc)
    try:
        from lib.search.search_runtime import shutdown_search_browser_runtime
        await asyncio.to_thread(shutdown_search_browser_runtime)
    except Exception as exc:
        log.warning('[Server] search browser shutdown failed: %s', exc)
        raise  # Keep dependencies live if browser work is still physically owned.
    await _stop_independent_producers(log)
    try:
        if 'lib.cgroup_guard' in sys.modules:
            from lib.cgroup_guard import stop_monitor
            await asyncio.to_thread(stop_monitor, timeout=2.0)
    except Exception as exc:
        log.warning('[Server] cgroup monitor stop failed: %s', exc)
    if app is not None:
        try:
            worker_runtime = sys.modules.get('lib.durable_worker.runtime')
            if worker_runtime is not None:
                await asyncio.to_thread(worker_runtime.stop_workers, app)
        except Exception as exc:
            log.warning('[Server] durable worker stop failed: %s', exc)
            # Closing the authority while an effect still owns a context would
            # violate quiescence. Leave it live for the process shutdown backstop.
            raise

        try:
            from lib.server.server_background_services import (
                stop_registered_background_services,
            )
            service_stop_started = time.monotonic()
            await asyncio.wait_for(
                asyncio.to_thread(
                    stop_registered_background_services, app, timeout=2.0),
                timeout=10.0,
            )
            if app.extensions.get('tofu_registered_background_services'):
                raise RuntimeError('Process background services have not quiesced')
            service_stop_elapsed = time.monotonic() - service_stop_started
            log.info('[Server] process background services stop took %.1fs',
                     service_stop_elapsed)
            if service_stop_elapsed >= 0.5:
                _shutdown_timing_to_stderr(
                    f'process background services stop took {service_stop_elapsed:.1f}s')
        except TimeoutError:
            log.warning('[Server] process background shutdown exceeded 10s')
            _shutdown_timing_to_stderr('process background services stop exceeded 10s')
            raise
        except Exception as exc:
            log.warning('[Server] process background shutdown failed: %s', exc)
            raise  # A scheduler/bootstrap writer still owns its storage context.
    try:
        from lib.storage.warmup import stop_keep_warm
        await asyncio.to_thread(stop_keep_warm, timeout=2.0)
    except Exception as exc:
        log.warning('[Server] storage warm-up shutdown failed: %s', exc)
    try:
        from lib.storage.mcp_declarations import stop_legacy_slot_compaction
        await asyncio.to_thread(stop_legacy_slot_compaction, timeout=2.0)
    except Exception as exc:
        log.warning('[Server] MCP declaration compaction shutdown failed: %s', exc)
    try:
        from lib.pricing import stop_pricing_refresh
        await asyncio.to_thread(stop_pricing_refresh, timeout=2.0)
    except Exception as exc:
        log.warning('[Server] pricing refresh shutdown failed: %s', exc)
    try:
        from lib.mcp.startup import stop_mcp_auto_connect
        await asyncio.to_thread(stop_mcp_auto_connect, timeout=2.0)
    except Exception as exc:
        log.warning('[Server] MCP startup worker shutdown failed: %s', exc)
    try:
        from lib.mcp.client import disconnect_all_bridges
        await asyncio.to_thread(disconnect_all_bridges)
    except Exception as exc:
        log.warning('[Server] MCP bridge shutdown failed: %s', exc)
    try:
        from lib.agent_core.admission import controller as admission_controller
        await asyncio.to_thread(admission_controller.shutdown, timeout=0.5)
    except Exception as exc:
        log.warning('[Server] admission heartbeat stop failed: %s', exc)
    try:
        from lib.tasks_pkg.event_delivery import shutdown_event_delivery
        if not await asyncio.to_thread(shutdown_event_delivery, timeout=10.0):
            log.warning('[Server] task event delivery drain incomplete; pending evidence was not discarded')
    except Exception as exc:
        log.warning('[Server] task event delivery shutdown failed: %s', exc)
    try:
        from lib.agent_core.push import hub as push_hub
        await asyncio.to_thread(push_hub.stop)
    except Exception as exc:
        log.warning('[Server] push bus stop failed: %s', exc)
    try:
        from lib.http_transport.client import close_http_clients
        await close_http_clients()
    except Exception as exc:
        log.warning('[Server] HTTP client pool stop failed: %s', exc)
    try:
        from lib.runtime.runtime_state_store import get_store
        runtime_close = getattr(get_store(), 'close', None)
        if callable(runtime_close):
            await asyncio.to_thread(runtime_close)
    except Exception as exc:
        log.warning('[Server] runtime-state stop failed: %s', exc)
    # Key-health and per-key API usage counters are coalesced in memory.
    # Persist each once every dispatch producer and client pool has stopped,
    # independently so one failed flush cannot skip the other; an unused
    # owner stays unloaded.
    try:
        await asyncio.to_thread(
            _shutdown_loaded_module, 'lib.key_stats', 'shutdown_key_stats')
    except Exception as exc:
        log.warning('[Server] key-stats flush failed: %s', exc)
    try:
        await asyncio.to_thread(
            _shutdown_loaded_module, 'lib.usage_tracker', 'flush')
    except Exception as exc:
        log.warning('[Server] usage-counter flush failed: %s', exc)

    try:
        from lib.tasks_pkg.event_log import (
            stop_sidecar_batcher,
            stop_storage_maintenance,
        )
        await asyncio.to_thread(stop_storage_maintenance, timeout=2.0)
        await asyncio.to_thread(stop_sidecar_batcher, timeout=3.0)
    except Exception as exc:
        log.warning('[Server] event-storage shutdown failed: %s', exc)

    # Stop the storage authority only after every known background producer and
    # event writer has quiesced.  Keeping this last prevents normal shutdown
    # from looking like an unexpected sidecar outage to active writers.
    await _stop_storage_boundary_for_shutdown(log)
    total = time.monotonic() - shutdown_started
    log.info('[Server] production runtime shutdown took %.1fs', total)
    _shutdown_timing_to_stderr(
        f'production runtime shutdown took {total:.1f}s')


__all__ = [
    'graceful_shutdown_signals',
    'http_keep_alive_timeout_seconds',
    'request_graceful_shutdown',
    'shutdown_hard_deadline_seconds',
    'shutdown_production_runtime',
    'start_shutdown_hard_deadline',
    'wait_for_shutdown_trigger',
]
