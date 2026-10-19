"""Production startup and shutdown owned by Quart's native lifespan.

The HTTP application factory is intentionally safe to use in tests and schema
tools.  This module adds the process-wide production owners explicitly, so the
CLI and ``hypercorn asgi:app`` execute the same database/bootstrap contract.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import time
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from lib.app.app_lifecycle import add_shutdown_handler, add_startup_handler
from lib import boot_profile as _boot_profile
from lib import startup_phases as _startup_phases


Callback = Callable[..., Any]


# ── Post-serving background boot runner ─────────────────────────────────
#
# Owns the startup-phase state machine once the HTTP listener is allowed to
# open.  The production lifespan runs only the listener-critical inline
# phase (frontend assets) and then hands the
# remaining required phases — storage sidecar, recovery, model routing,
# imports, workers, optional services, readiness announcement — to the
# runner, which executes them in one serving-loop task while Hypercorn
# already accepts connections.
#
# Truthfulness contract (consumed by the HTTP boot gate and the browser
# splash via ``GET /api/v4/boot`` and the ``boot`` projection of
# ``GET /api/health``):
#
# - ``pending``    — created, inline phases not run yet (pre-serving only).
# - ``running``    — a phase is executing; ``currentPhase`` names it.
# - ``interrupted``— shutdown was requested between phases; the process is
#                    draining and will not become ready.
# - ``ready``      — every phase completed; the startup gate is open.
# - ``failed``     — a background phase raised; the process STAYS UP (owner
#                    ruling 2026-09-20) and ``POST /api/v4/boot/retry`` re-runs
#                    from the failed phase.  Phases are revision-idempotent
#                    by construction, so retry never double-applies.
#
# Bounded self-healing before the latch: a background phase whose typed
# failure classifies itself retryable (``exc.retryable is True``, e.g.
# ``StorageError`` while the storage authority is starved by a transient
# host memory-pressure window) is re-run automatically with capped backoff
# (``_BOOT_PHASE_RETRY_BACKOFF_S``) inside a bounded time window
# (``_BOOT_PHASE_AUTO_RETRY_WINDOW_S``).  Only the typed classification
# qualifies — import errors and assertion bugs stay fail-fast — and the
# window is fixed, never an unbounded periodic repair loop.  When the
# window is exhausted the failure latches exactly as before.
#
# The runner never kills the process and never swallows the failure
# silently: the failing phase, its error and the retry count stay latched
# in the snapshot until a retry succeeds or the process stops.

_BOOT_RUNNER_STOP_TIMEOUT_S = 5.0

# Bounded auto-retry window for classified-retryable background phase
# failures.  The backoff schedule ramps quickly (10/30/60s) then settles at
# 90s so a sustained starvation window is outwaited without hammering the
# authority.  The window is TIME-boxed rather than attempt-boxed because a
# count cannot know how long one starved attempt burns: the 2026-09-22
# evening pressure episode starved the authority for ~10 minutes straight —
# the original 4-attempt budget (~3 minutes of waits) latched while the
# authority was still legitimately recovering, so the boot died with the
# fix already deployed.  20 minutes covers observed multi-minute episodes
# with headroom and still latches (bounded, never a periodic repair loop)
# when the authority is truly gone.  Tests shrink both knobs via monkeypatch.
_BOOT_PHASE_AUTO_RETRY_WINDOW_S = 1200.0
_BOOT_PHASE_RETRY_BACKOFF_S = (10.0, 30.0, 60.0, 90.0)


def _is_retryable_phase_failure(exc: BaseException) -> bool:
    """Only a typed failure that classifies ITSELF retryable qualifies."""
    return getattr(exc, 'retryable', None) is True


@dataclass(frozen=True)
class BootPhase:
    """One measured boot phase.

    ``args_factory`` (optional) derives positional args from the results of
    earlier phases at run time — this is how the readiness announcement
    receives the optional-services ``(mcp_config, feishu_ok)`` tuple without
    collapsing two contract phases into one.
    """

    index: int
    key: str
    label: str
    callback: Callback
    args: tuple = ()
    kwargs: dict = field(default_factory=dict)
    result_key: str | None = None
    args_factory: Callback | None = None


async def _invoke_phase(callback: Callback, *args: Any, **kwargs: Any) -> Any:
    """Run one phase body off-loop while supporting async seams."""
    if inspect.iscoroutinefunction(callback):
        return await callback(*args, **kwargs)
    result = await asyncio.to_thread(callback, *args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


class BootRunner:
    """Execute inline then background boot phases with a latched status."""

    def __init__(
        self,
        app: Any,
        *,
        phase_total: int,
        inline_phases: tuple[BootPhase, ...],
        background_phases: tuple[BootPhase, ...],
        stop_event: Any,
        logger: logging.Logger,
        boot: Callback,
        on_ready: Callback | None = None,
        on_failed: Callback | None = None,
        on_interrupted: Callback | None = None,
        on_resumed: Callback | None = None,
    ) -> None:
        self._app = app
        self._phase_total = phase_total
        self._inline_phases = inline_phases
        self._background_phases = background_phases
        self._stop_event = stop_event
        self._logger = logger
        self._boot = boot
        self._on_ready = on_ready
        self._on_failed = on_failed
        self._on_interrupted = on_interrupted
        self._on_resumed = on_resumed

        self._status = 'pending'
        self._phase_states: dict[int, dict[str, Any]] = {
            spec.index: {
                'index': spec.index,
                'key': spec.key,
                'label': spec.label,
                'status': 'pending',
                'duration_s': None,
                'error': None,
                'retry': 0,
            }
            for spec in (*inline_phases, *background_phases)
        }
        self._results: dict[str, Any] = {}
        self._measured: list[dict] = []
        self._retry_count = 0
        self._error_message: str | None = None
        self._startup_t0: float | None = None
        self._ready_at: float | None = None
        self._task: asyncio.Task | None = None
        self._done_event: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    # ── status projection ────────────────────────────────────────────

    @property
    def status(self) -> str:
        return self._status

    @property
    def measured(self) -> list[dict]:
        return list(self._measured)

    @property
    def task(self) -> Any:
        return self._task

    def snapshot(self) -> dict[str, Any]:
        """JSON-safe boot projection for the gate, probes and the splash."""
        phases = [
            dict(self._phase_states[spec.index])
            for spec in (*self._inline_phases, *self._background_phases)
        ]
        current = next(
            (p for p in phases if p['status'] == 'running'), None)
        failed = next(
            (p for p in phases if p['status'] == 'failed'), None)
        elapsed = (
            max(0.0, time.monotonic() - self._startup_t0)
            if self._startup_t0 is not None else None)
        return {
            'status': self._status,
            'phaseTotal': self._phase_total,
            'phases': phases,
            'currentPhase': current,
            'failedPhase': failed,
            'error': self._error_message,
            'retryCount': self._retry_count,
            'elapsedS': elapsed,
            'readyAt': self._ready_at,
        }

    async def wait_done(self) -> str:
        """Await a terminal status (ready/failed/interrupted). For tests."""
        if self._done_event is None:
            return self._status
        await self._done_event.wait()
        return self._status

    # ── execution ────────────────────────────────────────────────────

    async def run_inline(self) -> None:
        """Run the listener-critical phases; failures propagate (fail-fast)."""
        self._loop = asyncio.get_running_loop()
        self._done_event = asyncio.Event()
        self._startup_t0 = time.monotonic()
        self._status = 'running'
        async with self._app.app_context():
            for spec in self._inline_phases:
                await self._run_one(spec)
                if self._stop_requested():
                    self._interrupt()
                    return

    def start_background(self) -> None:
        """Schedule the remaining phases on the serving loop and return."""
        if self._status != 'running':
            raise RuntimeError(
                f'boot runner cannot start background phases from '
                f'status={self._status!r}')
        if not self._background_phases:
            self._finish()
            return
        coro = self._run_background(from_position=0)
        self._task = self._create_tracked_task(coro)

    def request_retry(self) -> str:
        """Re-run from the failed phase; returns the resulting status word."""
        if self._status == 'ready':
            return 'ready'
        if self._status != 'failed':
            return 'running'
        task = self._task
        if task is not None and not task.done():
            return 'running'
        failed_position = next(
            (pos for pos, spec in enumerate(self._background_phases)
             if self._phase_states[spec.index]['status'] == 'failed'),
            None,
        )
        if failed_position is None:
            return 'running'
        self._retry_count += 1
        self._error_message = None
        self._status = 'running'
        if self._done_event is not None:
            self._done_event.clear()
        self._logger.warning(
            '[Boot] operator retry #%d: re-running from phase %d/%d | %s',
            self._retry_count,
            self._background_phases[failed_position].index,
            self._phase_total,
            self._background_phases[failed_position].label,
        )
        coro = self._run_background(from_position=failed_position)
        self._task = self._create_tracked_task(coro)
        if self._on_resumed is not None:
            self._on_resumed(self)
        return 'started'

    async def stop(self, timeout: float = _BOOT_RUNNER_STOP_TIMEOUT_S) -> None:
        """Cancel the background task; bounded so shutdown never wedges."""
        task = self._task
        if task is None or task.done():
            return
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        except Exception as exc:
            self._logger.debug('[Boot] runner stop observed: %s', exc)

    # ── internals ────────────────────────────────────────────────────

    def _create_tracked_task(self, coro: Any) -> Any:
        loop = self._loop
        if loop is None:
            raise RuntimeError('boot runner has no serving loop yet')
        serving = self._app.extensions.get('tofu_serving_loop_lifecycle')
        runtime = (serving or {}).get('runtime')
        if runtime is not None:
            try:
                return runtime.create_task(coro, name='tofu-boot-runner')
            except RuntimeError:
                # Serving-loop runtime already stopping (shutdown raced the
                # boot): close the coroutine and report through the task
                # shape the shutdown path already tolerates.
                return loop.create_task(self._run_closed(coro))
        return loop.create_task(coro, name='tofu-boot-runner')

    async def _run_closed(self, coro: Any) -> None:
        coro.close()
        raise asyncio.CancelledError()

    def _stop_requested(self) -> bool:
        try:
            return bool(self._stop_event.is_set())
        except Exception as _e:
            logging.getLogger('server').debug('check stop requested failed: %s', _e)
            return False

    def _interrupt(self) -> None:
        self._status = 'interrupted'
        self._mark_done()
        if self._on_interrupted is not None:
            self._on_interrupted(self)

    def _finish(self) -> None:
        self._status = 'ready'
        self._ready_at = time.time()
        self._mark_done()
        if self._on_ready is not None:
            self._on_ready(self)

    def _fail(self, spec: BootPhase, exc: BaseException) -> None:
        self._status = 'failed'
        self._error_message = (
            f'phase {spec.index}/{self._phase_total} {spec.label}: '
            f'{type(exc).__name__}: {exc}')
        self._logger.error(
            '[Boot] startup phase %d/%d failed permanently (process stays '
            'up; POST /api/v4/boot/retry re-runs from here): %s',
            spec.index, self._phase_total, self._error_message)
        self._mark_done()
        if self._on_failed is not None:
            self._on_failed(self)

    def _mark_done(self) -> None:
        if self._done_event is not None:
            self._done_event.set()

    async def _run_background(self, *, from_position: int) -> None:
        try:
            async with self._app.app_context():
                for spec in self._background_phases[from_position:]:
                    if self._stop_requested():
                        self._interrupt()
                        return
                    if not await self._run_one_with_auto_retry(spec):
                        return
                if self._stop_requested():
                    self._interrupt()
                    return
                self._finish()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            # Defensive: app-context construction itself failed.
            self._fail(self._background_phases[from_position], exc)

    async def _run_one_with_auto_retry(self, spec: BootPhase) -> bool:
        """Run one background phase; return False when the boot latched failed.

        A classified-retryable failure re-runs the same phase in place with
        capped backoff before the latch — phases are revision-idempotent,
        the exact contract the operator retry already relies on.
        """
        auto_retry = 0
        first_failed_at: float | None = None
        while True:
            try:
                await self._run_one(spec)
                return True
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                now = time.monotonic()
                if first_failed_at is None:
                    first_failed_at = now
                if (not _is_retryable_phase_failure(exc)
                        or now - first_failed_at
                        >= _BOOT_PHASE_AUTO_RETRY_WINDOW_S
                        or self._stop_requested()):
                    self._fail(spec, exc)
                    return False
                auto_retry += 1
                delay = _BOOT_PHASE_RETRY_BACKOFF_S[
                    min(auto_retry - 1, len(_BOOT_PHASE_RETRY_BACKOFF_S) - 1)]
                # Surface the transient reason on the still-running phase so
                # the cold-start splash can show "storage unavailable,
                # retrying" instead of a silent, ever-growing elapsed timer.
                # The phase is re-running, so it reads 'running' (not
                # 'failed') while the backoff elapses; only an exhausted
                # budget latches 'failed' through _fail below.
                state = self._phase_states[spec.index]
                state['status'] = 'running'
                state['retry'] = auto_retry
                state['error'] = f'{type(exc).__name__}: {exc}'
                self._logger.warning(
                    '[Boot] startup phase %d/%d failed with a retryable '
                    'error (%s: %s); auto-retry %d in %.0fs '
                    '(bounded %.0fs window)',
                    spec.index, self._phase_total,
                    type(exc).__name__, exc,
                    auto_retry, delay, _BOOT_PHASE_AUTO_RETRY_WINDOW_S)
                await asyncio.sleep(delay)

    async def _run_one(self, spec: BootPhase) -> None:
        state = self._phase_states[spec.index]
        state['status'] = 'running'
        state['error'] = None
        state['retry'] = 0
        self._boot('[startup phase %d/%d] start | %s',
                   spec.index, self._phase_total, spec.label)
        started = time.monotonic()
        args = spec.args
        kwargs = dict(spec.kwargs)
        if spec.args_factory is not None:
            args, extra_kwargs = spec.args_factory(self._results)
            kwargs.update(extra_kwargs)
        try:
            result = await _invoke_phase(spec.callback, *args, **kwargs)
        except BaseException as exc:
            state['status'] = 'failed'
            state['error'] = f'{type(exc).__name__}: {exc}'
            self._boot('[startup phase %d/%d] failed | %s | %.1fs',
                       spec.index, self._phase_total, spec.label,
                       max(0.0, time.monotonic() - started))
            raise
        duration = max(0.0, time.monotonic() - started)
        state['status'] = 'done'
        state['duration_s'] = duration
        self._measured.append({
            'key': spec.key,
            'label': spec.label,
            'index': spec.index,
            'duration_s': duration,
        })
        if spec.result_key is not None:
            self._results[spec.result_key] = result
        self._boot('[startup phase %d/%d] done | %s | %.1fs',
                   spec.index, self._phase_total, spec.label, duration)



@dataclass(frozen=True)
class ProductionStartupSteps:
    """Required, fail-fast phases supplied by the server composition root.

    ``post_ready`` (optional) is best-effort recovery work that is safe to
    run after the readiness gate opens; it never delays readiness and its
    failure is logged, never latched into the boot status.
    """

    build_assets: Callback
    validate_storage_boundary: Callback
    init_database: Callback
    start_storage: Callback
    bootstrap_model_routing: Callback
    validate_imports: Callback
    start_workers: Callback
    post_ready: Callback | None = None


async def _invoke(callback: Callback, *args: Any, **kwargs: Any) -> Any:
    result = callback(*args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


def _default_boot(message: str, *args: Any) -> None:
    logging.getLogger('server').info(message, *args)


def _start_network_integrations(
    *, logger: logging.Logger, boot: Callback,
) -> tuple[dict[str, Any], bool]:
    """Start integrations used by API or task-worker processes."""
    boot('Configuring MCP auto-connect…')
    mcp_config: dict[str, Any] = {}
    try:
        from lib.mcp.config import load_mcp_config, migrate_legacy_config
        from lib.mcp.startup import start_mcp_auto_connect

        from lib.identity import PERSONAL_USER_ID
        from runtime_guards import load_deployment_configuration
        if load_deployment_configuration().mode == 'personal':
            migrate_legacy_config(user_id=PERSONAL_USER_ID)
            mcp_config = load_mcp_config(user_id=PERSONAL_USER_ID)
            start_mcp_auto_connect(mcp_config, logger=logger, owner_user_id=PERSONAL_USER_ID)
        else:
            logger.info('[MCP] Auto-connect awaits explicit owner-scoped startup configuration')
    except Exception as exc:
        logger.warning('[MCP] Auto-connect setup failed: %s', exc)

    try:
        from lib.llm_dispatch.health_local import start_local_health_checker

        start_local_health_checker()
    except Exception as exc:
        logger.warning('[HealthLocal] local-endpoint monitor failed: %s', exc)

    try:
        from lib.fs_keepalive import start_fs_keepalive

        start_fs_keepalive()
    except Exception as exc:
        logger.warning('FS keepalive failed: %s', exc)

    try:
        from lib.code_server_excludes import start_code_server_excludes_sync

        start_code_server_excludes_sync()
    except Exception as exc:
        logger.warning('code-server excludes sync failed: %s', exc)

    try:
        from lib.cross_dc import init_cross_dc_detection

        init_cross_dc_detection()
    except Exception as exc:
        logger.warning('Cross-DC detection failed: %s', exc)

    feishu_ok = False
    try:
        # GUI-saved credentials (server_config.json) are applied before the
        # enable check so a bot configured without env vars still starts.
        from lib.feishu.startup import (
            start_bot_from_saved_config as start_feishu_bot,
        )

        feishu_ok = bool(start_feishu_bot())
    except Exception as exc:
        logger.warning('Feishu Bot failed: %s', exc)
    return mcp_config, feishu_ok


def start_optional_production_services(
    *,
    shutdown_requested: Any,
    logger: logging.Logger,
    process_role: str = 'all',
    boot: Callback = _default_boot,
    request_graceful_shutdown: Callback | None = None,
) -> tuple[dict[str, Any], bool]:
    """Start best-effort integrations after the required bootstrap succeeds."""
    from lib.process_roles import (
        CAPABILITY_EVENT_MAINTENANCE,
        CAPABILITY_REQUEST_SERVICES,
        CAPABILITY_TASK_WORKERS,
        normalize_process_role,
        process_role_has,
        process_role_has_any,
    )

    process_role = normalize_process_role(process_role)
    from runtime_guards import distributed_preview_is_read_only

    if distributed_preview_is_read_only(os.environ):
        logger.info(
            '[Server] optional services disabled by distributed read-only '
            'preview fence (role=%s)', process_role)
        return {}, False
    try:
        from lib import cgroup_guard

        cgroup_guard.startup_self_check()

        def _recycle_oversized_worker(reason: str) -> None:
            if shutdown_requested.is_set():
                return
            logger.critical(
                '[Server] Controlled memory recycle requested: %s', reason)
            if request_graceful_shutdown is None:
                shutdown_requested.set()
            else:
                request_graceful_shutdown(
                    shutdown_requested,
                    logger=logger,
                    reason='memory_recycle',
                )

        cgroup_guard.start_monitor(
            recycle_callback=_recycle_oversized_worker)
    except Exception as exc:
        logger.warning(
            '[cgroup] pressure defenses failed to start: %s', exc)

    if shutdown_requested.is_set():
        logger.info(
            '[Server] Shutdown requested during import validation — '
            'skipping MCP + background starts.')
        return {}, False
    mcp_config: dict[str, Any] = {}
    feishu_ok = False
    if process_role_has_any(process_role, (
        CAPABILITY_REQUEST_SERVICES,
        CAPABILITY_TASK_WORKERS,
    )):
        mcp_config, feishu_ok = _start_network_integrations(
            logger=logger, boot=boot)

    # Retention is eager even when no new task emits an event. The event
    # writer itself remains lazy and both owners are stopped by shutdown.
    if process_role_has(process_role, CAPABILITY_EVENT_MAINTENANCE):
        try:
            from lib.tasks_pkg.event_log import start_storage_maintenance

            start_storage_maintenance()
        except Exception as exc:
            logger.warning(
                '[Server] event storage maintenance failed to start: %s', exc)

    return mcp_config, feishu_ok


def register_production_lifecycle(
    app: Any,
    *,
    steps: ProductionStartupSteps,
    shutdown_requested: Any | None = None,
    logger: logging.Logger | None = None,
    boot: Callback = _default_boot,
    announce_ready: Callback | None = None,
    request_graceful_shutdown: Callback | None = None,
    optional_services: Callback = start_optional_production_services,
    shutdown_runtime: Callback | None = None,
    process_role: str = 'all',
) -> bool:
    """Register the production lifespan once on an assembled application."""
    marker = 'tofu_production_lifecycle_registered'
    if app.extensions.get(marker):
        return False

    from lib.process_roles import normalize_process_role

    log = logger or logging.getLogger('server')
    process_role = normalize_process_role(process_role)
    stop_event = (threading.Event()
                  if shutdown_requested is None else shutdown_requested)
    state = {
        'status': 'registered',
        'shutdown_requested': stop_event,
        'mcp_config': {},
        'feishu_ok': False,
        'process_role': process_role,
    }

    # The phase ordinal ↔ stable-key ↔ label mapping is owned by
    # lib.startup_phases so the restart progress model can key a phase
    # without depending on a human-readable (and translatable) label.
    required_phase_count = _startup_phases.REQUIRED_PHASE_COUNT
    phase_total = (
        required_phase_count
        if announce_ready is not None else required_phase_count - 1)

    def _phase_spec(index: int, callback: Callback, *args: Any,
                    result_key: str | None = None,
                    args_factory: Callback | None = None,
                    **kwargs: Any) -> BootPhase:
        return BootPhase(
            index=index,
            key=_startup_phases.phase_key(index),
            label=_startup_phases.phase_label(index),
            callback=callback,
            args=args,
            kwargs=kwargs,
            result_key=result_key,
            args_factory=args_factory,
        )

    def _run_optional_services(**kwargs: Any) -> Any:
        result = optional_services(**kwargs)
        mcp_config, feishu_ok = result
        state['mcp_config'] = mcp_config
        state['feishu_ok'] = bool(feishu_ok)
        return result

    inline_phases = (
        _phase_spec(1, steps.build_assets),
    )
    background_phases: list[BootPhase] = [
        # The HTTP shell needs no database. Prove exclusive ownership before
        # storage acquisition, while the default-deny API gate stays closed.
        _phase_spec(2, steps.validate_storage_boundary),
        # Every repository depends on the authenticated Sidecar. Its
        # handshake is a required startup gate, never a best-effort task
        # or a selectable mode — backgrounded, never made optional.
        _phase_spec(3, steps.start_storage),
        _phase_spec(4, steps.init_database),
        # The authority switch must finish before imports instantiate
        # the dispatcher or workers accept tasks. Migration is
        # revision-idempotent and never writes back to legacy config.
        _phase_spec(5, steps.bootstrap_model_routing),
        _phase_spec(6, steps.validate_imports),
        _phase_spec(7, steps.start_workers, app),
        _phase_spec(
            8, _run_optional_services,
            result_key='optional',
            shutdown_requested=stop_event,
            logger=log,
            boot=boot,
            process_role=process_role,
            request_graceful_shutdown=request_graceful_shutdown,
        ),
    ]
    if announce_ready is not None:
        background_phases.append(_phase_spec(
            9, announce_ready,
            args_factory=lambda results: (
                (results['optional'][0], results['optional'][1]), {}),
        ))

    def _handle_ready(runner: BootRunner) -> None:
        # Persist the measured boot shape so the NEXT in-app restart
        # budgets its progress bar from this host's own observed
        # phases instead of a universal hardcoded guess. Best-effort.
        _boot_profile.record_boot_phases(
            runner.measured,
            phase_total=phase_total,
            boot_to_ready_s=runner.snapshot()['elapsedS'] or 0.0,
        )
        gate = app.extensions.get('tofu_production_startup_gate')
        if gate is not None:
            gate.set()
        state['status'] = 'ready'
        _schedule_post_ready()

    def _schedule_post_ready() -> None:
        """Run composition-root deferred work after the gate, off the boot
        task so readiness is never held by best-effort recovery."""
        callback = steps.post_ready
        if callback is None:
            return

        async def _run() -> None:
            try:
                await _invoke_phase(callback)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning('[Server] post-ready deferred work failed: %s',
                            exc)

        serving = app.extensions.get('tofu_serving_loop_lifecycle') or {}
        runtime = serving.get('runtime')
        loop = getattr(runner, '_loop', None)
        if runtime is not None:
            runtime.create_task(_run(), name='tofu-post-ready-deferred')
        elif loop is not None:
            loop.create_task(_run(), name='tofu-post-ready-deferred')

    def _handle_failed(runner: BootRunner) -> None:
        # Owner ruling 2026-09-20: a backgrounded required phase that
        # fails leaves the process UP and answering (statics, health,
        # boot projection, retry) instead of tearing the listener down.
        state['status'] = 'boot_failed'

    def _handle_interrupted(runner: BootRunner) -> None:
        log.info(
            '[Server] Shutdown requested during background boot — '
            'skipping remaining startup phases.')
        state['status'] = 'interrupted'

    def _handle_resumed(runner: BootRunner) -> None:
        state['status'] = 'starting'

    runner = BootRunner(
        app,
        phase_total=phase_total,
        inline_phases=inline_phases,
        background_phases=tuple(background_phases),
        stop_event=stop_event,
        logger=log,
        boot=boot,
        on_ready=_handle_ready,
        on_failed=_handle_failed,
        on_interrupted=_handle_interrupted,
        on_resumed=_handle_resumed,
    )
    app.extensions['tofu_boot_runner'] = runner

    async def _startup() -> None:
        state['status'] = 'starting'
        try:
            # Only frontend validation holds the listener. Storage ownership
            # is still proven before acquisition, in the background runner;
            # HTTP shell/progress remain available even when that proof fails.
            await runner.run_inline()
        except BaseException:
            state['status'] = 'startup_failed'
            raise
        if runner.status == 'interrupted':
            return
        runner.start_background()

    async def _shutdown() -> None:
        state['status'] = 'stopping'
        stop_event.set()
        # A background boot still mid-flight holds no resources the
        # runtime shutdown stack cannot reclaim, but it must not keep
        # running phases against a storage authority that is about to
        # stop.  Cancel it first, bounded.
        try:
            await runner.stop()
        except Exception as exc:
            log.warning('[Server] boot runner stop failed: %s', exc)
        runtime = shutdown_runtime
        if runtime is None:
            from lib.server.server_shutdown import shutdown_production_runtime

            runtime = shutdown_production_runtime
        started = time.monotonic()
        try:
            from lib.server.server_shutdown import _shutdown_timing_to_stderr
            _shutdown_timing_to_stderr('production shutdown stack begin')
        except Exception as _e:
            logging.getLogger('server').debug('shutdown step failed: %s', _e)
            pass
        try:
            await _invoke(runtime, stop_event, app=app, logger=log)
        finally:
            elapsed = time.monotonic() - started
            log.info('[Server] production shutdown stack took %.1fs', elapsed)
            try:
                from lib.server.server_shutdown import _shutdown_timing_to_stderr
                _shutdown_timing_to_stderr(
                    f'production shutdown stack took {elapsed:.1f}s')
            except Exception as _e:
                logging.getLogger('server').debug('shutdown step failed: %s', _e)
                pass
            state['status'] = 'stopped'

    # Publish state before attaching handlers so a startup rollback can always
    # find the same shutdown event and cleanup metadata.
    app.extensions[marker] = True
    app.extensions['tofu_production_lifecycle'] = state
    app.extensions['tofu_shutdown_requested'] = stop_event
    add_startup_handler(app, _startup, name='tofu.production.startup')
    add_shutdown_handler(app, _shutdown, name='tofu.production.shutdown')
    return True


__all__ = [
    'BootPhase',
    'BootRunner',
    'ProductionStartupSteps',
    'register_production_lifecycle',
    'start_optional_production_services',
]
