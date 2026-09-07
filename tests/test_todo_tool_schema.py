"""todo_write model contract and bounded revision-state resource budget."""

from __future__ import annotations

import copy
import json

from jsonschema import Draft7Validator
import pytest

from lib.tools.gateway import sanitize_wire_tools, tool_schema_tokens
from lib.tools.todo import (
    TODO_MAX_CONTENT_CHARS,
    TODO_MAX_HISTORY_ENTRIES,
    TODO_MAX_ID_CHARS,
    TODO_MAX_ITEMS,
    TODO_MAX_REPLAN_REASON_CHARS,
    TODO_MAX_STACK_DEPTH,
    TODO_MAX_STATE_BYTES,
    TODO_WRITE_TOOL,
    apply_todo_operation,
    public_todo_state,
)


pytestmark = pytest.mark.unit


def _item(todo_id: str, content: str = 'Do work', status: str = 'in_progress'):
    return {'id': todo_id, 'content': content, 'status': status}


def _max_items(namespace: str) -> list[dict]:
    rows = []
    for index in range(TODO_MAX_ITEMS):
        prefix = f'{namespace[:2]}-{index:02d}-'
        todo_id = prefix + '😀' * (TODO_MAX_ID_CHARS - len(prefix))
        rows.append(_item(
            todo_id,
            '😀' * TODO_MAX_CONTENT_CHARS,
            'in_progress' if index == 0 else 'pending',
        ))
    return rows


def test_schema_keeps_revision_nesting_and_completion_contracts():
    function = TODO_WRITE_TOOL['function']
    desc = function['description'].lower()
    params = function['parameters']
    props = params['properties']

    for phrase in (
            'nontrivial', 'revisioned checklists', 'sync (default)',
            'full active list', 'cannot drop unfinished', 'reopen completed',
            'replan replaces unfinished work', 'requires reason',
            'active unfinished parent_todo_id', 'completion auto-pops',
            'completes the parent', 'never resend the parent list',
            'max one in_progress', 'prevent success'):
        assert phrase in desc, phrase
    assert props['operation']['enum'] == ['sync', 'push', 'replan']
    assert props['todos']['items']['properties']['status']['enum'] == [
        'pending', 'in_progress', 'blocked', 'completed']
    assert params['type'] == 'object'
    assert params['required'] == ['todos']


def test_schema_and_provider_preflight_enforce_operation_requirements():
    params = TODO_WRITE_TOOL['function']['parameters']
    Draft7Validator.check_schema(params)
    validator = Draft7Validator(params)
    rows = [_item('work')]

    assert validator.is_valid({'todos': rows})
    assert validator.is_valid({
        'operation': 'push', 'parent_todo_id': 'work', 'todos': rows})
    assert validator.is_valid({
        'operation': 'replan', 'reason': 'Requirements changed', 'todos': rows})
    assert not validator.is_valid({'operation': 'push', 'todos': rows})
    assert not validator.is_valid({
        'operation': 'push', 'parent_todo_id': 'work', 'todos': []})
    assert not validator.is_valid({'operation': 'replan', 'todos': rows})

    wire = [TODO_WRITE_TOOL]
    assert sanitize_wire_tools(wire) is wire
    kimi_parameters = sanitize_wire_tools(
        wire, model='kimi-k3')[0]['function']['parameters']
    assert kimi_parameters['type'] == 'object'
    assert 'anyOf' not in kimi_parameters


def test_schema_limits_share_the_runtime_resource_contract():
    props = TODO_WRITE_TOOL['function']['parameters']['properties']
    item_props = props['todos']['items']['properties']

    assert props['todos']['maxItems'] == TODO_MAX_ITEMS == 24
    assert props['parent_todo_id']['maxLength'] == TODO_MAX_ID_CHARS == 64
    assert item_props['id']['maxLength'] == TODO_MAX_ID_CHARS
    assert item_props['content']['maxLength'] == TODO_MAX_CONTENT_CHARS == 512
    assert props['reason']['maxLength'] == TODO_MAX_REPLAN_REASON_CHARS == 2048
    assert TODO_MAX_STACK_DEPTH == 6
    assert TODO_MAX_HISTORY_ENTRIES == 8
    assert tool_schema_tokens([TODO_WRITE_TOOL]) <= 325


@pytest.mark.parametrize(('field', 'value', 'error_fragment'), [
    ('id', 'x' * (TODO_MAX_ID_CHARS + 1), 'todo id exceeds'),
    ('content', 'x' * (TODO_MAX_CONTENT_CHARS + 1), 'todo content exceeds'),
])
def test_core_rejects_oversized_items_without_mutating_state(
        field, value, error_fragment):
    initial = apply_todo_operation(None, {'todos': [_item('root')]})['state']
    before = copy.deepcopy(initial)
    row = _item('root')
    row[field] = value

    outcome = apply_todo_operation(initial, {'todos': [row]})

    assert outcome['rejected'] is True
    assert error_fragment in outcome['reason']
    assert outcome['state'] == before
    assert initial == before


