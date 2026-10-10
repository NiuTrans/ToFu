"""Full-plan updates share one lifecycle across direct and aliased tool ingress."""
from __future__ import annotations

import copy

import pytest

from lib.tools.gateway import normalize_execute_request
from lib.tools.todo import TODO_WRITE_TOOL, apply_todo_write_to_task

pytestmark = pytest.mark.unit


def _apply(task, arguments, name='todo_write'):
    request = normalize_execute_request(
        {'calls': [{'name': name, 'arguments': arguments}]},
        catalog=[TODO_WRITE_TOOL], namespace_by_name={},
        gateway_call_id='plan-snapshot',
    )
    assert request['errors'] == []
    call = request['calls'][0]
    assert call['function']['name'] == 'todo_write'
    return apply_todo_write_to_task(task, call['_normalized_arguments'])


def test_same_call_creates_replaces_reopens_and_clears():
    task = {}
    for items in (
        [{'content': 'First task', 'status': 'in_progress'}],
        [{'content': 'Revised task', 'status': 'completed'}],
        [{'content': 'Revised task', 'status': 'pending'}],
        [],
        [{'content': 'Next task', 'status': 'pending'}],
    ):
        todos, _, outcome = _apply(task, {'todos': items})
        assert not outcome['rejected']
        assert todos == items
        assert task['_todoState']['todos'] == items
    assert task['_todoState']['version'] == 3
    assert task['_todoState']['update_count'] == 5
    assert 'stack' not in task['_todoState']
    assert 'history' not in task['_todoState']


def test_historical_replan_does_not_require_current_checklist():
    todos, _, outcome = _apply({}, {
        'operation': 'replan', 'parent_todo_id': 'unused',
        'reason': 'Continue follow-up work after restart.',
        'todos': [{'id': 'trace', 'content': 'Inspect feedback',
                   'status': 'in_progress'}],
    })
    assert not outcome['rejected']
    assert todos == [{'content': 'Inspect feedback', 'status': 'in_progress'}]


def test_codex_alias_preserves_explanation_and_replacement_semantics():
    task = {}
    _apply(task, {'todos': [{'content': 'Old scope', 'status': 'pending'}]})
    todos, _, outcome = _apply(task, {
        'explanation': 'The user narrowed the request.',
        'plan': [{'step': 'New scope', 'status': 'in_progress'}],
    }, 'update_plan')
    assert not outcome['rejected']
    assert todos == [{'content': 'New scope', 'status': 'in_progress'}]
    assert task['_todoState']['explanation'] == 'The user narrowed the request.'


def test_invalid_snapshot_cannot_clear_accepted_state():
    task = {}
    _apply(task, {'todos': [{'content': 'Keep work', 'status': 'pending'}]})
    before = copy.deepcopy(task['_todoState'])
    request = normalize_execute_request(
        {'calls': [{'name': 'todo_write', 'arguments': {
            'todos': [{'content': 'Invalid', 'status': 'not-a-status'}],
        }}]}, catalog=[TODO_WRITE_TOOL], namespace_by_name={},
        gateway_call_id='invalid-snapshot',
    )
    assert request['errors']
    assert not request['calls']
    assert task['_todoState'] == before


@pytest.mark.parametrize('arguments', [
    {'command': 'create', 'tasks': ['replacement'], 'todos': None},
    {'plan': [{'step': 'replacement', 'status': 'pending'}], 'todos': None},
    {'todos': [{'status': 'cancelled'}]},
])
def test_foreign_shape_cannot_repair_invalid_native_snapshot_to_clear(arguments):
    request = normalize_execute_request(
        {'calls': [{'name': 'todo_write', 'arguments': arguments}]},
        catalog=[TODO_WRITE_TOOL], namespace_by_name={}, gateway_call_id='bad-native',
    )
    assert request['errors'] and not request['calls']


def test_amazon_q_gateway_commands_and_explicit_empty_native_snapshot():
    task = {}
    todos, _, outcome = _apply(task, {'command': 'create', 'tasks': ['first', 'second']}, 'todo_list')
    assert not outcome['rejected'] and len(todos) == 2
    todos, _, outcome = _apply(task, {'command': 'complete', 'completed_indices': [0]}, 'todo_list')
    assert not outcome['rejected'] and todos[0]['status'] == 'completed'
    before = copy.deepcopy(task['_todoState'])
    _, _, outcome = _apply(task, {'command': 'load'}, 'todo_list')
    assert outcome['no_op'] and task['_todoState'] == before
    todos, _, outcome = _apply(task, {'command': 'create', 'tasks': ['not executed'], 'todos': []})
    assert not outcome['rejected'] and todos == []
