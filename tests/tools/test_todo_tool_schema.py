"""Executable specification for flat todo_write snapshots and resource limits."""

from __future__ import annotations

import copy
import json

from jsonschema import Draft7Validator
import pytest

from lib.tools.gateway import sanitize_wire_tools, tool_schema_tokens
from lib.tools.todo import (
    TODO_MAX_CONTENT_CHARS,
    TODO_MAX_EXPLANATION_CHARS,
    TODO_MAX_ITEMS,
    TODO_STATE_VERSION,
    TODO_WRITE_TOOL,
    VALID_STATUSES,
    apply_todo_operation,
    public_todo_state,
)

pytestmark = pytest.mark.unit


def _item(content='Do work', status='pending'):
    return {'content': content, 'status': status}


def test_schema_exposes_only_flat_snapshot_and_optional_explanation():
    params = TODO_WRITE_TOOL['function']['parameters']
    Draft7Validator.check_schema(params)
    assert params['type'] == 'object'
    assert params['required'] == ['todos']
    assert set(params['properties']) == {'todos', 'explanation'}
    rows = params['properties']['todos']
    assert set(rows['items']['properties']) == {'content', 'status'}
    assert set(rows['items']['required']) == {'content', 'status'}
    assert rows['items']['properties']['status']['enum'] == [
        'pending', 'in_progress', 'blocked', 'completed']
    assert set(VALID_STATUSES) == {'pending', 'in_progress', 'blocked', 'completed'}
    validator = Draft7Validator(params)
    assert validator.is_valid({'todos': []})
    assert validator.is_valid({'todos': [_item()], 'explanation': 'Changed plan'})
    assert not validator.is_valid({})
    assert not validator.is_valid({'todos': [{'content': 'Missing status'}]})
    assert not validator.is_valid({'todos': [_item(status='unknown')]})


def test_schema_provider_preflight_and_resource_limits():
    props = TODO_WRITE_TOOL['function']['parameters']['properties']
    assert props['todos']['maxItems'] == TODO_MAX_ITEMS == 24
    assert props['todos']['items']['properties']['content']['maxLength'] == TODO_MAX_CONTENT_CHARS == 512
    assert props['explanation']['maxLength'] == TODO_MAX_EXPLANATION_CHARS == 2048
    assert TODO_STATE_VERSION == 3
    assert tool_schema_tokens([TODO_WRITE_TOOL]) <= 325
    wire = [TODO_WRITE_TOOL]
    assert sanitize_wire_tools(wire) is wire
    kimi = sanitize_wire_tools(wire, model='kimi-k3')[0]['function']['parameters']
    assert kimi['type'] == 'object'
    assert 'anyOf' not in kimi
    assert set(kimi['properties']) == {'todos', 'explanation'}


@pytest.mark.parametrize('args', [
    None, [], 'invalid', {}, {'todos': None}, {'todos': {}}, {'todos': 'bad'},
    {'todos': [None]}, {'todos': ['bad']}, {'todos': [{}]},
    {'todos': [{'content': ' ', 'status': 'pending'}]},
    {'todos': [{'content': 12, 'status': 'pending'}]},
    {'todos': [{'content': 'missing status'}]},
    {'todos': [_item(status='unknown')]},
    {'todos': [_item(), None]},
    {'todos': [_item('x' * (TODO_MAX_CONTENT_CHARS + 1))]},
    {'todos': [_item(str(i)) for i in range(TODO_MAX_ITEMS + 1)]},
    {'todos': [], 'explanation': 'x' * (TODO_MAX_EXPLANATION_CHARS + 1)},
    {'todos': [], 'explanation': {}},
])
def test_invalid_snapshot_fails_closed_without_mutating_state_or_input(args):
    state = apply_todo_operation(None, {'todos': [_item('keep me')]})['state']
    before, args_before = copy.deepcopy(state), copy.deepcopy(args)
    out = apply_todo_operation(state, args)
    assert out['rejected'] is True
    assert out['reason']
    assert out['state'] == before
    assert out['todos'] == before['todos']
    assert state == before
    assert args == args_before


