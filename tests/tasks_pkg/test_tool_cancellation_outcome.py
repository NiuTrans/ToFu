"""Typed cooperative cancellation keeps the owning occurrence's terminal fact."""
from __future__ import annotations

from concurrent.futures import CancelledError
import logging

import pytest

from lib.llm.llm_errors import AbortedError
from lib.tasks_pkg.tool_runtime import ToolCancelled
from lib.tools.result_envelope import tool_result_error
from tests.tasks_pkg.test_tool_outcome_finality import (
    dispatch, observations as observations, task_for,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('mode', ['legacy', 'v2'])
@pytest.mark.parametrize('signal', ['flag', 'event', 'none'])
@pytest.mark.parametrize('exception_type', [AbortedError, CancelledError, ToolCancelled])
def test_cooperative_cancellation_requires_owner_signal_for_aborted_outcome(
        mode, signal, exception_type, observations, caplog):
    entered = []

    def handler(task, *args):
        entered.append(True)
        if signal == 'flag':
            task['aborted'] = True
        elif signal == 'event':
            task['abort_event'].set()
        raise exception_type('cooperative boundary')

    task = task_for(handler, mode)
    row, messages, captured = dispatch(task)
    expected = 'error' if signal == 'none' else 'aborted'
    assert entered == [True]
    assert row['status'] == expected
    assert row['_runtimeState'] == ('error' if signal == 'none' else 'cancelled')
    assert captured[0].execution_status == expected
    error = tool_result_error(messages[0]['content'])
    assert error.code == 'tool_execution_cancelled' and error.retryable is False
    assert [event['outcome'] for event in observations[1]] == [expected]
    assert not task.get('_activeToolContexts')
    if signal != 'none':
        assert not [record for record in caplog.records if record.levelno >= logging.ERROR]


@pytest.mark.parametrize('mode', ['legacy', 'v2'])
def test_real_failure_after_stop_keeps_its_failure_class(mode, observations):
    def handler(task, *args):
        task['aborted'] = True
        raise ConnectionError('source connection was lost')

    task = task_for(handler, mode)
    row, messages, captured = dispatch(task)
    assert row['status'] == captured[0].execution_status == 'error'
    assert tool_result_error(messages[0]['content']).code == 'tool_connection_failed'
    assert [event['outcome'] for event in observations[1]] == ['error']
    assert not task.get('_activeToolContexts')


@pytest.mark.parametrize('mode', ['legacy', 'v2'])
@pytest.mark.parametrize('exception_type', [AbortedError, ToolCancelled])
def test_cancelled_write_keeps_unknown_effect_recovery(mode, exception_type, observations):
    def handler(task, *args):
        task['aborted'] = True
        raise exception_type('stopped after the write entered')

    task = task_for(handler, mode, name='write_file', permission='write')
    row, messages, captured = dispatch(task, name='write_file')
    assert row['status'] == captured[0].execution_status == 'aborted'
    assert row['executionEffect']['state'] == 'unknown'
    error = tool_result_error(messages[0]['content'])
    assert error.code == 'tool_effect_unknown' and error.retryable is False
    assert 'Do not repeat' in error.next_action
    assert [event['outcome'] for event in observations[1]] == ['aborted']
    assert not task.get('_activeToolContexts')


@pytest.mark.parametrize('exception_type', [AbortedError, CancelledError, ToolCancelled])
def test_cancelled_batch_slot_keeps_siblings_without_runtime_fault_log(exception_type, caplog):
    from lib.tasks_pkg.handlers._adapter import run_batch_concurrent
    progress = []

    def worker(value):
        if value == 0:
            raise exception_type('cooperative boundary')
        return 'completed'

    result = run_batch_concurrent([0, 1], worker, max_workers=1,
        on_item=lambda index, item, value, ok: progress.append((index, value, ok)))
    assert result == [None, 'completed']
    assert sorted(progress) == [(0, None, False), (1, 'completed', True)]
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]
