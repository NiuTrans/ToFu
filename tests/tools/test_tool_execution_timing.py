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


def _slow(clock, seconds, original):
    def run(*args, **kwargs):
        clock.value += seconds
        return original(*args, **kwargs)
    return run


def _project_task(tmp_path, task_id):
    from lib.project_mod.config import build_root_policy
    return {'id': task_id, '_userId': 7, 'messages': [], 'toolRounds': [],
            'config': {'projectPath': str(tmp_path)},
            '_workspaceRootPolicy': build_root_policy(str(tmp_path))}


def _slow_records(caplog):
    return [record.tofu_event_fields for record in caplog.records
            if getattr(record, 'tofu_event_name', '') == 'tool_execution_slow']


def _prefetch(task, tmp_path, name, arguments, call_id):
    """Run one call on the streaming prefetch worker thread, exactly as submitted."""
    from lib.project_mod.config import clear_conv_state
    from lib.tasks_pkg.streaming_tool_executor import StreamingToolAccumulator
    accumulator = StreamingToolAccumulator(task, project_path=str(tmp_path))
    try:
        return accumulator._pool.submit(
            accumulator._execute_one_for_call, name, arguments, call_id).result()
    finally:
        accumulator.close(wait=True)
        clear_conv_state(task['id'])


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
    assert len(caplog.records[-1].tofu_event_fields['phases']) == 18
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


@pytest.mark.parametrize('acknowledged', [True, None])
def test_effect_intent_barrier_wait_is_attributed(clock, monkeypatch, caplog, acknowledged):
    from lib.tasks_pkg import manager
    from lib.tasks_pkg.executor._effects import begin_tool_effect, ToolEffectDurabilityError
    def barrier(task, event):
        clock.value += 2.5
        return acknowledged
    monkeypatch.setattr(manager, 'append_event', barrier)
    task, row = {'id': 'task-123', '_turnId': 'turn-1', '_attemptId': 'attempt-1'}, {}
    with timing.tool_execution_timing('edit_file'):
        if acknowledged:
            begin_tool_effect(task, 'edit_file', 'call-123', 1, row)
        else:
            with pytest.raises(ToolEffectDurabilityError):
                begin_tool_effect(task, 'edit_file', 'call-123', 1, row)
    phase = caplog.records[-1].tofu_event_fields['phases']['effect_intent_ack']
    assert phase['elapsed_ms'] == 2500.0 and phase['calls'] == 1
    assert row['executionEffect']['state'] == ('entered' if acknowledged else 'not_started')


def test_project_root_and_guidance_admission_is_attributed(clock, tmp_path, monkeypatch, caplog):
    from lib.project_mod.config import clear_conv_state
    from lib.tasks_pkg import project_guidance, workspace_delivery
    from lib.tasks_pkg.handlers import project as handler
    (tmp_path / 'notes.txt').write_text('private-file-content')
    task = _project_task(tmp_path, 'admission-timing-task')
    monkeypatch.setattr(project_guidance, 'check_project_guidance',
                        _slow(clock, 2, project_guidance.check_project_guidance))
    monkeypatch.setattr(workspace_delivery, 'rebind_project_tool_arguments',
                        _slow(clock, 4, workspace_delivery.rebind_project_tool_arguments))
    monkeypatch.setattr(handler, '_finalize_tool_round', lambda *_args, **_kwargs: None)
    try:
        with caplog.at_level(logging.WARNING, logger=timing.logger.name):
            with timing.tool_execution_timing('read_files', task_id=task['id'], tool_call_id='call-1'):
                result = handler._handle_project_tool(task, {}, 'read_files', 'call-1',
                    {'path': 'notes.txt'}, 1, {}, task['config'], str(tmp_path), True)
    finally:
        clear_conv_state(task['id'])
    assert 'private-file-content' in str(result)
    fields = next(record.tofu_event_fields for record in caplog.records
                  if getattr(record, 'tofu_event_name', '') == 'tool_execution_slow')
    assert fields['phases']['project_admission'] == {
        'elapsed_ms': 6000.0, 'calls': 3, 'bytes_read': 0, 'bytes_written': 0, 'records': 0}
    # The handler shares the guidance, target-resolution and file-read owners.
    assert fields['phases']['project_guidance']['calls'] == 1
    assert fields['phases']['path_resolution']['calls'] == 1
    assert fields['phases']['file_read']['bytes_read'] == len('private-file-content')
    assert fields['phases']['read_render']['calls'] == 2
    assert 'private-file-content' not in caplog.text


def test_streaming_prefetch_keeps_call_identity_and_same_budget(clock, monkeypatch, caplog):
    from lib.tasks_pkg.streaming_tool_executor import StreamingToolAccumulator
    accumulator = StreamingToolAccumulator({'id':'task-123','convId':'conv-123'}, project_path=None)
    def read(name, args):
        clock.value = 6
        return 'private-read-content'
    monkeypatch.setattr(accumulator, '_execute_one', read)
    try:
        assert accumulator._execute_one_for_call('read_files', {}, 'call-prefetch') == ('private-read-content',)
    finally:
        accumulator.close(wait=True)
    fields = next(record.tofu_event_fields for record in reversed(caplog.records)
                  if getattr(record, 'tofu_event_fields', {}).get('tool_call_id'))
    assert fields['tool_call_id'] == 'call-prefetch'
    assert fields['budget_ms'] == TOOL_EXECUTION_BUDGETS_MS['read_files']
    assert 'private-read-content' not in caplog.text


