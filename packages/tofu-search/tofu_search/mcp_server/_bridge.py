"""Owned demand bridge for the asyncio MCP server's synchronous pipelines.

Each call copies request ContextVars and retains its physical worker through
cancellation. Legacy concurrency settings size idle reuse, not admission.
"""
from __future__ import annotations

import asyncio
import contextvars
import os
import threading
from dataclasses import replace
from typing import Any, Callable, TypeVar

import anyio

from tofu_search._worker_executor import DemandThreadPoolExecutor
from tofu_search.config import bind_call_config, get_config
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
    stopped = threading.Event()
    config = get_config()
    inherited_stop = config.local_rerank_cancel_event

    class RequestStop:
        def is_set(self):
            return stopped.is_set() or (inherited_stop is not None and inherited_stop.is_set())

    def invoke():
        with bind_call_config(replace(config, local_rerank_cancel_event=RequestStop())):
            return fn(*args, **kwargs)

    context = contextvars.copy_context()
    future = _executor().submit(context.run, invoke)
    waiter = asyncio.wrap_future(future)
    try:
        return await asyncio.shield(waiter)
    except asyncio.CancelledError as cancellation:
        stopped.set()
        # Observe both AnyIO scope cancellation and direct Task.cancel first;
        # only retirement is shielded. Repeated Stop cannot detach the worker.
        with anyio.CancelScope(shield=True):
            while True:
                try:
                    await asyncio.shield(waiter)
                    break
                except asyncio.CancelledError as error:
                    if waiter.done():
                        raise cancellation from error
                except BaseException as error:
                    raise cancellation from error
        raise
