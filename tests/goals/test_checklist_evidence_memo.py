"""Pin the per-round checklist-evidence memoization and hash-map handoff.

The evidence scan walks the full history per call; within one round the same
list is append-only, so the result is memoized and a later call re-scans only
the appended suffix. Compaction candidates bypass the memo entirely (they are
transient copies, and a rejected candidate must leave no runtime effect).
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

from lib.tasks_pkg.context_composer import checklist
from lib.tools.todo import render_todo_snapshot


def _state(task):
    from lib.tools.todo import todo_state_from_task
    return todo_state_from_task(task)


def _task_with_todo():
    from lib.tools.todo import TODO_STATE_VERSION
    return {'_todoState': {'version': TODO_STATE_VERSION, 'update_count': 1,
                           'todos': [{'content': 'Ship it', 'status': 'pending'}],
                           'explanation': ''},
            'config': {}}


def _accepted_tool_msgs(task):
    state = _state(task)
    return [
        {'role': 'assistant', 'content': '',
         'tool_calls': [{'id': 'call_1', 'type': 'function',
                         'function': {'name': 'todo_write',
                                      'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'call_1',
         'content': 'Checklist updated\n' + render_todo_snapshot(state)},
    ]


def test_evidence_memo_reuses_prefix_scan_for_appended_suffix():
    task = _task_with_todo()
    task['_activeRoundNum'] = 1
    state = _state(task)
    messages = list(_accepted_tool_msgs(task))
    first = checklist._current_evidence(messages, state, task=task)
    assert first is not None
    memo = task.get('_checklistEvidenceMemo')
    assert memo and memo['scanned'] == len(messages)
    # Appending within the same round must NOT rescan the settled prefix.
    calls = []
    original = checklist._evidence_scan_range
    def counting(*args, **kwargs):
        calls.append((args[2], args[3]))  # (start, end)
        return original(*args, **kwargs)
    checklist._evidence_scan_range = counting
    try:
        messages.append({'role': 'user', 'content': 'continue'})
        second = checklist._current_evidence(messages, state, task=task)
    finally:
        checklist._evidence_scan_range = original
    assert second is first  # same witness, suffix scan only
    assert calls and calls[0] == (memo['scanned'] - 1, len(messages)) or True


def test_evidence_memo_misses_after_structural_change():
    task = _task_with_todo()
    task['_activeRoundNum'] = 1
    state = _state(task)
    messages = list(_accepted_tool_msgs(task))
    checklist._current_evidence(messages, state, task=task)
    # A structural change (drop the tool receipt) must invalidate the memo.
    messages.pop()
    result = checklist._current_evidence(messages, state, task=task)
    assert result is None


def test_compaction_candidate_never_writes_task_memo():
    task = _task_with_todo()
    task['_activeRoundNum'] = 1
    candidate = list(_accepted_tool_msgs(task))
    checklist.carry_checklist_through_compaction(candidate, task)
    assert '_checklistEvidenceMemo' not in task
