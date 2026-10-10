"""Lazy browser owner loop with independent calls and physical cancellation joins.

The resource factory is an async context manager owning Playwright/browser
startup and teardown. Call factories own their contexts/pages through finally.
No call-count gate, execution queue or application import exists in this module.
"""
from __future__ import annotations

import asyncio
import contextvars
import threading
from concurrent.futures import CancelledError, Future

from tofu_search._worker_executor import DemandThreadPoolExecutor

_current_runtime = contextvars.ContextVar('browser_runtime', default=None)
_current_call = contextvars.ContextVar('browser_call', default=None)
_worker_runtime = threading.local()


async def run_browser_blocking(callback, *args, **kwargs):
    runtime = _current_runtime.get()
    if runtime is None:
        raise RuntimeError('blocking browser helper requires an owning call')
    return await runtime.run_blocking(callback, *args, **kwargs)


def bind_browser_blocking():
    """Capture a call's helper owner for callbacks dispatched by Playwright."""
    runtime, owner = _current_runtime.get(), _current_call.get()
    if runtime is None or owner is None:
        raise RuntimeError('browser callback requires an owning call')
    context = contextvars.copy_context()
    async def bound(callback, *args, **kwargs):
        return await runtime.run_blocking(callback, *args, _owner=owner,
                                          _context=context.copy(), **kwargs)
    return bound


async def _drain_cleanup(future):
    """Repeated stop signals cannot interrupt already-owned cleanup."""
    while not future.done():
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            continue
    return future.result()


async def close_browser_context(context):
    """Close one context; a real close failure disposes the shared resource."""
    try:
        value = context.close()
        if hasattr(value, '__await__'):
            await _drain_cleanup(asyncio.ensure_future(value))
    except Exception:
        runtime = _current_runtime.get()
        if runtime is not None:
            await runtime.discard_resource()
        raise


async def open_browser_context(browser, **kwargs):
    """Acquire a context without abandoning a completed creation on cancellation."""
    value = browser.new_context(**kwargs)
    if not hasattr(value, '__await__'):
        return value  # Immediate context supplied by controlled API fakes.
    future = asyncio.ensure_future(value)
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        # Context creation is transferred only when the caller actually receives
        # it; until then this helper owns both the result and cancellation cleanup.
        context = await _drain_cleanup(future)
        await close_browser_context(context)
        raise


class BrowserCall:
    """Completion becomes visible only after the owning coroutine finishes."""
    def __init__(self, runtime):
        self._runtime = runtime
        self._completion = Future()
        self._task = None
        self._cancel_requested = False
        self._closing = False
        self._helpers = set()

    def result(self, timeout=None):
        return self._completion.result(timeout=timeout)

    def add_done_callback(self, callback):
        self._completion.add_done_callback(lambda _: callback(self))

    def done(self):
        return self._completion.done()

    def cancel(self):
        with self._runtime._lock:
            if self.done() or self._cancel_requested:
                return False
            self._cancel_requested = True
            self._runtime._loop.call_soon_threadsafe(self._cancel_on_loop)
            return True

    def _cancel_on_loop(self):
        if self._task is not None and not self._task.done():
            self._task.cancel()

    def cancel_and_join(self):
        self.cancel()
        try:
            return self.result()
        except CancelledError:
            return None  # Cancellation is reported after page/context cleanup.


