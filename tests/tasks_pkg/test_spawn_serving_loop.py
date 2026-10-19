#!/usr/bin/env python3
"""tests/tasks_pkg/test_spawn_serving_loop.py — F3 (pt_1acd0bcdb2174566):
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

import lib.tasks_pkg.spawn as tp
from lib.agent_core.worker_executor import (
    RecoverableAgentExecutor,
)

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


def test_queue_progress_has_sparse_bounded_invalidation(monkeypatch):
    snapshots = iter([
        {
            'taskState': 'queued', 'queuePosition': 7, 'queued': 7,
            'active': 4, 'capacity': 4, 'queuedForSeconds': 1,
        },
        {
            # A tail arrival changes total depth only; it must not fan out a
            # durable phase write to every task already ahead of it.
            'taskState': 'queued', 'queuePosition': 7, 'queued': 8,
            'active': 4, 'capacity': 4, 'queuedForSeconds': 2,
        },
    ])
    monkeypatch.setattr(
        tp, 'agent_scheduling_snapshot', lambda _task_id: next(snapshots))

    assert tp._queued_phase_candidate(_fake_task()) \
        == tp._queued_phase_candidate(_fake_task())
    assert [tp._queue_wait_bucket(value) for value in (0, 19, 20, 59)] \
        == [0, 0, 1, 2]
    # After the first minute there is at most one elapsed-only invalidation per
    # minute: 63 total buckets across a full queued hour, not one per poll.
    assert len({tp._queue_wait_bucket(second) for second in range(3601)}) == 63


def test_hop_to_serving_loop_from_loop_less_thread(monkeypatch, clean_spawn_state):
    """The successor must land on the serving loop's agent executor."""
    ran = threading.Event()
    seen = {}

    def _fake_run_task(task):
        seen['thread_name'] = threading.current_thread().name
        seen['is_main'] = threading.current_thread() is threading.main_thread()
        ran.set()

    monkeypatch.setattr('lib.tasks_pkg.orchestrator.api.run_task', _fake_run_task)

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
        async def drain_owned_loop():
            pending = [task for task in asyncio.all_tasks()
                       if task is not asyncio.current_task()]
            if pending:
                await asyncio.wait_for(asyncio.gather(*pending), timeout=5)
        try:
            # Runner entry precedes completion of its async wrapper. Join
            # both before stopping this test's loop, or GC logs the orphan
            # during an unrelated later test.
            asyncio.run_coroutine_threadsafe(drain_owned_loop(), loop).result(timeout=6)
        finally:
            loop.call_soon_threadsafe(loop.stop)
            loop_thread.join(timeout=5)
            pool.shutdown(wait=True)
            loop.close()


def test_daemon_fallback_kept_without_serving_loop(monkeypatch, clean_spawn_state):
    """No loop anywhere → daemon-thread fallback preserved (documented)."""
    captured = {}

    class _FakeThread:
        def __init__(self, target=None, args=(), name=None, daemon=None):
            captured.update(target=target, args=args, name=name, daemon=daemon)

        def start(self):
            captured['started'] = True

    monkeypatch.setattr(tp.threading, 'Thread', _FakeThread)
    monkeypatch.setattr('lib.tasks_pkg.orchestrator.api.run_task', lambda t: None)
    tp.spawn_task(_fake_task())
    assert captured.get('started') is True
    assert captured.get('daemon') is True
    assert captured.get('name', '').startswith('run_task-')


def test_in_loop_path_uses_ensure_future(monkeypatch, clean_spawn_state):
    """Caller inside a running loop → existing ensure_future path, no thread."""
    ran = threading.Event()
    monkeypatch.setattr('lib.tasks_pkg.orchestrator.api.run_task',
                        lambda t: ran.set())

    async def _driver():
        tp.spawn_task(_fake_task())
        for _ in range(200):
            if ran.is_set():
                return
            await asyncio.sleep(0.01)

    asyncio.run(_driver())
    assert ran.is_set()