def test_snapshot_lifecycle_replaces_drops_reopens_and_clears_without_gates():
    out = apply_todo_operation(None, {'todos': []})
    assert not out['rejected'] and not out['no_op']
    assert out['state']['update_count'] == 1
    assert set(out['state']) == {'version', 'todos', 'explanation', 'update_count'}
    assert not {'operation', 'history', 'stack'} & set(out)
    snapshots = [
        [_item('a', 'in_progress'), _item('b', 'blocked')],
        [_item('replacement', 'completed')],
        [_item('replacement', 'pending')],
        [],
    ]
    for count, todos in enumerate(snapshots, 2):
        previous = copy.deepcopy(out['state'])
        out = apply_todo_operation(previous, {'todos': todos})
        assert not out['rejected'] and not out['no_op']
        assert out['todos'] == todos
        assert out['state']['todos'] == todos
        assert out['state']['update_count'] == count
        repeat = apply_todo_operation(out['state'], {'todos': todos})
        assert not repeat['rejected'] and repeat['no_op']
        assert repeat['state'] == out['state']


def test_explanation_change_is_an_update_and_identical_repeat_is_not():
    args = {'todos': [_item()], 'explanation': 'Initial approach'}
    state = apply_todo_operation(None, args)['state']
    args['explanation'] = 'New evidence'
    out = apply_todo_operation(state, args)
    assert not out['no_op'] and not out['rejected']
    assert out['state']['explanation'] == 'New evidence'
    assert out['state']['update_count'] == 2
    assert apply_todo_operation(out['state'], args)['no_op'] is True
    assert state['explanation'] == 'Initial approach'


@pytest.mark.parametrize('operation', ['sync', 'replan'])
def test_legacy_operations_and_ids_are_accepted_without_revision_gates(operation):
    state = apply_todo_operation(None, {'todos': [_item('unfinished')]})['state']
    args = {'operation': operation, 'reason': 'Compatibility explanation',
            'parent_todo_id': 'obsolete',
            'todos': [{'id': 'old-id', **_item('replacement')}]}
    before = copy.deepcopy(args)
    out = apply_todo_operation(state, args)
    assert not out['rejected']
    assert out['todos'] == [_item('replacement')]
    assert out['state']['explanation'] == 'Compatibility explanation'
    assert args == before


def test_repeated_updates_do_not_accumulate_history_or_stack():
    state = None
    for revision in range(100):
        out = apply_todo_operation(state, {
            'todos': [_item(str(revision))], 'explanation': str(revision)})
        assert not out['rejected']
        state = out['state']
    assert state['update_count'] == 100
    assert set(state) == {'version', 'todos', 'explanation', 'update_count'}
    assert public_todo_state(state) == state


def test_maximum_unicode_snapshot_round_trips_with_bounded_size():
    args = {'todos': [_item('😀' * TODO_MAX_CONTENT_CHARS,
                           'in_progress' if i == 0 else 'pending')
                      for i in range(TODO_MAX_ITEMS)],
            'explanation': '😀' * TODO_MAX_EXPLANATION_CHARS}
    out = apply_todo_operation(None, args)
    assert not out['rejected']
    state = out['state']
    encoded = json.dumps(state, ensure_ascii=False).encode('utf-8')
    assert len(encoded) < 64 * 1024
    assert public_todo_state(json.loads(encoded)) == state


def test_legacy_projection_flattens_all_frames_without_losing_status_or_rows():
    expected = [_item(f'frame-{frame}-row-{row}',
                      ['pending', 'in_progress', 'blocked', 'completed'][row % 4])
                for frame in range(6) for row in range(24)]
    legacy = {'version': 2, 'stack': [
        {'checklist_id': f'cl-{frame}', 'parent_todo_id': 'parent',
         'revision': 2, 'todos': [dict(item, id=str(index))
                                for index, item in enumerate(expected[frame * 24:(frame + 1) * 24])]}
        for frame in range(6)], 'history': [{'kind': 'replan'}],
        'history_dropped': 0, 'root_completed': False, 'update_count': 7}
    before = copy.deepcopy(legacy)
    state = public_todo_state(legacy)
    assert set(state) == {'version', 'todos', 'explanation', 'update_count'}
    assert state['version'] == 3
    assert state['todos'] == expected
    assert state['update_count'] == 7
    assert public_todo_state(state) == state
    assert legacy == before
    state['todos'][0]['content'] = 'changed projection'
    assert legacy == before


def test_outcome_does_not_alias_caller_owned_rows():
    args = {'todos': [_item('original')]}
    state = apply_todo_operation(None, args)['state']
    args['todos'][0]['content'] = 'changed input'
    assert state['todos'][0]['content'] == 'original'
    out = apply_todo_operation(state, {'todos': [_item('next')]})
    out['state']['todos'][0]['content'] = 'changed output'
    assert state['todos'][0]['content'] == 'original'
