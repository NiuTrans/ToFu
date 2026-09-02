"""Tests for the read-before-edit gate's incremental satisfied-path cache.

The O(history) cost in ``lib/tasks_pkg/handlers/_read_gate.py`` was the
per-gated-write rescan of ``task['messages']`` (JSON-parsing every historical
tool call). The cache keeps the gate's decisions byte-identical while only
folding NEW messages on each subsequent write.
"""
from __future__ import annotations

import json
import os

import pytest


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Fresh freshness store + fresh read-gate cache for every test."""
    monkeypatch.delenv('TOFU_APPLY_DIFF_READ_GATE', raising=False)
    monkeypatch.delenv('TOFU_WRITE_FRESHNESS_GATE', raising=False)
    from lib import write_freshness
    from lib.tasks_pkg.handlers import _read_gate
    write_freshness._reset_for_tests()
    _read_gate._reset_satisfied_cache_for_tests()
    yield {'read_gate': _read_gate, 'write_freshness': write_freshness}
    write_freshness._reset_for_tests()
    _read_gate._reset_satisfied_cache_for_tests()


@pytest.fixture
def workspace(tmp_path):
    proj = tmp_path / 'proj'
    proj.mkdir()
    for name in ('a.py', 'b.py', 'c.py'):
        (proj / name).write_text('x = 1\n', encoding='utf-8')
    return str(proj)


def _make_task(messages=None, conv_id='c1', task_id='t1'):
    return {
        'id': task_id,
        'convId': conv_id,
        'messages': list(messages or []),
        'toolRounds': [],
    }


def _history_with_call(name, args_dict, result_text, tc_id):
    """A prior-turn assistant tool_call + its tool result (verbatim args)."""
    return [
        {
            'role': 'assistant',
            'tool_calls': [{
                'id': tc_id,
                'function': {
                    'name': name,
                    'arguments': json.dumps(args_dict),
                },
            }],
        },
        {'role': 'tool', 'tool_call_id': tc_id, 'content': result_text},
    ]


@pytest.mark.unit
def test_cache_semantics_identical_to_uncached(workspace, _isolate):
    """The cached wrapper returns exactly what the raw scan returns, across
    append-only growth (the case that used to re-parse the whole history)."""
    rg = _isolate['read_gate']
    task = _make_task(messages=_history_with_call(
        'read_files', {'path': 'a.py'}, '=== a.py ===\n  1: x = 1\n', 'tc_1'))

    uncached = rg._collect_satisfied_paths_from_messages(task, workspace)
    cached = rg._cached_satisfied_paths_from_messages(task, workspace)
    assert cached == uncached

    # Append a second satisfying pair → cache folds only the new messages.
    task['messages'] += _history_with_call(
        'write_file', {'path': 'b.py', 'content': 'y = 2\n'},
        'File created: b.py', 'tc_2')
    uncached = rg._collect_satisfied_paths_from_messages(task, workspace)
    cached = rg._cached_satisfied_paths_from_messages(task, workspace)
    assert cached == uncached
    assert len(cached) == 2


@pytest.mark.unit
def test_incremental_cache_only_scans_new_messages(workspace, _isolate, monkeypatch):
    """A gated write after the first one only folds NEW tool calls — the
    full-history scan never re-runs for an append-only prefix."""
    rg = _isolate['read_gate']
    task = _make_task(messages=_history_with_call(
        'read_files', {'path': 'a.py'}, '=== a.py ===\n  1: x = 1\n', 'tc_1'))
    task['messages'] += _history_with_call(
        'write_file', {'path': 'b.py', 'content': 'y = 2\n'},
        'File created: b.py', 'tc_2')

    seen_starts = []
    real_collect = rg._collect_satisfied_paths_from_messages

    def spy(task_arg, project_path_arg, start=0):
        seen_starts.append(start)
        return real_collect(task_arg, project_path_arg, start=start)

    monkeypatch.setattr(rg, '_collect_satisfied_paths_from_messages', spy)

    first = rg._cached_satisfied_paths_from_messages(task, workspace)
    assert seen_starts == [0]  # cold cache → full scan

    task['messages'] += _history_with_call(
        'read_files', {'path': 'c.py'}, '=== c.py ===\n  1: x = 1\n', 'tc_3')
    second = rg._cached_satisfied_paths_from_messages(task, workspace)

    # Only the 2 newly-appended messages were scanned, not the 4-message prefix.
    assert seen_starts == [0, 4], seen_starts
    assert len(second) == 3
    assert second >= first


@pytest.mark.unit
def test_cache_invalidates_on_list_replacement(workspace, _isolate, monkeypatch):
    """A reassigned messages list (compaction/rewrite) invalidates to a full
    rescan instead of trusting a stale count-based prefix."""
    rg = _isolate['read_gate']
    task = _make_task(messages=_history_with_call(
        'read_files', {'path': 'a.py'}, '=== a.py ===\n  1: x = 1\n', 'tc_1'))

    seen_starts = []
    real_collect = rg._collect_satisfied_paths_from_messages

    def spy(task_arg, project_path_arg, start=0):
        seen_starts.append(start)
        return real_collect(task_arg, project_path_arg, start=start)

    monkeypatch.setattr(rg, '_collect_satisfied_paths_from_messages', spy)

    rg._cached_satisfied_paths_from_messages(task, workspace)
    # Same content, NEW list object (what compaction/write-back does).
    task['messages'] = list(task['messages'])
    rg._cached_satisfied_paths_from_messages(task, workspace)

    assert seen_starts == [0, 0], seen_starts


@pytest.mark.unit
def test_cache_invalidates_on_shrink(workspace, _isolate, monkeypatch):
    """A shorter messages list invalidates to a full rescan."""
    rg = _isolate['read_gate']
    task = _make_task(messages=_history_with_call(
        'read_files', {'path': 'a.py'}, '=== a.py ===\n  1: x = 1\n', 'tc_1'))
    task['messages'] += _history_with_call(
        'write_file', {'path': 'b.py', 'content': 'y = 2\n'},
        'File created: b.py', 'tc_2')

    seen_starts = []
    real_collect = rg._collect_satisfied_paths_from_messages

    def spy(task_arg, project_path_arg, start=0):
        seen_starts.append(start)
        return real_collect(task_arg, project_path_arg, start=start)

    monkeypatch.setattr(rg, '_collect_satisfied_paths_from_messages', spy)

    rg._cached_satisfied_paths_from_messages(task, workspace)
    # Truncate in place (same list object, shorter) → count shrinks.
    del task['messages'][2:]
    rg._cached_satisfied_paths_from_messages(task, workspace)

    assert seen_starts == [0, 0], seen_starts


@pytest.mark.unit
def test_gate_decision_unchanged_by_cache(workspace, _isolate):
    """The full gate's allow/refuse decision is identical whether or not the
    incremental cache is warm."""
    rg = _isolate['read_gate']
    from lib.tasks_pkg.handlers._read_gate import check_read_before_edit

    task = _make_task(messages=_history_with_call(
        'read_files', {'path': 'a.py'}, '=== a.py ===\n  1: x = 1\n', 'tc_1'))

    args_a = {'path': 'a.py', 'search': 'x = 1', 'replace': 'x = 2'}
    args_b = {'path': 'b.py', 'search': 'x = 1', 'replace': 'x = 2'}

    assert check_read_before_edit(task, 'apply_diff', args_a, workspace) is None
    assert check_read_before_edit(task, 'apply_diff', args_b, workspace) is not None

    # Fresh cache → identical decisions (no warm-cache drift).
    rg._reset_satisfied_cache_for_tests()
    assert check_read_before_edit(task, 'apply_diff', args_a, workspace) is None
    assert check_read_before_edit(task, 'apply_diff', args_b, workspace) is not None
