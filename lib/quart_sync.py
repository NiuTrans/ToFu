"""Explicit bridges for legacy synchronous Quart route boundaries.

Quart executes ordinary ``def`` views in its executor, but request-body
properties and response helpers are async-native.  These wrappers are the only
place a synchronous view may cross back to the serving loop.  New ``async def``
views should await Quart directly and must not use this module.
"""

from __future__ import annotations

import asyncio
import inspect
import os
from concurrent.futures import TimeoutError as FuturesTimeoutError
from contextlib import suppress
from typing import Any

from lib.log import get_logger


logger = get_logger(__name__)


def sync_boundary_timeout() -> float | None:
    raw = os.environ.get('TOFU_SYNC_BODY_TIMEOUT', '') or '300'
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        logger.debug('[QuartSync] invalid timeout %r; using 300s: %s', raw, exc)
        return 300.0
    return None if value <= 0 else value


def await_on_loop(awaitable: Any, loop: asyncio.AbstractEventLoop,
                  timeout: float | None) -> Any:
    """Resolve an awaitable on ``loop`` from an executor thread."""
    future = asyncio.run_coroutine_threadsafe(awaitable, loop)
    try:
        return future.result(timeout=timeout)
    except FuturesTimeoutError:
        future.cancel()
        logger.error('[QuartSync] boundary timed out after %ss; cancelling',
                     timeout)
        raise


def resolve(awaitable: Any, *, timeout: float | None = None) -> Any:
    """Resolve a Quart awaitable without mutating Quart's module/classes."""
    if not inspect.isawaitable(awaitable):
        return awaitable
    running_loop = None
    with suppress(RuntimeError):
        running_loop = asyncio.get_running_loop()
    if running_loop is not None:
        if inspect.iscoroutine(awaitable):
            awaitable.close()
        raise RuntimeError(
            'lib.quart_sync cannot run on the event loop; await Quart directly')

    try:
        from lib.agent_core.push import hub
        serving_loop = getattr(hub, '_loop', None)
    except ImportError as exc:
        logger.debug('[QuartSync] serving-loop lookup unavailable: %s', exc)
        serving_loop = None
    if serving_loop is not None and serving_loop.is_running():
        return await_on_loop(
            awaitable, serving_loop,
            sync_boundary_timeout() if timeout is None else timeout)
    return asyncio.run(awaitable)


def request_json(*, force: bool = False, silent: bool = True) -> Any:
    from quart import request
    return resolve(request.get_json(force=force, silent=silent))


def request_files() -> Any:
    from quart import request
    return resolve(request.files)


def request_form() -> Any:
    from quart import request
    return resolve(request.form)


def request_data() -> Any:
    from quart import request
    return resolve(request.data)


def make_response(*args: Any, **kwargs: Any) -> Any:
    from quart import make_response as quart_make_response
    return resolve(quart_make_response(*args, **kwargs))


def send_file(*args: Any, **kwargs: Any) -> Any:
    from quart import send_file as quart_send_file
    if 'download_name' in kwargs:
        parameters = inspect.signature(quart_send_file).parameters
        if ('download_name' not in parameters
                and 'attachment_filename' in parameters):
            kwargs['attachment_filename'] = kwargs.pop('download_name')
    return resolve(quart_send_file(*args, **kwargs))


def send_from_directory(*args: Any, **kwargs: Any) -> Any:
    from quart import send_from_directory as quart_send_from_directory
    if 'download_name' in kwargs:
        parameters = inspect.signature(quart_send_from_directory).parameters
        if ('download_name' not in parameters
                and 'attachment_filename' in parameters):
            kwargs['attachment_filename'] = kwargs.pop('download_name')
    return resolve(quart_send_from_directory(*args, **kwargs))


__all__ = [
    'await_on_loop', 'make_response', 'request_data', 'request_files',
    'request_form', 'request_json', 'resolve', 'send_file',
    'send_from_directory', 'sync_boundary_timeout',
]
