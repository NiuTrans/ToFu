"""Production startup and shutdown owned by Quart's native lifespan.

The HTTP application factory is intentionally safe to use in tests and schema
tools.  This module adds the process-wide production owners explicitly, so the
CLI and ``hypercorn asgi:app`` execute the same database/bootstrap contract.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from lib.app_lifecycle import add_shutdown_handler, add_startup_handler


Callback = Callable[..., Any]


@dataclass(frozen=True)
class ProductionStartupSteps:
    """Required, fail-fast phases supplied by the server composition root."""

    build_assets: Callback
    init_database: Callback
    start_storage: Callback
    validate_imports: Callback
    start_workers: Callback


async def _invoke(callback: Callback, *args: Any, **kwargs: Any) -> Any:
    result = callback(*args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


async def _invoke_blocking(callback: Callback, *args: Any, **kwargs: Any) -> Any:
    """Run synchronous bootstrap work off-loop while supporting async seams."""
    if inspect.iscoroutinefunction(callback):
        return await callback(*args, **kwargs)
    result = await asyncio.to_thread(callback, *args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


def _default_boot(message: str, *args: Any) -> None:
    logging.getLogger('server').info(message, *args)


def start_optional_production_services(
    *,
    shutdown_requested: Any,
    logger: logging.Logger,
    boot: Callback = _default_boot,
    request_graceful_shutdown: Callback | None = None,
) -> tuple[dict[str, Any], bool]:
    """Start best-effort integrations after the required bootstrap succeeds."""
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

    boot('Configuring MCP auto-connect…')
    mcp_config: dict[str, Any] = {}
    try:
        from lib.mcp.config import load_mcp_config
        from lib.mcp.startup import start_mcp_auto_connect

        mcp_config = load_mcp_config()
        start_mcp_auto_connect(mcp_config, logger=logger)
    except Exception as exc:
        logger.warning('[MCP] Auto-connect setup failed: %s', exc)

    try:
        from lib.llm_dispatch.health_local import start_local_health_checker

        start_local_health_checker()
    except Exception as exc:
        logger.warning('[HealthLocal] Failed: %s', exc)

    try:
        from lib.llm_dispatch.autodiscover_local import start_local_autodiscovery

        start_local_autodiscovery()
    except Exception as exc:
        logger.warning('[AutoDiscover] Failed: %s', exc)

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
        from lib.feishu import ENABLED as FEISHU_ENABLED
        from lib.feishu import start_bot as start_feishu_bot

        if FEISHU_ENABLED:
            feishu_ok = bool(start_feishu_bot())
    except Exception as exc:
        logger.warning('Feishu Bot failed: %s', exc)

    # Retention is eager even when no new task emits an event. The event
    # writer itself remains lazy and both owners are stopped by shutdown.
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
) -> bool:
    """Register the production lifespan once on an assembled application."""
    marker = 'tofu_production_lifecycle_registered'
    if app.extensions.get(marker):
        return False

    log = logger or logging.getLogger('server')
    stop_event = (threading.Event()
                  if shutdown_requested is None else shutdown_requested)
    state = {
        'status': 'registered',
        'shutdown_requested': stop_event,
        'mcp_config': {},
        'feishu_ok': False,
    }

    async def _startup() -> None:
        state['status'] = 'starting'
        try:
            async with app.app_context():
                await _invoke_blocking(steps.build_assets)
                await _invoke_blocking(steps.init_database)
                if stop_event.is_set():
                    log.info(
                        '[Server] Shutdown requested during DB init — '
                        'skipping remaining startup phases.')
                    state['status'] = 'interrupted'
                    return

                # Sidecar-backed repositories are used by workers and optional
                # services.  Its authenticated health handshake is therefore a
                # required startup gate, never a best-effort background start.
                await _invoke_blocking(steps.start_storage)
                if stop_event.is_set():
                    log.info(
                        '[Server] Shutdown requested during storage startup — '
                        'skipping remaining startup phases.')
                    state['status'] = 'interrupted'
                    return

                await _invoke_blocking(steps.validate_imports)
                await _invoke_blocking(steps.start_workers, app)
                mcp_config, feishu_ok = await _invoke_blocking(
                    optional_services,
                    shutdown_requested=stop_event,
                    logger=log,
                    boot=boot,
                    request_graceful_shutdown=request_graceful_shutdown,
                )
                state['mcp_config'] = mcp_config
                state['feishu_ok'] = bool(feishu_ok)
                if stop_event.is_set():
                    state['status'] = 'interrupted'
                    return
                if announce_ready is not None:
                    await _invoke_blocking(
                        announce_ready, mcp_config, feishu_ok)
                gate = app.extensions.get('tofu_production_startup_gate')
                if gate is not None:
                    gate.set()
                state['status'] = 'ready'
        except BaseException:
            state['status'] = 'startup_failed'
            raise

    async def _shutdown() -> None:
        state['status'] = 'stopping'
        stop_event.set()
        runtime = shutdown_runtime
        if runtime is None:
            from lib.server_shutdown import shutdown_production_runtime

            runtime = shutdown_production_runtime
        try:
            await _invoke(runtime, stop_event, app=app, logger=log)
        finally:
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
    'ProductionStartupSteps',
    'register_production_lifecycle',
    'start_optional_production_services',
]
