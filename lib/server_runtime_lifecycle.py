"""Composition boundary for the process-wide Quart lifespan owners."""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable, Mapping
from typing import Any

from lib.log import get_logger
from lib.serving_loop_lifecycle import register_serving_loop_lifecycle


LifecycleRegistrar = Callable[..., bool]
_MODULE_LOG = get_logger(__name__)


def resolve_runtime_endpoint(
    *,
    host: Any = None,
    port: Any = None,
    environ: Mapping[str, str] | None = None,
) -> tuple[str, int]:
    """Resolve the effective watchdog endpoint with bounded port fallback."""
    env = os.environ if environ is None else environ
    runtime_host = str(
        host or env.get('_TOFU_RUNTIME_HOST')
        or env.get('TOFU_HOST') or '0.0.0.0')
    raw_port = (
        port if port is not None
        else env.get('_TOFU_RUNTIME_PORT') or env.get('TOFU_PORT') or '15000'
    )
    try:
        runtime_port = int(raw_port)
    except (TypeError, ValueError, OverflowError) as exc:
        _MODULE_LOG.debug('[Server] invalid runtime port %r: %s', raw_port, exc)
        runtime_port = 15000
    if not 1 <= runtime_port <= 65535:
        runtime_port = 15000
    return runtime_host, runtime_port


def register_runtime_lifecycle(
    app: Any,
    *,
    production_registrar: LifecycleRegistrar,
    shutdown_requested: Any = None,
    announce_ready: Callable[..., Any] | None = None,
    host: Any = None,
    port: Any = None,
    hooks: Any = None,
    fault_shm_log: Any = None,
    fault_log: Any = None,
    logger: logging.Logger | None = None,
    environ: Mapping[str, str] | None = None,
    process_role: str = 'all',
    serving_registrar: LifecycleRegistrar = register_serving_loop_lifecycle,
) -> bool:
    """Attach loop resources before production bootstrap using one stop event."""
    existing_event = app.extensions.get('tofu_shutdown_requested')
    stop_event = shutdown_requested or existing_event or threading.Event()
    runtime_host, runtime_port = resolve_runtime_endpoint(
        host=host, port=port, environ=environ)

    serving_registered = serving_registrar(
        app,
        shutdown_requested=stop_event,
        host=runtime_host,
        port=runtime_port,
        hooks=hooks,
        fault_shm_log=fault_shm_log,
        fault_log=fault_log,
        logger=logger,
        environ=environ,
        process_role=process_role,
    )
    production_registered = production_registrar(
        app,
        shutdown_requested=stop_event,
        announce_ready=announce_ready,
    )
    return serving_registered or production_registered


__all__ = ['register_runtime_lifecycle', 'resolve_runtime_endpoint']
