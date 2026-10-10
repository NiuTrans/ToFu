"""Pool Stop observes silent workers and drains resource owners before settlement."""
import threading
from concurrent.futures import Future

import pytest

from tests.tools.test_tool_settle_immediacy import _mk_task, _mk_tc, _run_pipeline
from tests.tools.test_tool_settle_immediacy import rec as recorder_fixture
from lib.tasks_pkg.tool_runtime import context_for_task, unregister_context
from lib.tasks_pkg.tool_dispatch import _pipeline

pytestmark = pytest.mark.unit

# Register the imported fixture explicitly without shadowing its Python name.
rec = recorder_fixture


@pytest.mark.parametrize('finish_first', [False, True])
def test_stop_cancels_silent_parallel_resources_before_terminal(monkeypatch, rec, finish_first):
    task = _mk_task()
    items = [_mk_tc('one', 'get_conversation', 1), _mk_tc('two', 'grep_search', 2)]
    started = [threading.Event(), threading.Event()]
    releases = [threading.Event(), threading.Event()]
    exited = [threading.Event(), threading.Event()]
    errors = []

    def execute(task, tc, name, call_id, args, rn, row, *rest, **kwargs):
        i = rn - 1
        context = context_for_task(task, round_num=rn, tool_call_id=call_id,
                                   tool_name=name, round_entry=row)
        context.register_cancel_callback(releases[i].set)
        started[i].set()
        try:
            assert releases[i].wait(5), 'test cleanup failed to release worker'
            # A real handler may still write state after its resource closes.
            # This must happen BEFORE the pipeline publishes terminal receipts.
            row['lastWorkerWrite'] = 'released'
            from lib.tasks_pkg.executor._finalize import _finalize_tool_round
            _finalize_tool_round(task, rn, row, [])
            context.settle_once('cancelled' if task['aborted'] else 'done')
            return call_id, 'worker result', False
        finally:
            unregister_context(context)
            exited[i].set()

    monkeypatch.setattr(_pipeline, '_execute_tool_one_in_pool', execute)
    monkeypatch.setattr(_pipeline, '_parallel_idle_reuse_hint', lambda *a, **k: 2)

    def run():
        try:
            _run_pipeline(task, items)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert all(event.wait(2) for event in started)
        if finish_first:
            releases[0].set()
            assert exited[0].wait(2)
        task['aborted'] = True
        thread.join(2)
        assert not thread.is_alive(), 'silent pool did not observe Stop/release resources'
        assert not errors
        assert all(event.is_set() for event in exited)
        assert not task.get('_activeToolContexts')
        for item in items:
            event = rec.find(item[2], 'tool_complete')
            assert event is not None
            assert event.get('status', item[5]['status']) in (
                {'done', 'aborted'} if finish_first else {'aborted'})
            assert 'Unknown tool' not in str(event)
        assert items[1][5]['status'] == 'aborted'
    finally:
        for release in releases:
            release.set()
        thread.join(5)
        assert not thread.is_alive()


def test_abort_before_harvesting_done_future_preserves_receipt():
    future = Future()
    future.set_result(('one', 'late result', False))
    assert list(_pipeline._completed_tools_until_cancel(
        {future}, {'aborted': True}, 0)) == [future, None]


def test_done_but_uncollected_stop_keeps_real_pipeline_receipt(monkeypatch, rec):
    task = _mk_task()
    item = _mk_tc('done', 'get_conversation', 1)
    original = _pipeline._completed_tools_until_cancel

    def stop_before_collection(futures, task, deadline):
        for future in futures:
            future.result(timeout=2)
        task['aborted'] = True
        yield from original(futures, task, deadline)

    monkeypatch.setattr(_pipeline, '_completed_tools_until_cancel', stop_before_collection)
    def execute(task, tc, name, call_id, args, rn, row, *rest, **kwargs):
        from lib.tasks_pkg.executor._finalize import _finalize_tool_round
        _finalize_tool_round(task, rn, row, [])
        return call_id, 'real completed receipt', False

    monkeypatch.setattr(_pipeline, '_execute_tool_one_in_pool', execute)
    messages = _run_pipeline(task, [item])
    assert item[5]['status'] == 'done'
    assert item[5]['toolContent'] == 'real completed receipt'
    assert messages[-1]['content'] == 'real completed receipt'
    assert rec.find('done', 'tool_result')['status'] == 'done'
    assert rec.find('done', 'tool_complete')['toolContent'] == 'real completed receipt'
    assert rec.count('done', 'tool_complete') == 1


