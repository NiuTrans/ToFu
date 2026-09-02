"""Resource bounds for live run-command recovery state."""

from __future__ import annotations

import pytest

from lib.project_mod.config import MAX_COMMAND_OUTPUT


pytestmark = pytest.mark.unit


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
