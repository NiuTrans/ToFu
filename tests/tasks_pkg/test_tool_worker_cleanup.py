"""Failed dispatch must release its pool and join accepted work before returning."""
from __future__ import annotations

import threading

import pytest

from lib.tasks_pkg.tool_dispatch import _pipeline
from lib.tasks_pkg.tool_display import _build_tool_round_entry
from lib.tools.contracts import ToolContractV2
from tests.tasks_pkg.test_tool_outcome_finality import (
    dispatch, observations as observations, task_for,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def pools(monkeypatch):
    created = []
    factory = _pipeline.InstrumentedThreadPoolExecutor

    def tracked(*args, **kwargs):
        pool = factory(*args, **kwargs)
        created.append(pool)
        return pool

    monkeypatch.setattr(_pipeline, 'InstrumentedThreadPoolExecutor', tracked)
    yield created
    for pool in created:
        pool.shutdown(wait=True, cancel_futures=True)


def test_heartbeat_start_failure_closes_the_acquired_pool(pools, observations, monkeypatch):
    error = RuntimeError('heartbeat startup failed')

    def fail_heartbeat(*args):
        raise error

    monkeypatch.setattr(_pipeline, '_start_tool_heartbeat', fail_heartbeat)
    with pytest.raises(RuntimeError) as caught:
        dispatch(task_for(lambda *args: pytest.fail('handler must not run')))
    assert caught.value is error
    assert len(pools) == 1
    with pytest.raises(RuntimeError):
        pools[0].submit(lambda: None)


def test_host_refusing_the_heartbeat_thread_still_runs_the_round(
    pools, observations, monkeypatch,
):
    # 2026-11-03: a container pids limit refused only the keepalive ticker;
    # the refusal must not turn into a task-fatal error for the whole turn.
    real_start = threading.Thread.start

    def refuse_ticker(thread):
        if thread.name.startswith('tool-hb-'):
            raise RuntimeError("can't start new thread")
        return real_start(thread)

    monkeypatch.setattr(threading.Thread, 'start', refuse_ticker)
    row, messages, _settled = dispatch(task_for(
        lambda task, tc, name, call_id, *rest: (call_id, 'completed observation', False)))

    assert row['status'] == 'done'
    assert 'completed observation' in messages[0]['content']
    assert len(pools) == 1
    with pytest.raises(RuntimeError):
        pools[0].submit(lambda: None)


def test_fatal_effect_error_cancels_and_joins_its_concurrent_sibling(
    pools, observations, monkeypatch,
):
    entered, release, exited = threading.Event(), threading.Event(), threading.Event()

    class FatalDispatch(BaseException):
        pass

    failure = FatalDispatch('effect owner failed')

    def read_handler(task, tc, name, call_id, arguments, number, row, *rest):
        from lib.tasks_pkg.tool_runtime import active_context_for_call
        context = active_context_for_call(
            task, round_num=number, tool_call_id=call_id, round_entry=row)
        context.register_cancel_callback(release.set)
        entered.set()
        try:
            assert release.wait(10), 'test must release its owned handler'
            return call_id, 'completed observation', False
        finally:
            exited.set()

    def fail_effect(*args, **kwargs):
        assert entered.wait(10), 'concurrent sibling must start before the injected failure'
        raise failure

    monkeypatch.setattr(_pipeline, '_execute_tool_one', fail_effect)
    task = task_for(read_handler)
    task['_toolContractDocumentsByName']['write_file'] = ToolContractV2(
        name='write_file', parameters={'type': 'object', 'properties': {}},
        model_description='Injected effect failure.', permission='write',
    ).search_document()
    calls = []
    for number, (name, arguments) in enumerate([
        ('fetch_url', {'url': 'https://example.test/read'}),
        ('write_file', {'path': '/unused/file', 'content': 'unused'}),
    ], 1):
        call_id = f'call-{number}'
        _, row, _ = _build_tool_round_entry(name, arguments, call_id, '{}', number, False)
        row['llmRound'] = 0
        call = {'id': call_id, 'type': 'function',
                'function': {'name': name, 'arguments': '{}'}}
        calls.append((call, name, call_id, arguments, number, row, None))
    task['toolRounds'] = [call[5] for call in calls]
    from lib.tasks_pkg.tool_runtime import context_for_task, unregister_context
    other_cancelled = threading.Event()
    other = context_for_task(
        task, round_num=9, tool_call_id='another-invocation',
        tool_name='fetch_url', round_entry={})
    other.register_cancel_callback(other_cancelled.set)
    try:
        with pytest.raises(FatalDispatch) as caught:
            _pipeline.execute_tool_pipeline(
                task, calls, {'autoApply': True}, None, False, [], [], [], 0, 'test-model')
        assert caught.value is failure
        assert exited.is_set(), 'failed invocation returned while its sibling still owned resources'
        assert not other_cancelled.is_set(), 'failure must not cancel another invocation'
        unregister_context(other)
        assert not task.get('_activeToolContexts')
    finally:
        unregister_context(other)
        release.set()
        for pool in pools:
            pool.shutdown(wait=True, cancel_futures=True)
