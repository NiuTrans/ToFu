"""Slow executions automatically retain bounded, correlated, content-free evidence."""
from types import SimpleNamespace
import logging

import pytest

from lib.tools import execution_timing as timing
from lib.tasks_pkg import turn_trace
from lib.turn_trace_contract import TOOL_EXECUTION_BUDGETS_MS

pytestmark = pytest.mark.unit


@pytest.fixture
def clock(monkeypatch):
    clock = SimpleNamespace(value=0.0)
    monkeypatch.setattr(timing, 'time', SimpleNamespace(monotonic=lambda: clock.value))
    return clock


def test_slow_edit_records_stages_counts_and_shared_budget(clock, caplog):
    with caplog.at_level(logging.WARNING, logger=timing.logger.name):
        with timing.tool_execution_timing('edit_file', task_id='task-123', conversation_id='conv-123', tool_call_id='call-123'):
            with timing.timing_phase('undo_index_read', bytes_read=318_000_000, records=3776):
                clock.value = 1.2
            clock.value = 30.119
    events = [record for record in caplog.records if getattr(record, 'tofu_event_name', '') == 'tool_execution_slow']
    assert len(events) == 1
    fields = events[0].tofu_event_fields
    assert fields['elapsed_ms'] == 30119
    assert fields['budget_ms'] == TOOL_EXECUTION_BUDGETS_MS['edit_file']
    assert fields['task_id'] == 'task-123' and fields['conversation_id'] == 'conv-123'
    assert fields['phases']['undo_index_read'] == {
        'elapsed_ms':1200.0, 'calls':1, 'bytes_read':318_000_000, 'bytes_written':0, 'records':3776}
    span = {'kind':'tool', 'attrs':{'toolName':'edit_file'}, 'tStart':0, 'tEnd':30119}
    turn_trace._apply_budget(span)
    assert span['overBudget'] is True and span['budgetMs'] == fields['budget_ms']


def test_fast_calls_and_arbitrary_shell_workloads_are_quiet(clock, caplog):
    with timing.tool_execution_timing('read_files'):
        clock.value = 0.1
    with timing.tool_execution_timing('run_command'):
        clock.value = 10000
    assert not caplog.records


def test_failed_execution_is_diagnosed_without_swallowing_failure(clock, caplog):
    with pytest.raises(RuntimeError, match='business failure'):
        with timing.tool_execution_timing('write_file'):
            clock.value = 4
            raise RuntimeError('business failure')
    fields = caplog.records[-1].tofu_event_fields
    assert fields['outcome'] == 'exception'
    assert 'business failure' not in str(fields)
    assert timing._current.get() is None


def test_nested_scopes_restore_parent_and_phase_memory_is_bounded(clock, caplog):
    with timing.tool_execution_timing('edit_file'):
        outer = timing._current.get()
        with timing.tool_execution_timing('read_files'):
            assert timing._current.get() is not outer
        assert timing._current.get() is outer
        for i in range(100):
            with timing.timing_phase(f'phase_{i}'):
                pass
        clock.value = 3
    assert len(caplog.records[-1].tofu_event_fields['phases']) == 16
    assert timing._current.get() is None


def test_diagnostic_sink_failure_never_changes_success(clock, monkeypatch):
    def fail(*a, **kw): raise OSError('diagnostic unavailable')
    monkeypatch.setattr(timing, 'log_event', fail)
    with timing.tool_execution_timing('edit_file'):
        clock.value = 4
    assert timing._current.get() is None


def test_normal_executor_uses_slow_tool_scope(clock, monkeypatch, caplog):
    from lib.tasks_pkg.executor import _execute as executor
    def dispatch(*args, **kwargs):
        with timing.timing_phase('handler_work'):
            clock.value = 30
        args[6]['_runtimeState'] = 'done'
        return ('call-123', 'secret-content-must-not-be-logged', False)
    monkeypatch.setattr(executor, '_dispatch_admitted_tool_one', dispatch)
    result = executor._execute_tool_one({'id':'task-123','convId':'conv-123'}, {}, 'edit_file',
        'call-123', {}, 1, {}, {}, None, False)
    assert result[1] == 'secret-content-must-not-be-logged'
    assert caplog.records[-1].tofu_event_fields['phases']['handler_work']['elapsed_ms'] == 30000
    assert 'secret-content' not in caplog.text


def test_streaming_prefetch_keeps_call_identity_and_same_budget(clock, monkeypatch, caplog):
    from lib.tasks_pkg.streaming_tool_executor import StreamingToolAccumulator
    accumulator = object.__new__(StreamingToolAccumulator)
    accumulator._task = {'id':'task-123','convId':'conv-123'}
    def read(name, args):
        clock.value = 6
        return 'private-read-content'
    monkeypatch.setattr(accumulator, '_execute_one', read)
    assert accumulator._execute_one_for_call('read_files', {}, 'call-prefetch') == 'private-read-content'
    fields = caplog.records[-1].tofu_event_fields
    assert fields['tool_call_id'] == 'call-prefetch'
    assert fields['budget_ms'] == TOOL_EXECUTION_BUDGETS_MS['read_files']
    assert 'private-read-content' not in caplog.text
