"""Specify per-round checklist evidence reuse and exact invalidation.

Each projection pairs the current history; unchanged receipts reuse their
interpretation while edited content and accepted state are verified again.
Compaction candidates bypass the memo entirely (they are
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


def test_evidence_memo_reuses_receipt_parsing_for_appended_suffix(monkeypatch):
    task = _task_with_todo()
    task['_activeRoundNum'] = 1
    state = _state(task)
    messages = list(_accepted_tool_msgs(task))
    first = checklist._current_evidence(messages, state, task=task)
    assert first is not None
    memo = task.get('_checklistEvidenceMemo')
    assert memo and len(memo['receipts']) == 1
    # Pair current messages again without reparsing unchanged evidence.
    calls = []
    original = checklist._accepted_result
    def counting(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)
    monkeypatch.setattr(checklist, '_accepted_result', counting)
    messages.append({'role': 'user', 'content': 'continue'})
    second = checklist._current_evidence(messages, state, task=task)
    assert second is first
    assert calls == []


def test_public_checklist_projection_reuses_normalized_state(monkeypatch):
    task = _task_with_todo()
    messages = _accepted_tool_msgs(task)
    calls = []
    original = checklist._accepted_result

    def counting(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(checklist, '_accepted_result', counting)
    for _ in range(3):
        assert checklist.build_checklist_context_block(messages, task) is None
    assert calls == [messages[-1]['content']]


def test_suffix_keeps_unmatched_calls_from_the_current_assistant_batch():
    task = _task_with_todo()
    assistant, receipt = _accepted_tool_msgs(task)
    assistant['tool_calls'].insert(0, {'id': 'read', 'type': 'function',
        'function': {'name': 'read_file', 'arguments': '{}'}})
    messages = [assistant, {'role': 'tool', 'tool_call_id': 'read', 'content': 'source'}]
    assert checklist._current_evidence(messages, _state(task), task=task) is None
    messages.append(receipt)
    cached = checklist._current_evidence(messages, _state(task), task=task)
    fresh = checklist._current_evidence(messages, _state(task), memoize=False)
    assert cached is fresh is receipt


def test_suffix_resets_call_identity_at_a_new_assistant_batch():
    task = _task_with_todo()
    assistant, receipt = _accepted_tool_msgs(task)
    messages = [assistant]
    assert checklist._current_evidence(messages, _state(task), task=task) is None
    messages.extend([{'role': 'assistant', 'tool_calls': [{'id': 'call_1',
        'function': {'name': 'read_file', 'arguments': '{}'}}]}, receipt])
    assert checklist._current_evidence(messages, _state(task), task=task) is None
    assert checklist.build_checklist_context_block(messages, task) is not None


@pytest.mark.parametrize('change', ['replace_prefix', 'replace_list', 'todo_content', 'explanation'])
def test_cached_evidence_is_revalidated_when_its_inputs_change(change):
    task = _task_with_todo()
    messages = _accepted_tool_msgs(task)
    assert checklist._current_evidence(messages, _state(task), task=task) is messages[-1]
    if change == 'replace_prefix':
        messages[-1] = {'role': 'user', 'content': 'replacement'}
    elif change == 'replace_list':
        messages = [{'role': 'user', 'content': 'replacement'}]
    elif change == 'todo_content':
        task['_todoState']['todos'][0]['content'] = 'Different work'
    else:
        task['_todoState']['explanation'] = 'New progress explanation'
    assert checklist._current_evidence(messages, _state(task), task=task) is None


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


@pytest.mark.parametrize('change', ['receipt_content', 'call_name', 'receipt_id'])
def test_same_round_in_place_mutation_cannot_reuse_old_evidence(change):
    task = _task_with_todo()
    messages = _accepted_tool_msgs(task)
    assert checklist.build_checklist_context_block(messages, task) is None
    if change == 'receipt_content':
        messages[-1]['content'] = 'Receipt clipped'
    elif change == 'call_name':
        messages[0]['tool_calls'][0]['function']['name'] = 'read_file'
    else:
        messages[-1]['tool_call_id'] = 'foreign'
    assert checklist.build_checklist_context_block(messages, task) is not None


@pytest.mark.parametrize('gateway', [False, True])
def test_cached_list_content_is_revalidated_and_removed_receipts_are_released(gateway):
    import json
    task = _task_with_todo()
    messages = _accepted_tool_msgs(task)
    if gateway:
        messages[0]['tool_calls'][0]['function']['name'] = 'execute_tools'
        messages[-1]['content'] = json.dumps({'results': [{
            'name': 'todo_write', 'status': 'done', 'output': messages[-1]['content']}]})
    messages[-1]['content'] = [{'type': 'text', 'text': messages[-1]['content']}]
    assert checklist.build_checklist_context_block(messages, task) is None
    messages[-1]['content'][0]['text'] = 'Clipped receipt'
    assert checklist.build_checklist_context_block(messages, task) is not None
    messages.clear()
    assert checklist.build_checklist_context_block(messages, task) is not None
    assert task['_checklistEvidenceMemo']['receipts'] == {}


def test_failed_suffix_scan_does_not_publish_partial_call_consumption(monkeypatch):
    task = _task_with_todo()
    assistant, receipt = _accepted_tool_msgs(task)
    messages = [assistant]
    assert checklist._current_evidence(messages, _state(task), task=task) is None
    memo = task['_checklistEvidenceMemo']
    messages.append(receipt)
    original = checklist._accepted_result
    with monkeypatch.context() as failure:
        def interrupted(*args):
            raise RuntimeError('interrupted evidence scan')
        failure.setattr(checklist, '_accepted_result', interrupted)
        with pytest.raises(RuntimeError, match='interrupted evidence scan'):
            checklist._current_evidence(messages, _state(task), task=task)
    assert checklist._accepted_result is original
    assert task['_checklistEvidenceMemo'] is memo and memo['receipts'] == {}
    assert checklist._current_evidence(messages, _state(task), task=task) is receipt


def test_consumed_call_cannot_authorize_a_second_receipt():
    task = _task_with_todo()
    assistant, receipt = _accepted_tool_msgs(task)
    messages = [assistant, dict(receipt, content='Incomplete receipt')]
    assert checklist._current_evidence(messages, _state(task), task=task) is None
    messages.append(receipt)
    assert checklist._current_evidence(messages, _state(task), task=task) is None
    assert checklist._current_evidence(messages, _state(task), memoize=False) is None


def test_compaction_candidate_never_writes_task_memo():
    task = _task_with_todo()
    task['_activeRoundNum'] = 1
    candidate = list(_accepted_tool_msgs(task))
    checklist.carry_checklist_through_compaction(candidate, task)
    assert '_checklistEvidenceMemo' not in task


def test_compaction_candidate_does_not_change_existing_round_memo():
    from copy import deepcopy
    task = _task_with_todo()
    messages = _accepted_tool_msgs(task)
    assert checklist.build_checklist_context_block(messages, task) is None
    memo = task['_checklistEvidenceMemo']
    before = deepcopy(memo)
    candidate = [{'role': 'user', 'content': 'compacted history'}]
    checklist.carry_checklist_through_compaction(candidate, task)
    assert task['_checklistEvidenceMemo'] is memo and memo == before
    assert checklist.build_checklist_context_block(candidate, task, memoize=False) is None
