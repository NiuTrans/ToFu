"""Owned demand bridge for the asyncio MCP server's synchronous pipelines.

Each call copies request ContextVars and retains its physical worker through
cancellation. Legacy concurrency settings size idle reuse, not admission.
"""
from __future__ import annotations

import asyncio
import contextvars
import os
import threading
from typing import Any, Callable, TypeVar

import anyio

from tofu_search._worker_executor import DemandThreadPoolExecutor
from tofu_search.log import get_logger

logger = get_logger(__name__)
T = TypeVar('T')
DEFAULT_MAX_CONCURRENCY = 4  # legacy idle hint
_ENV_MAX_CONCURRENCY = 'TOFU_SEARCH_MCP_MAX_CONCURRENCY'
_pool: DemandThreadPoolExecutor | None = None
_pool_lock = threading.Lock()


def _configured_concurrency() -> int:
    raw = os.environ.get(_ENV_MAX_CONCURRENCY, '').strip()
    if not raw:
        return DEFAULT_MAX_CONCURRENCY
    try:
        value = int(raw)
    except ValueError:
        logger.warning('[MCP] %s=%r is not an integer, using %d',
                       _ENV_MAX_CONCURRENCY, raw, DEFAULT_MAX_CONCURRENCY)
        return DEFAULT_MAX_CONCURRENCY
    if value < 1:
        logger.warning('[MCP] %s=%d is below 1, using 1', _ENV_MAX_CONCURRENCY, value)
        return 1
    return value



def _executor() -> DemandThreadPoolExecutor:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = DemandThreadPoolExecutor(
                max_workers=_configured_concurrency(),
                thread_name_prefix='tofu-search-mcp')
        return _pool


def shutdown_bridge() -> None:
    """Called after server transport closes; join every published call."""
    global _pool
    with _pool_lock:
        pool, _pool = _pool, None
    if pool is not None:
        pool.shutdown(wait=True)


def bridge_snapshot() -> dict[str, int]:
    """Observe running work without starting workers or granting a quota."""
    with _pool_lock:
        pool = _pool
    state = pool.scheduling_snapshot() if pool is not None else {}
    return {'limit': 0, 'in_use': state.get('active', 0),
            'idle_reuse_hint': _configured_concurrency(),
            'resident_threads': state.get('residentThreads', 0)}


async def run_blocking(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Await a demand worker and drain it when request cancellation wins."""
    context = contextvars.copy_context()
    future = _executor().submit(context.run, fn, *args, **kwargs)
    waiter = asyncio.wrap_future(future)
    cancellation = None
    # MCP's AnyIO scopes and direct asyncio Task.cancel both retain ownership.
    with anyio.CancelScope(shield=True):
        while True:
            try:
                result = await asyncio.shield(waiter)
                break
            except asyncio.CancelledError as error:
                if waiter.done():
                    if cancellation is not None:
                        raise cancellation from error
                    raise
                cancellation = error
            except BaseException as error:
                if cancellation is not None:
                    raise cancellation from error
                raise
        if cancellation is not None:
            raise cancellation
        return result
