#!/usr/bin/env python3
"""tests/test_spawn_serving_loop.py — F3 (pt_1acd0bcdb2174566):
spawn_task hops onto the registered serving loop instead of silently
degrading to a daemon thread.

Pre-F3 disease: queue-dispatch / reaper callbacks run on a finishing task's
WORKER thread, where ``asyncio.get_running_loop()`` fails — so every
queue-dispatched successor took ``threading.Thread(daemon=True)``:
uncapped (bypasses _agent_executor), loop-invisible, and killed mid-finally
at interpreter exit (no terminal floor → poll 404).

Faces:
  1. Loop-less caller + serving loop registered → run_task executes on the
     AGENT EXECUTOR of the serving loop (thread name proves the pool), not
     on a daemon thread. ★ failing-first: pre-F3 the thread is named
     ``run_task-…`` (daemon branch) and this assertion is red.
  2. No loop anywhere → the daemon-thread fallback is preserved verbatim
     (tests / Feishu bot / CLI contract).
  3. Caller already inside a running loop → unchanged ensure_future path.
"""

import asyncio
import threading
import time

import pytest

import lib.tasks_pkg as tp

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def clean_spawn_state():
    # Reset BEFORE too: set_serving_loop / set_agent_executor are process-global
    # singletons. Under xdist a FOREIGN file co-scheduled in this worker can set
    # them and never clean up — this file then inherits a poisoned loop/executor
    # (the parallel-only failure; 3/3 pass at -n 8 alone). autouse so no test in
    # this file can forget the guard.
    tp.set_serving_loop(None)
    tp.set_agent_executor(None)
    yield
    tp.set_serving_loop(None)
    tp.set_agent_executor(None)


def _fake_task():
    return {'id': 'task-f3-test', 'convId': 'conv-f3'}


def test_hop_to_serving_loop_from_loop_less_thread(monkeypatch, clean_spawn_state):
    """The successor must land on the serving loop's agent executor."""
    ran = threading.Event()
    seen = {}

    def _fake_run_task(task):
        seen['thread_name'] = threading.current_thread().name
        seen['is_main'] = threading.current_thread() is threading.main_thread()
        ran.set()

    monkeypatch.setattr('lib.tasks_pkg.orchestrator.run_task', _fake_run_task)

    from concurrent.futures import ThreadPoolExecutor
    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='f3-agent')
    tp.set_agent_executor(pool)

    loop = asyncio.new_event_loop()
    loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
    loop_thread.start()
    # run_forever races the assertions — wait for the loop to be live.
    for _ in range(200):
        if loop.is_running():
            break
        time.sleep(0.01)
    try:
        tp.set_serving_loop(loop)
        tp.spawn_task(_fake_task())  # called from the loop-less MAIN thread
        assert ran.wait(timeout=10), 'run_task never executed via the serving loop'
        # The executor pool's thread — NOT the daemon 'run_task-…' thread and
        # NOT the calling thread.
        assert seen['thread_name'].startswith('f3-agent'), seen
        assert seen['is_main'] is False
    finally:
        tp.set_serving_loop(None)
        tp.set_agent_executor(None)
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=5)
        pool.shutdown(wait=True)


def test_daemon_fallback_kept_without_serving_loop(monkeypatch, clean_spawn_state):
    """No loop anywhere → daemon-thread fallback preserved (documented)."""
    captured = {}

    class _FakeThread:
        def __init__(self, target=None, args=(), name=None, daemon=None):
            captured.update(target=target, args=args, name=name, daemon=daemon)

        def start(self):
            captured['started'] = True

    monkeypatch.setattr(tp.threading, 'Thread', _FakeThread)
    monkeypatch.setattr('lib.tasks_pkg.orchestrator.run_task', lambda t: None)
    tp.spawn_task(_fake_task())
    assert captured.get('started') is True
    assert captured.get('daemon') is True
    assert captured.get('name', '').startswith('run_task-')


def test_in_loop_path_uses_ensure_future(monkeypatch, clean_spawn_state):
    """Caller inside a running loop → existing ensure_future path, no thread."""
    ran = threading.Event()
    monkeypatch.setattr('lib.tasks_pkg.orchestrator.run_task',
                        lambda t: ran.set())

    async def _driver():
        tp.spawn_task(_fake_task())
        for _ in range(200):
            if ran.is_set():
                return
            await asyncio.sleep(0.01)

    asyncio.run(_driver())
    assert ran.is_set()
