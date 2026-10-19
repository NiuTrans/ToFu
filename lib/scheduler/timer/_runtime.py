"""Process-owned timer waiting and blocking-call lifecycle.

TimerWatcherHandle schedules one coroutine on a lazy shared loop. Blocking
storage/model work uses the demand executor; stop wakes waits and joins actual
work before releasing leases. shutdown_timer_watchers precedes store closure.
"""
from __future__ import annotations

import asyncio
import contextvars
import threading
from concurrent.futures import CancelledError as FutureCancelled, TimeoutError as FutureTimeout

from tofu_workers import DemandExecutor

from lib.log import get_logger

logger = get_logger(__name__)

_current_handle = contextvars.ContextVar('timer_watcher_handle', default=None)
_worker_owner = threading.local()


class TimerWatcherRuntime:
    def __init__(self):
        self._lock = threading.RLock()
        self._loop = None
        self._thread = None
        self._closing = False
        self._closed = False
        self._shutdown_lock = threading.Lock()
        self._handles = set()
        # No idle retention or active-work ceiling. These compatibility hints
        # do not gate submission; each accepted blocking call obtains a worker.
        self._executor = DemandExecutor(
            max_workers=1, queue_capacity=0, max_abandoned_workers=1,
            idle_retain_threads=0, thread_name_prefix='timer-work')

    def start(self, handle):
        with self._lock:
            if self._closing:
                raise RuntimeError('timer runtime is shutting down')
            if handle._future is not None:
                raise RuntimeError('timer watcher already started')
            if self._loop is None:
                loop = asyncio.new_event_loop()
                thread = threading.Thread(target=loop.run_forever,
                                          name='timer-wait-loop', daemon=True)
                try:
                    thread.start()
                except BaseException:
                    loop.close()
                    raise
                self._loop, self._thread = loop, thread
            coroutine = handle._run()
            try:
                handle._future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
            except BaseException:
                coroutine.close()
                raise
            self._handles.add(handle)
            handle._future.add_done_callback(lambda _: self._retire(handle))

    def _retire(self, handle):
        with self._lock:
            self._handles.discard(handle)

    def shutdown(self):
        if (threading.current_thread() is self._thread
                or getattr(_worker_owner, 'runtime', None) is self):
            raise RuntimeError('timer runtime cannot join its own worker')
        with self._shutdown_lock:
            if not self._closed:
                self._shutdown_owned()

    def _shutdown_owned(self):
        with self._lock:
            self._closing = True
            handles = tuple(self._handles)
            for handle in handles:
                handle.stop()
        # A logical stop cannot settle an executing synchronous callback.
        # Its owning coroutine drains it and releases its lease before joining.
        try:
            failures = []
            for handle in handles:
                try:
                    handle.join()
                except Exception as error:
                    failures.append(error)
        finally:
            self._executor.shutdown(wait=True)
            if self._loop is not None:
                self._loop.call_soon_threadsafe(self._loop.stop)
                self._thread.join()
                self._loop.close()
            self._closed = True
        if failures:
            raise failures[0]

    def snapshot(self):
        with self._lock:
            return {'watchers': len(self._handles),
                    'wait_threads': int(self._thread is not None and self._thread.is_alive())}


class TimerWatcherHandle:
    def __init__(self, *, target, name, runtime=None):
        self._target = target
        self.name = name
        self._runtime = runtime if runtime is not None else _get_runtime()
        self._future = None
        self._stop_requested = threading.Event()
        self._stop_event = None

    def start(self):
        if self._future is not None:
            raise RuntimeError('timer watcher already started')
        self._runtime.start(self)

    async def _run(self):
        self._stop_event = asyncio.Event()
        if self._stop_requested.is_set():
            self._stop_event.set()
        token = _current_handle.set(self)
        try:
            await self._target()
        finally:
            _current_handle.reset(token)

    def stop(self):
        self._stop_requested.set()
        if self._stop_event is not None and self.is_alive():
            self._runtime._loop.call_soon_threadsafe(self._stop_event.set)

    def join(self, timeout=None):
        if self._future is not None:
            try:
                self._future.result(timeout=timeout)
            except (FutureTimeout, FutureCancelled):
                return

    def is_alive(self):
        return self._future is not None and not self._future.done()


async def watcher_delay(seconds):
    handle = _current_handle.get()
    if handle is None:
        await asyncio.sleep(max(0, seconds))
        return True
    if handle._stop_requested.is_set():
        return False
    try:
        await asyncio.wait_for(handle._stop_event.wait(), timeout=max(0, seconds))
        return False
    except asyncio.TimeoutError:
        return not handle._stop_requested.is_set()


async def run_blocking(callback, *args, _allow_stopping=False, **kwargs):
    handle = _current_handle.get()
    if handle is None:
        raise RuntimeError('timer blocking call requires an owning watcher')
    if handle._stop_requested.is_set() and not _allow_stopping:
        raise asyncio.CancelledError
    context = contextvars.copy_context()

    def owned_call():
        _worker_owner.runtime = handle._runtime
        try:
            return context.run(callback, *args, **kwargs)
        finally:
            del _worker_owner.runtime

    future = asyncio.wrap_future(handle._runtime._executor.submit(owned_call))
    try:
        result = await asyncio.shield(future)
    except asyncio.CancelledError:
        # Task cancellation never transfers ownership of a physical callback.
        while not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                continue
            except Exception as diagnostic_error:
                logger.debug('run_blocking failure handled: %s', type(diagnostic_error).__name__)
                break  # The physical callback settled with its own failure.
        if future.done() and not future.cancelled():
            future.exception()  # Retrieve any error before propagating cancellation.
        raise
    if handle._stop_requested.is_set() and not _allow_stopping:
        raise asyncio.CancelledError
    return result


_runtime_lock = threading.Lock()
_runtime = None


def _get_runtime():
    global _runtime
    with _runtime_lock:
        if _runtime is None:
            _runtime = TimerWatcherRuntime()
        return _runtime


def shutdown_timer_watchers():
    # Create a closed, thread-free authority even if no watcher ever started:
    # a racing create must observe shutdown instead of opening a new runtime.
    _get_runtime().shutdown()
