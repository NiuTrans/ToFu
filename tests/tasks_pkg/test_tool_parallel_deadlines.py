"""Declared long waits retain their deadline without extending sibling calls."""
from __future__ import annotations

from concurrent.futures import wait as wait_for_futures
from copy import deepcopy
import json
import threading
import time
from types import SimpleNamespace

import pytest

from lib.tasks_pkg.tool_dispatch import _pipeline
from lib.tools.contracts import ToolContractV2
from lib.tools.result_envelope import tool_result_error
from tests.tasks_pkg.test_tool_outcome_finality import (
    observations as observations, task_for,
)
from tests.tools.test_tool_settle_immediacy import _mk_tc

pytestmark = pytest.mark.unit


@pytest.fixture
def controlled_waits(monkeypatch):
    """Advance only the collector clock; join every real handler we start."""
    state = SimpleNamespace(now=0.0, entered={}, releases={}, futures=[],
                            contents={}, after_deadline=None, cancelled=False,
                            stop_after_expiry=False)
    monkeypatch.delenv('TOOL_PARALLEL_TIMEOUT', raising=False)
    monkeypatch.setattr(_pipeline, 'time', SimpleNamespace(
        monotonic=lambda: state.now, time=time.time))
    collect = _pipeline._completed_tools_until_cancel

    def handler(task, tc, name, call_id, arguments, number, row, *rest):
        from lib.tasks_pkg.tool_runtime import active_context_for_call
        context = active_context_for_call(
            task, round_num=number, tool_call_id=call_id, round_entry=row)
        context.register_cancel_callback(state.releases[call_id].set)
        state.entered[call_id].set()
        assert state.releases[call_id].wait(5), 'test must release its handler'
        return call_id, state.contents[call_id], False

    def collect_after_elapsed(futures, task, deadlines):
        state.futures.extend(futures)
        assert all(event.wait(5) for event in state.entered.values())
        state.now = state.elapsed
        if state.cancelled:
            task['aborted'] = True
            for release in state.releases.values():
                release.set()
        yield from collect(futures, task, deadlines)

    def complete_after_deadline(pending, *, timeout, return_when):
        if state.after_deadline is not None:
            state.after_deadline()
            state.after_deadline = None
        if state.stop_after_expiry:
            state.stop_after_expiry = False
            state.task['aborted'] = True
            return set(), pending
        for release in state.releases.values():
            release.set()
        return wait_for_futures(pending, timeout=5, return_when=return_when)

    monkeypatch.setattr(_pipeline, '_completed_tools_until_cancel', collect_after_elapsed)
    monkeypatch.setattr(_pipeline, 'wait', complete_after_deadline)

    def run(*, elapsed, mixed=False, mode='v2', content=None):
        state.elapsed = elapsed
        names = ['await_project_workers'] + (['get_conversation'] if mixed else [])
        task = task_for(handler, mode, name=names[0])
        items = []
        for number, name in enumerate(names, 1):
            state.entered[name] = threading.Event()
            state.releases[name] = threading.Event()
            state.contents[name] = content if number == 1 and content else name + ' result'
            task['_toolContractDocumentsByName'][name] = ToolContractV2(
                name=name, parameters={'type': 'object', 'properties': {}},
                model_description='Observe independent work.',
                idempotency='read_only', effect_recovery='read_only',
            ).search_document()
            items.append(_mk_tc(name, name, number,
                                json.dumps({'timeout_seconds': 1800}) if name == 'await_project_workers' else '{}'))
        task['toolRounds'] = [item[5] for item in items]
        state.task, state.items = task, items
        messages, settled = [], []
        timed_out = _pipeline.execute_tool_pipeline(
            task, items, {}, None, False, [
                {'type': 'function', 'function': {
                    'name': name,
                    'parameters': {'type': 'object', 'properties': {
                        'timeout_seconds': {'type': 'number', 'default': 1800, 'maximum': 1800}}}},
                } for name in names if name == 'await_project_workers'
            ], messages, [], 0, 'test-model',
            internal_result_sink=SimpleNamespace(
                capture=lambda call_id, value: settled.append((call_id, value))))
        return task, items, messages, settled, timed_out

    state.run = run
    yield state
    for release in state.releases.values():
        release.set()
    for future in state.futures:
        future.result(timeout=5)


