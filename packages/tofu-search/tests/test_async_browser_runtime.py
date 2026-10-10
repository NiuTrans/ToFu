"""Owned async browser calls run concurrently and cancel only after cleanup."""
import asyncio
import contextvars
import threading
import time
from concurrent.futures import CancelledError
from contextlib import asynccontextmanager

import pytest

from tofu_search.fetch._async_browser_runtime import AsyncBrowserRuntime

pytestmark = pytest.mark.unit


def test_independent_calls_share_one_resource_and_owner_thread():
    owner = contextvars.ContextVar('test_browser_owner')
    started, closed = [], []
    waiting = threading.Event()
    entered = 0
    @asynccontextmanager
    async def factory():
        started.append(1)
        try:
            yield object()
        finally:
            closed.append(1)
    runtime = AsyncBrowserRuntime(factory)
    async def blocked(resource):
        nonlocal entered
        identity = owner.get()
        entered += 1
        if entered == 32:
            waiting.set()
        await asyncio.Event().wait()
        return identity
    calls=[]
    try:
        assert runtime.snapshot()=={'active_calls':0,'owner_threads':0}
        for index in range(32):
            token=owner.set(index)
            try:calls.append(runtime.submit(blocked))
            finally:owner.reset(token)
        assert waiting.wait(5)
        async def healthy(resource):
            assert owner.get()==100
            return 42
        token=owner.set(100)
        try: assert runtime.submit(healthy).result(2)==42
        finally:owner.reset(token)
        assert all(not call.done() for call in calls)
        assert runtime.snapshot()=={'active_calls':32,'owner_threads':1}
    finally:runtime.shutdown()
    assert started==closed==[1]
    assert all(call.done() for call in calls)
    assert runtime.snapshot()=={'active_calls':0,'owner_threads':0}


def test_cancel_completion_waits_for_context_cleanup():
    entered, closing, release, closed = (threading.Event() for _ in range(4))
    @asynccontextmanager
    async def factory():
        yield object()
    runtime=AsyncBrowserRuntime(factory)
    async def action(resource):
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            closing.set()
            while not release.is_set():
                await asyncio.sleep(.01)
            closed.set()
    call=runtime.submit(action)
    try:
        assert entered.wait(5)
        assert call.cancel()
        assert closing.wait(5)
        assert not call.cancel()  # Repeated cancellation cannot interrupt close.
        assert not call.done()
    finally:
        release.set()
        runtime.shutdown()
    assert closed.is_set()
    with pytest.raises(CancelledError):call.result()


def test_physical_start_failure_is_per_call_and_close_is_idempotent():
    @asynccontextmanager
    async def factory():
        raise OSError('Chromium launch failed')
        yield None
    runtime=AsyncBrowserRuntime(factory)
    async def action(resource):
        pytest.fail('startup failure invoked a browser action')
    calls=[runtime.submit(action) for _ in range(32)]
    try:
        for call in calls:
            with pytest.raises(OSError,match='Chromium launch failed'):call.result(5)
    finally:runtime.shutdown()
    runtime.shutdown()
    with pytest.raises(RuntimeError,match='shutting down'):runtime.submit(action)


def test_context_creation_cancel_closes_the_untransferred_result():
    from tofu_search.fetch._async_browser_runtime import open_browser_context
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    class Context:
        async def close(self): closed.set()
    class Browser:
        async def new_context(self, **kwargs):
            entered.set()
            while not release.is_set(): await asyncio.sleep(.01)
            return Context()
    @asynccontextmanager
    async def factory(): yield Browser()
    runtime=AsyncBrowserRuntime(factory)
    async def action(browser):
        context=await open_browser_context(browser)
        try: await asyncio.Event().wait()
        finally: await context.close()
    call=runtime.submit(action)
    try:
        assert entered.wait(5)
        call.cancel()
        assert not call.done() and not closed.is_set()
    finally:
        release.set()
        runtime.shutdown()
    assert closed.is_set()


def test_detached_callback_helper_remains_owned_until_physical_return():
    from tofu_search.fetch._async_browser_runtime import bind_browser_blocking
    entered, release, suppressed = threading.Event(), threading.Event(), threading.Event()
    children=[]
    @asynccontextmanager
    async def factory(): yield object()
    runtime=AsyncBrowserRuntime(factory)
    def blocked():
        entered.set()
        assert release.wait(10)
        return 'file bytes'
    async def action(browser):
        bound=bind_browser_blocking()
        async def callback():
            try: await bound(blocked)
            except asyncio.CancelledError: suppressed.set()
        children.append(asyncio.create_task(callback()))
        while not entered.is_set(): await asyncio.sleep(.01)
        return 'page completed'
    call=runtime.submit(action)
    try:
        assert entered.wait(5)
        deadline = time.monotonic() + 5
        while not call._closing and time.monotonic() < deadline:
            threading.Event().wait(.01)
        assert call._closing and not call.done()
    finally:
        release.set()
        assert call.result(5)=='page completed'
        runtime.shutdown()
    assert suppressed.is_set()
    assert runtime.snapshot()=={'active_calls':0,'owner_threads':0}


def test_context_close_failure_disposes_resource_before_call_completion():
    from tofu_search.fetch._async_browser_runtime import close_browser_context
    disposing, release, disposed = threading.Event(), threading.Event(), threading.Event()
    @asynccontextmanager
    async def factory():
        try: yield object()
        finally:
            disposing.set()
            while not release.is_set(): await asyncio.sleep(.01)
            disposed.set()
    class Context:
        async def close(self): raise OSError('context close failed')
    runtime=AsyncBrowserRuntime(factory)
    async def action(browser): await close_browser_context(Context())
    call=runtime.submit(action)
    try:
        assert disposing.wait(5)
        call.cancel()
        assert not call.done() and not disposed.is_set()
    finally:
        release.set()
        runtime.shutdown()
    assert disposed.is_set() and call.done()


def test_blocking_helper_cannot_join_its_own_runtime():
    @asynccontextmanager
    async def factory(): yield object()
    runtime=AsyncBrowserRuntime(factory)
    async def action(browser): return await runtime.run_blocking(runtime.shutdown)
    try:
        with pytest.raises(RuntimeError,match='cannot join'):
            runtime.submit(action).result(5)
    finally:runtime.shutdown()


def test_repeated_task_cancellation_cannot_abandon_context_creation():
    from tofu_search.fetch._async_browser_runtime import open_browser_context
    entered, release, closed = threading.Event(),threading.Event(),threading.Event()
    class Context:
        async def close(self): closed.set()
    class Browser:
        async def new_context(self,**kwargs):
            entered.set()
            while not release.is_set():await asyncio.sleep(.01)
            return Context()
    @asynccontextmanager
    async def factory():yield Browser()
    runtime=AsyncBrowserRuntime(factory)
    async def action(browser):await open_browser_context(browser)
    call=runtime.submit(action)
    try:
        assert entered.wait(5)
        call.cancel()
        runtime._loop.call_soon_threadsafe(call._task.cancel)
        assert not call.done()
    finally:
        release.set()
        runtime.shutdown()
    assert closed.is_set()
