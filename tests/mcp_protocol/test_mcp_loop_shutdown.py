"""Owner loop shutdown must dispose resources before returning admission."""
import asyncio
import threading

import pytest

pytestmark = pytest.mark.unit


def test_disconnect_closes_loop_and_drains_pending_work():
    from lib.mcp.client import MCPBridge
    bridge = MCPBridge(owner_user_id=1)
    loop = bridge._ensure_loop()
    thread = bridge._loop_thread
    entered = threading.Event()
    disposed = threading.Event()
    async def pending():
        entered.set()
        try:
            await asyncio.Future()
        finally:
            disposed.set()
    future = asyncio.run_coroutine_threadsafe(pending(), loop)
    assert entered.wait(1)
    try:
        bridge.disconnect_all()
        assert disposed.wait(1)
        assert not thread.is_alive()
        assert loop.is_closed()
        assert not bridge._registry_in_use
        assert future.done()
    finally:
        if not loop.is_closed():
            # Repair the old implementation's stopped loop for red-run cleanup.
            for task in asyncio.all_tasks(loop):
                task.cancel()
            loop.run_until_complete(asyncio.gather(*asyncio.all_tasks(loop), return_exceptions=True))
            loop.close()


def test_stopped_bridge_keeps_owner_identity_without_blocking_other_owners(monkeypatch):
    import lib.mcp.client as client
    monkeypatch.setattr(client, '_bridges', {})
    bridge = client.get_bridge(1)
    loop = bridge._ensure_loop()
    try:
        bridge.disconnect_all()
        assert client.get_bridge(2)._owner_user_id == 2
        assert client.get_bridge(1) is bridge and not bridge._registry_in_use
    finally:
        client.disconnect_all_bridges()
        if not loop.is_running() and not loop.is_closed():
            loop.close()


def test_slow_cleanup_retains_generation_without_blocking_other_owners(monkeypatch):
    import lib.mcp.client as client
    monkeypatch.setattr(client, '_bridges', {})
    bridge = client.get_bridge(1)
    loop = bridge._ensure_loop()
    thread = bridge._loop_thread
    join = thread.join
    monkeypatch.setattr(thread, 'join', lambda timeout=None: join(.02))
    entered, cancelling = threading.Event(), threading.Event()
    gate = asyncio.run_coroutine_threadsafe(_make_event(), loop).result(1)
    async def pending():
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cancelling.set()
            await gate.wait()
    future = asyncio.run_coroutine_threadsafe(pending(), loop)
    assert entered.wait(1)
    try:
        client.disconnect_all_bridges()
        assert cancelling.wait(1)
        assert thread.is_alive() and bridge._loop is loop
        assert bridge._registry_in_use
        assert client.get_bridge(2)._owner_user_id == 2
        with pytest.raises(RuntimeError, match='still stopping'):
            client.get_bridge(1)
        coroutine = _make_event()
        with pytest.raises(RuntimeError, match='retired|stopping'):
            bridge._run_async(coroutine, timeout=.1)
        assert coroutine.cr_frame is None
        client.disconnect_all_bridges()  # Must not stop the draining loop again.
        assert thread.is_alive() and not loop.is_closed()
    finally:
        loop.call_soon_threadsafe(gate.set)
        join(2)
    assert not thread.is_alive() and loop.is_closed()
    assert future.done()
    assert client.get_bridge(2)._owner_user_id == 2
    client.disconnect_all_bridges()


async def _make_event():
    return asyncio.Event()


def test_connection_cannot_publish_after_shutdown(monkeypatch):
    import lib.mcp.client as client
    bridge = client.MCPBridge(owner_user_id=1)
    monkeypatch.setattr(client, 'reconcile_for_connect', lambda: 0)
    def handshake(coroutine, **kwargs):
        coroutine.close()
        bridge.disconnect_all()
        return object(), []
    monkeypatch.setattr(bridge, '_run_async', handshake)
    with pytest.raises(RuntimeError, match='completed during shutdown'):
        bridge.connect_server('late', {'transport': 'streamable-http', 'url': 'http://127.0.0.1:1'})
    assert bridge._servers == {}
    assert bridge._configs == {}
    assert not bridge._started


def test_shutdown_failure_still_stops_owned_loop(monkeypatch):
    from lib.mcp.client import MCPBridge
    bridge = MCPBridge(owner_user_id=1)
    loop = bridge._ensure_loop()
    thread = bridge._loop_thread
    def fail():
        raise RuntimeError('injected cleanup failure')
    monkeypatch.setattr(bridge, '_disconnect_all_owned', fail)
    with pytest.raises(RuntimeError, match='injected cleanup failure'):
        bridge.disconnect_all()
    assert not thread.is_alive()
    assert loop.is_closed()
    assert not bridge._registry_in_use


def test_submission_and_shutdown_share_admission_boundary(monkeypatch):
    from concurrent.futures import CancelledError, ThreadPoolExecutor
    from lib.mcp.client import MCPBridge
    bridge = MCPBridge(owner_user_id=1)
    loop = bridge._ensure_loop()
    entered, release, closing, closed = (threading.Event() for _ in range(4))
    submit = asyncio.run_coroutine_threadsafe
    def paused_submit(coroutine, target_loop):
        entered.set()
        assert release.wait(2)
        return submit(coroutine, target_loop)
    monkeypatch.setattr(asyncio, 'run_coroutine_threadsafe', paused_submit)
    async def request():
        await asyncio.sleep(0)
        return 'done'
    coroutine = request()
    def close():
        closing.set()
        bridge.disconnect_all()
        closed.set()
    with ThreadPoolExecutor(max_workers=2) as pool:
        request_future = pool.submit(bridge._run_async, coroutine, 2)
        assert entered.wait(1)
        close_future = pool.submit(close)
        try:
            assert closing.wait(1)
            assert not closed.wait(.05), 'shutdown overtook an admitted submission'
        finally:
            release.set()
        close_future.result(2)
        # Admitted work settles exactly one of two ways at the shutdown
        # boundary: it runs to 'done', or it is cancelled. Fold cancellation
        # into a value so the assertion itself can never be laundered.
        try:
            outcome = request_future.result(2)
        except CancelledError:
            outcome = 'cancelled-by-shutdown'
        assert outcome in ('done', 'cancelled-by-shutdown')
    assert coroutine.cr_frame is None
    assert loop.is_closed()


def test_stale_handshake_submission_cannot_start_a_new_loop():
    from lib.mcp.client import MCPBridge
    bridge = MCPBridge(owner_user_id=1)
    generation = bridge._loop_generation
    bridge.disconnect_all()
    coroutine = _make_event()
    with pytest.raises(RuntimeError, match='shutdown boundary'):
        bridge._run_async(coroutine, lifecycle_generation=generation)
    assert coroutine.cr_frame is None
    assert bridge._loop is None
    assert not bridge._registry_in_use