@pytest.mark.parametrize('blocking_callback', [False, True])
def test_timeout_returns_without_draining_noncooperative_owner(monkeypatch, rec, blocking_callback):
    task = _mk_task()
    item = _mk_tc('timeout', 'get_conversation', 1)
    started = threading.Event()
    release = threading.Event()
    exited = threading.Event()
    callback_called = threading.Event()
    errors = []

    def callback():
        callback_called.set()
        release.wait(5)

    def execute(task, tc, name, call_id, args, rn, row, *rest, **kwargs):
        context = context_for_task(task, round_num=rn, tool_call_id=call_id,
                                   tool_name=name, round_entry=row)
        if blocking_callback:
            context.register_cancel_callback(callback)
        started.set()
        try:
            release.wait(5)
            # Honest legacy boundary: a timed-out thread is still able to write.
            row['lateWrite'] = True
            return call_id, 'too late', False
        finally:
            unregister_context(context)
            exited.set()

    monkeypatch.setattr(_pipeline, '_execute_tool_one_in_pool', execute)
    monkeypatch.setenv('TOOL_PARALLEL_TIMEOUT', '1')

    def run():
        try:
            _run_pipeline(task, [item])
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert started.wait(2)
        thread.join(2)
        assert not thread.is_alive(), 'timeout must not drain arbitrary threads/callbacks'
        assert not errors
        assert not exited.is_set()
        assert not callback_called.is_set()
        assert task.get('_activeToolContexts'), 'timeout does not kill the resource owner'
        assert item[5]['status'] == 'error'
        assert 'timed out' in item[5]['toolContent']
        assert rec.count('timeout', 'tool_complete') == 1
    finally:
        release.set()
        thread.join(5)
        assert exited.wait(2)
    assert item[5]['lateWrite'] is True


def test_noncooperative_worker_retains_ownership_until_cleanup(monkeypatch, rec):
    task = _mk_task()
    item = _mk_tc('owned', 'get_conversation', 1)
    started = threading.Event()
    release = threading.Event()
    cancellation_observed = threading.Event()
    errors = []

    def execute(task, tc, name, call_id, args, rn, row, *rest, **kwargs):
        context = context_for_task(task, round_num=rn, tool_call_id=call_id,
                                   tool_name=name, round_entry=row)
        context.register_cancel_callback(cancellation_observed.set)
        started.set()
        try:
            release.wait(5)
            row['lastWorkerWrite'] = 'before terminal'
            return call_id, 'late result', False
        finally:
            context.settle_once('cancelled')
            unregister_context(context)

    monkeypatch.setattr(_pipeline, '_execute_tool_one_in_pool', execute)

    def run():
        try:
            _run_pipeline(task, [item])
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert started.wait(2)
        task['aborted'] = True
        assert cancellation_observed.wait(2)
        assert thread.is_alive()
        assert rec.find('owned', 'tool_complete') is None
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert not errors
    assert item[5]['lastWorkerWrite'] == 'before terminal'
    assert item[5]['status'] == 'aborted'
    assert rec.count('owned', 'tool_complete') == 1


def test_sixty_four_parallel_tools_enter_one_idle_hint_and_settle_once(monkeypatch, rec):
    import time
    task=_mk_task();gate=threading.Barrier(64);calls=[]
    items=[_mk_tc(f'call-{i}','get_conversation',i+1) for i in range(64)]
    def execute(task,tc,name,call_id,args,rn,row,*rest,**kwargs):
        calls.append(call_id)
        gate.wait(timeout=5)
        return call_id,'completed',False
    monkeypatch.setattr(_pipeline,'_execute_tool_one_in_pool',execute)
    monkeypatch.setattr(_pipeline,'_parallel_idle_reuse_hint',lambda *a,**k:1)
    began=time.monotonic();messages=_run_pipeline(task,items)
    assert len(calls)==len(set(calls))==64
    replies=[row for row in messages if row.get('role')=='tool']
    assert len(replies)==64 and len({row['tool_call_id'] for row in replies})==64
    assert not [t for t in threading.enumerate() if t.name.startswith('tool-parallel_')]
    print(f'tool-demand calls=64 idle_hint=1 replies=64 settled_ms={(time.monotonic()-began)*1000:.2f} retained_threads=0')


def test_unstarted_parallel_tool_failure_uses_the_normal_settlement_lane(monkeypatch, rec):
    task=_mk_task();ran=[];original=threading.Thread.start
    def start(thread):
        if thread.name.startswith('tool-parallel_'):
            raise RuntimeError('injected physical thread failure')
        return original(thread)
    monkeypatch.setattr(threading.Thread,'start',start)
    monkeypatch.setattr(_pipeline,'_execute_tool_one_in_pool',lambda *a,**k:ran.append(1))
    messages=_run_pipeline(task,[_mk_tc('unstarted','get_conversation',1)])
    assert ran==[]
    replies=[row for row in messages if row.get('role')=='tool']
    assert len(replies)==1
    assert 'injected physical thread failure' in str(replies[0])