def test_task_stays_pending_until_a_physical_agent_worker_is_acquired(
        monkeypatch, clean_spawn_state):
    """A delayed physical worker handoff is not reported as running."""
    release_first = threading.Event()
    first_started = threading.Event()
    second_started = threading.Event()
    from concurrent.futures import ThreadPoolExecutor
    class ControlledHandoff(ThreadPoolExecutor):
        """Test-only physical handoff delay through the public executor port."""
        def __init__(self):
            super().__init__(max_workers=1,thread_name_prefix='semantic-agent')
            self.jobs={}
        def submit(self,fn,*args,**kwargs):
            future=super().submit(fn,*args,**kwargs)
            self.jobs[fn._tofu_task_id]=(future,time.monotonic())
            return future
        def scheduling_snapshot(self,task_id=None):
            if task_id not in self.jobs:return {'taskState':'unknown'}
            queued=[key for key,(future,at) in self.jobs.items() if not future.running() and not future.done()]
            active=sum(future.running() for future,at in self.jobs.values())
            future,at=self.jobs[task_id]
            return dict(taskState='queued' if task_id in queued else 'running' if future.running() else 'done',
                queuePosition=queued.index(task_id)+1 if task_id in queued else 0,
                queuedForSeconds=time.monotonic()-at,queued=len(queued),active=active,capacity=2)
    pool=ControlledHandoff()
    tp.set_agent_executor(pool)
    phases = []
    monkeypatch.setattr(
        'lib.agent_core.events.emit_phase',
        lambda task, phase, **fields: phases.append((task, phase, fields)),
    )
    first = {'id': 'task-semantic-first', 'convId': 'conv-a', 'status': 'pending'}
    second = {
        'id': 'task-semantic-second', 'convId': 'conv-b', 'status': 'pending',
        # A long queue wait must not count as post-entry worker silence.
        'created_at': time.time() - 10_000,
    }

    def _runner(task):
        if task['id'] == first['id']:
            first_started.set()
            release_first.wait(timeout=5)
        else:
            second_started.set()

    async def _driver():
        tp.spawn_task(first, runner=_runner)
        for _ in range(200):
            if first_started.is_set():
                break
            await asyncio.sleep(0.01)
        assert first_started.is_set()
        assert first['status'] == 'running'

        tp.spawn_task(second, runner=_runner)
        for _ in range(200):
            if pool.scheduling_snapshot(second['id'])['taskState'] == 'queued':
                break
            await asyncio.sleep(0.01)
        snapshot = pool.scheduling_snapshot(second['id'])
        assert snapshot['taskState'] == 'queued'
        assert snapshot['queuePosition'] == 1
        assert snapshot['queuedForSeconds'] >= 0
        assert second['status'] == 'pending'
        assert not second_started.is_set()
        for _ in range(200):
            if any(phase == 'executor_queued' for _, phase, _ in phases):
                break
            await asyncio.sleep(0.01)
        queued_phases = [
            fields for owner, phase, fields in phases
            if owner is second and phase == 'executor_queued'
        ]
        assert queued_phases
        assert queued_phases[-1]['detailKey'] \
            == 'stream.phase.executorQueuedWithMetrics'
        assert queued_phases[-1]['detailArgs'] == {
            'position': 1,
            'queued': 1,
            'active': 1,
            'capacity': 2,
            'waitSeconds': 0,
        }

        release_first.set()
        for _ in range(200):
            if second_started.is_set():
                break
            await asyncio.sleep(0.01)
        assert second_started.is_set()
        assert second['status'] == 'running'
        assert second['_workerStartedAt'] > second['created_at']
        assert second['_t_last_event'] == second['_workerStartedAt']
        assert second['_dispatch_heartbeat'] == second['_workerStartedAt']

    try:
        asyncio.run(_driver())
    finally:
        release_first.set()
        tp.set_agent_executor(None)
        pool.shutdown(wait=True, cancel_futures=True)


def test_pre_worker_rejection_uses_canonical_terminal_settlement(
        monkeypatch, clean_spawn_state):
    calls = []
    monkeypatch.setattr(
        'lib.tasks_pkg.manager.finalize_chat_task_error',
        lambda task, error, **kwargs: calls.append((task, error, kwargs)),
    )
    task = {
        'id': 'task-rejected', 'convId': 'conv-rejected',
        'status': 'pending', 'config': {'model': 'test-model'},
    }

    tp._finalize_rejected_submission(task, RuntimeError('queue full'))

    assert len(calls) == 1
    assert calls[0][0] is task
    assert calls[0][1]['kind'] == 'task_start_failed'
    assert calls[0][2] == {'flow_reason': 'executor_start_failed'}


