"""Tests for the async-native long-poll wait (#2).

The browser/desktop poll routes were converted to ``async def`` so the
Hypercorn worker thread is RELEASED during the up-to-8s wait instead of
blocking on a ``threading.Event``. The wait is an asyncio.Event woken
cross-thread from the SYNC enqueue path via ``loop.call_soon_threadsafe``.

These tests assert, WITHOUT a real OS-thread block or a running server:
  * the route handlers are coroutine functions;
  * an enqueued command wakes an awaiting ``wait_for_commands_async``
    (cross-thread, since the enqueue runs on a worker thread);
  * per-client routing + the §3 TTL cutoff still hold on the async path;
  * the async-waiter registry never leaks (timeout, success, cancellation).
"""

import asyncio
import threading
import time

import pytest

import lib.browser.queue as q
from lib.desktop import bridge as db


BROWSER_OWNER = '101'
BROWSER_PROTOCOL = 2


def _browser_command(command_id, *, client_id='dev1', owner_user_id=BROWSER_OWNER,
                     created_at=None, timeout=30):
    """Build one command using the queue's owner/device invariant."""
    return {
        'id': command_id, 'type': 'list_tabs', 'params': {},
        'event': threading.Event(), 'result': None, 'error': None,
        'created_at': time.time() if created_at is None else created_at,
        'picked_up': False, 'target_client': client_id,
        'claimed_client_id': '', 'claimed_owner_user_id': '',
        'owner_user_id': owner_user_id,
        'timeout': timeout, 'cancelled': False,
    }


def _register_browser(client_id='dev1', owner_user_id=BROWSER_OWNER):
    q.mark_poll(
        client_id,
        owner_user_id=owner_user_id,
        protocol_version=BROWSER_PROTOCOL,
        capabilities=[],
    )


