"""Native Quart lifespan registration for the assembled application."""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Callable
from typing import Any


LifecycleHandler = Callable[[], Any]


def _handler_name(handler: LifecycleHandler, name: str | None) -> str:
    if name:
        return name
    module = getattr(handler, '__module__', '')
    qualname = getattr(handler, '__qualname__', repr(handler))
    return f'{module}.{qualname}'.lstrip('.')


def _add_handler(
    app: Any,
    phase: str,
    handler: LifecycleHandler,
    *,
    name: str | None = None,
) -> bool:
    register_app_lifecycle(app)
    handlers = app.extensions[f'tofu_{phase}_handlers']
    resolved_name = _handler_name(handler, name)
    if any(existing_name == resolved_name for existing_name, _ in handlers):
        return False
    handlers.append((resolved_name, handler))
    app.extensions['tofu_lifecycle'][f'{phase}_handlers'] = tuple(
        item_name for item_name, _ in handlers)
    return True


def add_startup_handler(
    app: Any,
    handler: LifecycleHandler,
    *,
    name: str | None = None,
) -> bool:
    """Register application-owned startup work on Quart's serving loop."""
    return _add_handler(app, 'startup', handler, name=name)


def add_shutdown_handler(
    app: Any,
    handler: LifecycleHandler,
    *,
    name: str | None = None,
) -> bool:
    """Register cleanup work run by Quart before its serving loop closes."""
    return _add_handler(app, 'shutdown', handler, name=name)


async def _invoke(handler: LifecycleHandler) -> None:
    result = handler()
    if inspect.isawaitable(result):
        await result


def register_app_lifecycle(app: Any) -> None:
    """Install idempotent startup/shutdown hooks and expose lifecycle state."""
    extensions = app.extensions
    if extensions.get('tofu_lifecycle_registered'):
        return
    state = {
        'status': 'created',
        'started_at': None,
        'stopped_at': None,
        'loop': None,
        'current_handler': None,
        'startup_handlers': (),
        'shutdown_handlers': (),
        'startup_completed': (),
        'shutdown_completed': (),
        'shutdown_errors': (),
        'shutdown_ran': False,
    }
    extensions['tofu_lifecycle_registered'] = True
    extensions['tofu_lifecycle'] = state
    extensions['tofu_startup_handlers'] = []
    extensions['tofu_shutdown_handlers'] = []

    async def _run_shutdown_handlers(*, final_status: str) -> BaseException | None:
        if state['shutdown_ran']:
            return None
        state['shutdown_ran'] = True
        completed = []
        errors = []
        first_error = None
        state['status'] = 'stopping'
        for name, handler in reversed(
                tuple(extensions['tofu_shutdown_handlers'])):
            state['current_handler'] = name
            try:
                await _invoke(handler)
                completed.append(name)
                state['shutdown_completed'] = tuple(completed)
            except BaseException as exc:
                errors.append((name, repr(exc)))
                state['shutdown_errors'] = tuple(errors)
                if first_error is None:
                    first_error = exc
        state.update(status=final_status, stopped_at=time.time(), loop=None,
                     current_handler=None)
        return first_error

    @app.before_serving
    async def _tofu_startup() -> None:
        if state['status'] == 'serving':
            return
        completed = []
        state.update(status='starting', started_at=time.time(), stopped_at=None,
                     loop=asyncio.get_running_loop(), current_handler=None,
                     startup_completed=(), shutdown_ran=False, shutdown_completed=(),
                     shutdown_errors=())
        try:
            for name, handler in tuple(extensions['tofu_startup_handlers']):
                state['current_handler'] = name
                await _invoke(handler)
                completed.append(name)
                state['startup_completed'] = tuple(completed)
        except BaseException:
            state.update(status='startup_failed', current_handler=None)
            # Quart does not invoke after_serving when startup itself fails.
            # Roll back every registered owner now; all shutdown handlers are
            # required to be idempotent and may safely observe partial start.
            # Preserve the startup exception even if cleanup also fails.
            await _run_shutdown_handlers(final_status='startup_failed')
            raise
        state.update(status='serving', current_handler=None)

    @app.after_serving
    async def _tofu_shutdown() -> None:
        first_error = await _run_shutdown_handlers(final_status='stopped')
        if first_error is not None:
            raise first_error


__all__ = [
    'add_shutdown_handler',
    'add_startup_handler',
    'register_app_lifecycle',
]