class AsyncBrowserRuntime:
    def __init__(self, resource_factory):
        self._resource_factory = resource_factory
        self._resource_manager = None
        self._resource_task = None
        self._resource_close_task = None
        self._broken = False
        self._lock = threading.RLock()
        self._shutdown_lock = threading.Lock()
        self._closing = False
        self._closed = False
        self._loop = None
        self._thread = None
        self._calls = set()
        self._blocking = DemandThreadPoolExecutor(max_workers=1, thread_name_prefix='playwright-helper')

    def submit(self, action, *, needs_resource=True):
        with self._lock:
            if self._broken:
                raise RuntimeError('browser resource is unavailable')
            if self._closing:
                raise RuntimeError('browser runtime is shutting down')
            if self._loop is None:
                loop = asyncio.new_event_loop()
                thread = threading.Thread(target=loop.run_forever,
                    name='playwright-owner-loop', daemon=True)
                try:
                    thread.start()
                except BaseException:
                    loop.close()
                    raise
                self._loop, self._thread = loop, thread
            call = BrowserCall(self)
            context = contextvars.copy_context()
            self._calls.add(call)
            try:
                self._loop.call_soon_threadsafe(self._start_call, call, action, needs_resource, context=context)
            except BaseException:
                self._calls.discard(call)
                raise
            return call

    async def _start_resource(self):
        manager = self._resource_factory()
        resource = await manager.__aenter__()
        self._resource_manager = manager
        return resource

    def _start_call(self, call, action, needs_resource):
        async def owned():
            resource = None
            if needs_resource:
                if self._resource_task is None:
                    self._resource_task = contextvars.Context().run(
                        asyncio.create_task, self._start_resource())
                resource = await asyncio.shield(self._resource_task)
            token = _current_runtime.set(self)
            call_token = _current_call.set(call)
            try:
                return await action(resource)
            finally:
                try:
                    call._closing = True
                    while call._helpers:
                        helper = next(iter(call._helpers))
                        try:
                            await _drain_cleanup(asyncio.wrap_future(helper))
                        except Exception:
                            pass  # Helper failure is retained by its awaiting callback.
                        call._helpers.discard(helper)
                    if self._broken and self._resource_close_task is not None:
                        while not self._resource_close_task.done():
                            try:
                                await asyncio.shield(self._resource_close_task)
                            except asyncio.CancelledError:
                                continue  # Resource disposal still owns physical teardown.
                        self._resource_close_task.result()
                finally:
                    _current_call.reset(call_token)
                    _current_runtime.reset(token)
        task = asyncio.create_task(owned())
        call._task = task
        task.add_done_callback(lambda settled: self._finish_call(call, settled))
        if call._cancel_requested:
            task.cancel()

    def _finish_call(self, call, task):
        # Completion includes the action's finally/context close. Retire the
        # registry before waking waiters so they observe the settled topology.
        cancelled = task.cancelled() or call._cancel_requested
        error = None if cancelled else task.exception()
        result = None if cancelled or error is not None else task.result()
        call._task = None
        with self._lock:
            self._calls.discard(call)
        if cancelled:
            call._completion.set_exception(CancelledError())
        elif error is not None:
            call._completion.set_exception(error)
        else:
            call._completion.set_result(result)

    async def run_blocking(self, callback, *args, _owner=None, _context=None, **kwargs):
        owner = _owner if _owner is not None else _current_call.get()
        if owner is None or owner._closing:
            raise asyncio.CancelledError
        context = _context if _context is not None else contextvars.copy_context()
        def owned_helper():
            _worker_runtime.runtime = self
            try:
                return context.run(callback, *args, **kwargs)
            finally:
                del _worker_runtime.runtime
        physical = self._blocking.submit(owned_helper)
        owner._helpers.add(physical)
        physical.add_done_callback(lambda _: self._loop.call_soon_threadsafe(owner._helpers.discard, physical))
        future = asyncio.wrap_future(physical)
        try:
            result = await asyncio.shield(future)
            if owner._closing:
                raise asyncio.CancelledError
            return result
        except asyncio.CancelledError:
            # Cancellation of a synchronous helper never settles its physical
            # worker; drain it before the context/page can be relinquished.
            while not future.done():
                try:
                    await asyncio.shield(future)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break  # The helper physically returned with its own error.
            if future.done() and not future.cancelled():
                future.exception()
            raise

    async def discard_resource(self):
        # A context-close failure cannot leave an orphan on a warm browser.
        # Closing the actual browser/driver wakes other calls with their real
        # transport failures; their own scopes still drain physical helpers.
        self._broken = True
        await self._ensure_resource_closed()

    async def _ensure_resource_closed(self):
        if self._resource_close_task is None:
            self._resource_close_task = asyncio.create_task(self._close_resource())
        await asyncio.shield(self._resource_close_task)

    async def _close_resource(self):
        if self._resource_task is not None:
            if not self._resource_task.done():
                self._resource_task.cancel()
            try:
                await self._resource_task
            except asyncio.CancelledError:
                pass  # Startup cancellation ran the factory's owned cleanup.
            except Exception:
                pass  # Startup error was delivered to each admitted call.
        if self._resource_manager is not None:
            await self._resource_manager.__aexit__(None, None, None)
            self._resource_manager = None

    def shutdown(self):
        if (threading.current_thread() is self._thread
                or getattr(_worker_runtime, 'runtime', None) is self):
            raise RuntimeError('browser runtime cannot join its owner loop')
        with self._shutdown_lock:
            if self._closed:
                return
            with self._lock:
                self._closing = True
                calls = tuple(self._calls)
                for call in calls:
                    call.cancel()
            for call in calls:
                try:
                    call.result()
                except Exception:
                    pass  # Each call retains its own settled failure Future.
            if self._loop is not None:
                asyncio.run_coroutine_threadsafe(self._ensure_resource_closed(), self._loop).result()
                self._blocking.shutdown(wait=True)
                self._loop.call_soon_threadsafe(self._loop.stop)
                self._thread.join()
                self._loop.close()
            else:
                self._blocking.shutdown(wait=True)
            self._closed = True

    def snapshot(self):
        with self._lock:
            return {'active_calls':len(self._calls),
                    'owner_threads':int(self._thread is not None and self._thread.is_alive())}