def _run_async(coro):
    """Drive a coroutine on a private loop (repo has no pytest-asyncio)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture(autouse=True)
def _clean_state():
    with q._commands_lock:
        q._commands.clear()
    with q._async_waiters_lock:
        q._async_waiters.clear()
    with db.command_queue_lock:
        db.command_queue.clear()
        db._agents.clear()
    with db._async_waiters_lock:
        db._async_waiters.clear()
    yield
    with q._commands_lock:
        q._commands.clear()
    with q._async_waiters_lock:
        q._async_waiters.clear()
    with db.command_queue_lock:
        db.command_queue.clear()
        db._agents.clear()
    with db._async_waiters_lock:
        db._async_waiters.clear()


@pytest.mark.unit
class TestCoroutineHandlers:
    def test_browser_queue_async_is_coroutine(self):
        assert asyncio.iscoroutinefunction(q.wait_for_commands_async)
        # The sync variant must remain sync for non-async callers.
        assert not asyncio.iscoroutinefunction(q.wait_for_commands)

    def test_desktop_bridge_async_is_coroutine(self):
        assert asyncio.iscoroutinefunction(db.take_pending_commands_async)


@pytest.mark.unit
class TestBrowserAsyncWait:
    def test_fast_path_returns_already_queued(self):
        # A command already waiting is returned without registering a waiter.
        cmd = _browser_command('c1')
        with q._commands_lock:
            q._commands['c1'] = cmd

        async def go():
            return await q.wait_for_commands_async(
                timeout=2, client_id='dev1', owner_user_id=BROWSER_OWNER)
        out = _run_async(go())
        assert [c['id'] for c in out] == ['c1']
        # No waiter should linger.
        with q._async_waiters_lock:
            assert q._async_waiters == []

    def test_enqueue_wakes_awaiter_cross_thread(self):
        """The core guarantee: a command queued from a WORKER THREAD while the
        coroutine is awaiting wakes it via call_soon_threadsafe."""
        _register_browser()

        delivered = {}

        async def waiter():
            t0 = time.monotonic()
            cmds = await q.wait_for_commands_async(
                timeout=5, client_id='dev1', owner_user_id=BROWSER_OWNER)
            delivered['cmds'] = cmds
            delivered['elapsed'] = time.monotonic() - t0

        def enqueue_from_thread():
            # Runs on a separate (sync tool) thread, like a real tool call.
            time.sleep(0.3)
            # send_browser_command blocks until the result resolves; we don't
            # care about its result here, just that it ENQUEUES + wakes us.
            q.send_browser_command(
                'list_tabs', timeout=1, client_id='dev1',
                owner_user_id=BROWSER_OWNER)

        def go():
            loop = asyncio.new_event_loop()
            try:
                t = threading.Thread(target=enqueue_from_thread)
                wait_task = loop.create_task(waiter())
                loop.call_soon(t.start)
                loop.run_until_complete(wait_task)
                t.join(timeout=3)
            finally:
                loop.close()

        go()
        assert delivered.get('cmds'), 'awaiter was not woken by cross-thread enqueue'
        assert delivered['cmds'][0]['type'] == 'list_tabs'
        # Must have woken promptly (~0.3s), NOT waited the full 5s timeout.
        assert delivered['elapsed'] < 2.0, f"woke too late: {delivered['elapsed']:.2f}s"
        # Registry cleaned up after return.
        with q._async_waiters_lock:
            assert q._async_waiters == []

    def test_timeout_returns_empty_and_cleans_registry(self):
        async def go():
            return await q.wait_for_commands_async(
                timeout=0.3, client_id='nobody',
                owner_user_id=BROWSER_OWNER)
        out = _run_async(go())
        assert out == []
        with q._async_waiters_lock:
            assert q._async_waiters == []

    def test_per_client_routing_on_async_path(self):
        # A command targeted at dev-A must NOT be delivered to dev-B's poll.
        cmd = _browser_command('cA', client_id='devA')
        with q._commands_lock:
            q._commands['cA'] = cmd

        async def go():
            return await q.wait_for_commands_async(
                timeout=0.4, client_id='devB', owner_user_id=BROWSER_OWNER)
        out = _run_async(go())
        assert out == []  # not for devB
        # And the command is still pending (not consumed by the wrong client).
        with q._commands_lock:
            assert 'cA' in q._commands and not q._commands['cA']['picked_up']

    def test_ttl_cutoff_on_async_path(self):
        # Command older than its caller timeout must not be delivered.
        cmd = _browser_command(
            'old', created_at=time.time() - 31, timeout=30)
        with q._commands_lock:
            q._commands['old'] = cmd

        async def go():
            return await q.wait_for_commands_async(
                timeout=0.3, client_id='dev1',
                owner_user_id=BROWSER_OWNER)
        assert _run_async(go()) == []

    def test_cancellation_cleans_registry(self):
        """A cancelled await (client disconnect) must deregister its waiter."""
        async def go():
            task = asyncio.ensure_future(
                q.wait_for_commands_async(
                    timeout=10, client_id='disc',
                    owner_user_id=BROWSER_OWNER))
            await asyncio.sleep(0.2)  # let it register + start awaiting
            with q._async_waiters_lock:
                assert len(q._async_waiters) == 1  # registered
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            with q._async_waiters_lock:
                assert q._async_waiters == []  # cleaned up on cancel
        _run_async(go())


@pytest.mark.unit
class TestDesktopAsyncWait:
    def test_enqueue_wakes_awaiter_cross_thread(self):
        delivered = {}
        db.register_agent('agent-A', {'name': 'test'}, user_id='owner')

        async def waiter():
            t0 = time.monotonic()
            cmds = await db.take_pending_commands_async(
                timeout=5, agent_id='agent-A', user_id='owner')
            delivered['cmds'] = cmds
            delivered['elapsed'] = time.monotonic() - t0

        def enqueue_from_thread():
            time.sleep(0.3)
            db.send_desktop_command(
                'desktop_list_files', {'path': '~'}, timeout=1,
                user_id='owner')

        def go():
            loop = asyncio.new_event_loop()
            try:
                t = threading.Thread(target=enqueue_from_thread)
                wait_task = loop.create_task(waiter())
                loop.call_soon(t.start)
                loop.run_until_complete(wait_task)
                t.join(timeout=3)
            finally:
                loop.close()

        go()
        assert delivered.get('cmds'), 'desktop awaiter not woken cross-thread'
        assert delivered['cmds'][0]['type'] == 'desktop_list_files'
        assert delivered['elapsed'] < 2.0
        with db._async_waiters_lock:
            assert db._async_waiters == []

    def test_timeout_cleans_registry(self):
        db.register_agent('agent-A', {'name': 'test'}, user_id='owner')

        async def go():
            return await db.take_pending_commands_async(
                timeout=0.3, agent_id='agent-A', user_id='owner')
        assert _run_async(go()) == []
        with db._async_waiters_lock:
            assert db._async_waiters == []
