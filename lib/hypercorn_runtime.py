"""Hypercorn configuration boundary for the Quart production server."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from typing import Any

from hypercorn.config import Config

from lib.storage.startup_budget import lifespan_startup_timeout


def build_hypercorn_config(
    host: str,
    port: int,
    *,
    keep_alive_timeout: float,
    tls_cert: str = '',
    tls_key: str = '',
    environ: Mapping[str, str] | None = None,
    logger: Any = None,
) -> Config:
    """Build validated transport settings without importing ``server.py``."""
    env = os.environ if environ is None else environ
    log = logger or logging.getLogger(__name__)
    config = Config()
    config.bind = [f'{host}:{int(port)}']
    config.accesslog = logging.getLogger('hypercorn.access')
    config.errorlog = logging.getLogger('hypercorn.error')
    config.keep_alive_timeout = float(keep_alive_timeout)
    # Hypercorn otherwise cancels Quart's entire startup lifespan at 60s.
    # Initial fastpath activation is a bounded, capacity-checked migration and
    # owns a longer sidecar budget; preserve that budget plus time for the
    # remaining required phases instead of killing and restarting the copy.
    config.startup_timeout = lifespan_startup_timeout(env)
    # /api/push accepts only tiny subscription/control frames and 16 KiB
    # control-RPC requests. Reject oversized WebSocket messages in Hypercorn
    # before Quart allocates/parses a 16 MiB default frame.
    config.websocket_max_message_size = 64 * 1024
    try:
        config.graceful_timeout = float(
            env.get('TOFU_GRACEFUL_TIMEOUT', '') or '3')
    except (TypeError, ValueError, OverflowError) as exc:
        log.debug('[Server] bad TOFU_GRACEFUL_TIMEOUT, defaulting: %s', exc)
        config.graceful_timeout = 3.0

    try:
        backlog = int(env.get('TOFU_LISTEN_BACKLOG', '0') or '0')
    except (TypeError, ValueError, OverflowError) as exc:
        log.debug('[Server] bad TOFU_LISTEN_BACKLOG, defaulting: %s', exc)
        backlog = 0
    config.backlog = backlog if backlog > 0 else 1024

    if tls_cert and tls_key:
        config.certfile = tls_cert
        config.keyfile = tls_key
    return config


__all__ = ['build_hypercorn_config']
