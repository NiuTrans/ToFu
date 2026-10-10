"""Todo progress is recoverable context, never independent completion authority."""
import copy
import threading

import pytest

from lib.tasks_pkg.context_composer.checklist import build_checklist_context_block
from lib.tasks_pkg.orchestrator._resume_state import (
    ContinueResumeStateProtocolError, apply_resume_state, prepare_resume_state,
)
from lib.tasks_pkg.stream_handler.api import analyse_stream_result

pytestmark = pytest.mark.unit


def snapshot(todos=None):
    return {'version': 3, 'todos': todos or [], 'update_count': 1, 'explanation': 'Latest evidence'}


@pytest.mark.parametrize('status', ['pending', 'in_progress', 'completed', 'blocked'])
def test_todo_status_does_not_veto_provider_stop(status):
    task = {'id': 'testtodo', '_userId': 1, 'events': [],
            'events_lock': threading.Lock(), 'aborted': False,
            '_todos': [{'content': 'Work', 'status': status}]}
    messages = [{'role': 'user', 'content': 'Report the result'}]
    result = analyse_stream_result(
        assistant_msg={'role': 'assistant', 'content': 'The result is ready.', 'reasoning_content': ''},
        last_finish_reason='stop', task=task, tid='testtodo', model='test-model',
        round_num=2, _premature_retry_count=0, messages=messages,
        usage={'_stream_anomaly': False, '_empty_stop': False, '_chunks_received': 10})
    assert result['action'] == 'break'
    assert result['last_finish_reason'] == 'stop'
    assert '_todo_blocked' not in task and '_todo_continuation_count' not in task
    assert len(messages) == 1


def test_empty_accepted_snapshot_overrides_stale_legacy_list():
    task = {'_todoState': snapshot(), '_todos': [{'content': 'Stale work', 'status': 'pending'}]}
    block = build_checklist_context_block([], task)
    assert 'accepted todo list is empty' in block.content
    assert 'Stale work' not in block.content
    assert 'Latest evidence' in block.content


@pytest.mark.parametrize('mutation', [
    lambda s: s.update(version=True),
    lambda s: s.update(extra='unknown'),
    lambda s: s.update(update_count=True),
    lambda s: s.update(explanation='x' * 2049),
    lambda s: s.update(todos=[{'content': 'x', 'status': 'pending'}] * 145),
    lambda s: s.update(todos=[{'content': 'x' * 513, 'status': 'pending'}]),
    lambda s: s.update(todos=[{'content': 'x', 'status': 'invalid'}]),
    lambda s: s.update(todos=[{'content': 'x', 'status': [], 'id': 'old'}]),
])
def test_invalid_v3_fails_before_any_mutation(mutation):
    state = snapshot()
    mutation(state)
    task = {'content': 'unchanged', 'content_lock': threading.Lock()}
    before = dict(task)
    messages = [{'role': 'user', 'content': 'continue'}]
    with pytest.raises(ContinueResumeStateProtocolError):
        apply_resume_state(task=task, cfg={'contentPrefix': 'changed', 'checkpointTodoState': state},
                           messages=messages, model='test', tid='test')
    assert task == before
    assert messages == [{'role': 'user', 'content': 'continue'}]


def legacy_snapshot(depth=2, width=2):
    return {'version': 2, 'update_count': 2, 'root_completed': False, 'history': [],
            'stack': [{'checklist_id': f'frame-{level}', 'revision': 1,
                       'parent_todo_id': '0' if level else None,
                       'todos': [{'id': str(i), 'content': f'Work {level}/{i}',
                                  'status': 'in_progress' if i == 0 else 'pending'}
                                 for i in range(width)]} for level in range(depth)]}


def test_legacy_resume_flattens_all_ancestors_without_losing_unfinished_work():
    original = legacy_snapshot(6, 24)
    before = copy.deepcopy(original)
    prepared = prepare_resume_state({'checkpointTodoState': original})
    state = prepared.checkpoint_todo_state
    assert state['version'] == 3
    assert len(state['todos']) == 144
    assert {row['content'] for row in state['todos']} == {
        f'Work {level}/{i}' for level in range(6) for i in range(24)}
    assert all(set(row) == {'content', 'status'} for row in state['todos'])
    assert original == before


def test_migrated_large_snapshot_roundtrips_without_losing_blocked_or_active_rows():
    original = legacy_snapshot(6, 24)
    original['stack'][0]['todos'][1]['status'] = 'blocked'
    first = prepare_resume_state({'checkpointTodoState': original}).checkpoint_todo_state
    second = prepare_resume_state({'checkpointTodoState': first}).checkpoint_todo_state
    assert second == first
    assert len(second['todos']) == 144
    assert second['todos'][1]['status'] == 'blocked'
    assert sum(item['status'] == 'in_progress' for item in second['todos']) == 6


def test_empty_completed_legacy_root_is_a_valid_empty_snapshot():
    old = legacy_snapshot(1, 1)
    old['stack'][0]['todos'] = []
    old['root_completed'] = True
    result = prepare_resume_state({'checkpointTodoState': old}).checkpoint_todo_state
    assert result['version'] == 3 and result['todos'] == []


def test_legacy_original_byte_limit_precedes_flattening():
    original = legacy_snapshot()
    original['history'] = [{'reason': 'x' * 1_500_001}]
    with pytest.raises(ContinueResumeStateProtocolError):
        prepare_resume_state({'checkpointTodoState': original})
