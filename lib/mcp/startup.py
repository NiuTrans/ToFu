"""Owned, shutdown-aware MCP prewarm and auto-connect worker."""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping
from typing import Any


_worker: threading.Thread | None = None
_worker_stop = threading.Event()
_worker_lock = threading.Lock()


def _run_auto_connect(
    enabled: int,
    *,
    logger: logging.Logger,
) -> None:
    try:
        try:
            from lib.mcp.client import prewarm_all_vendored
            warmed = prewarm_all_vendored()
            if warmed:
                logger.info('[MCP] Pre-warm: %s', warmed)
        except Exception as exc:
            logger.warning('[MCP] Pre-warm failed: %s', exc)

        if enabled <= 0 or _worker_stop.is_set():
            return
        try:
            from lib.mcp.client import get_bridge
            bridge = get_bridge()
            result = bridge.connect_all()
            if _worker_stop.is_set():
                # Shutdown may have raced an uninterruptible handshake. Never
                # let that late completion resurrect connections after the
                # production cleanup already started.
                bridge.disconnect_all()
                return
            total = sum(len(tools) for tools in result.values())
            logger.info(
                '[MCP] Auto-connect: %d servers, %d tools', len(result), total)
        except Exception as exc:
            logger.error('[MCP] Auto-connect failed: %s', exc, exc_info=True)
    finally:
        # Keep the finished object visible until the next start/stop takes the
        # lock; ownership state is reconciled there without a self-join.
        logger.debug('[MCP] startup worker finished')


def start_mcp_auto_connect(
    config: Mapping[str, Mapping[str, Any]] | None,
    *,
    logger: logging.Logger | None = None,
) -> bool:
    """Start one prewarm/connect worker; duplicate starts share its owner."""
    global _worker
    log = logger or logging.getLogger(__name__)
    enabled = sum(
        1 for row in (config or {}).values() if row.get('enabled', True))
    with _worker_lock:
        if _worker is not None and _worker.is_alive():
            return False
        _worker_stop.clear()
        thread = threading.Thread(
            target=_run_auto_connect,
            kwargs={'enabled': enabled, 'logger': log},
            name='mcp-auto-connect',
            daemon=True,
        )
        _worker = thread
        try:
            thread.start()
        except BaseException:
            _worker = None
            _worker_stop.set()
            raise
    return True


def stop_mcp_auto_connect(*, timeout: float = 2.0) -> bool:
    """Prevent a late connect and wait boundedly for the startup worker."""
    global _worker
    _worker_stop.set()
    with _worker_lock:
        thread = _worker
    if thread is None:
        return True
    if thread.is_alive():
        thread.join(max(0.0, timeout))
    stopped = not thread.is_alive()
    if stopped:
        with _worker_lock:
            if _worker is thread:
                _worker = None
    return stopped


__all__ = ['start_mcp_auto_connect', 'stop_mcp_auto_connect']
