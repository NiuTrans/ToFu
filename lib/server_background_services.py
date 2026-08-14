"""Lifecycle-owned background services and crash-resume sweeps."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping
from typing import Any


def start_background_services(
    app: Any,
    *,
    load_saved_proxy_config: Callable[[], Any],
    bootstrap_personal_key: Callable[[], Any],
    logger: logging.Logger | None = None,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Start optional workers after required database bootstrap completes.

    Imports stay inside this function so importing the HTTP application cannot
    launch threads, touch persisted job state or probe the network.
    """
    log = logger or logging.getLogger('server')
    env = os.environ if environ is None else environ

    try:
        from routes import start_registered_background_services

        start_registered_background_services(app)
    except Exception as exc:
        log.warning('[Server] route background services failed: %s', exc)

    # Proxy state must be installed before the first network probe, and the
    # first-boot key requires the initialized system database.
    load_saved_proxy_config()
    bootstrap_personal_key()

    try:
        from lib.billing.janitor import start_janitor

        start_janitor()
    except Exception as exc:
        log.warning('[Billing] janitor failed to start: %s', exc)
    try:
        from lib.netpath import start_prober

        start_prober()
    except Exception as exc:
        log.warning('Failed to start netpath prober: %s', exc)

    try:
        from lib.integration_control import ensure_worker_started

        if ensure_worker_started():
            log.info('[Server] deterministic integration worker armed')
    except Exception as exc:
        log.warning('[Server] integration worker start failed: %s', exc)

    try:
        from lib.motion_video.engine import resume_interrupted_jobs

        resumed = resume_interrupted_jobs()
        if resumed:
            log.info(
                '[Server] resumed %d interrupted motion job(s)', resumed)
    except Exception as exc:
        log.warning('[Server] motion job resume failed: %s', exc)

    try:
        from lib.research import resume_interrupted_research

        resumed = resume_interrupted_research()
        if resumed:
            log.info(
                '[Server] resumed %d interrupted research job(s)', resumed)
    except Exception as exc:
        log.warning('[Server] research job resume failed: %s', exc)

    try:
        from lib.slides.engine import resume_interrupted_decks

        resumed = resume_interrupted_decks()
        if resumed:
            log.info(
                '[Server] resumed %d interrupted slides job(s)', resumed)
    except Exception as exc:
        log.warning('[Server] slides job resume failed: %s', exc)

    try:
        from lib.paper.podcast_engine import mark_interrupted_podcasts

        mark_interrupted_podcasts()
    except Exception as exc:
        log.warning('[Server] podcast interrupted sweep failed: %s', exc)

    try:
        from lib.desktop.pairing import maybe_start_responder

        responder = maybe_start_responder(
            int(env.get('_TOFU_RUNTIME_PORT') or '15000'),
            bind_host=env.get('_TOFU_RUNTIME_HOST') or '',
        )
        if responder is not None:
            log.info(
                '[Server] LAN discovery responder up on UDP 15001 (%s)',
                responder.url,
            )
    except Exception as exc:
        log.warning('[Server] LAN discovery responder failed: %s', exc)


__all__ = ['start_background_services']