@pytest.mark.parametrize('mode', ['legacy', 'v2'])
@pytest.mark.parametrize('elapsed,payload', [
    (301, {'workers': [{'status': 'completed'}], 'wait': 'settled'}),
    (1800, {'workers': [{'status': 'running'}], 'wait': 'timeout'}),
    (1806, {'workers': [{'status': 'running'}], 'wait': 'timeout'}),
])
def test_worker_wait_returns_after_default_pool_deadline(
        controlled_waits, observations, mode, elapsed, payload):
    task, items, messages, settled, timed_out = controlled_waits.run(
        elapsed=elapsed, mode=mode, content=json.dumps(payload))
    assert not timed_out, messages
    assert items[0][5]['status'] == 'done'
    assert tool_result_error(messages[0]['content']) is None
    assert payload['wait'] in messages[0]['content']
    assert settled[0][1].execution_status == 'ok'
    assert len(observations[1]) == 1
    assert not task.get('_activeToolContexts')


@pytest.mark.parametrize('mode', ['legacy', 'v2'])
def test_long_wait_does_not_extend_or_repeat_a_timed_out_sibling(
        controlled_waits, observations, mode):
    state = controlled_waits

    def sibling_already_settled():
        row = state.items[1][5]
        assert row['status'] == 'error', 'sibling must settle at its own deadline'
        assert any(event.get('type') == 'tool_complete'
                   and event.get('toolCallId') == 'get_conversation'
                   for event in observations[0])

    state.after_deadline = sibling_already_settled
    task, items, messages, settled, timed_out = state.run(elapsed=301, mixed=True, mode=mode)
    assert timed_out
    assert [item[5]['status'] for item in items] == ['done', 'error']
    assert 'await_project_workers result' in messages[0]['content']
    error = tool_result_error(messages[1]['content'])
    assert error.code == 'tool_execution_timeout' and error.retryable
    assert 'get_conversation result' not in messages[1]['content']
    assert len(settled) == len(observations[1]) == 2
    before = deepcopy(messages)
    for future in state.futures:
        future.result(timeout=5)
    assert messages == before
    assert not task.get('_activeToolContexts')


def test_noncooperative_long_wait_still_has_a_finite_outer_deadline(
        controlled_waits, observations):
    task, items, messages, settled, timed_out = controlled_waits.run(elapsed=1861)
    assert timed_out and items[0][5]['status'] == 'error'
    assert tool_result_error(messages[0]['content']).code == 'tool_execution_timeout'
    assert '_declaredTimeoutMs' not in items[0][5], 'only the declared timer attributes itself'
    assert settled[0][1].execution_status == 'error'
    for future in controlled_waits.futures:
        future.result(timeout=5)
    assert not task.get('_activeToolContexts'), 'expiry cancels the abandoned wait handler'


def test_stop_still_interrupts_a_long_wait(controlled_waits, observations):
    controlled_waits.cancelled = True
    task, items, messages, settled, timed_out = controlled_waits.run(elapsed=301)
    assert not timed_out
    assert items[0][5]['status'] == 'aborted'
    assert settled[0][1].execution_status == 'aborted'
    assert not task.get('_activeToolContexts')


def test_stop_after_sibling_timeout_joins_the_remaining_resource_owners(
        controlled_waits, observations):
    controlled_waits.stop_after_expiry = True
    task, items, messages, settled, timed_out = controlled_waits.run(elapsed=301, mixed=True)
    assert timed_out
    assert [item[5]['status'] for item in items] == ['aborted', 'error']
    assert len(settled) == 2
    assert not task.get('_activeToolContexts'), 'Stop must join even after a sibling timed out'