def test_actual_thread_start_failure_retains_terminal_error(monkeypatch):
    calls = []
    monkeypatch.setattr(
        'lib.tasks_pkg.manager.finalize_chat_task_error',
        lambda task, error, **kwargs: calls.append((task, error, kwargs)))
    task = {'id': 'thread-failed', 'status': 'pending',
            'config': {'model': 'test-model'}}
    tp._finalize_rejected_submission(task, RuntimeError("can't start new thread"))
    assert len(calls) == 1
    assert calls[0][1]['kind'] == 'task_start_failed'
    assert calls[0][2]['flow_reason'] == 'executor_start_failed'


@pytest.mark.parametrize('route', ['serving-loop', 'owned-executor'])
def test_stop_before_worker_admission_settles_once_without_start_error(monkeypatch, caplog, route):
    from concurrent.futures import ThreadPoolExecutor
    from unittest.mock import Mock
    task = {'id': 'cancelled-before-entry', 'status': 'pending',
            'abort_event': threading.Event()}
    task['abort_event'].set()
    stopped, failed, runner, preserved = Mock(), Mock(), Mock(), Mock()
    monkeypatch.setattr('lib.tasks_pkg.manager.finalize_chat_task_aborted', stopped)
    monkeypatch.setattr('lib.tasks_pkg.manager.finalize_chat_task_error', failed)
    monkeypatch.setattr(tp, '_settle_unscheduled_workspace', preserved)
    if route == 'serving-loop':
        async def drive():
            await tp._executor_runner(asyncio.get_running_loop(), runner, task)()
        asyncio.run(drive())
    else:
        with ThreadPoolExecutor(max_workers=1) as executor:
            tp.spawn_task(task, runner=runner, executor=executor).result(timeout=5)
    stopped.assert_called_once_with(task)
    preserved.assert_called_once_with(task)
    runner.assert_not_called()
    failed.assert_not_called()
    assert not [record for record in caplog.records if record.levelname == 'ERROR']


@pytest.mark.parametrize('internal_fence', [False, True])
def test_admission_authority_rejection_is_not_user_cancellation(monkeypatch, internal_fence):
    from lib.llm.llm_errors import AbortedError
    from unittest.mock import Mock
    task = {'id': 'rejected-authority', 'status': 'pending'}
    if internal_fence:
        task.update(aborted=True, _abort_reason='storage_authority_integrity')
    failure = AbortedError('authority rejected admission')
    monkeypatch.setattr(tp, '_mark_worker_started', Mock(side_effect=failure))
    stopped = Mock()
    monkeypatch.setattr('lib.tasks_pkg.manager.finalize_chat_task_aborted', stopped)
    with pytest.raises(AbortedError) as caught:
        tp._owned_task_callable(Mock(), task, threading.Event(), threading.Lock())()
    assert caught.value is failure
    stopped.assert_not_called()


@pytest.mark.parametrize('native,pending',[(True,False),(True,True),(False,False)])
def test_worker_preserves_acknowledged_native_stop_without_terminal_rewrite(monkeypatch,native,pending):
    from lib.tasks_pkg import spawn
    from unittest.mock import Mock
    task={'id':'stopped','status':'aborted','_terminalPersistencePending':pending}
    if native:task['_nativePreparedInvocation']=object()
    monkeypatch.setattr(spawn,'_mark_worker_started',lambda *args:True)
    preserve=Mock();failed=Mock()
    monkeypatch.setattr(spawn,'_settle_unscheduled_workspace',preserve)
    monkeypatch.setattr('lib.tasks_pkg.manager.finalize_chat_task_error',failed)
    failure=asyncio.CancelledError()
    def run(task):
        # Durable terminal release may remove application ports before Stop
        # propagates back through the worker's existing failure boundary.
        task['_nativePreparedInvocation']=None
        raise failure
    owned=spawn._owned_task_callable(run,task,threading.Event(),threading.Lock())
    with pytest.raises(asyncio.CancelledError) as caught:owned()
    assert caught.value is failure
    assert failed.call_count==(0 if native and not pending else 1)
    preserve.assert_called_once_with(task)
