"""Lifecycle-owned background services and crash-resume sweeps."""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable, Mapping
from typing import Any


_LAN_DISCOVERY_LOCK = threading.Lock()
_LAN_DISCOVERY_RESPONDER: Any | None = None


def start_lan_discovery_responder(
    port: int,
    *,
    bind_host: str,
    environ: Mapping[str, str],
) -> Any | None:
    """Start or reuse the one LAN responder owned by this request process."""
    global _LAN_DISCOVERY_RESPONDER
    from lib.desktop.pairing import maybe_start_responder

    with _LAN_DISCOVERY_LOCK:
        current = _LAN_DISCOVERY_RESPONDER
        if current is not None and current.is_running():
            return current
        responder = maybe_start_responder(
            port, environ=environ, bind_host=bind_host)
        if responder is not None:
            _LAN_DISCOVERY_RESPONDER = responder
        return responder


def stop_lan_discovery_responder(timeout: float = 2.0) -> bool:
    """Stop the exact process-owned LAN responder without duplicating it."""
    global _LAN_DISCOVERY_RESPONDER
    with _LAN_DISCOVERY_LOCK:
        responder = _LAN_DISCOVERY_RESPONDER
    if responder is None:
        return True
    stopped = responder.stop(timeout=timeout)
    if stopped:
        with _LAN_DISCOVERY_LOCK:
            if _LAN_DISCOVERY_RESPONDER is responder:
                _LAN_DISCOVERY_RESPONDER = None
    return stopped


def start_background_services(
    app: Any,
    *,
    process_role: str = 'all',
    load_saved_proxy_config: Callable[[], Any],
    bootstrap_personal_key: Callable[[], Any],
    logger: logging.Logger | None = None,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Start lifecycle-owned workers after required storage recovery.

    Imports stay inside this function so importing the HTTP application cannot
    launch threads, touch persisted job state or probe the network. The caller
    runs this function off the event loop and does not announce readiness until
    every owner has either started or reported its best-effort failure.
    """
    from lib.process_roles import (
        CAPABILITY_NETWORK_CONFIGURATION,
        CAPABILITY_REQUEST_SERVICES,
        CAPABILITY_TASK_WORKERS,
        normalize_process_role,
        process_role_has,
    )

    log = logger or logging.getLogger('server')
    env = os.environ if environ is None else environ
    process_role = normalize_process_role(process_role)
    from runtime_guards import distributed_preview_is_read_only

    if distributed_preview_is_read_only(env):
        log.info(
            '[Server] background services disabled by distributed read-only '
            'preview fence (role=%s)', process_role)
        return
    owns_network_configuration = process_role_has(
        process_role, CAPABILITY_NETWORK_CONFIGURATION)
    owns_request_services = process_role_has(
        process_role, CAPABILITY_REQUEST_SERVICES)
    owns_task_workers = process_role_has(
        process_role, CAPABILITY_TASK_WORKERS)

    try:
        from routes import start_registered_background_services

        start_registered_background_services(
            app, process_role=process_role)
    except Exception as exc:
        log.warning('[Server] route background services failed: %s', exc)

    # Turn-search materialization is owned by the Storage Sidecar. Starting a
    # second web-process worker would put historical repair back on the
    # authority command lane and duplicate work across API replicas.

    # Proxy state must be installed before the first network probe, and the
    # first-boot key requires the initialized system database.
    if owns_network_configuration:
        load_saved_proxy_config()
    if owns_request_services:
        bootstrap_personal_key()

    if owns_request_services:
        try:
            from lib.netpath import start_prober

            start_prober()
        except Exception as exc:
            log.warning('Failed to start netpath prober: %s', exc)

    if owns_task_workers:
        try:
            from lib.integration_control import ensure_worker_started

            if ensure_worker_started():
                log.info('[Server] deterministic integration worker armed')
        except Exception as exc:
            log.warning('[Server] integration worker start failed: %s', exc)

    if owns_task_workers:
        try:
            from lib.motion_video.engine import resume_interrupted_jobs

            resumed = resume_interrupted_jobs()
            if resumed:
                log.info(
                    '[Server] resumed %d interrupted motion job(s)', resumed)
        except Exception as exc:
            log.warning('[Server] motion job resume failed: %s', exc)

        try:
            from lib.research.api import resume_interrupted_research

            resumed = resume_interrupted_research()
            if resumed:
                log.info(
                    '[Server] resumed %d interrupted research job(s)', resumed)
        except Exception as exc:
            log.warning('[Server] research job resume failed: %s', exc)

        try:
            from lib.slides.api import resume_interrupted_decks

            resumed = resume_interrupted_decks()
            if resumed:
                log.info(
                    '[Server] resumed %d interrupted slides job(s)', resumed)
        except Exception as exc:
            log.warning('[Server] slides job resume failed: %s', exc)

        try:
            from lib.paper.podcast_engine.worker import mark_interrupted_podcasts

            mark_interrupted_podcasts()
        except Exception as exc:
            log.warning('[Server] podcast interrupted sweep failed: %s', exc)

    if owns_request_services:
        try:
            responder = start_lan_discovery_responder(
                int(env.get('_TOFU_RUNTIME_PORT') or '15000'),
                environ=env,
                bind_host=env.get('_TOFU_RUNTIME_HOST') or '',
            )
            if responder is not None:
                log.info(
                    '[Server] LAN discovery responder up on UDP 15001 (%s)',
                    responder.url,
                )
        except Exception as exc:
            log.warning('[Server] LAN discovery responder failed: %s', exc)


__all__ = [
    'start_background_services',
    'start_lan_discovery_responder',
    'stop_lan_discovery_responder',
]
