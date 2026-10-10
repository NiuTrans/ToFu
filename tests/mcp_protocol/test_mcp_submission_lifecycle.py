"""MCP cross-thread submission and shutdown-wait ownership boundaries."""
import asyncio
from concurrent.futures import TimeoutError
import threading

import pytest

pytestmark = pytest.mark.unit


async def noop():
    return 1


@pytest.mark.parametrize('method', ['_run_async', '_run_async_with_timeout'])
def test_rejected_loop_submission_closes_unsubmitted_coroutine(monkeypatch, method):
    import lib.mcp.client as client
    bridge = client.MCPBridge(owner_user_id=1)
    monkeypatch.setattr(bridge, '_ensure_loop', lambda **kwargs: object())
    def reject(*args):
        raise RuntimeError('event loop is closed')
    monkeypatch.setattr(asyncio, 'run_coroutine_threadsafe', reject)
    coroutine = noop()
    try:
        with pytest.raises(RuntimeError, match='closed'):
            getattr(bridge, method)(coroutine, timeout=.1)
        assert coroutine.cr_frame is None
    finally:
        coroutine.close()


def test_shutdown_wait_startup_failure_closes_unsubmitted_coroutine(monkeypatch):
    import lib.mcp.client as client
    bridge = client.MCPBridge(owner_user_id=1)
    def reject(**kwargs):
        raise RuntimeError('startup refused')
    monkeypatch.setattr(bridge, '_ensure_loop', reject)
    coroutine = noop()
    try:
        with pytest.raises(RuntimeError, match='startup refused'):
            bridge._run_async_with_timeout(coroutine, .1)
        assert coroutine.cr_frame is None
    finally:
        coroutine.close()


def test_shutdown_wait_timeout_cancels_waiter_without_cancelling_owner_completion():
    import lib.mcp.client as client
    from types import SimpleNamespace
    bridge = client.MCPBridge(owner_user_id=1)
    loop = bridge._ensure_loop()
    observed = {}
    cancelled = threading.Event()
    async def prepare():
        observed['closed'] = asyncio.get_running_loop().create_future()
    asyncio.run_coroutine_threadsafe(prepare(), loop).result(1)
    handle = SimpleNamespace(_shutdown_event=None, _closed_future=observed['closed'])
    async def wait():
        try:
            await bridge._async_signal_shutdown(handle)
        finally:
            cancelled.set()
    try:
        with pytest.raises(TimeoutError):
            bridge._run_async_with_timeout(wait(), .02)
        assert cancelled.wait(1), 'timed-out waiter remained resident'
        async def inspect():
            return observed['closed'].cancelled()
        assert asyncio.run_coroutine_threadsafe(inspect(), loop).result(1) is False
    finally:
        async def cleanup():
            if not observed['closed'].done():
                observed['closed'].set_result(None)
        asyncio.run_coroutine_threadsafe(cleanup(), loop).result(1)
        bridge.disconnect_all()
        # Close the captured loop only after its owning thread has stopped.
        if not loop.is_running() and not loop.is_closed():
            loop.close()