def test_core_rejects_oversized_list_reason_and_stack_without_partial_change():
    initial = apply_todo_operation(None, {'todos': [_item('root')]})['state']
    too_many = [_item(str(index), status='pending')
                for index in range(TODO_MAX_ITEMS + 1)]
    rejected = apply_todo_operation(initial, {'todos': too_many})
    assert rejected['rejected'] and f'at most {TODO_MAX_ITEMS}' in rejected['reason']
    assert len(rejected['state']['stack']) == 1

    rejected = apply_todo_operation(initial, {
        'operation': 'replan',
        'reason': 'x' * (TODO_MAX_REPLAN_REASON_CHARS + 1),
        'todos': [_item('next')],
    })
    assert rejected['rejected'] and 'replan reason exceeds' in rejected['reason']
    assert len(rejected['state']['stack']) == 1

    state = initial
    for depth in range(1, TODO_MAX_STACK_DEPTH):
        parent_id = state['stack'][-1]['todos'][0]['id']
        outcome = apply_todo_operation(state, {
            'operation': 'push',
            'parent_todo_id': parent_id,
            'todos': [_item(f'child-{depth}')],
        })
        assert not outcome['rejected']
        state = outcome['state']
    before = copy.deepcopy(state)
    rejected = apply_todo_operation(state, {
        'operation': 'push',
        'parent_todo_id': state['stack'][-1]['todos'][0]['id'],
        'todos': [_item('too-deep')],
    })
    assert rejected['rejected'] and 'nesting is limited' in rejected['reason']
    assert rejected['state'] == before and state == before


def test_history_is_tail_bounded_and_dropped_count_is_observable():
    state = apply_todo_operation(None, {'todos': [_item('initial')]})['state']
    total_replans = TODO_MAX_HISTORY_ENTRIES + 3
    for revision in range(total_replans):
        outcome = apply_todo_operation(state, {
            'operation': 'replan',
            'reason': f'reason-{revision}',
            'todos': [_item(f'item-{revision}')],
        })
        assert not outcome['rejected']
        state = outcome['state']

    assert len(state['history']) == TODO_MAX_HISTORY_ENTRIES
    assert state['history_dropped'] == 3
    assert state['history'][0]['reason'] == 'reason-3'
    assert state['history'][-1]['reason'] == f'reason-{total_replans - 1}'

    legacy = copy.deepcopy(state)
    legacy['history'] = [{'kind': 'old'}] * (TODO_MAX_HISTORY_ENTRIES + 2)
    legacy['history_dropped'] = 4
    projected = public_todo_state(legacy)
    assert len(projected['history']) == TODO_MAX_HISTORY_ENTRIES
    assert projected['history_dropped'] == 6


def test_maximum_legal_state_fits_resume_budget_and_round_trips():
    state = apply_todo_operation(None, {'todos': _max_items('r0')})['state']
    for revision in range(TODO_MAX_HISTORY_ENTRIES + 2):
        outcome = apply_todo_operation(state, {
            'operation': 'replan',
            'reason': '😀' * TODO_MAX_REPLAN_REASON_CHARS,
            'todos': _max_items(f'r{revision + 1}'),
        })
        assert not outcome['rejected']
        state = outcome['state']

    for depth in range(1, TODO_MAX_STACK_DEPTH):
        parent_id = state['stack'][-1]['todos'][0]['id']
        outcome = apply_todo_operation(state, {
            'operation': 'push',
            'parent_todo_id': parent_id,
            'todos': _max_items(f'd{depth}'),
        })
        assert not outcome['rejected']
        state = outcome['state']

    public = public_todo_state(state)
    encoded_bytes = len(json.dumps(
        public, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8'))
    assert encoded_bytes <= TODO_MAX_STATE_BYTES

    from lib.tasks_pkg.orchestrator._resume_state import _strict_todo_state
    assert _strict_todo_state(public) == public


def test_resume_rejects_unbounded_stack_history_or_serialized_state():
    from lib.tasks_pkg.orchestrator._resume_state import (
        ContinueResumeStateProtocolError,
        _strict_todo_state,
    )

    state = apply_todo_operation(None, {'todos': [_item('root')]})['state']
    too_much_history = copy.deepcopy(state)
    too_much_history['history'] = [
        {'kind': 'old'} for _ in range(TODO_MAX_HISTORY_ENTRIES + 1)]
    with pytest.raises(ContinueResumeStateProtocolError, match='history exceeds'):
        _strict_todo_state(too_much_history)

    too_deep = copy.deepcopy(state)
    too_deep['stack'] *= TODO_MAX_STACK_DEPTH + 1
    with pytest.raises(ContinueResumeStateProtocolError, match='stack exceeds'):
        _strict_todo_state(too_deep)

    oversized = copy.deepcopy(state)
    oversized['history'] = [{'blob': 'x' * TODO_MAX_STATE_BYTES}]
    with pytest.raises(ContinueResumeStateProtocolError, match='serialized UTF-8'):
        _strict_todo_state(oversized)
