"""Resource bounds for live run-command recovery state."""

from __future__ import annotations

import pytest

from lib.project_mod.config import MAX_COMMAND_OUTPUT


pytestmark = pytest.mark.unit


@pytest.mark.parametrize('typed_sink', [False, True])
def test_command_timing_sample_survives_wall_jumps_and_progress_coalescing(monkeypatch, typed_sink):
    from lib.agent_core import execution_clock as clocks
    from lib.tasks_pkg.handlers import code_exec
    from lib.tasks_pkg.tool_runtime import ToolExecutionContext
    import lib.tasks_pkg.manager as manager

    ticks = [10_000_000_000]
    monkeypatch.setattr(clocks.time, 'monotonic_ns', lambda: ticks[0])
    clock = clocks.ExecutionClock.start(origin_at_ms=1_000_000)
    task = {'id': 'timing-sample-task', '_userId': 1, '_executionClock': clock,
            '_suppressCheckpoint': True}
    round_entry = {'toolCallId': 'timing-call', 'toolName': 'run_command'}
    events = []
    monkeypatch.setattr(code_exec, 'append_event', lambda _task, event: events.append(dict(event)))
    monkeypatch.setattr(manager, 'append_event', lambda _task, event: events.append(dict(event)))
    monkeypatch.setattr(code_exec, '_LiveQrScanner', None)
    lifecycle = code_exec._RunCommandSpawnLifecycle(task, 1, round_entry, grace_ms=5000)
    context = ToolExecutionContext(task=task, round_num=1, tool_call_id='timing-call',
        tool_name='run_command', owner_user_id=1, round_entry=round_entry) if typed_sink else None
    callback = code_exec._make_run_command_progress_cb(
        task, 1, round_entry, 'synthetic command', runtime_context=context, lifecycle=lifecycle)
    ticks[0] += 100_000_000
    lifecycle(9_000_000, 9_030_000)
    assert round_entry['execStartTs'] == 1_000_100
    assert round_entry['deadlineTs'] == 1_030_100
    ticks[0] += 500_000_000
    monkeypatch.setattr(clocks.time, 'time', lambda: -1000.0)
    try:
        lifecycle.ensure_live()
        callback('stdout', 'reported output')
        callback.flush()
    finally:
        lifecycle.finish()
        if typed_sink:
            callback.finalize_output(complete=True)
    samples = [event for event in events if 'execTimingAtMs' in event]
    assert samples
    assert all(event['execTimingAtMs'] == 1_000_600 for event in samples)
    assert all(event['execElapsedMs'] == 500 for event in samples)
    assert all(event['deadlineRemainingMs'] == 29500 for event in samples)
    assert round_entry['execElapsedMs'] == 500


def test_command_sample_requires_a_live_clock_and_valid_spawn_origin():
    from lib.tasks_pkg.handlers.code_exec import _command_progress_timing
    from lib.agent_core.execution_clock import ExecutionClock
    assert _command_progress_timing({}, {'execStartTs': 1000}) == {}
    clock = ExecutionClock.start()
    assert _command_progress_timing({'_executionClock': clock}, {'execStartTs': 1}) == {}


def test_run_command_recovery_output_keeps_bounded_prefix_and_tail(monkeypatch):
    from lib.tasks_pkg.handlers import code_exec

    emitted = []
    monkeypatch.setattr(
        code_exec, 'append_event', lambda _task, event: emitted.append(event))
    monkeypatch.setattr(code_exec, '_LiveQrScanner', None)
    round_entry = {
        'toolCallId': 'call-1',
        'toolName': 'run_command',
        'status': 'executing',
    }
    callback = code_exec._make_run_command_progress_cb(
        {'id': 'task-1'}, 3, round_entry, 'chatty-command')
    raw = 'A' * 80_000 + 'M' * 120_000 + 'Z' * 80_000

    callback('stdout', raw)
    callback.flush()

    partial = round_entry['_partialOutput']
    assert len(partial) <= MAX_COMMAND_OUTPUT
    assert partial.startswith('A' * 1000)
    assert partial.endswith('Z' * 1000)
    assert 'live output truncated: 280,000 chars total' in partial
    assert round_entry['_partialOutputTotalChars'] == len(raw)
    assert round_entry['_partialOutputTruncated'] is True
    assert ''.join(event['chunk'] for event in emitted) == raw