def test_slow_prefetched_read_attributes_its_wall_time(clock, tmp_path, monkeypatch, caplog):
    from lib.artifacts import file_snapshot
    from lib.project_mod import read_tools
    from lib.tasks_pkg import coordinator_policy, project_guidance, workspace_delivery
    (tmp_path / 'AGENTS.md').write_text('private-rule-content')
    (tmp_path / 'notes.txt').write_text('private-file-content\n')
    steps = {(coordinator_policy, 'coordinator_scope'): 0.5,
             (workspace_delivery, 'route_project_tool_args'): 1,
             (project_guidance, 'read_scoped_guidance'): 2,
             (file_snapshot, 'read_project_snapshot'): 3,
             (read_tools, '_extract_symbols'): 4}
    for (owner, name), seconds in steps.items():
        monkeypatch.setattr(owner, name, _slow(clock, seconds, getattr(owner, name)))
    task = _project_task(tmp_path, 'prefetch-timing-task')
    with caplog.at_level(logging.WARNING, logger=timing.logger.name):
        result = _prefetch(task, tmp_path, 'read_files',
                           {'reads': [{'path': 'notes.txt'}]}, 'call-prefetch')
    assert 'private-file-content' in result[0]
    [fields] = _slow_records(caplog)
    assert fields['tool_call_id'] == 'call-prefetch' and fields['elapsed_ms'] == 10500
    phases = fields['phases']
    assert phases['coordinator_policy']['elapsed_ms'] == 500.0
    assert phases['project_admission'] == {
        'elapsed_ms': 3000.0, 'calls': 2, 'bytes_read': 0, 'bytes_written': 0, 'records': 0}
    assert phases['project_guidance'] == {
        'elapsed_ms': 2000.0, 'calls': 1, 'bytes_read': 0, 'bytes_written': 0, 'records': 1}
    assert phases['path_resolution']['calls'] == 1
    assert phases['file_read'] == {
        'elapsed_ms': 3000.0, 'calls': 1, 'bytes_read': len('private-file-content\n'),
        'bytes_written': 0, 'records': 0}
    assert phases['read_render']['elapsed_ms'] == 4000.0
    assert 'dispatch_authority' not in phases
    assert 'notes.txt' not in str(fields) and 'AGENTS' not in str(fields)
    assert 'private-file-content' not in caplog.text and 'private-rule-content' not in caplog.text


def test_fast_prefetched_read_still_logs_nothing(clock, tmp_path, monkeypatch, caplog):
    from lib.artifacts import file_snapshot
    (tmp_path / 'notes.txt').write_text('private-file-content\n')
    budget = TOOL_EXECUTION_BUDGETS_MS['read_files'] / 1000
    monkeypatch.setattr(file_snapshot, 'read_project_snapshot',
                        _slow(clock, budget - 0.1, file_snapshot.read_project_snapshot))
    task = _project_task(tmp_path, 'prefetch-fast-task')
    with caplog.at_level(logging.DEBUG, logger=timing.logger.name):
        result = _prefetch(task, tmp_path, 'read_files',
                           {'reads': [{'path': 'notes.txt'}]}, 'call-fast')
    assert 'private-file-content' in result[0]
    assert not [record for record in caplog.records if record.name == timing.logger.name]


@pytest.mark.parametrize('name, arguments', [
    ('search_files', {'pattern': 'private', 'path': 'notes.txt'}),
    ('find_files', {'pattern': '*.txt', 'path': '.'}),
])
def test_slow_prefetched_search_and_find_share_admission_phases(
        clock, tmp_path, monkeypatch, caplog, name, arguments):
    from lib.tasks_pkg import project_guidance
    (tmp_path / 'AGENTS.md').write_text('private-rule-content')
    (tmp_path / 'notes.txt').write_text('private-file-content\n')
    seconds = TOOL_EXECUTION_BUDGETS_MS[name] / 1000 + 1
    monkeypatch.setattr(project_guidance, 'read_scoped_guidance',
                        _slow(clock, seconds, project_guidance.read_scoped_guidance))
    task = _project_task(tmp_path, f'prefetch-{name}-task')
    with caplog.at_level(logging.WARNING, logger=timing.logger.name):
        _prefetch(task, tmp_path, name, arguments, f'call-{name}')
    [fields] = _slow_records(caplog)
    phases = fields['phases']
    assert phases['project_guidance']['elapsed_ms'] == seconds * 1000
    assert phases['project_admission']['elapsed_ms'] == seconds * 1000
    assert phases['path_resolution']['calls'] == 1
    assert 'file_read' not in phases
    assert 'private-file-content' not in caplog.text and 'notes.txt' not in str(fields)


def test_dispatch_authority_storage_reads_are_attributed(clock, tmp_path, monkeypatch, caplog):
    import lib.integration_state_repository as workspaces
    from lib.llm.llm_errors import ToolAdmissionRefused
    from lib.project_dispatch import admission
    from lib.tasks_pkg import persistence_wait
    def validated(task, operation, **kwargs):
        clock.value += 4
        return 'validated-grant'
    monkeypatch.setattr(persistence_wait, 'wait_for_persistence', validated)
    monkeypatch.setattr(workspaces, 'find_workspace',
                        _slow(clock, 2, lambda *args, **kwargs: None))
    task = {'id': 'dispatch-task', 'config': {admission.KEY: {}}}
    with pytest.raises(ToolAdmissionRefused):
        with timing.tool_execution_timing('read_files'):
            assert admission.validated_dispatch(task) == 'validated-grant'
            admission.owned_workspace(task, {'projectPath': str(tmp_path), 'owner': 7})
    fields = _slow_records(caplog)[-1]
    assert fields['outcome'] == 'exception'
    assert fields['phases']['dispatch_authority'] == {
        'elapsed_ms': 6000.0, 'calls': 2, 'bytes_read': 0, 'bytes_written': 0, 'records': 0}