def test_run_command_recovery_output_is_exact_below_budget(monkeypatch):
    from lib.tasks_pkg.handlers import code_exec

    monkeypatch.setattr(code_exec, 'append_event', lambda _task, _event: None)
    monkeypatch.setattr(code_exec, '_LiveQrScanner', None)
    round_entry = {'toolCallId': 'call-2', 'toolName': 'run_command'}
    callback = code_exec._make_run_command_progress_cb(
        {'id': 'task-2'}, 4, round_entry, 'small-command')

    callback('stdout', 'prefix')
    callback('stderr', '-suffix')
    callback.flush()

    assert round_entry['_partialOutput'] == 'prefix-suffix'
    assert round_entry['_partialOutputTotalChars'] == len('prefix-suffix')
    assert '_partialOutputTruncated' not in round_entry


def test_short_runtime_command_skips_progress_and_terminal_frames(monkeypatch):
    """The production ToolExecutionContext path also takes the short fast path."""
    from lib.tasks_pkg.handlers import code_exec
    from lib.tasks_pkg.tool_runtime.context import ToolExecutionContext
    import lib.tasks_pkg.manager as manager

    emitted = []
    monkeypatch.setattr(
        code_exec, 'append_event',
        lambda _task, event: emitted.append(dict(event)))
    monkeypatch.setattr(
        manager, 'append_event',
        lambda _task, event: emitted.append(dict(event)))
    round_entry = {'toolCallId': 'call-fast', 'toolName': 'run_command'}
    task = {'id': 'task-fast', '_userId': 1}
    context = ToolExecutionContext(
        task=task, round_num=5, tool_call_id='call-fast',
        tool_name='run_command', owner_user_id=1,
        round_entry=round_entry)
    lifecycle = code_exec._RunCommandSpawnLifecycle(
        task, 5, round_entry, grace_ms=5_000)
    callback = code_exec._make_run_command_progress_cb(
        task, 5, round_entry, 'printf ok', runtime_context=context,
        lifecycle=lifecycle)

    lifecycle(1_000.0, None)
    callback('stdout', 'ok')
    assert lifecycle.finish() is False
    callback.flush()
    artifact = callback.finalize_output(complete=True)

    assert emitted == []
    assert artifact.spilled is False
    assert artifact.size_bytes == 0
    assert '_partialOutput' not in round_entry


def test_chatty_command_promotes_to_live_before_time_grace(monkeypatch):
    """Early output above the coalescing budget is streamed, never discarded."""
    from lib.tasks_pkg.handlers import code_exec
    import lib.tasks_pkg.manager as manager

    emitted = []
    checkpoints = []
    monkeypatch.setattr(
        code_exec, 'append_event',
        lambda _task, event: emitted.append(dict(event)))
    monkeypatch.setattr(
        manager, 'checkpoint_task_partial',
        lambda _task, force=False: checkpoints.append(force))
    round_entry = {'toolCallId': 'call-chatty', 'toolName': 'run_command'}
    task = {'id': 'task-chatty'}
    lifecycle = code_exec._RunCommandSpawnLifecycle(
        task, 6, round_entry, grace_ms=5_000)
    callback = code_exec._make_run_command_progress_cb(
        task, 6, round_entry, 'chatty', lifecycle=lifecycle)
    chunk = 'x' * code_exec._COALESCE_BYTES

    lifecycle(2_000.0, None)
    callback('stdout', chunk)
    assert lifecycle.finish() is True
    callback.flush()

    assert any(event.get('execStartTs') == 2_000.0 for event in emitted)
    assert ''.join(event.get('chunk', '') for event in emitted) == chunk
    assert checkpoints == [True]
